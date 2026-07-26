# Prebuilt binaries

The only place in this repo where a `.dll` is checked in on purpose. It exists so
that installing does not require Visual Studio, vcpkg, or a C++ toolchain:
`install-to-tradestation.bat` falls back to these when there is no local build.

```
x86-windows/   TS2Python.dll + libzmq-mt-4_3_5.dll   <- what TradeStation loads
x64-windows/   TS2Python.dll + libzmq-mt-4_3_5.dll   <- developer smoke testing only
```

TradeStation is a 32-bit process: **x86 is the one that gets installed.** The x64
pair is here only because the same source builds both, and the installer picks by
the architecture of the TradeStation executable it finds rather than by the
architecture of Windows.

Tested on Windows 11 (x64) with TradeStation 10.

## What has to sit beside TS2Python.dll

`dumpbin /dependents TS2Python.dll`, minus the Windows-supplied entries:

| Needed | Where it comes from |
| --- | --- |
| `libzmq-mt-4_3_5.dll` | shipped here — the versioned name moves with the pinned vcpkg revision, so never type it from memory |
| `MSVCP140.dll`, `MSVCP140_ATOMIC_WAIT.dll`, `VCRUNTIME140.dll` (x64 also `VCRUNTIME140_1.dll`) | Microsoft Visual C++ 2015-2022 Redistributable, **x86** for the DLL TradeStation loads |
| `api-ms-win-crt-*.dll`, `KERNEL32`, `WS2_32`, `IPHLPAPI`, `ADVAPI32` | Windows itself (UCRT ships with Windows 10/11) |

The DLL is built against the dynamic CRT (`/MD`), so the Redistributable is a real
requirement, not a nicety. Most machines running TradeStation already have it;
when it is missing, EasyLanguage reports only that the DLL could not be loaded and
names nothing. `MSVCP140_ATOMIC_WAIT.dll` is the newest of the three and the one a
stale Redistributable tends to be missing.

## Refreshing these

Rebuild, then copy - there is no automation, and a stale binary here is worse than
no binary:

```powershell
cd cpp
build.bat                                          # Release, x86 and x64
copy /y Release\*.dll     prebuilt\x86-windows\
copy /y x64\Release\*.dll prebuilt\x64-windows\
```

## Third-party

`libzmq-mt-4_3_5.dll` is ZeroMQ 4.3.5, redistributed unmodified under the Mozilla
Public License 2.0 - see [`LICENSE.libzmq.txt`](LICENSE.libzmq.txt). Everything
else here is this project's own code under the repository [`LICENSE`](../../LICENSE).
