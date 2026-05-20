// TS2Python bridge implementation — see docs/design.md §3.2 / §5
// and docs/error_codes.md for the semantics enforced here.

#include "ts2python.h"

#include <zmq.hpp>

#include <chrono>
#include <cstdio>
#include <cstring>
#include <mutex>

#if defined(_WIN32)
#  ifndef WIN32_LEAN_AND_MEAN
#    define WIN32_LEAN_AND_MEAN
#  endif
#  include <windows.h>
#endif

namespace {

constexpr int kDllVersion = 6;

std::mutex       g_mutex;
// Raw pointers, never destroyed implicitly. See pin_self_module_once()
// for the full rationale — short version: zmq_ctx_term() joins the ZMQ
// I/O thread, which deadlocks / crashes when called under the Windows
// loader lock during DLL_PROCESS_DETACH. Keeping globals as raw pointers
// means static teardown is a no-op. Explicit cleanup stays available in
// EL_Shutdown for the standalone test harness path.
zmq::context_t*  g_ctx  = nullptr;
zmq::socket_t*   g_sock = nullptr;

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

// Parse EL-formatted bar time "yyyy-MM/dd-HH:mm:ss" 24-hour (e.g. "2026-04/18-13:30:45")
// into unix epoch seconds (UTC). The EL-supplied timestamp is *always*
// America/New_York wall-clock (TradeStation chart TZ for US equities), so
// we interpret it explicitly via std::chrono::zoned_time rather than the
// host-local std::mktime() path used in v4 (which silently produced the
// wrong epoch on non-ET hosts — e.g. -8h on TPE in EST, -12h in EDT).
//
// v6 switched from 12-hour+tt ("01:30:45 PM") to 24-hour ("13:30:45")
// because Windows FormatTime("tt") emits the locale's AM/PM designator —
// "上午"/"下午" on zh-TW hosts — which neither the sscanf %2s here nor
// Python's %p strptime can match, collapsing every bar onto today's
// receive-time minute.
//
// On Windows the IANA tzdb comes from system ICU (Win10 1903+); on Linux
// it reads /usr/share/zoneinfo. Returns false on parse error or if the
// requested zone is unknown — caller emits ts_utc = 0.0 in that case and
// the Python side falls back to parsing ts_str directly.
bool parse_el_timestamp_to_utc(const char* s, double* out_epoch) {
    if (s == nullptr || out_epoch == nullptr) return false;
    if (s[0] == '\0') return false;

    int year = 0, mon = 0, day = 0, hour = 0, minute = 0, sec = 0;
    const int matched = std::sscanf(
        s, "%4d-%2d/%2d-%2d:%2d:%2d",
        &year, &mon, &day, &hour, &minute, &sec);
    if (matched != 6) return false;
    if (hour < 0 || hour > 23) return false;

    using namespace std::chrono;
    try {
        const auto ymd = year_month_day{
            std::chrono::year{year},
            std::chrono::month{static_cast<unsigned>(mon)},
            std::chrono::day{static_cast<unsigned>(day)},
        };
        if (!ymd.ok()) return false;

        const local_seconds local_t =
            local_days{ymd} + hours{hour} + minutes{minute} + seconds{sec};
        const auto zoned = zoned_time{"America/New_York", local_t};
        const sys_seconds utc = zoned.get_sys_time();
        *out_epoch = static_cast<double>(utc.time_since_epoch().count());
        return true;
    } catch (...) {
        // Unknown tz, ambiguous local time during DST fold, or out-of-range —
        // signal failure, caller emits ts_utc = 0.0 and Python re-parses ts_str.
        return false;
    }
}

}  // namespace

extern "C" {

TS2P_API int TS2P_CALL EL_DllVersion(void) {
    return kDllVersion;
}

TS2P_API int TS2P_CALL EL_Init(const char* zmq_endpoint) {
    if (zmq_endpoint == nullptr) return -4;
    try {
        std::lock_guard<std::mutex> lock(g_mutex);
        if (g_sock) return 1;  // idempotent re-init

        auto* ctx  = new zmq::context_t(1);
        auto* sock = new zmq::socket_t(*ctx, zmq::socket_type::pub);
        // PUB silently drops past SNDHWM (PUB never blocks publisher).
        // 100k * ~448B payload ≈ 45MB per subscriber pipe — buys ~30 min
        // of SUB stall at 50 tps, still safe inside TS's 32-bit address space.
        sock->set(zmq::sockopt::sndhwm, 100000);
        sock->set(zmq::sockopt::linger, 0);
        sock->bind(zmq_endpoint);

        g_ctx  = ctx;
        g_sock = sock;
        pin_self_module_once();  // stay resident for the life of the host
        return 0;
    } catch (const zmq::error_t&) {
        return -3;
    } catch (...) {
        return -3;
    }
}

TS2P_API int TS2P_CALL EL_PublishTick(
    const char* symbol,
    const char* el_timestamp,
    double      price,
    double      volume,
    double      bid,
    double      ask,
    double      tick_count)
{
    if (symbol == nullptr) return -4;

    double ts_utc_epoch = 0.0;
    parse_el_timestamp_to_utc(el_timestamp, &ts_utc_epoch);  // 0.0 on failure
    const char* ts_str = (el_timestamp != nullptr) ? el_timestamp : "";

    try {
        std::lock_guard<std::mutex> lock(g_mutex);
        if (!g_sock) return -1;

        char payload[448];
        const int n = std::snprintf(
            payload, sizeof(payload),
            "{\"v\":1,\"kind\":\"tick\",\"ts\":%.6f,\"ts_utc\":%.6f,\"ts_str\":\"%s\","
            "\"px\":%.6f,\"vol\":%.6f,"
            "\"bid\":%.6f,\"ask\":%.6f,\"tc\":%.0f}",
            recv_unix_seconds(), ts_utc_epoch, ts_str, price, volume, bid, ask, tick_count);
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

TS2P_API int TS2P_CALL EL_PublishTickEx(
    const char* symbol,
    const char* el_timestamp,
    double      bar_open,
    double      bar_high,
    double      bar_low,
    double      bar_close,
    double      volume,
    double      bid,
    double      ask,
    double      tick_count)
{
    if (symbol == nullptr) return -4;

    double ts_utc_epoch = 0.0;
    parse_el_timestamp_to_utc(el_timestamp, &ts_utc_epoch);  // 0.0 on failure
    const char* ts_str = (el_timestamp != nullptr) ? el_timestamp : "";

    try {
        std::lock_guard<std::mutex> lock(g_mutex);
        if (!g_sock) return -1;

        char payload[512];
        const int n = std::snprintf(
            payload, sizeof(payload),
            "{\"v\":1,\"kind\":\"bar_1m\",\"ts\":%.6f,\"ts_utc\":%.6f,\"ts_str\":\"%s\","
            "\"o\":%.6f,\"h\":%.6f,\"l\":%.6f,\"c\":%.6f,"
            "\"vol\":%.6f,\"bid\":%.6f,\"ask\":%.6f,\"tc\":%.0f}",
            recv_unix_seconds(), ts_utc_epoch, ts_str,
            bar_open, bar_high, bar_low, bar_close,
            volume, bid, ask, tick_count);
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
    return 0;
}

}  // extern "C"
