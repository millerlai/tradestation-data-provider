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
//  -3  socket/context creation failed, or connect() rejected the endpoint
//      string. NOT "the endpoint was already in use" -- this process only
//      ever connects, and connect() does not fail for that reason.
//  -4  invalid argument (null pointer, non-representable quantity, ...)
//  -6  ABI mismatch — the caller is an .ELD older than this protocol
//  -7  no subscriber yet — RETRYABLE, and the normal state at startup
//  -8  endpoint conflict — this chart asked for an endpoint other than the
//      one already connected to by the first chart
//  -9  the subscription queue could not be read, so "is anyone listening"
//      has no answer this call
// -10  published with no subscriber for the symbol — the point is lost.
//      Reported once per episode per chart, not once per bar. Also fires
//      when the far end (the hub) disconnects: caught by a socket monitor,
//      since the connect-side pipe survives a reconnect and no unsubscribe
//      is ever generated for it.
//
// -5 is RETIRED, not free. It meant "unsupported bar type / interval", from
// back when the DLL mapped a chart onto a timeframe vocabulary and refused
// what it had no word for. Nothing refuses an interval any more. Per
// ../contract/error_codes.md a retired code is never reused.

// Connect the publisher (once per process) and announce this chart.
//
// Every chart running the indicator calls this with its own identity. The
// socket is created and connected by whichever chart gets here first; the
// session id and the sequence counters are stamped only on that first
// connect, so a second chart — or a re-Verify — does not look like a
// publisher restart to subscribers.
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
//   rc  0  connected, a subscriber is attached, and this chart's hello was
//          sent
//   rc  1  this exact chart already announced in this session; nothing to do
//   rc -2  the hello frame could not be sent
//   rc -3  socket/context creation failed, or connect() rejected the
//          endpoint string
//   rc -4  zmq_endpoint or symbol was NULL
//   rc -7  no subscriber on the control topic yet — call again next bar
//   rc -8  a different endpoint is already connected to — see below
//   rc -9  the XPUB subscription queue could not be read
//
// -9 EXISTS TO KEEP -7 HONEST. Subscriptions arrive as readable messages, so
// "nobody has subscribed yet" and "the socket could not be read" both end
// with an empty subscriber set. Reporting the second as -7 told the operator
// it was the normal startup state — which the indicator logs once and then
// retries in silence forever, while the consumer sits there running. They
// are different facts and now they have different codes.
//
// ONE ENDPOINT PER PROCESS. The first chart to get here creates the socket
// and connects it; every later chart is handed that same socket. A chart
// naming a different endpoint is therefore refused with -8 rather than being
// silently published through the first chart's connection, which is
// indistinguishable from working until someone notices the consumer on the
// named endpoint has been idle all session.
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
// THE NAME CHANGED WITH THE SIGNATURE, AND THAT IS THE GUARD. A revision of
// this DLL called this `EL_Init` — the name the superseded protocol's
// one-parameter init already had. __stdcall makes the callee pop the
// arguments, so a stale .ELD resolved that name, called it with one argument
// against five, and corrupted the stack: TradeStation misbehaved or died with
// no return code, and nothing on the callee side can see how many arguments
// the caller pushed.
//
// Renaming restores the gate in both directions. A stale .ELD now resolves
// the one-parameter `EL_Init` TOMBSTONE below, its stack balances, and it
// gets -6 in the Print Log. A current .ELD run against an older DLL finds no
// `EL_InitChart` at all and fails at DefineDLLFunc resolution during Verify,
// before anything executes. The DLL and the .ELD are still one unit and must
// be installed together — but a mismatch is now legible instead of fatal.
TS2P_API int TS2P_CALL EL_InitChart(
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

// TOMBSTONES. All three return -6 and do nothing.
//
// Each keeps the name AND the signature it had in the superseded protocol.
// That pairing is the point: __stdcall has the callee pop the arguments, so
// a name that survives a signature change corrupts the caller's stack rather
// than returning an error. Held at the old arity, a stale .ELD's call
// balances and gets a readable -6 in the Print Log instead of a crash.
//
// EL_Init is the one that matters. It is the gate: every publish sits behind
// a successful init, so a stale .ELD stops here and never reaches a publish
// signature that moved underneath it. That gate was briefly given away by
// reusing `EL_Init` for the five-parameter init — see EL_InitChart above —
// and renaming put it back.
//
// Do not delete these until it is safe to assume no old .ELD survives.
TS2P_API int TS2P_CALL EL_Init(const char* zmq_endpoint);

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

// ABI version of this DLL build. Currently 4.
//
// It is NOT the wire version. Point frames are byte-for-byte what `proto` 2
// always was, and every recorded fixture still validates — what changed is
// the C ABI and an additive control frame on its own topic, which a proto-2
// consumer simply never subscribes to. Bumping `proto` would have
// invalidated every fixture to describe a frame the point schema does not
// cover.
//
// 3 -> 4 is the init export's rename to EL_InitChart. Renaming an export IS
// an ABI change, and this number is what says so.
//
// Takes no arguments, so its signature can never drift — it is the one
// export an indicator can call unconditionally against any build to ask
// "who are you" before touching anything version-specific.
TS2P_API int TS2P_CALL EL_DllVersion(void);

#ifdef __cplusplus
}  // extern "C"
#endif

#endif  // TS2PYTHON_H
