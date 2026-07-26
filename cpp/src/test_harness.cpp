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
                                      450.0 + i * 0.1, 100.0,
                                      449.99, 450.01, 1.0);
        if (rc != 0) {
            std::fprintf(stderr, "[harness] publish rc=%d on %s\n", rc, sym);
            return 3;
        }
    }

    // Also exercise the OHLC bar path so subscribers see at least one
    // {"kind":"bar"} message on SPY. Goes through EL_PublishBar — the entry
    // point the indicator actually calls — rather than the EL_PublishTickEx
    // shim, which run_bars covers separately.
    const int rc_bar = EL_PublishBar(
        "SPY", ts_el,
        /*bar_type*/     1,
        /*bar_interval*/ 1,
        /*open*/  450.10,
        /*high*/  450.75,
        /*low*/   449.80,
        /*close*/ 450.40,
        /*volume*/ 12000.0,
        /*bid*/    450.39,
        /*ask*/    450.41,
        /*tc*/     140.0);
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
                                       /*price*/ 812.0, /*volume*/ 0.0,
                                       /*bid*/ 0.0, /*ask*/ 0.0, /*tc*/ 1.0);
    if (rc_tick != 0) {
        std::fprintf(stderr, "[harness] EL_PublishTick rc=%d\n", rc_tick);
        return 3;
    }

    // A *non-index* symbol with no quote. $TICK alone cannot pin semantics.md
    // §3.1 down: a binding that ignores wire nulls entirely still passes,
    // because §3.2 makes it blank $TICK's quotes anyway. SPY has no such
    // fallback — reading 0.0 here is the failure the rule exists to catch.
    const int rc_spy_tick = EL_PublishTick("SPY", ts_el,
                                           /*price*/ 450.40, /*volume*/ 100.0,
                                           /*bid*/ 0.0, /*ask*/ 0.0, /*tc*/ 1.0);
    if (rc_spy_tick != 0) {
        std::fprintf(stderr, "[harness] EL_PublishTick rc=%d\n", rc_spy_tick);
        return 3;
    }

    const int rc_bar = EL_PublishTickEx("SPY", ts_el,
                                        450.10, 450.75, 449.80, 450.40,
                                        /*volume*/ 12000.0,
                                        /*bid*/ 0.0, /*ask*/ 0.0, /*tc*/ 140.0);
    if (rc_bar != 0) {
        std::fprintf(stderr, "[harness] EL_PublishTickEx rc=%d\n", rc_bar);
        return 3;
    }
    return 0;
}

// Every non-1m timeframe the wire can name, plus the refusal path. Without
// this mode every recorded fixture is "tf":"1m", so wire_timeframe()'s
// mapping — the whole point of wire v3 — has no end-to-end coverage and a
// regression (say BarInterval 60 mapped to "60m") reaches a binding before
// anything notices.
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
        {2,  1,  "1d"},
    };
    for (const auto& c : mappable) {
        const int rc = EL_PublishBar("SPY", ts_el, c.bar_type, c.bar_interval,
                                     450.10, 450.75, 449.80, 450.40,
                                     12000.0, 450.39, 450.41, 140.0);
        if (rc != 0) {
            std::fprintf(stderr, "[harness] EL_PublishBar(%s) rc=%d\n", c.label, rc);
            return 3;
        }
    }

    // The legacy shim. Its signature is frozen ABI, and it can only ever have
    // meant 1m — keep that pinned to a recorded frame.
    const int rc_legacy = EL_PublishTickEx("QQQ", ts_el,
                                           380.10, 380.75, 379.80, 380.40,
                                           9000.0, 380.39, 380.41, 88.0);
    if (rc_legacy != 0) {
        std::fprintf(stderr, "[harness] EL_PublishTickEx rc=%d\n", rc_legacy);
        return 3;
    }

    // Unmappable intervals must be refused with -5 and publish nothing. A
    // 2-minute chart is the realistic case; weekly is the other shape.
    const Case refused[] = {
        {1,  2, "2 minute"},
        {3,  1, "weekly"},
    };
    for (const auto& c : refused) {
        const int rc = EL_PublishBar("SPY", ts_el, c.bar_type, c.bar_interval,
                                     1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0);
        if (rc != -5) {
            std::fprintf(stderr,
                         "[harness] expected rc=-5 for %s, got %d\n", c.label, rc);
            return 3;
        }
    }
    std::printf("[harness] bars: 6 published, 2 refused with rc=-5\n");
    return 0;
}

// The first and last bar of a US regular session. semantics.md §2 requires a
// fixture covering both ends: left- vs right-labelling is the classic silent
// market-data bug, and both readings look correct in isolation — they differ
// by exactly one bar.
int run_session_edges(const Options& o) {
    (void)o;
    std::printf("[harness] session_edges: RTH first and last 1m bar\n");
    // Left-labelled, so an RTH 1m session runs 09:30..15:59, never 16:00.
    struct Edge { const char* ts_el; double open; double close; };
    const Edge edges[] = {
        {"2026-04/20-09:30:00", 450.10, 450.40},   // first bar, covers [09:30, 09:31)
        {"2026-04/20-15:59:00", 452.80, 453.05},   // last  bar, covers [15:59, 16:00)
    };
    for (const auto& e : edges) {
        const int rc = EL_PublishBar("SPY", e.ts_el, /*bar_type*/ 1, /*bar_interval*/ 1,
                                     e.open, e.close + 0.20, e.open - 0.15, e.close,
                                     12000.0, e.close - 0.01, e.close + 0.01, 140.0);
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
        const int rc = EL_PublishTick("SPY", "", 450.0, 100.0, 449.99, 450.01, 1.0);
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
                    100.0 + t * 0.01, 10.0,
                    99.99, 100.01, 1.0);
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
    std::printf("[harness] EL_Init(%s)\n", o.endpoint.c_str());
    int rc = EL_Init(o.endpoint.c_str());
    if (rc < 0) {
        std::fprintf(stderr, "[harness] EL_Init failed rc=%d\n", rc);
        return 1;
    }
    // Idempotency check — second call returns 1.
    const int rc2 = EL_Init(o.endpoint.c_str());
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
