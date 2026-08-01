// TS2Python bridge implementation — see ../contract/ for the wire format
// and ../contract/error_codes.md for the return codes enforced here.

#include "ts2python.h"

#include <zmq.hpp>

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <unordered_map>

#if defined(_WIN32)
#  ifndef WIN32_LEAN_AND_MEAN
#    define WIN32_LEAN_AND_MEAN
#  endif
#  include <windows.h>
#endif

namespace {

constexpr int kDllVersion = 1;

std::mutex       g_mutex;
// Raw pointers, never destroyed implicitly. See pin_self_module_once()
// for the full rationale — short version: zmq_ctx_term() joins the ZMQ
// I/O thread, which deadlocks / crashes when called under the Windows
// loader lock during DLL_PROCESS_DETACH. Keeping globals as raw pointers
// means static teardown is a no-op. Explicit cleanup stays available in
// EL_Shutdown for the standalone test harness path.
zmq::context_t*  g_ctx  = nullptr;
zmq::socket_t*   g_sock = nullptr;

// ---- gap detection --------------------------------------------------------
//
// PUB/SUB is fire-and-forget: PUB drops silently past SNDHWM and never
// blocks, SUB drops silently past RCVHWM. Neither side reports it, so
// without a sequence number a subscriber cannot tell a quiet market from
// a lost one — which matters when the data feeds trading decisions and
// model training.
//
// g_seq is per-symbol rather than global because a subscriber may filter
// on a single topic; a global counter's gaps would be indistinguishable
// from other symbols' traffic it never asked for.
//
// g_sid marks the publisher's session so a subscriber can tell "the DLL
// restarted and counters reset" from "we lost 4000 messages". Stamped
// once per successful EL_Init.
//
// It is microseconds, not seconds. At one-second resolution two sessions
// starting inside the same wall-clock second — a test harness rerun, or a
// script doing EL_Shutdown + EL_Init — share an id, so the subscriber reads
// the restart as a sequence regression instead: its expectation stays parked
// at the old session's high-water mark, and everything genuinely lost in the
// new session's first messages is invisible while messages_lost still reads
// 0. Microseconds also stay under 2^53, so a binding that parses JSON numbers
// as double still reads the value exactly.
//
// Both are guarded by g_mutex, which already serialises every publish.
std::uint64_t                                     g_sid = 0;
std::unordered_map<std::string, std::uint64_t>    g_seq;

// Reserve the next sequence number for `symbol`. Must be called with
// g_mutex held.
//
// The number is consumed even if the send that follows fails. That is
// deliberate: a reserved-but-unsent number surfaces at the subscriber as
// a gap, which is exactly what happened. Incrementing only on success
// would hide real losses behind a contiguous sequence.
std::uint64_t reserve_seq(const char* symbol) {
    return ++g_seq[std::string(symbol)];
}

// ---- quote availability --------------------------------------------------
//
// EL passes InsideBid / InsideAsk, which are live-quote functions. They
// return 0 whenever there is no quote to report:
//
//   - historical replay (chart loading, or any non-realtime bar)
//   - symbols that have no quote at all, e.g. breadth indices
//
// Forwarding that 0 verbatim puts a number on the wire that reads as a
// $0.00 quote, and leaves every binding to independently remember that 0
// means "absent". That is exactly the kind of rule that survives only in
// prose and gets missed by the next implementation — this repo already
// has one such rule (see contract/semantics.md §3).
//
// Emitting JSON null instead makes the wire say what it means. Bindings
// that already model an optional quote need no special case at all.
//
// The guard is written as !(v > 0.0) so NaN lands on the null branch too.
void format_quote(char* out, std::size_t n, double v) {
    if (!(v > 0.0)) {
        std::snprintf(out, n, "null");
    } else {
        std::snprintf(out, n, "%.6f", v);
    }
}

// ---- quantities -----------------------------------------------------------
//
// EasyLanguage has no 64-bit integer type — DefineDLLFunc offers `int`
// (32-bit) and `double`, and a single day's volume on a heavily traded penny
// stock exceeds 2^31 — so the five quantity words arrive as double and are
// narrowed here, once, before they reach the wire.
//
// 9.0e15 is just under 2^53, the largest integer a double represents exactly.
// Past that the double has already lost the low bits, so the value being
// narrowed is not the value EL held; converting it anyway would emit a
// precise-looking integer that is simply wrong. The bound also catches NaN
// and infinity, both of which are undefined behaviour to cast.
//
// Written as a rejection rather than a clamp on purpose: a clamped quantity
// is indistinguishable from a real one downstream, which is the failure mode
// this whole protocol revision exists to eliminate. -4 is loud.
bool to_int64(double v, std::int64_t* out) {
    if (!(v >= -9.0e15 && v <= 9.0e15)) return false;  // NaN / inf / too large
    *out = static_cast<std::int64_t>(v);
    return true;
}

// ---- timeframe ------------------------------------------------------------
//
// Maps EasyLanguage's BarType / BarInterval onto the wire's `tf` vocabulary,
// which is the same set of strings the storage layer partitions on.
//
// The mapping lives here, not in EasyLanguage, for the same reason quote
// normalisation does: there is one C ABI and many possible callers, and a
// rule re-derived per caller is a rule that eventually disagrees with itself.
//
// Returns nullptr for anything with no wire representation. Callers must
// treat that as a hard error rather than guessing — BarType 1 covers every
// intraday minute chart, so a wrong guess files 5-minute bars under 1m and
// nothing downstream can tell.
const char* wire_timeframe(int bar_type, int bar_interval) {
    if (bar_type == 1) {           // intraday, BarInterval in minutes
        switch (bar_interval) {
            case 1:  return "1m";
            case 5:  return "5m";
            case 15: return "15m";
            case 30: return "30m";
            case 60: return "1h";
            default: return nullptr;
        }
    }
    // Daily. TradeStation 10 reports BarInterval = 0 on a daily chart, not 1
    // — measured on a live install, where an SPY daily chart logged
    // "bar_type=2.00 bar_interval=0.00" and this function refused it with -5.
    // 1 is accepted alongside it because that is what the ABI has documented
    // since it shipped, and the DLL sits in installs this repo cannot see.
    //
    // Values above 1 are still refused rather than folded into "1d": on
    // BarType 2 the interval is a day multiplier, so a 2-day chart would
    // otherwise land in the 1d partition looking exactly like real daily data.
    if (bar_type == 2 && (bar_interval == 0 || bar_interval == 1)) return "1d";
    return nullptr;                                        // weekly/monthly/P&F/...
}

// Pin the DLL into the host process's address space on first successful
// EL_Init. TradeStation calls FreeLibrary when the user disables or
// removes the indicator; without pinning, that unload would trigger the
// C runtime's static-destructor chain for any leftover zmq_context_t
// global, whose destructor calls zmq_ctx_term() → joins the ZMQ I/O
// thread under loader lock → deadlock / crash inside TradeStation.
//
// Pinning turns FreeLibrary into a ref-count decrement that never
// reaches zero, so the DLL stays mapped until TS itself exits. At
// process termination raw-pointer globals have no destructors, so no
// unsafe cleanup runs at all — the OS reclaims memory cleanly.
void pin_self_module_once() {
#if defined(_WIN32)
    static std::once_flag once;
    std::call_once(once, [] {
        HMODULE hmod = nullptr;
        ::GetModuleHandleExW(
            GET_MODULE_HANDLE_EX_FLAG_PIN |
                GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS,
            reinterpret_cast<LPCWSTR>(&pin_self_module_once),
            &hmod);
    });
#endif
}

// Receive-side wall-clock timestamp (ts on the wire). Stamped by the DLL
// at the moment the EL call lands — authoritative for live aggregation.
double recv_unix_seconds() {
    using namespace std::chrono;
    const auto ns = duration_cast<nanoseconds>(
                        system_clock::now().time_since_epoch())
                        .count();
    return static_cast<double>(ns) / 1e9;
}

// Session id source. See g_sid for why seconds are not enough.
std::uint64_t recv_unix_microseconds() {
    using namespace std::chrono;
    return static_cast<std::uint64_t>(
        duration_cast<microseconds>(system_clock::now().time_since_epoch()).count());
}

// The EL timestamp is no longer parsed here.
//
// The superseded protocol carried a third time field, ts_utc: this DLL's own
// zoned_time("America/New_York") reading of el_timestamp, published alongside
// the raw string purely as a cross-check. It was never authoritative — a
// binding parses ts_str itself, because the DLL host's tz database can be
// stale in a way the binding host's is not — and shipping a value that every
// binding must be told not to trust is worse than not shipping it.
//
// Two things are given up with it, both recorded in ../contract/wire.md:
// the ts_utc-vs-ts drift check was the only signal that the two hosts'
// tz databases disagreed, and parsing here doubled as a format check, so an
// unparseable el_timestamp now reaches the binding intact instead of being
// flagged at the publisher.

}  // namespace

extern "C" {

TS2P_API int TS2P_CALL EL_DllVersion(void) {
    return kDllVersion;
}

static int init_impl(const char* zmq_endpoint) {
    if (zmq_endpoint == nullptr) return -4;
    try {
        std::lock_guard<std::mutex> lock(g_mutex);
        if (g_sock) return 1;  // idempotent re-init

        auto* ctx  = new zmq::context_t(1);
        auto* sock = new zmq::socket_t(*ctx, zmq::socket_type::pub);
        // PUB silently drops past SNDHWM (PUB never blocks publisher).
        // 100k * ~512B payload ≈ 51MB per subscriber pipe — buys ~30 min
        // of SUB stall at 50 tps, still safe inside TS's 32-bit address space.
        // Drops past this point are invisible here; the `seq` field is what
        // lets the subscriber notice them.
        sock->set(zmq::sockopt::sndhwm, 100000);
        sock->set(zmq::sockopt::linger, 0);
        sock->bind(zmq_endpoint);

        g_ctx  = ctx;
        g_sock = sock;
        // New publisher session: stamp its id and restart every counter.
        // Only on a real init — the idempotent path above returned 1
        // without touching either, so a re-Verify of the indicator does
        // not look like a restart to subscribers.
        g_sid = recv_unix_microseconds();
        g_seq.clear();
        pin_self_module_once();  // stay resident for the life of the host
        return 0;
    } catch (const zmq::error_t&) {
        return -3;
    } catch (...) {
        return -3;
    }
}

TS2P_API int TS2P_CALL EL_Init3(const char* zmq_endpoint) {
    return init_impl(zmq_endpoint);
}

// ---- tombstones -----------------------------------------------------------
//
// The init exports of the superseded protocol. They still exist, and they
// never initialise anything.
//
// EL_PublishTick and EL_PublishBar kept their names but changed arity. Under
// __stdcall the callee pops the arguments, so an .ELD built against the old
// signatures would corrupt the stack rather than fail — TradeStation
// misbehaves or dies, and there is no return code to look at.
//
// Every publish call in the indicator sits behind a successful init, so
// stopping the old .ELD here stops it everywhere. Returning -6 rather than
// dropping the export is what puts a readable "EL_Init2 FAILED rc=-6" in the
// Print Log instead of an unexplained DefineDLLFunc resolution failure.
//
// Do not delete these until it is safe to assume no old .ELD survives in any
// install this repo cannot see — which is not a thing this repo can observe.
TS2P_API int TS2P_CALL EL_Init(const char* /*zmq_endpoint*/) {
    return -6;
}

TS2P_API int TS2P_CALL EL_Init2(const char* /*zmq_endpoint*/,
                                int /*publisher_version*/) {
    return -6;
}

TS2P_API int TS2P_CALL EL_PublishTick(
    const char* symbol,
    const char* el_timestamp,
    double      price,
    double      volume,
    double      ticks,
    double      upticks,
    double      downticks,
    double      open_interest,
    double      bid,
    double      ask)
{
    if (symbol == nullptr) return -4;

    // Narrow before taking a sequence number: a rejected argument is not a
    // lost message, so it must not leave a gap in the subscriber's count.
    std::int64_t q_volume, q_ticks, q_upticks, q_downticks, q_oi;
    if (!to_int64(volume,        &q_volume)    ||
        !to_int64(ticks,         &q_ticks)     ||
        !to_int64(upticks,       &q_upticks)   ||
        !to_int64(downticks,     &q_downticks) ||
        !to_int64(open_interest, &q_oi)) return -4;

    const char* ts_str = (el_timestamp != nullptr) ? el_timestamp : "";

    try {
        std::lock_guard<std::mutex> lock(g_mutex);
        if (!g_sock) return -1;

        const std::uint64_t seq = reserve_seq(symbol);

        char bid_s[32], ask_s[32];
        format_quote(bid_s, sizeof(bid_s), bid);
        format_quote(ask_s, sizeof(ask_s), ask);

        char payload[640];
        const int n = std::snprintf(
            payload, sizeof(payload),
            "{\"proto\":1,\"kind\":\"tick\",\"seq\":%llu,\"sid\":%llu,"
            "\"ts\":%.6f,\"ts_str\":\"%s\","
            "\"px\":%.6f,"
            "\"el_volume\":%lld,\"el_ticks\":%lld,\"el_upticks\":%lld,"
            "\"el_downticks\":%lld,\"el_open_interest\":%lld,"
            "\"bid\":%s,\"ask\":%s}",
            static_cast<unsigned long long>(seq),
            static_cast<unsigned long long>(g_sid),
            recv_unix_seconds(), ts_str, price,
            static_cast<long long>(q_volume),
            static_cast<long long>(q_ticks),
            static_cast<long long>(q_upticks),
            static_cast<long long>(q_downticks),
            static_cast<long long>(q_oi),
            bid_s, ask_s);
        if (n <= 0 || static_cast<size_t>(n) >= sizeof(payload)) return -4;

        zmq::message_t topic(symbol, std::strlen(symbol));
        zmq::message_t body(payload, static_cast<size_t>(n));

        const auto r1 = g_sock->send(topic, zmq::send_flags::sndmore);
        if (!r1) return -2;
        const auto r2 = g_sock->send(body, zmq::send_flags::none);
        if (!r2) return -2;
        return 0;
    } catch (const zmq::error_t&) {
        return -2;
    } catch (...) {
        return -2;
    }
}

// Shared bar publisher. `tf` must already be a valid wire timeframe.
static int publish_bar_impl(
    const char* symbol,
    const char* el_timestamp,
    const char* tf,
    double      bar_open,
    double      bar_high,
    double      bar_low,
    double      bar_close,
    double      volume,
    double      ticks,
    double      upticks,
    double      downticks,
    double      open_interest)
{
    if (symbol == nullptr) return -4;

    // See EL_PublishTick: narrow before reserving a sequence number.
    std::int64_t q_volume, q_ticks, q_upticks, q_downticks, q_oi;
    if (!to_int64(volume,        &q_volume)    ||
        !to_int64(ticks,         &q_ticks)     ||
        !to_int64(upticks,       &q_upticks)   ||
        !to_int64(downticks,     &q_downticks) ||
        !to_int64(open_interest, &q_oi)) return -4;

    const char* ts_str = (el_timestamp != nullptr) ? el_timestamp : "";

    try {
        std::lock_guard<std::mutex> lock(g_mutex);
        if (!g_sock) return -1;

        const std::uint64_t seq = reserve_seq(symbol);

        char payload[768];
        const int n = std::snprintf(
            payload, sizeof(payload),
            "{\"proto\":1,\"kind\":\"bar\",\"tf\":\"%s\",\"seq\":%llu,\"sid\":%llu,"
            "\"ts\":%.6f,\"ts_str\":\"%s\","
            "\"o\":%.6f,\"h\":%.6f,\"l\":%.6f,\"c\":%.6f,"
            "\"el_volume\":%lld,\"el_ticks\":%lld,\"el_upticks\":%lld,"
            "\"el_downticks\":%lld,\"el_open_interest\":%lld}",
            tf,
            static_cast<unsigned long long>(seq),
            static_cast<unsigned long long>(g_sid),
            recv_unix_seconds(), ts_str,
            bar_open, bar_high, bar_low, bar_close,
            static_cast<long long>(q_volume),
            static_cast<long long>(q_ticks),
            static_cast<long long>(q_upticks),
            static_cast<long long>(q_downticks),
            static_cast<long long>(q_oi));
        if (n <= 0 || static_cast<size_t>(n) >= sizeof(payload)) return -4;

        zmq::message_t topic(symbol, std::strlen(symbol));
        zmq::message_t body(payload, static_cast<size_t>(n));

        const auto r1 = g_sock->send(topic, zmq::send_flags::sndmore);
        if (!r1) return -2;
        const auto r2 = g_sock->send(body, zmq::send_flags::none);
        if (!r2) return -2;
        return 0;
    } catch (const zmq::error_t&) {
        return -2;
    } catch (...) {
        return -2;
    }
}

TS2P_API int TS2P_CALL EL_PublishBar(
    const char* symbol,
    const char* el_timestamp,
    int         bar_type,
    int         bar_interval,
    double      bar_open,
    double      bar_high,
    double      bar_low,
    double      bar_close,
    double      volume,
    double      ticks,
    double      upticks,
    double      downticks,
    double      open_interest)
{
    const char* tf = wire_timeframe(bar_type, bar_interval);
    // No guessing. An unmappable interval published as some default would
    // land in the wrong partition, and nothing downstream could detect it.
    if (tf == nullptr) return -5;
    return publish_bar_impl(symbol, el_timestamp, tf,
                            bar_open, bar_high, bar_low, bar_close,
                            volume, ticks, upticks, downticks, open_interest);
}

TS2P_API int TS2P_CALL EL_Shutdown(void) {
    // Only safe to call from a regular process path (e.g. the standalone
    // test harness). Not called from the EL indicator because EL has no
    // unload hook — the DLL is pinned instead, see pin_self_module_once().
    std::lock_guard<std::mutex> lock(g_mutex);
    delete g_sock; g_sock = nullptr;
    delete g_ctx;  g_ctx  = nullptr;
    g_seq.clear();
    g_sid = 0;
    return 0;
}

}  // extern "C"
