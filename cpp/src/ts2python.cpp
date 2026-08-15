// TS2Python bridge implementation — see ../contract/ for the wire format
// and ../contract/error_codes.md for the return codes enforced here.

#include "ts2python.h"

#include <zmq.hpp>

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>
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

// 4, not 3: the init export was renamed (EL_Init -> EL_InitChart) to put the
// signature-change gate back. Renaming an export IS an ABI change, which is
// exactly what this number is for. The WIRE is untouched — point frames are
// still `proto` 2 and every recorded fixture stays valid.
constexpr int kDllVersion = 4;

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

// Fixed inproc endpoint for the DLL's own socket monitor (see g_monitor
// below). One socket per process, so a fixed name can never collide with a
// second instance of anything in this process.
constexpr char kMonitorEndpoint[] = "inproc://ts2py.monitor";

std::mutex       g_mutex;
// Raw pointers, never destroyed implicitly. See pin_self_module_once()
// for the full rationale — short version: zmq_ctx_term() joins the ZMQ
// I/O thread, which deadlocks / crashes when called under the Windows
// loader lock during DLL_PROCESS_DETACH. Keeping globals as raw pointers
// means static teardown is a no-op. Explicit cleanup stays available in
// EL_Shutdown for the standalone test harness path.
zmq::context_t*  g_ctx  = nullptr;
zmq::socket_t*   g_sock = nullptr;
// PAIR socket wired to g_sock's own event stream (ZMQ_EVENT_DISCONNECTED
// only, via zmq_socket_monitor). It exists for one reason: this process is
// now on the CONNECT side of its socket, and libzmq keeps a connect-side
// pipe alive across a TCP reconnect (ZMQ_IMMEDIATE=0, the default) — so
// when the hub dies, the pipe is never terminated, xpub_t::xpipe_terminated
// never runs, and no unsubscribe is ever generated. Without this monitor,
// g_sub_topics would never learn the hub is gone. See "L4" in
// docs/plans/transport-hub-2026-08-15.md.
//
// Same raw-pointer / no-destructor rule as g_ctx and g_sock, same reason —
// see pin_self_module_once(). Created once, alongside g_sock, in
// EL_InitChart's first-caller branch; closed in EL_Shutdown BEFORE g_sock
// and g_ctx.
zmq::socket_t*   g_monitor = nullptr;

// The endpoint g_sock is actually connected to.
//
// Only the first chart to reach EL_InitChart creates the socket; every later
// chart passes its own `zmq_endpoint` and gets that same socket. Without
// recording what it is connected to, a chart configured with a different
// endpoint was registered, announced and reported as success while
// publishing through the connection the FIRST chart made — a consumer on
// the endpoint that chart names receives nothing, forever, with no rc and no
// log line to say why. Comparing against this is what turns that into -8.
std::string      g_endpoint;

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
// once per successful EL_InitChart.
//
// It is microseconds, not seconds. At one-second resolution two sessions
// starting inside the same wall-clock second — a test harness rerun, or a
// script doing EL_Shutdown + EL_InitChart — share an id, so the subscriber reads
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
// One entry per chart that has called EL_InitChart. TradeStation runs the
// indicator once per chart, each with its own symbol and interval, and all
// of them share this one DLL and one socket.
//
// The registry exists so a consumer restart does not need TradeStation
// touched. EL_InitChart runs once per chart, on its first bar; if the only
// record of a chart were that call, a consumer that started, stopped and
// started again would never learn what is attached until every chart was
// re-Verified by hand. Instead the DLL re-announces everything it knows the
// moment a subscriber appears.
//
// A plain vector: a TradeStation workspace holds a handful of charts, and
// the linear scan happens once per chart on its first bar and once per
// subscriber attach. Guarded by g_mutex like everything else here.
struct Chart {
    std::string   symbol;
    int           category;
    int           bar_type;
    int           bar_interval;
    bool          announced;
    // A publish for this chart reached nobody and that has ALREADY been
    // reported. Cleared the moment a subscriber for the symbol reappears, so
    // each no-subscriber episode costs one rc instead of one per bar.
    bool          void_reported;
    // Ordering only, never a clock: bumped on registration and on every
    // publish, so the overflow policy below can evict whichever chart has
    // gone longest without publishing.
    std::uint64_t last_used;
};
std::vector<Chart> g_charts;

// Hard cap on the registry, and the counter the eviction order reads.
//
// Entries are never removed on their own: the wire carries no chart-closed
// signal and TradeStation gives EasyLanguage no per-chart unload hook, so a
// chart whose symbol or interval is edited leaves its old 4-tuple behind for
// good. Unbounded, a day of symbol-hopping grows this vector without limit
// inside a 32-bit process, and both the EL_InitChart scan and the announce sweep
// then run over every dead entry under g_mutex on the publish path.
//
// 24 is past any real workspace; it bounds growth rather than being reached.
// On overflow the LEAST RECENTLY PUBLISHED entry goes, not the oldest — the
// oldest is usually the first chart opened and still live, while the entries
// worth losing are exactly the ones that have not published since they were
// abandoned.
constexpr std::size_t kMaxCharts = 24;
std::uint64_t         g_use_tick = 0;

// Topics with at least one subscriber attached, as reported by XPUB.
//
// XPUB (not PUB) is what makes EL_InitChart able to answer "is anyone actually
// listening". A subscription arrives on the socket as a readable message:
// 0x01 followed by the topic on subscribe, 0x00 on unsubscribe. Non-verbose
// XPUB reports only the first subscriber per topic and only the last
// unsubscribe, which is exactly the "is at least one attached" question
// being asked here.
std::unordered_set<std::string> g_sub_topics;

// Would a subscription to `sub` deliver a message published on `topic`?
//
// PREFIX MATCH, not equality. ZMQ_SUBSCRIBE is a prefix filter, so a
// subscriber that asked for "" gets every topic, and one that asked for "__"
// gets the control topic too. Matching on equality left EL_InitChart returning -7
// forever against a perfectly good subscriber — which is exactly what
// `contract/tools/record.py` does by default, and it deadlocked the whole
// publisher.
bool subscription_covers(const std::string& sub, const std::string& topic) {
    return sub.size() <= topic.size() && topic.compare(0, sub.size(), sub) == 0;
}

bool covers_control_topic(const std::string& s) {
    return subscription_covers(s, kControlTopic);
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

// True once a subscription is attached that would deliver THIS SYMBOL'S
// topic. Must be called with g_mutex held.
//
// Deliberately not the control topic: a consumer subscribes per symbol, so
// "a hello would reach someone" and "this point will reach someone" are
// different questions. A chart on a symbol the consumer never asked for is
// announced but never delivered, and that is precisely the case EL_Publish
// used to report as rc 0.
bool symbol_subscribed(const char* symbol) {
    const std::string topic(symbol);
    for (const std::string& s : g_sub_topics) {
        if (subscription_covers(s, topic)) return true;
    }
    return false;
}

// Index of the chart with this identity, or g_charts.size() if unknown.
// Must be called with g_mutex held.
std::size_t find_chart(const char* symbol, int category,
                       int bar_type, int bar_interval) {
    for (std::size_t i = 0; i < g_charts.size(); ++i) {
        const Chart& c = g_charts[i];
        if (c.symbol == symbol && c.category == category &&
            c.bar_type == bar_type && c.bar_interval == bar_interval) {
            return i;
        }
    }
    return g_charts.size();
}

// Add a chart, evicting the least recently published entry if the registry
// is already at kMaxCharts. Must be called with g_mutex held.
std::size_t register_chart(const char* symbol, int category,
                           int bar_type, int bar_interval) {
    if (g_charts.size() >= kMaxCharts) {
        std::size_t victim = 0;
        for (std::size_t i = 1; i < g_charts.size(); ++i) {
            if (g_charts[i].last_used < g_charts[victim].last_used) victim = i;
        }
        g_charts.erase(g_charts.begin() +
                       static_cast<std::ptrdiff_t>(victim));
    }
    g_charts.push_back(Chart{std::string(symbol), category, bar_type,
                             bar_interval, false, false, ++g_use_tick});
    return g_charts.size() - 1;
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
// EL_InitChart. TradeStation calls FreeLibrary when the user disables or
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

// Put the socket back on a message boundary after a two-frame send was
// abandoned between its frames. Must be called with g_mutex held, and only
// when g_sock exists.
//
// ZMQ MULTIPART IS STATEFUL. Once the topic frame has been accepted with
// `sndmore`, the socket is mid-message: whatever is sent next becomes a
// CONTINUATION of that message rather than the start of a new one. So a body
// frame that fails does not merely lose its own message — it silently
// re-frames every message after it. The next EL_Publish's topic arrives as
// frame 3 of the abandoned hello, which means XPUB routes the point to
// whoever matched `__ts2py__` instead of to the symbol's subscribers, and a
// consumer calling recv_multipart() gets four frames where the binding
// unpacks two.
//
// An empty terminating frame closes the message. It can fail too, and then
// the socket is past saving from here — but it must never mask the failure
// that got us here, so it is swallowed.
void close_abandoned_message() {
    try {
        zmq::message_t empty;
        (void)g_sock->send(empty, zmq::send_flags::none);
    } catch (...) {
        // Nothing further to try.
    }
}

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
    // A failed topic frame queues nothing, so the socket is still on a
    // boundary and there is nothing to close. Once it succeeds, every exit
    // from here has to leave the socket on a boundary again.
    if (!g_sock->send(topic, zmq::send_flags::sndmore)) return false;
    try {
        if (!g_sock->send(body, zmq::send_flags::none)) {
            close_abandoned_message();
            return false;
        }
    } catch (...) {
        close_abandoned_message();
        throw;  // the caller's handler decides the rc; the socket is clean
    }

    c.announced = true;
    return true;
}

// Drain g_monitor's event queue non-blockingly. Must be called with g_mutex
// held, and only when g_monitor exists.
//
// libzmq keeps a CONNECT-side pipe alive across a TCP reconnect
// (ZMQ_IMMEDIATE=0, the default), so when the hub dies the pipe is NOT
// terminated, xpub_t::xpipe_terminated never runs on it, and NO unsubscribe
// is ever generated — g_sub_topics would stay populated forever, and
// EL_Publish would keep returning 0 for bars that reach nobody. This
// monitor is the only way this process learns the hub is gone. See "L4" in
// docs/plans/transport-hub-2026-08-15.md.
//
// A monitor message is TWO frames: a 6-byte event id + value, then the
// endpoint address string. Both are read every iteration so the socket is
// never left mid-message — the same hazard close_abandoned_message() exists
// for on g_sock.
//
// A read failure here is swallowed, unconditionally, and NEVER becomes a
// caller-visible rc — there is no -9-shaped code for it, on purpose. -9
// exists to keep -7 honest about "is anyone subscribed", a question this
// function does not answer, so failing EL_InitChart over a monitor hiccup
// would conflate two different facts on every subsequent init.
void drain_monitor() {
    if (!g_monitor) return;
    try {
        zmq::message_t event_frame;
        zmq::message_t addr_frame;
        while (g_monitor->recv(event_frame, zmq::recv_flags::dontwait)) {
            // The address frame follows; read it so the socket stays on a
            // message boundary for the next iteration.
            //
            // GUARDED BY more(), AND NON-BLOCKING, both deliberately. This
            // runs under g_mutex on the publish path, so a recv that blocks
            // here does not merely stall a drain — it freezes every chart in
            // the process, inside TradeStation, with no way out. ZMQ delivers
            // a multipart message to the receiver atomically, so dontwait is
            // guaranteed to find the second frame; the guard costs nothing
            // and means a monitor protocol that ever stops being two frames
            // degrades to a dropped event instead of a hang.
            if (event_frame.more()) {
                (void)g_monitor->recv(addr_frame, zmq::recv_flags::dontwait);
            }

            std::uint16_t event_id = 0;
            if (event_frame.size() >= sizeof(event_id)) {
                std::memcpy(&event_id, event_frame.data(), sizeof(event_id));
            }
            if (event_id != ZMQ_EVENT_DISCONNECTED) continue;

            // The hub is gone. Clear what it takes for EL_Publish to start
            // reporting -10 again, and re-arm every chart's hello so the
            // consumer that comes back with it hears from all of them —
            // the same recovery a fresh subscribe already gets below.
            g_sub_topics.clear();
            for (auto& c : g_charts) {
                c.announced = false;
            }
        }
    } catch (...) {
        // Never propagates. See the function comment: no rc exists for "the
        // monitor queue could not be read", on purpose.
    }
}

// Read whatever subscription traffic XPUB has queued, and re-announce every
// known chart when a consumer attaches. Must be called with g_mutex held.
//
// Called from both EL_InitChart and EL_Publish. EL_InitChart alone would not be
// enough: it runs once per chart, on that chart's first bar, so a consumer
// restarting an hour later would find nothing announcing itself and no
// second EL_InitChart coming. Draining on every publish is what makes the
// consumer restartable without touching TradeStation.
//
// RETURNS false if the queue could not be drained, and THE TWO CALLERS WANT
// OPPOSITE THINGS with that:
//
//   EL_InitChart must fail (-9). Its entire output is the answer to "is anyone
//   listening", and a swallowed recv error leaves g_sub_topics empty, which
//   is indistinguishable from "nobody has subscribed yet" — so init returned
//   -7, which the indicator treats as the normal startup state and prints
//   exactly once. The consumer is up, nothing publishes all session, and
//   there is no error on either side.
//
//   EL_Publish must NOT fail. It drains BEFORE it sends, so letting a drain
//   error out would skip the send entirely — and EasyLanguage does not retry
//   a failed publish, so a transient EINTR would DELETE a real bar rather
//   than delay it. Subscription bookkeeping is not worth a data point; the
//   next publish drains again.
bool drain_subscriptions() {
    if (!g_sock) return true;

    bool drained = true;
    try {
        zmq::message_t msg;
        // NOTE: the announce pass is deliberately OUTSIDE this try, so a recv
        // that throws mid-queue still announces whatever was already learned.
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
            // six-second gap got both. XPUB_VERBOSE (set at socket creation
            // below) is what makes this reliable: without it XPUB reports
            // only the FIRST subscriber per topic and the overlapping one is
            // invisible.
            //
            // The cost is a duplicate hello for a consumer that subscribes
            // to two topics both covering this one (say "" and __ts2py__).
            // A repeated announcement is idempotent downstream; a missing
            // one leaves the consumer blind to the whole workspace.
            //
            // Its predecessor's hellos died with it and TradeStation will
            // not call EL_InitChart again for a chart already on screen, so
            // everything this DLL knows goes out again.
            //
            // Only the INVALIDATION happens here. The sending is done by the
            // pass below, which runs on every drain rather than only on a
            // subscribe — that is what makes a hello that failed get retried
            // on the next publish instead of waiting for the next attach.
            for (auto& c : g_charts) {
                c.announced = false;
            }
        }
    } catch (...) {
        // RECORDED, not hidden. This used to be swallowed outright, which
        // turned a broken socket into rc -7 — "retryable, and the normal
        // state at startup". The caller decides what it costs.
        drained = false;
    }

    // Hub-disconnect detection. Deliberately does not touch `drained` — see
    // drain_monitor()'s own comment for why a monitor read failure is never
    // conflated with "the subscription queue could not be read" (-9).
    drain_monitor();

    // Announce every chart not yet announced to the CURRENT subscriber set.
    //
    // Per chart try/catch, not one around the loop: a single chart's hello
    // failing used to abort the sweep and the rest of the subscription queue
    // with it, leaving charts 3..N with announced == false and nothing that
    // would ever retry them — the sweep only ran on a new subscribe, and
    // TradeStation does not call EL_InitChart again for a chart already on screen.
    if (!control_topic_subscribed()) return drained;
    for (auto& c : g_charts) {
        if (c.announced) continue;
        try {
            send_hello(c);  // sets announced only when both frames went out
        } catch (...) {
            // Left unannounced on purpose: the next drain retries it. It does
            // NOT clear `drained` — this loop announces OTHER charts, and one
            // of them failing is not a reason to fail the caller's own init.
        }
    }
    return drained;
}

}  // namespace

extern "C" {

TS2P_API int TS2P_CALL EL_DllVersion(void) {
    return kDllVersion;
}

TS2P_API int TS2P_CALL EL_InitChart(const char* zmq_endpoint,
                                    const char* symbol,
                                    int         category,
                                    int         bar_type,
                                    int         bar_interval) {
    if (zmq_endpoint == nullptr || symbol == nullptr) return -4;
    try {
        std::lock_guard<std::mutex> lock(g_mutex);

        if (!g_sock) {
            // RAII UNTIL EVERY STEP BELOW SUCCEEDS, then hand ownership to
            // the raw globals. This process is now the CONNECT side: a hub
            // binds zmq_endpoint and every chart process connects to it, and
            // connect() does not fail because someone else already holds the
            // endpoint — only bind() ever did that. So -3 is rare now, not
            // the ordinary case it used to be. But the indicator still
            // retries EL_InitChart on EVERY bar of EVERY chart while
            // InitDone is False, with no backoff, so whatever CAN still
            // throw here (a malformed endpoint string, the process being out
            // of sockets, a monitor registration failure) must still cost
            // nothing to fail repeatedly: a leaked context has already
            // started libzmq's I/O and reaper threads, and inside 32-bit
            // TradeStation that exhausts threads and address space in
            // seconds once several charts are loaded, with libzmq's own
            // win_assert then aborting the host process.
            //
            // ctx is declared FIRST so it is destroyed LAST: both sockets
            // below must close before zmq_ctx_term() runs, and linger is set
            // to 0 on the publish socket so closing never blocks.
            std::unique_ptr<zmq::context_t> ctx(new zmq::context_t(1));
            // XPUB, not PUB. Same send semantics; the difference is that a
            // subscription arrives as a readable message, which is the only
            // way this side can answer "is anyone actually listening" — and
            // without that, init reports success into a void.
            std::unique_ptr<zmq::socket_t> sock(
                new zmq::socket_t(*ctx, zmq::socket_type::xpub));
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

            // Socket monitor — registered BEFORE connect(), so a disconnect
            // can never race the connection it is meant to observe. It is
            // the only signal this process gets that the hub is gone; the
            // why and the how are on g_monitor and drain_monitor() above.
            // ZMQ_EVENT_DISCONNECTED is the one event this design acts on,
            // so it is the only one requested.
            if (zmq_socket_monitor(sock->handle(), kMonitorEndpoint,
                                   ZMQ_EVENT_DISCONNECTED) != 0) {
                throw zmq::error_t();
            }
            std::unique_ptr<zmq::socket_t> monitor(
                new zmq::socket_t(*ctx, zmq::socket_type::pair));
            monitor->connect(kMonitorEndpoint);

            sock->connect(zmq_endpoint);

            g_ctx      = ctx.release();
            g_sock     = sock.release();
            g_monitor  = monitor.release();
            g_endpoint = zmq_endpoint;
            // New publisher session: stamp its id and restart every counter.
            // Only on the first successful connect — a second chart, or a
            // re-Verify, must not look like a publisher restart to
            // subscribers.
            g_sid = recv_unix_microseconds();
            g_seq.clear();
            g_charts.clear();
            g_sub_topics.clear();
            pin_self_module_once();  // stay resident for the life of the host
        } else if (g_endpoint != zmq_endpoint) {
            // A later chart asking for a DIFFERENT endpoint. Only the first
            // chart creates the socket, so this one's points would go to the
            // endpoint that chart connected to while a consumer on the
            // endpoint THIS chart names receives nothing — and it used to be
            // told rc 0, print "publishing starts now", and leave no trace of
            // the substitution anywhere. Refusing is the only answer that is
            // not a lie.
            return -8;
        }

        // A drain that failed cannot answer the question the next line asks.
        // Reporting -7 off an empty g_sub_topics would say "nobody has
        // subscribed yet" about a socket that could not be read at all.
        if (!drain_subscriptions()) return -9;

        // Nobody is listening yet. Not a failure: TradeStation routinely
        // starts before the consumer does. The indicator leaves InitDone
        // False on a negative rc and calls again on the next bar, so this
        // resolves itself the moment the consumer comes up — and until it
        // does, no publish is attempted into a socket that would drop it.
        if (!control_topic_subscribed()) return -7;

        // Register only AFTER the gate. Registering first put charts whose
        // init had NOT succeeded into the registry, where the attach sweep
        // announced them anyway: the consumer was told a chart was live while
        // its indicator still had InitDone False and was publishing nothing,
        // and that chart's first successful init then reported rc 1 — whose
        // documented meaning is "already announced, nothing to do". A chart
        // that gets -7 calls again on its next bar, so nothing is lost by
        // waiting until there is someone to announce it to.
        std::size_t idx = find_chart(symbol, category, bar_type, bar_interval);
        if (idx == g_charts.size()) {
            idx = register_chart(symbol, category, bar_type, bar_interval);
        }
        // Whether this chart was already known AND announced before this
        // call — that is the whole difference between rc 1 and rc 0. The
        // drain above ran before the chart was registered, so nothing can
        // have announced it behind this call's back.
        const bool announced_before = g_charts[idx].announced;

        if (!announced_before && !send_hello(g_charts[idx])) return -2;

        // 0 = this call is what put the chart on the wire.
        // 1 = the chart was already announced before this call.
        return announced_before ? 1 : 0;
    } catch (const zmq::error_t&) {
        // Context/socket creation failed, a sockopt or connect() rejected
        // the endpoint string, or the monitor could not be registered.
        // Bind exclusivity is gone now that this process only ever
        // connects, so this used to be the ordinary case and now rarely
        // fires.
        return -3;
    } catch (...) {
        return -3;
    }
}

// ---- tombstones -----------------------------------------------------------
//
// Exports of the superseded protocol. They still exist, they publish nothing,
// and every one of them keeps its OLD signature on purpose.
//
// That is the entire mechanism. __stdcall has the callee pop the arguments,
// so an export whose NAME outlives a signature change corrupts the caller's
// stack instead of returning an error — TradeStation misbehaves or dies and
// there is no return code to look at. Keeping the old name bound to the old
// ARITY means a stale .ELD's call balances, lands on a function that does
// nothing, and gets a readable -6 in the Print Log.
//
// EL_Init is one of these again because the init export is now EL_InitChart.
// A revision of this DLL reused the name `EL_Init` for a five-parameter
// function, which gave the gate away: a stale one-argument call resolved it,
// ran it, and corrupted the stack inside init before reaching any publish.
// The rename puts the guard back in BOTH directions — a stale .ELD lands
// here, and a current .ELD run against an older DLL finds no EL_InitChart
// and fails at Verify before anything executes.
//
// Do not delete these until it is safe to assume no old .ELD survives.
TS2P_API int TS2P_CALL EL_Init(const char* /*zmq_endpoint*/) {
    return -6;
}

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
        // is what re-announces every known chart to it — EL_InitChart runs once
        // per chart and will not run again for one already on screen.
        //
        // The result is deliberately IGNORED. Unlike init, this call has a
        // real data point in hand and drains before sending it, so failing
        // here would drop the bar — and EL does not retry a failed publish.
        // The next publish drains again.
        (void)drain_subscriptions();

        // WILL THIS POINT REACH ANYONE? The -7 gate only ever covered init.
        // EasyLanguage latches InitDone True on the first success and never
        // runs the init block again, so once the consumer restarts mid-session
        // every frame in the gap goes into a socket with no subscriber — ZMQ
        // discards it and reports success, and this returned 0 for a bar that
        // no longer exists anywhere. Nothing backfills it.
        //
        // The question is per SYMBOL, not the control topic: a consumer
        // subscribes to the symbols it was configured with, so a chart on one
        // it never asked for is announced and then silently undelivered.
        //
        // Reported once per EPISODE. An ordinary consumer restart would
        // otherwise put a line in the Print Log for every chart on every bar,
        // and the indicator prints any negative rc unconditionally.
        const bool        heard = symbol_subscribed(symbol);
        const std::size_t idx =
            find_chart(symbol, category, bar_type, bar_interval);
        if (idx != g_charts.size()) g_charts[idx].last_used = ++g_use_tick;

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

        // Same boundary rule as send_hello: once the topic frame is in, the
        // socket is mid-message and every exit has to close it. Leaving it
        // open re-frames the NEXT chart's point onto this abandoned one.
        const auto r1 = g_sock->send(topic, zmq::send_flags::sndmore);
        if (!r1) return -2;
        try {
            const auto r2 = g_sock->send(body, zmq::send_flags::none);
            if (!r2) {
                close_abandoned_message();
                return -2;
            }
        } catch (...) {
            close_abandoned_message();
            throw;  // caught below and reported as -2
        }

        // The frame went out. Whether it went anywhere is the other question.
        if (idx == g_charts.size()) {
            // No registry entry to latch on — evicted at kMaxCharts, or a
            // publish that never went through init. Report every time rather
            // than never.
            return heard ? 0 : -10;
        }
        if (heard) {
            g_charts[idx].void_reported = false;
            return 0;
        }
        if (g_charts[idx].void_reported) return 0;  // already said so
        g_charts[idx].void_reported = true;
        return -10;
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
    //
    // The try/catch is not decoration. This is an extern "C" __stdcall
    // boundary and EasyLanguage cannot unwind a C++ exception across it, so
    // one escaping here terminates TradeStation rather than returning a code
    // — and the .ELD does bind this export, which puts it within reach of any
    // script in the workspace. lock_guard can throw std::system_error, and
    // zmq_close / zmq_ctx_term run inside the deletes below.
    try {
        std::lock_guard<std::mutex> lock(g_mutex);
        // Monitor first: it holds a PAIR socket connected to an inproc
        // endpoint g_sock's context owns, so it must go before g_sock and
        // g_ctx for the same reason ctx is declared last of the three when
        // they are created — see g_monitor's own comment.
        delete g_monitor; g_monitor = nullptr;
        delete g_sock;    g_sock    = nullptr;
        delete g_ctx;     g_ctx     = nullptr;
        g_endpoint.clear();
        g_seq.clear();
        g_charts.clear();
        g_sub_topics.clear();
        g_sid = 0;
        return 0;
    } catch (...) {
        return -3;
    }
}

}  // extern "C"
