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
//  -7  no subscriber yet — RETRYABLE, and the normal state at startup

// Bind the publisher (once per process) and announce this chart.
//
// Every chart running the indicator calls this with its own identity. The
// socket is bound by whichever chart gets here first; the session id and the
// sequence counters are stamped only on that first bind, so a second chart —
// or a re-Verify — does not look like a publisher restart to subscribers.
//
// WHAT MAKES THIS DIFFERENT FROM A PLAIN INIT: it does not return 0 until a
// subscriber is actually attached. The socket is XPUB rather than PUB, which
// means the DLL is told when someone subscribes; until the control topic has
// a subscriber this returns -7 and publishes nothing. That is not a failure —
// it is the expected answer every time TradeStation starts before the
// consumer does, and the indicator is written to retry on the next bar.
//
// Why it matters: PUB/SUB drops everything sent with no subscriber attached
// and reports nothing. Without this gate an operator sees "init ok" in the
// Print Log while every frame goes in the bin.
//
//   rc  0  bound, a subscriber is attached, and this chart's hello was sent
//   rc  1  this exact chart already announced in this session; nothing to do
//   rc -3  bind / socket create failed
//   rc -4  zmq_endpoint or symbol was NULL
//   rc -7  no subscriber on the control topic yet — call again next bar
//
// On success a hello frame goes out on the CONTROL topic (not the symbol's),
// carrying symbol / category / bar_type / bar_interval. It has to be a
// separate topic: a consumer subscribes per symbol, so a chart on a symbol
// it never asked for could not be announced on that symbol's own topic —
// and a chart nobody is subscribed to is exactly what an operator needs
// told. See ../contract/wire.md.
//
// Charts are remembered. When a subscriber attaches, drops and attaches
// again — restarting the consumer — every chart is re-announced without
// TradeStation having to re-Verify a single indicator.
//
// THE NAME IS REUSED AND THAT IS A HAZARD. `EL_Init` was the init export of
// the superseded protocol, with one parameter instead of five. __stdcall
// makes the callee pop the arguments, so an .ELD still bound to the old
// one-argument EL_Init will resolve this symbol, call it, and corrupt the
// stack — TradeStation misbehaves or dies rather than returning a code. No
// code here can prevent that; nothing on the callee side can see how many
// arguments the caller pushed. The DLL and the .ELD are one unit and must be
// installed together. The other direction is safe: an .ELD built against the
// superseded EL_Init3 finds no such export and fails at Verify.
TS2P_API int TS2P_CALL EL_Init(
    const char* zmq_endpoint,
    const char* symbol,        // EL `GetSymbolName`
    int         category,      // EL `Category`
    int         bar_type,      // EL `BarType`
    int         bar_interval); // EL `BarInterval`

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
// The one publisher. Everything TradeStation hands the indicator for a data
// point goes out, whatever kind of chart produced it.
//
// There is no tick/bar split any more, and no field is dropped for being
// "meaningless on this chart type". A tick chart supplies Open/High/Low/Close
// (equal to each other on a 1-tick series) and a bar chart supplies
// InsideBid/InsideAsk; both used to be discarded by the indicator, on its own
// judgement, off the wire. That judgement is the consumer's, and a publisher
// that bakes in what a number means today breaks the day TradeStation changes
// what it means.
//
// bar_type / bar_interval / category are EasyLanguage's own words for what
// this chart and symbol are. They travel verbatim; nothing here maps them to
// a timeframe name or refuses an interval it does not recognise.
TS2P_API int TS2P_CALL EL_Publish(
    const char* symbol,
    const char* el_timestamp,   // EL Date+Time "yyyy-MM/dd-HH:mm:ss" 24-hour,
                                // America/New_York wall clock. Verbatim; not
                                // parsed here. May be NULL / "".
    int         bar_type,       // EL `BarType`
    int         bar_interval,   // EL `BarInterval`
    int         category,       // EL `Category`
    double      bar_open,       // EL `Open`
    double      bar_high,       // EL `High`
    double      bar_low,        // EL `Low`
    double      bar_close,      // EL `Close`
    double      volume,         // EL `Volume`
    double      ticks,          // EL `Ticks`
    double      upticks,        // EL `UpTicks`
    double      downticks,      // EL `DownTicks`
    double      open_interest,  // EL `OpenInt`
    double      bid,            // EL `InsideBid`
    double      ask);           // EL `InsideAsk`

// TOMBSTONES. Both return -6 and publish nothing.
//
// They kept their names across a signature change once already, which on
// __stdcall corrupts the caller's stack rather than returning an error. The
// names stay exported so an indicator built against the superseded protocol
// gets a readable -6 in the Print Log instead of a crash.
//
// They no longer protect anything on their own. The guard used to be that
// every publish sat behind an init whose name had changed, so an old .ELD
// stopped at init and never reached a signature that had moved underneath
// it. EL_Init's name is now reused with a different arity, which removes
// that gate — an old .ELD corrupts the stack inside EL_Init before these
// are ever called. They are kept because deleting an export can only make
// the failure less legible, not because they still catch anything.
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
    double      ask);

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

// ABI version of this DLL build. Currently 3.
//
// It is NOT the wire version. Point frames are byte-for-byte what `proto` 2
// always was, and every recorded fixture still validates — what changed is
// the C ABI (EL_Init's signature) and an additive control frame on its own
// topic, which a proto-2 consumer simply never subscribes to. Bumping
// `proto` would have invalidated every fixture to describe a frame the
// point schema does not cover.
//
// Takes no arguments, so its signature can never drift — it is the one
// export an indicator can call unconditionally against any build to ask
// "who are you" before touching anything version-specific.
TS2P_API int TS2P_CALL EL_DllVersion(void);

#ifdef __cplusplus
}  // extern "C"
#endif

#endif  // TS2PYTHON_H
