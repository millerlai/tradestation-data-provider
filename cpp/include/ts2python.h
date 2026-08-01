// TS2Python bridge — C ABI exported from TS2Python.dll.
//
// Called from TradeStation EasyLanguage via DefineDLLFunc. All functions use
// __stdcall (EL's default DLL calling convention on Win32) and C linkage.
//
// See ../contract/wire.md for the wire format and ../contract/error_codes.md
// for the return-code semantics.

#ifndef TS2PYTHON_H
#define TS2PYTHON_H

#ifdef __cplusplus
extern "C" {
#endif

#if defined(_WIN32)
#  ifdef TS2PYTHON_EXPORTS
#    define TS2P_API __declspec(dllexport)
#  else
#    define TS2P_API __declspec(dllimport)
#  endif
#  define TS2P_CALL __stdcall
#else
#  define TS2P_API
#  define TS2P_CALL
#endif

// Return codes (see ../contract/error_codes.md).
//   0  success
//   1  already initialized (idempotent; not an error)
//  -1  not initialized
//  -2  zmq send failed
//  -3  init failed (bind / socket create)
//  -4  invalid argument (null pointer, non-representable quantity, ...)
//  -5  unsupported bar type / interval (no wire timeframe for it)
//  -6  ABI mismatch — the caller is an .ELD older than this protocol

// Initialise the publisher and bind the PUB socket. Idempotent: a second
// call returns 1 without rebinding, and without restamping the session id,
// so re-Verifying an indicator does not look like a restart to subscribers.
//
// THE NAME IS THE COMPATIBILITY GATE. EL_PublishTick and EL_PublishBar below
// kept their names across the protocol rewrite but not their signatures, and
// __stdcall makes the callee pop the arguments — so a mismatched call
// corrupts the stack instead of failing. What makes that unreachable is that
// every publish call in the EasyLanguage indicator sits behind a successful
// init, and init is the one export that was renamed:
//
//   new .ELD + old DLL  ->  no EL_Init3 export, DefineDLLFunc fails at Verify
//   old .ELD + new DLL  ->  EL_Init / EL_Init2 tombstones return -6
//
// Both directions stop before a single publish runs. See ../contract/wire.md.
TS2P_API int TS2P_CALL EL_Init3(const char* zmq_endpoint);

// Tombstones. These were the init exports of the superseded protocol; they
// remain exported so that an indicator still bound to them gets a readable
// -6 in TradeStation's Print Log rather than an unexplained resolution
// failure. They never initialise anything.
TS2P_API int TS2P_CALL EL_Init(const char* zmq_endpoint);
TS2P_API int TS2P_CALL EL_Init2(const char* zmq_endpoint, int publisher_version);

// Publish a single trade print (EasyLanguage BarType 0, BarInterval 1).
//
// The five quantity parameters are EasyLanguage's reserved words of the same
// name, forwarded verbatim — this ABI performs no selection or conversion
// between them. They are double because EasyLanguage has no 64-bit integer
// type; each is narrowed to int64 before it reaches the wire, and a value
// that will not survive that narrowing returns -4 rather than being clamped.
//
// Note `volume` and `ticks` swap meaning between intraday and daily charts
// (../contract/semantics.md §3.4). Deciding which one means "total share
// volume" is the caller's business, not this ABI's — an earlier version made
// that choice here and had to stamp a publisher-convention version on every
// payload to say which rule it had applied.
TS2P_API int TS2P_CALL EL_PublishTick(
    const char* symbol,
    const char* el_timestamp,   // EL bar time "yyyy-MM/dd-HH:mm:ss" 24-hour in
                                // America/New_York wall-clock. Passed through
                                // verbatim as ts_str and NOT parsed here.
                                // May be NULL / "".
    double      price,
    double      volume,         // EL `Volume`
    double      ticks,          // EL `Ticks`
    double      upticks,        // EL `UpTicks`
    double      downticks,      // EL `DownTicks`
    double      open_interest,  // EL `OpenInt`
    double      bid,
    double      ask);

// Publish a complete OHLC bar at any supported interval.
//
// bar_type / bar_interval come straight from EasyLanguage's reserved words
// of the same name. The mapping to a wire timeframe lives here rather than
// in EL so that every caller of this ABI agrees on it:
//
//   bar_type 1 (intraday), bar_interval 1/5/15/30/60  ->  1m/5m/15m/30m/1h
//   bar_type 2 (daily),    bar_interval 0 or 1        ->  1d
//
// TradeStation 10 reports BarInterval = 0 on a daily chart; 1 is what this
// ABI documented before that was measured. Both mean the same chart.
//
// Anything else returns -5 without publishing. Guessing an interval would
// file bars under the wrong partition, which nothing downstream can detect.
//
// No bid / ask: a live-quote function describes the moment of the call,
// which on a bar is its last print, not the bar.
TS2P_API int TS2P_CALL EL_PublishBar(
    const char* symbol,
    const char* el_timestamp,
    int         bar_type,
    int         bar_interval,
    double      bar_open,
    double      bar_high,
    double      bar_low,
    double      bar_close,
    double      volume,         // EL `Volume`
    double      ticks,          // EL `Ticks`
    double      upticks,        // EL `UpTicks`
    double      downticks,      // EL `DownTicks`
    double      open_interest); // EL `OpenInt`

TS2P_API int TS2P_CALL EL_Shutdown(void);

// ABI version of this DLL build. Currently 1, paired with wire `proto` 1.
//
// Takes no arguments, so its signature can never drift — it is the one
// export an indicator can call unconditionally against any build to ask
// "who are you" before touching anything version-specific.
TS2P_API int TS2P_CALL EL_DllVersion(void);

#ifdef __cplusplus
}  // extern "C"
#endif

#endif  // TS2PYTHON_H
