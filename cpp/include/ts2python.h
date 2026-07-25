// TS2Python bridge — C ABI exported from TS2Python.dll.
//
// Called from TradeStation EasyLanguage via DefineDLLFunc. All functions use
// __stdcall (EL's default DLL calling convention on Win32) and C linkage.
//
// See ../contract/ for the wire format and ../contract/error_codes.md for
// the return-code semantics.

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
//  -4  invalid argument (null pointer etc.)

TS2P_API int TS2P_CALL EL_Init(const char* zmq_endpoint);

TS2P_API int TS2P_CALL EL_PublishTick(
    const char* symbol,
    const char* el_timestamp,   // EL bar time "yyyy-MM/dd-HH:mm:ss" 24-hour in
                                // America/New_York wall-clock; parsed into
                                // ts_utc (real UTC epoch) on the wire and
                                // also passed through verbatim as ts_str.
                                // May be NULL / "".
    double      price,
    double      volume,
    double      bid,
    double      ask,
    double      tick_count);

// Publish a complete OHLC bar. Used by the EL indicator when BarType != 0
// (e.g. minute bars), so that Open/High/Low are preserved instead of being
// collapsed to a single close price. Wire payload carries "kind":"bar_1m"
// and o/h/l/c fields; the Python provider emits a Bar directly and bypasses
// the tick aggregator on that path.
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
    double      tick_count);

TS2P_API int TS2P_CALL EL_Shutdown(void);

// Version identifier for this DLL build (bumps independently of the wire
// protocol version carried in the payload's "v" field). Current pairing is
// ABI 7 <-> wire 2; see ../contract/compat.md for the full matrix.
TS2P_API int TS2P_CALL EL_DllVersion(void);

#ifdef __cplusplus
}  // extern "C"
#endif

#endif  // TS2PYTHON_H
