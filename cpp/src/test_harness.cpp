// Standalone test harness: exercises TS2Python.dll without TradeStation.
//
// Usage:
//   TS2Python_TestHarness.exe                                 # default run
//   TS2Python_TestHarness.exe --endpoint tcp://127.0.0.1:5555
//   TS2Python_TestHarness.exe --mode noquote                  # bid/ask absent
//   TS2Python_TestHarness.exe --mode bars                     # every tf + the -5 path
//   TS2Python_TestHarness.exe --mode session                  # RTH first/last bar
//   TS2Python_TestHarness.exe --mode stress --rate 10000 --seconds 10
//   TS2Python_TestHarness.exe --mode multithread --threads 8 --per-thread 5000
//
// Pair with `contract/tools/record.py` in another window to see the wire
// output, or to record a conformance fixture. Exits 0 on success,
// non-zero on any init / publish failure.
//
// Every run also asserts the ABI version and that the EL_Init / EL_Init2
// tombstones refuse with -6 before doing anything else. Those two exports
// are what stop an .ELD built against the superseded protocol from reaching
// a publish function whose signature changed underneath it.

#pragma warning(disable: 4819)

#include "ts2python.h"
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

namespace {

struct Options {
    std::string endpoint = "tcp://127.0.0.1:5555";
    std::string mode = "smoke";   // smoke | noquote | bars | session | stress | multithread
    int         rate = 10000;
    int         seconds = 5;
    int         threads = 4;
    int         per_thread = 5000;
    int         warmup_ms = 250;
};

// Quantities in the shape EasyLanguage hands over on an intraday chart:
// Volume is the up-tick share volume (so it equals UpTicks), Ticks is the
// total (so it equals UpTicks + DownTicks). Keeping the synthetic numbers
// internally consistent means a fixture recorded from this harness still
// demonstrates the relationship a binding must not try to "fix".
struct Quantities { double volume, ticks, upticks, downticks, open_interest; };
constexpr Quantities kTickQty = {100.0, 180.0, 100.0,  80.0, 0.0};
constexpr Quantities kBarQty  = {12000.0, 21000.0, 12000.0, 9000.0, 0.0};
constexpr Quantities kNoQty   = {0.0, 0.0, 0.0, 0.0, 0.0};   // breadth indices

Options parse_args(int argc, char** argv) {
    Options o;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        auto next = [&](const char* flag) -> std::string {
            if (i + 1 >= argc) {
                std::fprintf(stderr, "missing value for %s\n", flag);
                std::exit(2);
            }
            return argv[++i];
        };
        if      (a == "--endpoint")   o.endpoint   = next("--endpoint");
        else if (a == "--mode")       o.mode       = next("--mode");
        else if (a == "--rate")       o.rate       = std::atoi(next("--rate").c_str());
        else if (a == "--seconds")    o.seconds    = std::atoi(next("--seconds").c_str());
        else if (a == "--threads")    o.threads    = std::atoi(next("--threads").c_str());
        else if (a == "--per-thread") o.per_thread = std::atoi(next("--per-thread").c_str());
        else if (a == "--warmup-ms")  o.warmup_ms  = std::atoi(next("--warmup-ms").c_str());
        else if (a == "-h" || a == "--help") {
            std::puts(
                "TS2Python_TestHarness options:\n"
                "  --endpoint <tcp://...>    default tcp://127.0.0.1:5555\n"
                "  --mode <smoke|noquote|bars|session|stress|multithread>\n"
                "  --rate <msgs/sec>         stress mode target rate\n"
                "  --seconds <N>             stress mode duration\n"
                "  --threads <N>             multithread mode threads\n"
                "  --per-thread <N>          multithread messages per thread\n"
                "  --warmup-ms <N>           sleep after init (default 250ms)\n");
            std::exit(0);
        } else {
            std::fprintf(stderr, "unknown arg: %s\n", a.c_str());
            std::exit(2);
        }
    }
    return o;
}

int run_smoke(const Options& o) {
    (void)o;
    std::printf("[harness] smoke: publishing 5 ticks across 3 topics\n");
    const char* symbols[] = {"SPY", "QQQ", "VXX"};
    // Real-EL-shaped timestamp so the parser path gets exercised.
    const char* ts_el = "2026-04/18-13:30:45";
    for (int i = 0; i < 5; ++i) {
        const char* sym = symbols[i % 3];
        const int rc = EL_PublishTick(sym, ts_el,
                                      450.0 + i * 0.1,
                                      kTickQty.volume, kTickQty.ticks,
                                      kTickQty.upticks, kTickQty.downticks,
                                      kTickQty.open_interest,
                                      449.99, 450.01);
        if (rc != 0) {
            std::fprintf(stderr, "[harness] publish rc=%d on %s\n", rc, sym);
            return 3;
        }
    }

    // Also exercise the OHLC bar path so subscribers see at least one
    // {"kind":"bar"} message on SPY. Reuses the tick's :45 timestamp on
    // purpose — that is what gives the smoke fixture coverage of the
    // floor-to-the-minute rule in semantics.md §2.1.
    const int rc_bar = EL_PublishBar(
        "SPY", ts_el,
        /*bar_type*/     1,
        /*bar_interval*/ 1,
        /*open*/  450.10,
        /*high*/  450.75,
        /*low*/   449.80,
        /*close*/ 450.40,
        kBarQty.volume, kBarQty.ticks,
        kBarQty.upticks, kBarQty.downticks, kBarQty.open_interest);
    if (rc_bar != 0) {
        std::fprintf(stderr, "[harness] EL_PublishBar rc=%d\n", rc_bar);
        return 3;
    }
    return 0;
}

// Reproduces what EL sends when there is no quote to report: historical
// replay, and symbols that never carry one (breadth indices). InsideBid /
// InsideAsk are 0 in both cases, and the DLL must turn that into JSON null
// rather than a $0.00 quote. Without this mode the null path is unreachable
// from the harness, and so unrecordable as a fixture.
int run_noquote(const Options& o) {
    (void)o;
    std::printf("[harness] noquote: publishing with bid=ask=0 (history-replay shape)\n");
    const char* ts_el = "2026-04/18-13:31:00";

    const int rc_tick = EL_PublishTick("$TICK", ts_el,
                                       /*price*/ 812.0,
                                       kNoQty.volume, kNoQty.ticks,
                                       kNoQty.upticks, kNoQty.downticks,
                                       kNoQty.open_interest,
                                       /*bid*/ 0.0, /*ask*/ 0.0);
    if (rc_tick != 0) {
        std::fprintf(stderr, "[harness] EL_PublishTick rc=%d\n", rc_tick);
        return 3;
    }

    // A *non-index* symbol with no quote. $TICK alone cannot pin semantics.md
    // §3.1 down: a binding that ignores wire nulls entirely still passes,
    // because §3.2 makes it blank $TICK's quotes anyway. SPY has no such
    // fallback — reading 0.0 here is the failure the rule exists to catch.
    const int rc_spy_tick = EL_PublishTick("SPY", ts_el,
                                           /*price*/ 450.40,
                                           kTickQty.volume, kTickQty.ticks,
                                           kTickQty.upticks, kTickQty.downticks,
                                           kTickQty.open_interest,
                                           /*bid*/ 0.0, /*ask*/ 0.0);
    if (rc_spy_tick != 0) {
        std::fprintf(stderr, "[harness] EL_PublishTick rc=%d\n", rc_spy_tick);
        return 3;
    }

    // A bar in the same recording. Bars carry no quote at all now, so this
    // frame is here to prove the absence is structural rather than a
    // history-replay artefact a binding might try to fill in.
    const int rc_bar = EL_PublishBar("SPY", ts_el,
                                     /*bar_type*/ 1, /*bar_interval*/ 1,
                                     450.10, 450.75, 449.80, 450.40,
                                     kBarQty.volume, kBarQty.ticks,
                                     kBarQty.upticks, kBarQty.downticks,
                                     kBarQty.open_interest);
    if (rc_bar != 0) {
        std::fprintf(stderr, "[harness] EL_PublishBar rc=%d\n", rc_bar);
        return 3;
    }
    return 0;
}

// Every non-1m timeframe the wire can name, plus the refusal path. Without
// this mode every recorded fixture is "tf":"1m", so wire_timeframe()'s
// mapping has no end-to-end coverage and a regression (say BarInterval 60
// mapped to "60m") reaches a binding before anything notices.
int run_bars(const Options& o) {
    (void)o;
    std::printf("[harness] bars: EL_PublishBar across every mappable interval\n");
    const char* ts_el = "2026-04/20-13:30:00";

    struct Case { int bar_type; int bar_interval; const char* label; };
    const Case mappable[] = {
        {1,  5,  "5m"},
        {1, 15,  "15m"},
        {1, 30,  "30m"},
        {1, 60,  "1h"},
        {2,  0,  "1d (BarInterval 0 — what TradeStation 10 reports)"},
        {2,  1,  "1d (BarInterval 1 — what the ABI documented first)"},
    };
    for (const auto& c : mappable) {
        const int rc = EL_PublishBar("SPY", ts_el, c.bar_type, c.bar_interval,
                                     450.10, 450.75, 449.80, 450.40,
                                     kBarQty.volume, kBarQty.ticks,
                                     kBarQty.upticks, kBarQty.downticks,
                                     kBarQty.open_interest);
        if (rc != 0) {
            std::fprintf(stderr, "[harness] EL_PublishBar(%s) rc=%d\n", c.label, rc);
            return 3;
        }
    }

    // Unmappable intervals must be refused with -5 and publish nothing. A
    // 2-minute chart is the realistic case; weekly is the other shape.
    const Case refused[] = {
        {1,  2, "2 minute"},
        {3,  1, "weekly"},
        {2,  2, "2 day"},   // BarType 2 accepts 0/1 only; 2 is a day multiplier
    };
    for (const auto& c : refused) {
        const int rc = EL_PublishBar("SPY", ts_el, c.bar_type, c.bar_interval,
                                     1.0, 1.0, 1.0, 1.0,
                                     1.0, 1.0, 1.0, 1.0, 1.0);
        if (rc != -5) {
            std::fprintf(stderr,
                         "[harness] expected rc=-5 for %s, got %d\n", c.label, rc);
            return 3;
        }
    }
    std::printf("[harness] bars: 6 published, 3 refused with rc=-5\n");
    return 0;
}

// The first and last bar of a US regular session. semantics.md §2 requires a
// fixture covering both ends: left- vs right-labelling is the classic silent
// market-data bug, and both readings look correct in isolation — they differ
// by exactly one bar.
int run_session_edges(const Options& o) {
    (void)o;
    std::printf("[harness] session_edges: RTH first and last 1m bar\n");
    // EasyLanguage stamps a bar with its CLOSE time, so the wire carries
    // 09:31..16:00 for the session §2 labels 09:30..15:59. Publish what
    // TradeStation actually publishes: this fixture used to emit the
    // contract's own labels, which made it agree with the spec by
    // construction and test nothing — and that is precisely how a
    // right-labelled bar reached storage unnoticed. The binding is what
    // must step back onto the left edge, and this is what proves it does.
    struct Edge { const char* ts_el; double open; double close; };
    const Edge edges[] = {
        {"2026-04/20-09:31:00", 450.10, 450.40},   // first bar, covers [09:30, 09:31)
        {"2026-04/20-16:00:00", 452.80, 453.05},   // last  bar, covers [15:59, 16:00)
    };
    for (const auto& e : edges) {
        const int rc = EL_PublishBar("SPY", e.ts_el, /*bar_type*/ 1, /*bar_interval*/ 1,
                                     e.open, e.close + 0.20, e.open - 0.15, e.close,
                                     kBarQty.volume, kBarQty.ticks,
                                     kBarQty.upticks, kBarQty.downticks,
                                     kBarQty.open_interest);
        if (rc != 0) {
            std::fprintf(stderr, "[harness] EL_PublishBar(%s) rc=%d\n", e.ts_el, rc);
            return 3;
        }
    }
    return 0;
}

int run_stress(const Options& o) {
    using clock = std::chrono::steady_clock;
    const auto deadline = clock::now() + std::chrono::seconds(o.seconds);
    const auto period = std::chrono::nanoseconds(
        static_cast<long long>(1000000000.0 / (o.rate > 0 ? o.rate : 1)));
    long long sent = 0;
    long long failed = 0;
    auto next_tick = clock::now();

    while (clock::now() < deadline) {
        const int rc = EL_PublishTick("SPY", "", 450.0,
                                      kTickQty.volume, kTickQty.ticks,
                                      kTickQty.upticks, kTickQty.downticks,
                                      kTickQty.open_interest,
                                      449.99, 450.01);
        if (rc != 0) ++failed;
        else ++sent;
        next_tick += period;
        std::this_thread::sleep_until(next_tick);
    }
    std::printf("[harness] stress: sent=%lld failed=%lld over %ds (target %d/s)\n",
                sent, failed, o.seconds, o.rate);
    return failed == 0 ? 0 : 4;
}

int run_multithread(const Options& o) {
    std::atomic<long long> sent{0};
    std::atomic<long long> failed{0};
    std::vector<std::thread> workers;
    workers.reserve(o.threads);

    for (int t = 0; t < o.threads; ++t) {
        workers.emplace_back([t, &o, &sent, &failed]() {
            std::string sym = "T" + std::to_string(t);
            for (int i = 0; i < o.per_thread; ++i) {
                const int rc = EL_PublishTick(
                    sym.c_str(), "",
                    100.0 + t * 0.01,
                    10.0, 18.0, 10.0, 8.0, 0.0,
                    99.99, 100.01);
                if (rc != 0) failed.fetch_add(1, std::memory_order_relaxed);
                else sent.fetch_add(1, std::memory_order_relaxed);
            }
        });
    }
    for (auto& w : workers) w.join();

    std::printf("[harness] multithread: threads=%d per_thread=%d sent=%lld failed=%lld\n",
                o.threads, o.per_thread,
                sent.load(), failed.load());
    return failed.load() == 0 ? 0 : 5;
}

}  // namespace

int main(int argc, char** argv) {
    const Options o = parse_args(argc, argv);

    std::printf("[harness] dll version = %d\n", EL_DllVersion());
    if (EL_DllVersion() != 1) {
        std::fprintf(stderr, "[harness] expected ABI 1, got %d\n", EL_DllVersion());
        return 1;
    }

    // Tombstone check, BEFORE any real init — this is the guard the whole
    // rename rests on. EL_PublishTick and EL_PublishBar kept their names but
    // changed arity, and __stdcall would corrupt the stack rather than fail,
    // so an .ELD built against the superseded protocol must be stopped at
    // init. Verifying it here means the protection is regression-tested on
    // every harness run instead of resting on a manual check nobody repeats.
    const int rc_tomb1 = EL_Init(o.endpoint.c_str());
    const int rc_tomb2 = EL_Init2(o.endpoint.c_str(), 1);
    if (rc_tomb1 != -6 || rc_tomb2 != -6) {
        std::fprintf(stderr,
                     "[harness] tombstones must return -6, got EL_Init=%d EL_Init2=%d\n",
                     rc_tomb1, rc_tomb2);
        return 2;
    }
    std::printf("[harness] tombstones EL_Init / EL_Init2 refuse with -6\n");

    std::printf("[harness] EL_Init3(%s)\n", o.endpoint.c_str());
    const int rc = EL_Init3(o.endpoint.c_str());
    if (rc < 0) {
        std::fprintf(stderr, "[harness] init failed rc=%d\n", rc);
        return 1;
    }
    // Idempotency check — a second call returns 1 without rebinding or
    // restamping the session id, which is what keeps a re-Verify of the
    // indicator from looking like a publisher restart to subscribers.
    const int rc2 = EL_Init3(o.endpoint.c_str());
    if (rc2 != 1) {
        std::fprintf(stderr, "[harness] expected rc=1 on second init, got rc=%d\n", rc2);
        return 2;
    }

    // Give subscribers a moment to connect before publishing — PUB sockets
    // drop messages silently when nobody is attached.
    if (o.warmup_ms > 0) {
        std::this_thread::sleep_for(std::chrono::milliseconds(o.warmup_ms));
    }

    int result = 0;
    if      (o.mode == "smoke")       result = run_smoke(o);
    else if (o.mode == "noquote")     result = run_noquote(o);
    else if (o.mode == "bars")        result = run_bars(o);
    else if (o.mode == "session")     result = run_session_edges(o);
    else if (o.mode == "stress")      result = run_stress(o);
    else if (o.mode == "multithread") result = run_multithread(o);
    else {
        std::fprintf(stderr, "[harness] unknown mode: %s\n", o.mode.c_str());
        result = 2;
    }

    EL_Shutdown();
    return result;
}
