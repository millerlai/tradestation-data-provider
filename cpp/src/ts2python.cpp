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
#include <unordered_set>
#include <vector>

#if defined(_WIN32)
#  ifndef WIN32_LEAN_AND_MEAN
#    define WIN32_LEAN_AND_MEAN
#  endif
#  include <windows.h>
#endif

namespace {

constexpr int kDllVersion = 3;

// Where hello frames go. NOT a symbol topic.
//
// A consumer subscribes per symbol, from a list it was configured with. A
// chart on a symbol that is not on that list is precisely the case an
// operator needs told about — and announcing it on its own symbol topic
// would deliver it to nobody. So the announcement rides a fixed topic the
// consumer always subscribes to, and the topic is what tells a hello frame
// apart from a point frame. No discriminator field on the payload, and the
// point frame is untouched.
//
// The leading underscores keep it out of TradeStation's symbol space: ZMQ
// SUBSCRIBE is a prefix match, so a topic that could prefix a real symbol
// (or be prefixed by one) would cross-deliver.
constexpr char kControlTopic[] = "__ts2py__";

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

// ---- chart registry ------------------------------------------------------
//
// One entry per chart that has called EL_Init. TradeStation runs the
// indicator once per chart, each with its own symbol and interval, and all
// of them share this one DLL and one socket.
//
// The registry exists so a consumer restart does not need TradeStation
// touched. EL_Init runs once per chart, on its first bar; if the only
// record of a chart were that call, a consumer that started, stopped and
// started again would never learn what is attached until every chart was
// re-Verified by hand. Instead the DLL re-announces everything it knows the
// moment a subscriber appears.
//
// A plain vector: a TradeStation workspace holds a handful of charts, and
// the linear scan happens once per chart on its first bar and once per
// subscriber attach. Guarded by g_mutex like everything else here.
struct Chart {
    std::string symbol;
    int         category;
    int         bar_type;
    int         bar_interval;
    bool        announced;
};
std::vector<Chart> g_charts;

// Topics with at least one subscriber attached, as reported by XPUB.
//
// XPUB (not PUB) is what makes EL_Init able to answer "is anyone actually
// listening". A subscription arrives on the socket as a readable message:
// 0x01 followed by the topic on subscribe, 0x00 on unsubscribe. Non-verbose
// XPUB reports only the first subscriber per topic and only the last
// unsubscribe, which is exactly the "is at least one attached" question
// being asked here.
std::unordered_set<std::string> g_sub_topics;

// Would a subscription to `s` deliver the control topic?
//
// PREFIX MATCH, not equality. ZMQ_SUBSCRIBE is a prefix filter, so a
// subscriber that asked for "" gets every topic including this one, and one
// that asked for "__" gets it too. Matching on equality left EL_Init
// returning -7 forever against a perfectly good subscriber — which is
// exactly what `contract/tools/record.py` does by default, and it deadlocked
// the whole publisher.
bool covers_control_topic(const std::string& s) {
    const std::string ctrl(kControlTopic);
    return s.size() <= ctrl.size() && ctrl.compare(0, s.size(), s) == 0;
}

// True once a subscriber is attached that would RECEIVE the control topic —
// i.e. once a hello would actually reach someone. Must be called with
// g_mutex held. A linear scan over a handful of topics, once per publish.
bool control_topic_subscribed() {
    for (const std::string& s : g_sub_topics) {
        if (covers_control_topic(s)) return true;
    }
    return false;
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

// Publish one chart's hello on the control topic. Must be called with
// g_mutex held, and only when g_sock exists.
//
// The frame declares proto 2 like every other frame on this socket. It is
// not a point and does not pretend to be one: it carries no OHLC, no
// quantity words and no quote, and it is told apart by its topic. A
// consumer that only wants points never subscribes here and cannot see it.
bool send_hello(Chart& c) {
    const std::uint64_t seq = reserve_seq(kControlTopic);

    char payload[512];
    const int n = std::snprintf(
        payload, sizeof(payload),
        "{\"proto\":2,\"seq\":%llu,\"sid\":%llu,\"ts\":%.6f,"
        "\"symbol\":\"%s\",\"category\":%d,"
        "\"bar_type\":%d,\"bar_interval\":%d}",
        static_cast<unsigned long long>(seq),
        static_cast<unsigned long long>(g_sid),
        recv_unix_seconds(),
        c.symbol.c_str(), c.category, c.bar_type, c.bar_interval);
    if (n <= 0 || static_cast<size_t>(n) >= sizeof(payload)) return false;

    zmq::message_t topic(kControlTopic, std::strlen(kControlTopic));
    zmq::message_t body(payload, static_cast<size_t>(n));
    if (!g_sock->send(topic, zmq::send_flags::sndmore)) return false;
    if (!g_sock->send(body, zmq::send_flags::none)) return false;

    c.announced = true;
    return true;
}

// Read whatever subscription traffic XPUB has queued, and re-announce every
// known chart when a consumer attaches. Must be called with g_mutex held.
//
// Called from both EL_Init and EL_Publish. EL_Init alone would not be
// enough: it runs once per chart, on that chart's first bar, so a consumer
// restarting an hour later would find nothing announcing itself and no
// second EL_Init coming. Draining on every publish is what makes the
// consumer restartable without touching TradeStation.
void drain_subscriptions() {
    if (!g_sock) return;

    try {
        zmq::message_t msg;
        // An XPUB subscription message is 0x01 or 0x00 followed by the raw
        // topic. dontwait returns an empty result on EAGAIN rather than
        // throwing, so this drains the queue and stops.
        while (g_sock->recv(msg, zmq::recv_flags::dontwait)) {
            if (msg.size() < 1) continue;
            const auto* d = static_cast<const unsigned char*>(msg.data());
            std::string topic(reinterpret_cast<const char*>(d + 1), msg.size() - 1);

            if (d[0] == 0) {
                g_sub_topics.erase(topic);
                continue;
            }
            if (d[0] != 1) continue;
            g_sub_topics.insert(topic);
            if (!covers_control_topic(topic)) continue;

            // ANNOUNCE ON THE SUBSCRIBE ITSELF, not on a 0->1 transition in
            // the subscriber count. A consumer restarting can overlap its
            // predecessor by a few milliseconds, and libzmq then never sees
            // the topic reach zero subscribers — measured, the reconnecting
            // consumer got no hellos at all, while the same test with a
            // six-second gap got both. XPUB_VERBOSE (set at bind) is what
            // makes this reliable: without it XPUB reports only the FIRST
            // subscriber per topic and the overlapping one is invisible.
            //
            // The cost is a duplicate hello for a consumer that subscribes
            // to two topics both covering this one (say "" and __ts2py__).
            // A repeated announcement is idempotent downstream; a missing
            // one leaves the consumer blind to the whole workspace.
            //
            // Its predecessor's hellos died with it and TradeStation will
            // not call EL_Init again for a chart already on screen, so
            // everything this DLL knows goes out again.
            for (auto& c : g_charts) {
                c.announced = false;
            }
            for (auto& c : g_charts) {
                send_hello(c);
            }
        }
    } catch (...) {
        // Nothing here is worth failing the caller's publish over; the next
        // call drains again.
    }
}

}  // namespace

extern "C" {

TS2P_API int TS2P_CALL EL_DllVersion(void) {
    return kDllVersion;
}

TS2P_API int TS2P_CALL EL_Init(const char* zmq_endpoint,
                               const char* symbol,
                               int         category,
                               int         bar_type,
                               int         bar_interval) {
    if (zmq_endpoint == nullptr || symbol == nullptr) return -4;
    try {
        std::lock_guard<std::mutex> lock(g_mutex);

        if (!g_sock) {
            auto* ctx = new zmq::context_t(1);
            // XPUB, not PUB. Same send semantics; the difference is that a
            // subscription arrives as a readable message, which is the only
            // way this side can answer "is anyone actually listening" — and
            // without that, init reports success into a void.
            auto* sock = new zmq::socket_t(*ctx, zmq::socket_type::xpub);
            // Report EVERY subscription, not just the first per topic.
            // A consumer restart can overlap its predecessor briefly, and
            // without this the newcomer's subscription is swallowed as a
            // duplicate — it then receives no chart announcements at all.
            // See drain_subscriptions().
            sock->set(zmq::sockopt::xpub_verbose, 1);
            // Silently drops past SNDHWM (never blocks the publisher).
            // 100k * ~512B payload ≈ 51MB per subscriber pipe — buys ~30 min
            // of SUB stall at 50 tps, still safe inside TS's 32-bit address
            // space. Drops past this point are invisible here; the `seq`
            // field is what lets the subscriber notice them.
            sock->set(zmq::sockopt::sndhwm, 100000);
            sock->set(zmq::sockopt::linger, 0);
            sock->bind(zmq_endpoint);

            g_ctx  = ctx;
            g_sock = sock;
            // New publisher session: stamp its id and restart every counter.
            // Only on the first bind — a second chart, or a re-Verify, must
            // not look like a publisher restart to subscribers.
            g_sid = recv_unix_microseconds();
            g_seq.clear();
            g_charts.clear();
            g_sub_topics.clear();
            pin_self_module_once();  // stay resident for the life of the host
        }

        // Register before draining: if the drain finds a consumer attaching
        // right now, this chart is announced by the re-announce sweep rather
        // than being missed until its next bar.
        //
        // An INDEX, not a pointer. drain_subscriptions() below does not
        // resize g_charts today, and a pointer would be silently invalidated
        // the day it does.
        std::size_t idx = g_charts.size();
        for (std::size_t i = 0; i < g_charts.size(); ++i) {
            const Chart& c = g_charts[i];
            if (c.symbol == symbol && c.category == category &&
                c.bar_type == bar_type && c.bar_interval == bar_interval) {
                idx = i;
                break;
            }
        }
        if (idx == g_charts.size()) {
            g_charts.push_back(Chart{std::string(symbol), category,
                                     bar_type, bar_interval, false});
        }
        // Whether this chart was ALREADY known and announced before this
        // call. Read before the drain, because the drain's re-announce sweep
        // can set `announced` on a chart this very call just registered —
        // and reporting that as rc 1 ("nothing to do") would be a lie about
        // who did the announcing.
        const bool announced_before = g_charts[idx].announced;

        drain_subscriptions();

        // Nobody is listening yet. Not a failure: TradeStation routinely
        // starts before the consumer does. The indicator leaves InitDone
        // False on a negative rc and calls again on the next bar, so this
        // resolves itself the moment the consumer comes up — and until it
        // does, no publish is attempted into a socket that would drop it.
        if (!control_topic_subscribed()) return -7;

        if (!g_charts[idx].announced && !send_hello(g_charts[idx])) return -2;

        // 0 = this call is what put the chart on the wire, whether it sent
        // the hello itself or the attach sweep did it a microsecond earlier.
        // 1 = the chart was already announced before this call.
        return announced_before ? 1 : 0;
    } catch (const zmq::error_t&) {
        return -3;
    } catch (...) {
        return -3;
    }
}

// ---- tombstones -----------------------------------------------------------
//
// The publish exports of the superseded protocol. They still exist, and they
// never publish anything.
//
// EL_PublishTick and EL_PublishBar kept their names but changed arity. Under
// __stdcall the callee pops the arguments, so an .ELD built against the old
// signatures would corrupt the stack rather than fail — TradeStation
// misbehaves or dies, and there is no return code to look at.
//
// These no longer stop anything by themselves. The guard used to be that
// every publish sat behind an init export whose name had changed, so an old
// .ELD failed at init and never reached them. EL_Init's name is now reused
// with five parameters instead of one, so an old .ELD corrupts the stack
// inside EL_Init first. Keeping these exported can still only help: a
// missing export produces a DefineDLLFunc failure that names no cause,
// while -6 puts a readable line in the Print Log.
TS2P_API int TS2P_CALL EL_PublishTick(
    const char* /*symbol*/, const char* /*el_timestamp*/, double /*price*/,
    double /*volume*/, double /*ticks*/, double /*upticks*/,
    double /*downticks*/, double /*open_interest*/,
    double /*bid*/, double /*ask*/) {
    return -6;
}

TS2P_API int TS2P_CALL EL_PublishBar(
    const char* /*symbol*/, const char* /*el_timestamp*/,
    int /*bar_type*/, int /*bar_interval*/,
    double /*bar_open*/, double /*bar_high*/, double /*bar_low*/,
    double /*bar_close*/, double /*volume*/, double /*ticks*/,
    double /*upticks*/, double /*downticks*/, double /*open_interest*/) {
    return -6;
}


TS2P_API int TS2P_CALL EL_Publish(
    const char* symbol,
    const char* el_timestamp,
    int         bar_type,
    int         bar_interval,
    int         category,
    double      bar_open,
    double      bar_high,
    double      bar_low,
    double      bar_close,
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

        // A consumer may have restarted since the last point. Draining here
        // is what re-announces every known chart to it — EL_Init runs once
        // per chart and will not run again for one already on screen.
        drain_subscriptions();

        const std::uint64_t seq = reserve_seq(symbol);

        char bid_s[32], ask_s[32];
        format_quote(bid_s, sizeof(bid_s), bid);
        format_quote(ask_s, sizeof(ask_s), ask);

        char payload[768];
        const int n = std::snprintf(
            payload, sizeof(payload),
            "{\"proto\":2,\"seq\":%llu,\"sid\":%llu,"
            "\"ts\":%.6f,\"ts_str\":\"%s\","
            "\"bar_type\":%d,\"bar_interval\":%d,\"category\":%d,"
            "\"o\":%.6f,\"h\":%.6f,\"l\":%.6f,\"c\":%.6f,"
            "\"el_volume\":%lld,\"el_ticks\":%lld,\"el_upticks\":%lld,"
            "\"el_downticks\":%lld,\"el_open_interest\":%lld,"
            "\"bid\":%s,\"ask\":%s}",
            static_cast<unsigned long long>(seq),
            static_cast<unsigned long long>(g_sid),
            recv_unix_seconds(), ts_str,
            bar_type, bar_interval, category,
            bar_open, bar_high, bar_low, bar_close,
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

TS2P_API int TS2P_CALL EL_Shutdown(void) {
    // Only safe to call from a regular process path (e.g. the standalone
    // test harness). Not called from the EL indicator because EL has no
    // unload hook — the DLL is pinned instead, see pin_self_module_once().
    std::lock_guard<std::mutex> lock(g_mutex);
    delete g_sock; g_sock = nullptr;
    delete g_ctx;  g_ctx  = nullptr;
    g_seq.clear();
    g_charts.clear();
    g_sub_topics.clear();
    g_sid = 0;
    return 0;
}

}  // extern "C"
