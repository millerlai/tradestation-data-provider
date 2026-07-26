# tradestation-data-provider

[![CI](https://github.com/millerlai/tradestation-data-provider/actions/workflows/ci.yml/badge.svg)](https://github.com/millerlai/tradestation-data-provider/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> 📖 [繁體中文版 README](README.zh-TW.md)

Market data out of **TradeStation**, for subscribers in any language.

A TradeStation EasyLanguage indicator hands ticks and 1-minute bars to a C++
bridge DLL, which publishes them over ZeroMQ. Anything that speaks the protocol
can consume the feed.

```mermaid
---
config:
  flowchart:
    defaultRenderer: elk
---
flowchart TB
    subgraph PROD["Producer — fixed"]
        direction TB
        TS["TradeStation Desktop"]
        EL["EL Exporter Indicator"]
        DLL["TS2Python.dll<br/>C++ · Win32 x86 · ABI 8"]
        TS --> EL --> DLL
    end
    subgraph CON["Contract — the product"]
        WIRE["wire v3<br/>2-frame ZMQ · JSON"]
        SEM["semantics.md<br/>the rules a schema cannot express"]
        FIX["conformance fixtures"]
    end
    subgraph BIND["Subscriber bindings — extensible"]
        direction LR
        PY["Python<br/>reference"]
        GO["Go<br/>future"]
        RS["Rust · C#<br/>future"]
    end
    DLL -->|"ZMQ PUB<br/>tcp://127.0.0.1:5555"| WIRE
    WIRE -.->|specifies| PY
    WIRE -.->|specifies| GO
    WIRE -.->|specifies| RS
    FIX ==>|must pass| PY
    FIX ==>|must pass| GO
    FIX ==>|must pass| RS

    classDef existing fill:#e9ecef,stroke:#adb5bd,color:#495057
    classDef added fill:#d4edda,stroke:#28a745,color:#155724
    class TS,EL,DLL,PY existing
    class WIRE,SEM,FIX added
```

## The product is the wire contract

What this repo promises is **the protocol on the wire**, not any one client
library. [`contract/`](contract/) is the source of truth; the Python package is
the reference binding, and the template for the next one.

Any parsing rule that lives only inside a binding is a bug — it will be missed
by the next implementation. This repo has already seen that happen: the former
spec had drifted to describing fields the DLL no longer emitted, and nobody
noticed, because nothing checked.

## Layout

| Path | What it is |
| --- | --- |
| [`contract/`](contract/) | **Wire spec, semantics, and conformance fixtures.** Start here to write a binding |
| [`EL/`](EL/) | EasyLanguage exporter indicator — the upstream origin of the feed |
| [`cpp/`](cpp/) | C++ bridge DLL (Win32 x86) and its standalone test harness |
| [`bindings/python/`](bindings/python/) | Reference Python binding — ingestion runtime, pluggable sinks, Parquet store |
| [`docs/`](docs/) | Architecture and migration notes |

## Quick start

**Consuming the feed in Python** → [`bindings/python/README.md`](bindings/python/README.md),
or go straight to the runnable scripts in
[`bindings/python/examples/`](bindings/python/examples/). Two of the four need
neither TradeStation nor the DLL: one replays the recorded fixtures in
[`contract/fixtures/`](contract/fixtures/) through the real binding, the other
demonstrates the storage tiers on data it generates itself.

**Writing a binding in another language** → [`contract/README.md`](contract/README.md).
Read [`contract/semantics.md`](contract/semantics.md) before writing any parsing
code: it holds the rules JSON Schema cannot express, and those are where
bindings actually diverge.

**Building the DLL** → [`cpp/README.md`](cpp/README.md)

```powershell
cd cpp
.\setup-build-env.bat     # once per clone: vcpkg submodule, bootstrap, deps
.\verify-build-env.bat    # exit 0 = ready; names the fix for anything missing
.\build.bat               # Release, x86 + x64  ->  cpp\Release\
```

`build.bat` locates MSBuild itself, so no Developer Command Prompt is needed.
CMake works too and writes somewhere else — mind the path when you go looking
for the harness:

```powershell
cmake --preset x86-release          # or x86-release-vs2022
cmake --build --preset x86-release  #  ->  cpp\build\x86-release\Release\
```

**Inspecting the wire without TradeStation:**

```powershell
# terminal A — drives the DLL directly. Path depends on which toolchain built it:
#   build.bat / Visual Studio  ->  cpp\Release\
#   cmake --preset             ->  cpp\build\x86-release\Release\
cpp\Release\TS2Python_TestHarness.exe --mode smoke --warmup-ms 8000

# terminal B
python contract/tools/record.py
```

## Versioning

Three version numbers move independently; the pairing that matters is in
[`contract/compat.md`](contract/compat.md).

| Version | Current | Who cares |
| --- | ---: | --- |
| Wire (`"v"` in the payload) | 3 | Every binding |
| DLL ABI (`EL_DllVersion()`) | 8 | Every binding |
| Python package | 0.2.0 | Python consumers only |

Wire v1 and v2 are superseded but **still supported**: the DLL lives inside a user's
TradeStation install and does not update when a binding does.

## Status

Windows only on the producer side — TradeStation Desktop is a 32-bit Windows
process, so the DLL must be built as Win32 (x86). Subscribers have no such
constraint.

Data collection only. No strategy, order routing, or risk logic lives here; that
belongs to whatever consumes the feed.

## License

MIT — see [`LICENSE`](LICENSE).
