# pdvp architecture

The consistent model. Written from requirements, not from what currently
exists — where the two disagree, this document wins.

---

## 1. Principles

These seven rules decide every question below. When a design choice feels
arbitrary, it is because one of these was not applied.

**P0 — pdvp is a programmable module and a program at the same time.** The
command API's caller is *any* code: the human at the prompt, a script, a thread
draining an event subscription. The REPL is one caller among several and gets no
privileges. Anything that would only be true for the prompt — a global "current
frame", printing instead of returning, a rule that some context may not call
something — is a design error, not a special case.

**P1 — DAP is asynchronous; pdvp's synchrony is a policy, not a foundation.**
Every layer below the command surface is async. Blocking lives in exactly one
place: a helper the command layer calls when the caller asks it to (§3). No
lower layer knows whether the caller intends to wait.

**P2 — Each layer speaks one vocabulary and has exactly one consumer above it.**
Generality (filters, multi-subscriber dispatch, pluggable sinks) is added when a
second consumer appears, not in anticipation of one.

**P3 — Mutual-exclusion locks are never held across a wait.** The state lock and
the client registry lock protect short critical sections and are released before
anything blocks. This is what makes SIGINT reentrancy and concurrent commands
safe *without* a global command lock — which cannot exist, because `cont()`
holding it would make `pause()` unsendable.

The **thread control right** (§4) is deliberately not one of these. It is a
queue for a shared resource rather than a guard around a critical section, and
holding it across a run is its entire purpose. It is not a global command lock
because `pause()` and every read bypass it.

**P4 — Every blocking wait is woken by exactly one of: its message arriving, or
the connection dying.** There are no timeouts on protocol-sequenced waits; we
trust the debugger core, and a dead peer is already covered by transport
keepalive. This makes the death path load-bearing: it must be provably total.

**P5 — Register interest before triggering it.** Events routinely precede the
response that caused them (`initialized` arrives before the `attach` response;
`stopped` can precede the `continue` response). Any "do X then wait for Y" is a
race unless the wait is armed first.

**P6 — Handles are epoch-scoped.** A `frameId` or `variablesReference` is
meaningless outside the stop it was minted in. pydevd does not enforce this — it
will happily return a stack for a running thread — so we enforce it ourselves or
we silently serve wrong data.

---

## 2. Layers

Five layers. Each names its vocabulary and, below the command surface, its
single consumer.

```
  callers:   REPL prompt    user scripts    subscription consumers
                   │              │                 │      ▲
                   └──────────────┴────────┬────────┘      │ drains
                                           │               │
                                           v               │
                                       Commands ───────────┤ subscribes
                                           │               │
                              ┌────────────┴───────┐       │
                              v                    v       │
                           Session  ──owns──>   Client     │
                              │                    │       │
                              │                    v       │
                              │                Transport   │
                              │                            │
                              └──────> Event bus ──────────┘

  Console — inferior stdout/stderr and async stop announcements
```

This is a facade over a stack, not a stack. `Commands` is not "the top layer"
whose consumer is the prompt; it is a surface that any caller uses, and the event
bus is one of those callers (P0). Drawing it as a linear stack produced a
contradiction — the bus sitting "below" commands while calling up into them.

Commands both sits above the bus and subscribes to it, and publishes its own
events to it. That is not a cycle, because the bus never calls into anything:
fan-out only queues, so nothing above it can be re-entered. Wiring is bottom-up
and acyclic —

```python
bus     = EventBus()                                    # program lifetime
session = Session(bus)                                  # program lifetime
client  = Client(transport, on_event=session.on_event)  # per connection
```

— so the handshake needs no special case. `initialized` is awaited by an ordinary
subscription taken out before `initialize` is sent.

The bus and Session are constructed once and outlive any one connection; only
Client is per-connection. A subscription taken before a `run()` still works after
the next one, which is what "a user's subscription is long-lived" has to mean.

### Responses and events

A DAP message is either a response or an event, and the two are delivered by
different machinery because they *are* different — not as a design choice:

```
                          ┌─> response table   keyed by seq, point-to-point
reader ──> reduce state ──┤
                          └─> bus fan-out      keyed by name, one copy each
```

A response has exactly one interested caller, identified by `seq`. An event has
N, identified by name. Nothing else motivates the split, and there is no third
mechanism: **event waiting happens in exactly one place, the bus.**

Commands use both, from above: `client.request(...)` for the response,
`bus.subscribe(...)` for the event. Client therefore has no event-waiting API of
its own, which is what keeps it a plain DAP client (P2).

The reader thread must never block on anything user code can influence, so
fan-out puts into unbounded queues. That is one line of policy, not a hazard to
design around.

### Layer 0 — Transport

*Vocabulary:* bytes and framed messages. *Consumer:* Client.

Owns the socket and its keepalive. Two constructors: `connect()` for a pydevd
somebody else started, `accept()` for one we spawned — the asymmetry is confined
to construction and nothing above sees it.

The **only** layer with timeouts (connect, accept, reader join). Everything above
is timeout-free by P4.

Errors: `OSError` for the socket, `ProtocolError` for malformed framing.

### Layer 1 — Client

*Vocabulary:* DAP requests, responses, events. *Consumer:* Session, and only
Session.

Knows nothing about pdvp's model — no threads-as-state, no breakpoints-as-state,
no session. It is reusable against any DAP peer, which is the test for whether
something belongs here.

```
send(request)          -> Pending    # allocate seq, register slot, transmit
request(request)       -> Response   # send(...).wait(), plus a check on success
close()
on_event: Callable     # single sink, invoked on the reader thread; constructor arg
```

`Pending.wait()` blocks until its response arrives or the connection dies, and
returns the response whether or not it succeeded. `Pending` is a context manager
that unregisters on exit.

`request()` **raises** `RequestFailed` on `success: false`. Under concurrent
callers a failure attributed to the wrong command is worse than an exception, so
the convenient surface raises and the primitive returns the failed response for
anyone who wants to inspect it.

The sink receives `schema.Event` for anything the peer sent, and exactly one
`ConnectionClosed` when the connection is over. `ConnectionClosed` is a small
record, deliberately **not** a `schema.Event`: DAP has no such event, and
inventing one would mean every consumer has to know which of its "DAP" events are
actually ours. It carries whether the close was deliberate, which is the
difference between a clean teardown and a failure — and it travels the ordinary
sink, so nothing above needs a second channel to learn about death.

A reverse request (adapter→client) is answered with `success: false`. We
implement none of them, and a peer waiting on an answer it will never get is
worse than a refusal.

**No event-waiting API.** Client correlates responses and forwards events; it
never blocks anyone on an event. A command that needs one subscribes to the bus
before sending (P5). This is what keeps Client reusable against any DAP peer —
it needs nothing above it.

The response table is therefore a plain `dict[seq, slot]` plus the death record.
Death walks one dict, once.

**`on_event` is one attribute, not a subscriber list.** Client has one consumer
(P2). Multi-subscriber fan-out lives in Layer 3. It is a **constructor
argument**, not an attribute assigned afterwards: the reader thread must not be
running before its sink exists.

Locks:

- `_registry` — covers `seq`, the response table, the death record, and the
  deliberate-close flag. Seq allocation and slot registration must be atomic
  with each other, or the reader can deliver a response before its slot
  exists. Registration must be atomic with death, or a caller registered just
  after the death sweep blocks forever (P4).
- `_send` — socket writes only. A DAP message is header plus body across
  multiple writes; interleaving corrupts the stream.

These are deliberately separate. Holding the registry lock across a blocking
`send()` would couple every pending caller to socket backpressure. Two sends from
different threads may then reach the wire out of seq order, which is harmless:
`seq` is an identifier, not an ordering constraint, and two calls from the *same*
thread still serialize by being sequential. Cross-thread ordering was never
implied by anything.

Owns the reader thread. The reader is not optional: `python -i repl.py` blocks in
CPython's own readline, so nothing can pump a select loop on the main thread.

### Layer 2 — Session

*Vocabulary:* pdvp's model — threads, frames, breakpoints, modes, epochs.
*Consumers:* Commands, and the event bus.

Consumes `Client.on_event` **on the reader thread** and reduces it into state.
This is deliberate: internal liveness must never depend on user code. User code
only ever sees events through a subscription queue it drains on its own thread
(Layer 3).

Owns the one state lock. Critical sections are all "read two fields" or "write
two fields"; a single lock is correct and nothing finer is warranted.

#### Three lifetimes

State is grouped by what kills it, and each group has exactly one reset:

| Lifetime | Contents | Reset by |
|---|---|---|
| **Connection** | capabilities, breakpoint ids and `verified`, `sourceMap`, modules, loaded sources | disconnect, for any reason |
| **Process** | thread table | debuggee exit/terminate, and by implication disconnect |
| **Stop epoch** | frames, variables, goto/stepIn targets, `exceptionInfo` | that thread resuming |

A `sourceReference` is connection-lifetime per the DAP spec, not process-lifetime
— which is why `disconnect()` (leaving the debuggee alive) still clears it.

#### Per-thread record

```
ThreadState:
    id, name
    stopped:   bool
    epoch:     int          # incremented every time this thread resumes
    reason:    str | None   # valid while stopped
```

A single `running: bool` cannot represent non-stop mode, where some threads run
while others are parked. The per-thread table is required, not tidy. In all-stop
mode it is trivially uniform, so one structure serves both modes.

#### Cursor

`current_thread_id`, and `current_frame` as `(thread_id, epoch, frame_id)` —
`FrameHandle` in the code, held in a `ContextVar`.

The cursor is **context-local**, not global. The REPL has one; each user thread
draining a subscription has its own. `frame()`, `up()`, `down()` and thread
selection change an implicit argument to the *caller's* next command, so a global
cursor would mean one consumer silently changing what another's next `locals()`
means — and two user threads calling `frame()` would simply race.

Context-local is what lets P0 hold without a "may not move the cursor" rule:
every caller gets the full API because every caller has its own cursor.

A context that has never set one — a worker thread, a subscription drainer —
reads through to a session-wide default: **the last thread to stop**. Setting a
cursor detaches that context from the default permanently. This is not a
convenience: the REPL's own context never sets a cursor explicitly either, so
without the fallback the human would have to type `thread(1)` before their first
`cont()`. It applies to state-changing commands for the same reason. P0 holds
because nobody is privileged — every context that has not chosen sees the same
default.

Switching threads clears the frame cursor unconditionally: a frame handle from
thread A is meaningless once B is selected, independent of running/stopped.

Nothing else clears it, and nothing else can: a lifetime reset may run on any
thread, and a `ContextVar` is only writable from the context that owns it. It
does not need to. Both resets empty the thread table, so a cursor left over from
a dead session fails its guard rather than reading anything.

#### Two independent read guards

Handles need **both** of these. Neither implies the other.

**1. Epoch match.** Every handle carries the epoch it was minted in. Using one at
a different epoch is a clean error — "frame is stale, the program has resumed
since" — not a pydevd error and not wrong data.

Epochs are drawn from a session-wide seed rather than restarting at zero per
thread. pydevd hands out small thread ids and reuses them across runs, so a
per-thread counter would let a handle from the previous session validate against
a brand new thread that happened to inherit its id.

**2. The thread must be stopped.** Epochs only move on resume, so a *running*
thread's stack churns continuously at a constant epoch. Frame-scoped reads
(`stackTrace`, `scopes`, `variables`, `evaluate`, `exceptionInfo`) are therefore
gated on the per-thread `stopped` flag before the request is issued.

Both live here because pydevd enforces neither. Measured (§8): against a running
thread it answers `stackTrace` with a torn snapshot and returns an **empty
variables list** — success-shaped garbage, which a caller renders as "this frame
has no locals" rather than "that question was meaningless."

#### The reducer may not issue requests

It runs on the reader thread, so waiting for a response would be the reader
waiting on itself: instant deadlock. Same family as "the reader must never block
on the public queue." Anything the reducer wants that requires a round trip must
be deferred to a caller's thread.

This is why stop events are not enriched eagerly. An `Event` carries the raw DAP
body plus the epoch it belongs to, and exposes `top_frame` as a **lazy property**
that fetches on first access — from the *subscriber's* thread, so no deadlock,
no cost for subscribers that never look, and a clean "this stop is over" if the
epoch has moved.

#### Reducer rules

| Input | Effect |
|---|---|
| `stopped(tid, allThreadsStopped)` | mark `tid` (or all) stopped, record reason |
| resume request sent | bump epoch optimistically for the threads we expect to resume |
| `continued` event | mark running, bump if not already |
| `thread` started/exited | add/remove from the table |
| `exited` / `terminated` | process-lifetime reset |
| `disconnected` | connection-lifetime reset |

The `continue` response itself is not part of this table: `_issue()` discards
`request(SESSION.client)`'s return value entirely, so `allThreadsContinued` on
the response is never read. Reconciliation happens exactly once, through the
`continued` event and the per-thread `pending_resume` flag — if the epoch was
already bumped at send, the event just clears the flag; if it wasn't (a resume
we didn't issue), the event bumps it then. The response was never load-bearing;
an earlier draft of this table implied a second reconciliation point that the
code doesn't have and doesn't need.

The reducer cannot ask pydevd anything, so the one thing it cannot do for
itself — reconcile the table against a `threads` response — is fed back by the
caller that made the round trip (`adopt_threads`). A thread first mentioned by a
`stopped` event joins the table there and then, which is what makes `connect()`
to a pydevd that started without us work at all: it replays no `thread` events
for threads that already exist.

Bump **at send**, not on the `continued` event. An early bump costs a spurious
"stale frame" error on a race; a late bump returns wrong data silently. Fail in
the direction that is visible.

### Layer 3 — Event bus

*Vocabulary:* pdvp events. *Consumers:* user code, and Commands.

This is the **only** place anything waits for an event. Commands are not a
special case: `cont()` opens a private, short-lived subscription exactly the way
user code opens a long-lived one.

#### The events are ours, not DAP's

The bus carries pdvp types, declared statically — one frozen dataclass per event.
Some are translated from a DAP event; others are ours and have no DAP
counterpart. A subscriber cannot tell which is which, and that is the point: the
frontend can add an event without the debugger core having anything to say.

Forwarding `schema.Event` straight through would make that impossible, because
every event on the bus would have to be one DAP has a name for.

**Subscriptions key on the class, not the name.** It is typo-proof, `match=` gets
a typed argument, and a base class subscribes to a family — `ThreadEvent` takes
every per-thread event, no arguments takes everything. A name-keyed bus can
express none of that. Names are still accepted and resolved to classes at
subscribe time, so a typo is a `LookupError` rather than a stream that never
fires.

#### One event for the end of things

`SessionEnded(reason, exit_code)` covers the debuggee exiting, the debuggee being
terminated, and the connection dying. Those are three signals for one fact,
because we tie the inferior's lifetime to the connection's (§6) — so **the first
signal wins and latches**, and one ending is one event. `SessionStarted` re-arms
the latch for the next connection.

It is in **every** subscription's type set, added whether or not it was asked
for. That is how P4 lands here: a wait ends because its event arrived or because
the session ended, and there is no third outcome and nothing to forget to check
for. A waiter is obliged to expect it; `get()` returns it like any other event,
so nothing raises out of the queue layer.

There is **no dispatch thread and no callback registry**. The whole API is a
subscription — a blocking queue the subscriber owns:

```
bus.subscribe(*types, match=None, maxsize=0) -> Subscription
bus.publish(event)

Subscription:
    get(timeout=None) -> Event    # blocks; woken by close() and by SessionEnded
    __iter__                      # for event in sub: ...
    close()
    __enter__ / __exit__
```

`match` is an optional predicate applied at fan-out, so a command waiting on one
thread's `stopped` does not hand-roll a discard loop. It is never applied to
`SessionEnded`: a filter that could reject the ending would be a filter that can
hang a wait.

```python
with bus.subscribe(Stopped, match=lambda e: e.thread_id == tid) as sub:
    client.request(continue_request)
    match sub.get():
        case Stopped() as stop: ...
        case SessionEnded() as end: ...
```

Subscribing before sending is what satisfies P5, and it makes the order the two
messages reach the wire irrelevant — the `stopped` is queued whether or not the
`continue` response beat it.

`get()` blocks — this is not polling. The subscriber's thread sleeps until an
event arrives. Running that loop on a thread is the subscriber's job; we do not
wrap it, because wrapping it is a line of user code and owning their threads
would drag the whole restricted-context problem back in.

Since every consumer runs on a thread it owns, a consumer that blocks in
`cont()` blocks only its own event stream. That removes at the root what a
shared dispatch thread creates: head-of-line blocking between consumers, a
"handlers must not block" contract, a guard to enforce it, and the
restricted-context contradiction with P0. **There is no context anywhere with a
reduced API** — P0 holds literally rather than by exception.

"Dispatch" reduces to a fan-out loop on the reader thread: copy the subscription
list under a short lock, non-blocking put into each.

#### Bus properties

- **Events are immutable.** They are shared by reference across subscribers, so
  fan-out costs N references rather than N copies — and a subscriber cannot
  corrupt another's view.
- **Events carry their own snapshot.** Subscribers run late; reading live state
  would show them a different world than the event describes.
- **Fan-out never blocks the reader.** Queues are unbounded by default, so `put()`
  cannot stall. `maxsize` is available per subscription for a consumer that knows
  it will lag and prefers drop-oldest to unbounded growth — a memory policy, and
  the only thing it protects against is a subscription nobody drains.
- **Ordering holds within a subscription**, and is not promised across them.
- **P4 lands here for events.** A thread blocked in `get()` must be woken by
  `SessionEnded` and by `close()`, or it hangs at exit. Since commands wait here
  too, this is the *only* place an event wait can hang — the obligation is not
  duplicated in Client, which owes the same totality to responses alone.
- **A broken subscriber cannot break the bus.** `match` runs on the reader
  thread, so an exception from one is caught, counted on the subscription, and
  treated as no-match. The reader survives; the ending still gets through.

Double-visibility remains and is now unremarkable: a consumer that calls `cont()`
receives the next `stopped` as its return value *and* finds it in its own queue.
That is the subscriber's own business to drain or ignore.

Synthetic events — those pdvp invents because DAP has no equivalent:

| Event | Why it must be synthetic |
|---|---|
| `SessionStarted` | DAP has no notion of *our* session |
| `SessionEnded` | the peer cannot report its own death, and `disconnect()` produces none of exited/terminated |

The rule: never invent an event that merely renames a DAP one. There is no
`breakpoint_hit` event — that is `Stopped` with `reason == "breakpoint"`. A DAP
event we have no type for is published as `UnhandledDapEvent` rather than
dropped, so a core that grows a message shows up in a subscriber's stream
instead of vanishing.

### Layer 4 — Commands

*Vocabulary:* the user-facing API. *Callers:* the REPL prompt, user scripts, and
user threads draining subscriptions — all equal (P0).

Uses Client for requests and Session for state and guards. This is the **only**
layer that blocks, and only when the mode says to (P1).

Commands **return** values; they do not print. `Status`, `Error`, `StopResult`
and the info types carry a `__repr__`, so the REPL's ordinary expression echo
renders them and a programmatic caller gets a usable object. Whether anything
reaches the terminal is the caller's choice, which is what makes the same
function usable from the prompt and from a script.

### Console

Single owner of **asynchronous** terminal output: inferior stdout/stderr
passthrough and stop announcements in non-stop mode. Not command results — those
are return values. Having one owner is what makes redrawing the prompt correct
instead of a scatter of ad-hoc writes.

---

## 3. Execution modes

pydevd supports both models at runtime. We expose both; removing non-stop would
be an implementation excuse, not a design.

|  | all-stop | non-stop |
|---|---|---|
| Breakpoint hit | all threads suspend | only the hitting thread |
| `stopped` event | one, `allThreadsStopped: true` | one per thread, `allThreadsStopped: false` |
| `cont()` | resumes everything | resumes only that thread; the rest keep running |
| stepping | one thread steps, others stay parked | same |
| Prompt | reachable only when fully stopped | reachable while other threads run |
| Epoch bump | all threads | the resumed thread |

### Who decides whether a resume blocks

**The caller, not the mode.** `cont()`, `step()`, `next()`, `finish()` and
`jump()` block until the thread they named stops, in *both* modes;
`cont(wait=False)` returns a `Resumption` to `.wait()` on later, in both modes.

These are two independent axes and this document previously conflated them. The
mode decides **what stops**; the caller decides **whether the command returns**.
gdb keeps them separate too: in non-stop a plain `continue` still blocks until
the current thread stops, and background execution is asked for explicitly with
`continue &` / `step &`. `cont(wait=False)` is that `&`.

The rejected alternative was letting the mode decide both — non-stop implying a
non-blocking `cont()`. Its justification is that a blocking `cont()` stops you
touching the other suspended threads while one runs, and that presumes a
controller with something else to do while blocked. The human at the prompt has
nothing else to do: they can only type one command at a time. The capability is
real only for a second actor, or for someone who genuinely wants several threads
in flight at once — and that is precisely the caller who can ask for it.

The constraint underneath is the frontend, and it is the same one that decides
§4. An IDE's Continue button can afford to be fully asynchronous because a GUI
has many output streams to carry the resulting state; a REPL has one, so
asynchrony there reads as output arriving from nowhere. We are tied to REPL
machinery by design, so synchronous is the default and asynchrony is opt-in.

Non-stop keeps what it is actually for: other threads keep running while you are
blocked, hit their own breakpoints independently, and announce those stops
through the bus (§2, Console) rather than freezing until you are done.

This is what makes P1 non-negotiable. Whether a command is synchronous is
decided per call, so synchrony cannot be built into anything below the command
layer.

### The one command-layer decision point

```
resume(thread_id, request, wait=True) -> StopResult | Resumption
```

`wait=True` returns a resolved `StopResult`; `wait=False` returns a `Resumption`.
`cont()`, `step()`, `next()`, `finish()`, `jump()` and the
post-`configurationDone` initial stop all route through it. One place knows about
the mode, and the mode no longer decides the return type — the argument does.

The wait must **always** match on `threadId`. In non-stop another thread's
breakpoint would otherwise satisfy a `cont()` on this one, reporting the wrong
location and leaving the intended thread still running. Since blocking is no
longer mode-dependent, neither is the match: it is required on every wait, in
every mode.

### Where the cursor lands after a stop

**The cursor moves to the thread whose stop resolved the caller's own wait, and
nothing else moves it.** One rule, two different-looking outcomes:

- **all-stop** — the resume affected every thread, so whichever thread stopped is
  the one this caller was waiting for, and the cursor follows it there. The
  switch is **announced** when it actually changed threads, the way gdb prints
  "[Switching to Thread N]". Without that the report describes thread 5's frame
  while the cursor still points at thread 2, and the caller's next `bt()`
  contradicts the line they just read.
- **non-stop** — the wait matches on `threadId`, so only the named thread's stop
  resolves it and the cursor never moves somewhere the caller did not ask to go.
  Another thread hitting a breakpoint meanwhile is announced (§2, Console) and
  followed up with an explicit `thread(5)` if the caller cares.
- **`wait=False`** — nothing resolved, so nothing moves. The cursor moves if and
  when the `Resumption` is waited on.

Since the cursor is context-local (§2), this only ever moves the cursor of the
context that made the call. A stop nobody was waiting for moves no cursor at all.

The frame cursor always resets to the new top frame, in every case: frame handles
are epoch-scoped (P6) and the resume that just ended invalidated the old one.

### Interrupt

One rule, and it does not branch on the mode: **`interrupt()` pauses every
thread we believe is running.**

- In all-stop that is redundant in principle — pausing any single thread
  suspends everything — but it is what makes it *robust*. pydevd's pause is
  tracer-based (§8), so a thread parked in a C-level call never suspends;
  "pause the one thread you picked" can therefore silently do nothing. Pausing
  all of them means any one of them landing stops the world.
- In non-stop it is stop-the-world, which is the only sensible reading: there is
  no single "current run" to interrupt.
- Pause's idempotence (§8) carries both — the requests that hit already-suspended
  threads emit nothing.

Ctrl+C routes here only when **something is running**, which in non-stop means
any thread, not merely a resume this context is blocked in. At an idle prompt it
stays the REPL's line-clear.

`interrupt()` is also what establishes "no resume we initiated is in flight",
which is the precondition for switching into all-stop below.

### Wiring

Mode is `config.non_stop`, applied at attach:

- `stopAllThreadsOnSuspend = not non_stop` — the attach-argument spelling;
  `multiThreadsSingleNotification` is the `setDebuggerProperty` spelling of the
  same `py_db` field and is *not* read from attach arguments (§8)
- `steppingResumesAllThreads = False` — **always pinned**

The second flag needs pinning because it defaults to `True` and has no
`setDebuggerProperty` path (attach-time only). Left alone it produces a broken
hybrid: breakpoints stop one thread, but any step resumes everything — neither
mode. Pinning it to `False` costs nothing in all-stop, where every thread is
already suspended, so per-thread stepping is indistinguishable from all-thread
stepping.

That leaves the mode as the single user-facing switch, and it *is* toggleable
mid-session via `setDebuggerProperty` — better than gdb, which makes you decide
before running.

### Switching modes

The precondition is **no resume we initiated is in flight**. It is deliberately
not "every thread is suspended": a thread blocked in a C-level call never
suspends (§8), so that spelling would make any program calling `join()` unable
to switch modes for its whole life. A thread we did not resume and cannot pause
is not a hazard to the change — it is not ours to stop.

That makes mode the first setting that cannot stay a plain `config.non_stop = True`
once connected, because an assignment cannot refuse. Before `run()` it is a
config field; after, it is a command that can return an `Error`.

The command is two steps, not one: set the property, then **re-commit every
breakpoint**. pydevd stamps each breakpoint's suspend policy from the mode at
`setBreakpoints` time (§8), so breakpoints installed before the flip keep the
old behaviour and produce a hybrid — new stops reported one way, old breakpoints
suspending the other. `commit_all()` already does the second half. None of this
reaches the command surface; it is what the one command does internally.

Both steps, plus the precondition check itself, run under the control right
(§4), held for `None` — every thread — for the same reason an all-stop resume
holds it there: the check and the flip have to be atomic against a concurrent
`cont()`/`step()`/etc., or a resume from another caller can land in the gap and
produce the exact hybrid this procedure exists to prevent. This makes the mode
switch a state-changing command like any other, not a special case bolted on
beside them.

---

## 4. Concurrent callers and the thread control right

P0 means several callers may hold the command API at once. The cursor is not the
shared resource — that is context-local (§2). The genuinely shared resource is
**the run state of each inferior thread**, and the API partitions by how it
relates to that:

| Class | Operations | Requires |
|---|---|---|
| **state-changing** | `continue`, `step`/`next`/`finish`, `pause`, `goto`, the mode switch (`non_stop()`) | the control right (`pause` exempt — see below) |
| **frame-scoped reads** | `stackTrace`, `scopes`, `variables`, `evaluate`, `exceptionInfo` | the thread stopped |
| **state-independent** | `threads`, `setBreakpoints`, `modules`, `loadedSources`, `disconnect` | nothing |

The mode switch belongs in this row, not off to the side as a configuration
concern: `config.non_stop` is only ever the default the *next* `run()`/`connect()`
attaches with (§3); once connected, changing the live mode is a command that
mutates the run state every other row in this table protects, and it is keyed
and guarded exactly like one — `None` (every thread), the same key an all-stop
resume already uses.

### Why a right is needed at all

Without one, two callers resuming the same thread produce **one** resumption —
pydevd resumes on the first `continue`, the second is a no-op, and both
subscriptions match the same `stopped`. Two commands, one run, success reported
to both. A
silently swallowed command is the worst outcome available; the other races
degrade acceptably (a `step` against a running thread is a clean pydevd error, and
a concurrent `pause` correctly surfaces as `reason: "pause"` — the Ctrl+C path).

With the right, N continues become N serialized runs.

### Shape

- Acquired by every state-changing operation, from before the send; how long it
  is held depends on whether the caller waits (below).
- **`pause` is exempt.** Ending somebody else's run is precisely its job (§3,
  Interrupt), and measured (§8) it is idempotent — a second pause on an
  already-suspended thread emits nothing at all — so it cannot corrupt another
  caller's armed subscription. The exemption is what keeps Ctrl+C working and is the
  escape hatch when one caller's long run is making another wait.
- Reads and state-independent operations never touch it.
- No timeout, per P4; released when the stop arrives or the connection dies. No
  deadlock is possible, because the holder is woken by the reader thread (§2,
  two delivery paths) — nothing the right blocks is needed to release it.

**The key follows the mode; the duration follows the call.** Both are
consequences rather than special cases:

- **non-stop** — a resume affects one thread, so the key is that thread's id.
- **all-stop** — a resume affects everything, so the key is `None`, which is
  already what a resume naming no thread passes. Nothing chooses between the
  two spellings.
- **`wait=True`** — held from the send until the stop, so the run is the unit
  that is serialized. This is the case that matters: N continues become N
  serialized runs instead of one run reported as N successes.
- **`wait=False`** — held only across the send. That still earns its keep: it
  makes the second caller's request provably arrive while the first thread is
  already moving, so pydevd refuses it loudly instead of accepting a
  near-simultaneous duplicate as a no-op success.

Because blocking is the caller's choice rather than the mode's (§3), the right
has the same meaning in both modes — it does not degenerate into an all-stop
mechanism.

### Sequence atomicity

The right has two levels. The first is internal, the second is user-facing API.

**Implicit — every state-changing command acquires and releases around itself.**
Nobody writes anything. This is what makes concurrent callers safe by default,
and the human at the prompt never needs to know it exists.

**Explicit — `control()` extends the hold across a sequence.** Per-command
acquisition prevents corruption but not interleaving: `cont(); bt()` can have
another caller's `cont()` land in the gap, so the `bt()` reads a thread somebody
else resumed. When that matters, the caller says so:

```python
with pdvp.control(thread=2):    # user-facing
    cont()
    bt()
    step()
```

Same primitive, longer hold — so acquisition must be **reentrant per owning
context**: inside the block, `cont()`'s own implicit acquire is a no-op rather
than a self-deadlock.

In practice `control()` is for scripts and subscription-draining threads. The
REPL never needs it, because the human is sequential anyway. Its value is that
contention becomes visible in the code rather than emergent.

### Usability

Contention is rare in practice. The realistic patterns are **observer +
controller** (one driver, N read-only subscribers — zero contention),
**handoff** (script runs to a condition, human takes over — sequential), and
**per-thread division** (non-stop: script drives A, human drives B — different
resources). Genuine simultaneous competition for one thread is usually a bug in
the caller.

The design bar is therefore "degrade predictably", not "make competition
pleasant". Two things meet it: a blocked acquisition must announce itself rather
than hang mutely ("waiting for control of thread 2"), and Ctrl+C must remain the
escape hatch — which it is, because `pause` bypasses the right, ends the holder's
run, and releases it.

### Rejected: durable thread ownership

The alternative was for a claim to outlive the run: `thread(2)` takes ownership
of thread 2 and keeps it until released, others get an error instead of a queue,
and sequence atomicity comes free (so `control()` would not exist). It is the
simpler concurrency story and it was rejected anyway, for two reasons.

**It taxes the extension path.** Consider the smallest realistic automation: a
subscriber that steps a few times when a breakpoint is hit. Under a per-run hold
this needs nothing — the driver's hold ends at the stop, the subscriber's `step()`
acquires and releases around itself, and the brief queue between them resolves in
microseconds. Under durable ownership the same handler needs release machinery
threaded back into whatever command started the run, plus an answer for how the
human gets the thread back afterwards. The ceremony lands on exactly the code we
want people to be able to write casually.

**Blocking beats erroring for the pattern people actually write.** The objection
to a queue is that a queued command was decided in a world that no longer exists
by the time it runs. That is true of genuine contention — which §4 already
classifies as a caller bug — and false of the common case, which is a *handoff at
a stop*: the previous holder is already returning, so the wait is negligible and
the world has not moved. Erroring there would force every handler into a retry
loop for a right that is free by the time it retries.

What the hold guarantees, then, is narrow and deliberate: it prevents the
*silent* failure, two commands producing one run and reporting success to both.
Past that we assume the person writing automation — the user themselves, or a
plugin author they chose to trust — knows what their code does. That assumption
is what licenses us to not build ownership, revocation, stealing, or a `release()`.

The same assumption rejects a second alternative for the same reason: forcing
every state-changing command through a wrapper that *requires* a key, so a new
one cannot be written without acquiring the right. It would have caught a real
bug once (§9 tracked it before the fix landed), but it optimizes for the wrong
threat model — protecting commands from each other — when the one this section
actually protects against is the silent double-resume, and the realistic
callers (Usability, above) are cooperative, not adversarial. Ceremony aimed
at a caller who isn't there is exactly the tax the paragraph above declines to
pay. The fix that was actually needed was smaller: the one command that was
supposed to already follow this section's convention and didn't (`non_stop()`)
now does, same as every other row in the table above.

### Automation and reporting

Reaction to an event belongs on the bus (§2, Layer 3), and nowhere else. In
particular **the stop report is not an extension point**: the hooks that
contribute to a `StopResult`'s suffix exist to compose *that command's own
output* — re-evaluated `display()` expressions, a temporary breakpoint's
auto-clear line, both of which gdb also prints as part of the stop announcement.
They are pure: they return a line or nothing, change no state, issue no requests.
A callback list that fires on a stop looks like an event system and will attract
uses that belong on the bus, so its name and contract have to say what it is.

A subscriber that changes the program's state owns the reporting of what it did.
Its output can otherwise land ahead of the blocked caller's own `StopResult` —
it wakes when the event is published, which is before that caller returns — and
a bare `print` collides with a half-typed prompt. The async-safe print that
clears the line, writes, and redraws the prompt is therefore part of the public
contract, not an internal detail.

---

## 5. Threads and locks

Four threads, and one thing that looks like a thread but isn't.

| Thread | Role |
|---|---|
| **main** | REPL: at the prompt, or inside one command |
| **reader** | recv → parse → reduce → resolve responses → fan out events |
| **user threads** | zero or more, owned by user code, draining subscriptions and issuing commands |
| **pty pump** | inferior output → Console |

**SIGINT is not a thread.** Python runs signal handlers on the main thread, so
Ctrl+C during a blocked `cont()` *re-enters* main while it waits. Interrupt is
reentrancy, not a data race — which is exactly why P3 exists: if main held the
state lock while blocked, the handler's `pause()` would self-deadlock on a
non-reentrant lock.

Lock order, where more than one is ever held: **Session before Client, never the
reverse.** The reducer takes the session lock and does not call back into Client
while holding it.

---

## 6. Death and teardown

Connection death is reported once, at the Client layer, through `on_event` — the
ordinary path, so the bus needs no separate channel to learn the connection is
gone. The reducer turns it into `SessionEnded`, the same event the debuggee
exiting produces, because they are the same fact. Every pending response is woken
by the death sweep and every blocked `get()` by the ending (P4).

- `close()` is idempotent, and safe to call from any caller including the reader
  thread itself (it must not self-join).
- The deliberate-close flag is set **before** shutting the socket down, so the
  reader racing to declare death first still reports "closed locally" rather
  than misreporting a user's `disconnect()` as the peer vanishing.
- The DAP connection and any process we spawned share one lifetime: tearing down
  either tears down both.

**Interrupt is not a teardown path.** Ctrl+C during a blocked `cont()` sends
`pause`; pydevd suspends the thread and emits `stopped` with `reason: "pause"`,
which the already-armed subscription resolves on. The wait ends because what it was
waiting for happened — no abandonment, no distinction between "at the prompt"
and "inside a wait" needed.

The one requirement this places on `interrupt()` is that it **must not block**.
Waiting for the `pause` response inside a signal handler achieves nothing: what
resolves the outer wait is the `stopped` event, not the response. So interrupt
sends and returns — the canonical asynchronous command, and an illustration of
P1 rather than an exception to it.

A genuinely deadlocked pydevd is out of scope: TCP stays healthy (the kernel
answers keepalive probes regardless of application state), so nothing
distinguishes it from a slow one. We trust the core; a dead *peer* is covered by
transport keepalive.

---

## 7. Decided

- Client is async; `request()` is `send().wait()` plus a success check, and raises
  `RequestFailed` where the primitive returns the failed response
- Responses correlate by `seq` inside Client; events wait only on the bus. Client
  has no event-waiting API, so it stays a plain DAP client
- The bus carries pdvp event types, declared statically, and subscriptions key on
  the class — so a family subscribes with a base class and our own events are
  indistinguishable from translated ones
- One `SessionEnded` for the end of things: debuggee exit, termination and
  connection death are one fact, first signal latches. It is in every
  subscription whether or not it was asked for
- The bus and Session are program-lifetime; only Client is per-connection
- Commands use both surfaces from above: `client.request()` for the response,
  `bus.subscribe()` for the event. A command's subscription is private and
  short-lived; a user's is long-lived. Same mechanism
- Subscribing before the trigger is the only correct spelling of "do X then
  await Y"
- No timeouts above Transport
- Reducer on the reader thread; user code sees events only via subscription
  queues it drains on its own threads — no dispatch thread, no callback registry
- Per-thread `(stopped, epoch)` table; single `running` bool deleted
- Epoch-tagged handles, bumped at resume-send
- Both execution modes supported; `steppingResumesAllThreads` pinned false
- The mode decides what stops; the **caller** decides whether the command
  returns. `wait=True` everywhere by default, `wait=False` is gdb's `continue &`
- The wait always matches on `threadId`, in every mode
- The cursor follows the stop that resolved this caller's own wait, and nothing
  else. In all-stop that can change threads, and the switch is announced
- Cursor is context-local; every caller has the full API. A context that never
  set one reads the last thread to stop
- `interrupt()` pauses every thread believed to be running, in both modes — one
  rule, and robust against threads pydevd's tracer cannot reach
- `interrupt()` is fire-and-forget; the armed subscription resolves on `stopped`
- Thread control right: implicit per state-changing command, explicitly holdable
  via `control()`, `pause` exempt. Key follows the mode, duration follows the call
- Durable per-thread ownership rejected: it taxes the extension path, and for a
  handoff at a stop a queue beats an error
- Mode switching requires no resume in flight — not "every thread suspended"
- The stop report composes one command's output; it is not an extension point.
  Reaction belongs on the bus, and a subscriber owns its own reporting
- Frame-scoped reads gated on the stopped flag, separately from the epoch check
- The reducer never issues requests; `Event.top_frame` is lazy

---

## 8. Measured pydevd behaviour

Observed against pydevd 3.5.0, not inferred from the spec. Reproducible with
`samples/targets/two_threads.py` and `samples/targets/busy_thread.py`.

| Question | Answer |
|---|---|
| Default stop mode | **non-stop** — `allThreadsStopped: false`, one `stopped` per thread; two threads suspend independently |
| Switch | `py_db.multi_threads_single_notification`, default `False` (`pydevd.py:480`) |
| How to flip | attach arg `multiThreadsSingleNotification`, attach arg `stopAllThreadsOnSuspend`, or CLI `--debug-mode debugpy-dap`; also mid-session via `setDebuggerProperty` |
| `continue(tid)` in non-stop | resumes only that thread, `allThreadsContinued: false`; others stay parked |
| `continue(tid)` in all-stop | **`tid` is discarded** — `on_continue_request` rewrites it to `"*"`, resumes everything, replies `allThreadsContinued: true`. No error, no warning |
| all-stop attach key | `stopAllThreadsOnSuspend` — `multiThreadsSingleNotification` is the `setDebuggerProperty` spelling and is *not* read from attach arguments |
| Stepping | resumes **all** threads — `steppingResumesAllThreads` defaults `True` on attach and has no `setDebuggerProperty` path |
| `supportsSingleThreadExecutionRequests` | never advertised; `singleThread` exists only in the generated schema and no handler reads it, so granularity comes from the mode and never from the request |
| Breakpoint suspend policy | **stamped at `setBreakpoints` time** — `suspend_policy = "ALL" if py_db.multi_threads_single_notification else "NONE"` (`pydevd_process_net_command_json.py:767`, `:806`). Flipping the mode later does not revisit breakpoints already installed |
| `stackTrace` on a running thread | **succeeds** with a torn snapshot (observed depths 1, 12, 1 on the same thread) |
| `variables` on a running thread's frame | **succeeds, returns `[]`** — success-shaped garbage, no error |
| Second `pause` while running | one `stopped` event total for two in-flight pauses |
| `pause` on an already-suspended thread | emits **nothing** — fully idempotent |
| `pause` on a thread parked in a C-level call | **never suspends** — pause is tracer-based, so a thread blocked in `join()`/`acquire()`/a blocking read is unreachable until it returns to Python |
| Debuggee finishing | `terminated` only — **`exited` is never sent**, so the exit code can only come from the process we spawned |

Spec defaults worth remembering, since they are not uniform:
`StoppedEvent.allThreadsStopped` missing means *only* that thread stopped, but
`ContinueResponse.allThreadsContinued` missing means **all** threads resumed. A
generic "absent is false" helper gets the second one backwards.

## 9. Open

- Whether `before_prompt` becomes a real hook or stays out of the event model.
- **`SessionEnded.exit_code` is still unfilled for the common case.** pydevd
  never sends `exited` (§8), so the only wire path in `events.py` (`"exited":
  ... exit_code=_field(body, "exitCode")`) never fires in practice. `end_process()`
  (session.py) calls `self.process.child.wait()` on the process we spawned but
  discards the return code — it never reaches `SessionEnded`. The layer that
  owns the child still has to carry that value into the ending; nothing does
  yet, so the field stays `None` for a normal exit.
- **Composite commands.** `until()` was a temporary breakpoint, a `cont()` and a
  clear — three round trips presented as one command, and it was removed rather
  than kept working by accident. What such a command means when it is
  interrupted halfway, and who owns the state it leaves behind, needs deciding
  before any of them come back.
- **`threads()`'s auto-select cursor side effect.** `commands/stack.py` sets
  `SESSION.current_thread_id = thread_list[0]["id"]` when the caller has no
  cursor yet — but per §2, setting a cursor detaches that caller from the
  `_last_stopped` fallback *permanently*, and the doc is explicit that no
  command should do this passively ("this is not a convenience"). Currently
  dead in practice, since `_handshake()` always blocks for an initial stop
  before returning control, so `current_thread_id` is never `None` by the time
  a caller can reach `threads()`. Worth deleting the auto-select (or reading
  `_last_stopped` directly for display) so the behavior matches the invariant
  it currently only violates by accident of call order.
- **The package doesn't import on Windows.** `launch.py` (`import pty`) and
  `console.py` (`import readline`, `import termios`, `import tty`) all import
  these POSIX-only stdlib modules unconditionally at module scope, with no
  `sys.platform`/`os.name` branching anywhere in the codebase. Both sit in the
  middle of the import graph, not off to the side — `session.py` imports
  `launch`, and `commands/execution.py`/`lifecycle.py` import from `console`
  — so `import pdvp` itself fails on Windows with `ModuleNotFoundError`
  before any pdvp code runs, not just the PTY-passthrough feature
  degrading. Not a priority right now; noted so it isn't rediscovered from
  scratch later. If it becomes one, the cheap first step is making the two
  imports lazy/guarded so the package is at least importable, with the
  PTY-dependent features (owned-PTY passthrough, `run()`'s default
  stdin/stdout/stderr) failing with a clear error on Windows rather than
  `ModuleNotFoundError` — real terminal support (msvcrt/ConPTY) is a
  separate, much larger effort.
- Whether `model.py`'s result objects should also grow methods (not just
  fields) for pleasant non-REPL/scripting use, and whether `InfoSections`/
  `ExceptionInfo` should keep the real schema object instead of the current
  `.body.to_dict()` flattening — neither piece of the output/return-type
  redesign (see "Decided, not yet built" below) is resolved. Also
  unresolved: a broader audit for exceptions other than `DAPError` escaping
  the command surface where they shouldn't ("Decided and built" below, the
  `DAPError`-leak fix entry) — real implementation-time work, not started.

### Decided, not yet built: the output/return-type redesign

A design pass on `model.py`'s result types and the core/extra package split,
across several conversations — decided, but not all of it implemented yet.
`Status` (currently just a falsy-aware `str`) is the remaining symptom of
what started it: ad hoc stringification standing in for real fields. Points
1, 2, and 5 of the original 9-point list are done — see "Decided and built"
below; what's left:

1. **Exception documentation reframed, not just deprioritized.** Since
   nothing should raise under normal operation (see "Decided and built"
   below), `Raises:` isn't the useful thing to document — the useful
   equivalent is which `Error` *kinds* (`model.ErrorKind`, now built — see
   "Decided and built") a command can return: `Returns Error(kind=...)
   when:` instead of `Raises:`.
2. **Core/extra split — by compositeness, not by ptpython dependency.**
   Core is the thin one-request-one-response layer plus the machinery that
   supports it (session, cursor, control rights, event bus, config, launch,
   transport/client, the primitive commands). Extra is everything built on
   top: composite/multi-request commands (a future `until()` — temp
   breakpoint + `cont()` + clear), "viewers" (planned), ptpython/
   prompt_toolkit integration (`completion.py`, `highlighting.py`,
   `keybindings.py`), and any future rich-rendering hook. Audience test:
   core is for anyone who'd rather compose primitives than reach for a
   canned convenience — a script, a plugin, a pre-configured debug suite, or
   an AI agent sketching the three-line equivalent of a composite command
   itself. Extra is the convenience layer for interactive humans and more
   elaborate tooling — why `display()` (removed, see below) would have
   belonged there despite importing nothing ptpython-specific.
3. **Rendering hook (`pt_repr`-style) — out of scope for now.** Depends on
   the core/extra split existing as a real package boundary, not just a
   documented intention. Two directions floated, not chosen between: a
   duck-typed method returning a plain `list[tuple[str, str]]` (structurally
   identical to prompt_toolkit's `FormattedText`, no import needed in core),
   or a `functools.singledispatch`-style renderer registry living entirely
   in extra.

None of this is implemented. Point 1 is unblocked now that the taxonomy
exists; points 2 and 3 depend on each other (3 needs 2's package boundary to
exist) but not on point 1.

### Decided and built

Layers 0–4 are built to this document, including the parts this section used to
list as missing:

- `resume()` (`commands/execution.py`) is the single decision point every
  state-changing command routes through, exactly as §3 describes — the
  `_internal.py` this section used to point at is gone, folded into
  `execution.py` directly.
- The thread control right (`Session.ControlRights`) is built and acquired in
  `resume()`; `control()` holds the same primitive across a sequence,
  reentrant per owner. `pause` bypasses it, per §4.
- **The wait matches on `threadId`.** `_issue()`'s subscription filters on
  `event.all_threads or event.thread_id == key`, so another thread's stop can
  no longer satisfy a wait it wasn't meant for.
- The cursor rule is written and enforced: `report_stop()` moves only the
  calling context's cursor, to the thread that resolved its own wait, and
  announces `[Switching to thread N]` only when that thread actually changed —
  matching §3's all-stop/non-stop split.
- `config.non_stop` exists, `multiThreadsSingleNotification` is set at attach,
  `steppingResumesAllThreads` is pinned `False`, and `non_stop()` is a real
  mode-switch command, a state-changing operation like any other in §4's
  table: it holds the control right for `None` across the whole
  precondition-check/flip/re-commit sequence, so it's atomic against a
  concurrent `resume()` and refuses cleanly when a resume is genuinely in
  flight.
- `Resumption` is built: `cont(wait=False)` (and friends) return one, `.wait()`
  resolves it once and caches the result. There is no separate `wait(tid)`
  command — `.wait()` on the returned `Resumption` is the whole interface, and
  that's now a decision rather than an open question.
- A stop that resolves nobody's wait gets exactly an announcement
  (`lifecycle._dispatch` → `print_async`) and nothing else — decided, not just
  observed.
- `interrupt()` pauses every thread believed running, not just the caller's
  current one — robust against the tracer-based-pause gap in §8, matching §3.
- Stdin passthrough (`console.StdinPassthrough`) is wired to whichever call is
  actually blocked in `Resumption.wait()` — foreground/background falls out of
  that for free, since a `wait=False` resume gets no passthrough until
  something waits on it.
- **Fixed: `non_stop()` re-checks the connection after acquiring the right.**
  It used to check `SESSION.client is None` only *before* `control.hold(...)`,
  never after — unlike `resume()`, which re-checks `require_connected()` once
  it actually holds the key, because whoever held it may have ended the
  session in the meantime. A disconnect landing while `non_stop()` was queued
  behind another holder used to reach `SESSION.client.set_debugger_property(...)`
  on a `None` client (`AttributeError`) instead of a clean `Error(...)`. Now
  applies the same rule `resume()` already did. Regression test:
  `test_non_stop_re_checks_the_connection_after_the_right_is_acquired` in
  `pdvp/test/test_execution.py`.
- **Removed: `stop_report_lines`/`display()`/`undisplay()`.** The hook list,
  `_show_display()`, and `SESSION.displays` are gone from
  `commands/execution.py`/`commands/inspection.py`/`session.py`; `StopResult`
  no longer has a `suffix` field. See point 1 of the output/return-type
  redesign above for why. Not relocated to extra — extra isn't a real
  package boundary yet, and a vestigial `display()` that stores expressions
  nothing reads back is worse than no `display()` at all. Revisit if/when
  extra exists.
- **Fixed: every `DAPError` leak named in the redesign's point 2 now returns
  `Error(...)` instead of raising or crashing.** `execution.py`'s `_issue()`
  catches `DAPError` around the resume request and returns `Error` (cleaning
  up the same resume/subscription state the `BaseException` branch already
  did); `resume()`/`non_stop()` propagate it. `breakpoints.py`'s
  `commit_source_breakpoints()`/`commit_function_breakpoints()` now catch
  `DAPError` and turn a failed response (`success=False`) into `Error`
  instead of `raise model.PDVPError()`; `commit_all()`, `sbreak()`,
  `fbreak()`, `clear()`, `enable()`/`disable()`, and `lifecycle.py`'s
  `_handshake()` all propagate the result instead of discarding it. The
  broader stray-exception audit (index/key/attribute errors from
  unexpected-shaped DAP responses, not just `DAPError`) is still open, not
  started.
- **Built: `RunResult`/`ConnectResult` wrap `StopResult`.** `run()`/`restart()`
  return `RunResult` (adds `killed_previous`, `spawned_pid`, `connected_to`);
  `connect()` returns `ConnectResult` (adds just `connected_to`); `cont()`/
  `step()`/`next()`/`finish()` keep plain `StopResult`. `lifecycle.py`'s
  `_handshake()` now returns a bare `StopResult` and no longer builds
  `prefix_lines` text itself — `_run()`/`_connect()` wrap its result into the
  richer type afterward, carrying `spawned_pid`/`connected_to`/
  `killed_previous` as real fields instead of only inside the `prefix` string
  (which the two subclasses still compute, for the repr).
- **Built: `StopResult` carries the source line it stopped at.**
  `execution.py`'s `report_stop()` calls a new `_source_line_at(top_frame)`
  that reads the one line at the frame's `path:line` and wraps it in a
  one-entry `model.SourceLines` — gdb's convention, not a window (`ls()` is
  still how to get one). Degrades to `source=None` on any `OSError` or a
  frame with no source path, never fails the stop itself. `RunResult`/
  `ConnectResult` forward it too, so the initial stop after `run()`/
  `connect()` shows it as well. Regression tests:
  `test_stop_result_carries_the_single_source_line_when_reachable` and
  `test_stop_result_source_is_none_when_the_file_is_not_reachable` in
  `pdvp/test/test_execution.py`.
- **Built: `model.Breakpoints` is a real `dict[int, Breakpoint]` subclass.**
  `commands/breakpoints.py`'s `breakpoints()` now returns
  `model.Breakpoints(SESSION.Breakpoints)` instead of the bare dict; the
  file-grouped, function-breakpoint-listing `__repr__` is computed on demand
  from each `Breakpoint`'s own `isinstance`/fields (files sorted by path,
  each file's breakpoints sorted by line) rather than tracked as separate
  constructor arguments. No exception-filter section anymore — `catch()`
  doesn't exist yet, so there was nothing real to display there. Tests:
  `pdvp/test/test_breakpoints.py`'s `breakpoints()` section.
- **Built: the `Error` taxonomy — hybrid `ErrorKind` enum + two typed
  subclasses.** `model.py` gained `ErrorKind` (20 members covering every
  distinct failure the command surface returns — not connected, already
  connected, no current thread/frame/file, thread running, no such
  thread/frame, no frames, no jump target, program not running, resume in
  flight, no script, no active session, launch failed, handshake failed,
  source unavailable, line number required, stale frame, pydevd refused)
  and `Error.kind`, now a
  **required** keyword-only field — `Error(message)` without `kind=` raises
  `TypeError`, deliberately: no silent "uncategorized" default to make it
  easy to skip. Two kinds carry real structured data instead of just the
  tag: `StaleFrameError(thread_id, stale_epoch, current_epoch)` (from
  `session.py`'s `require_frame()`) and `PydevdRefused(message, cause=None)`
  (`cause` is the underlying `dap.DAPError` when there was one, `None` for a
  `success=False` response with no exception) — both subclass `Error`, so
  `isinstance(x, Error)`/the falsy contract are unchanged everywhere that
  already relies on them. Every `Error(...)`/former bare-`DAPError`-message
  call site across `session.py` and every `commands/*.py` module now passes
  a kind or is one of the two subclasses — audited exhaustively, not
  spot-checked (see `pdvp/test/test_model.py` for the taxonomy's own
  contract tests, and the strengthened stale-frame/DAPError tests in
  `test_execution.py`/`test_inspect.py`). One incidental fix found while
  auditing: `jump()` (`execution.py`) had a dead `try/except DAPError`
  around its `resume()` call — `resume()` itself stopped being able to raise
  `DAPError` when `_issue()` was fixed earlier this session (see the
  `DAPError`-leak entry above), so the outer catch was unreachable; removed.

One caveat from this section is still real and worth keeping, moved here rather
than filed as unmigrated: the control right is taken before the resume request
is sent, so a `pause` racing the send arrives while the thread is still
suspended and, per §8, emits nothing — leaving the run unbounded. Anything
automating "resume, then interrupt" must wait for the `continued` event, not
for the right to change hands.

### Fixed: the spawned interpreter

`launch.py`'s `build_spawn_argv()` used to reuse `config.vm_type` — pydevd's
own `--vm_type` wire concept (`"python"`/`"jython"`), never itself emitted to
argv — as the literal executable `Popen` execs. That's not a test-only
problem: `spawn_pydevd()` is the one code path both `run()` and every
DAP-integration test's `session()` helper share, so a bare `"python"` not
being on `PATH` (this box only has `python3` — not unusual; many distros drop
the unversioned symlink per PEP 394) broke `run()` itself, not just its tests.
Fixed by adding `config.python_executable` (`--python`, or `PYTHON_EXECUTABLE`
in the environment), defaulting to `sys.executable` — the interpreter already
running us, guaranteed to exist and already absolute — and used only when
`vm_type` is `PYTHON`; the Jython path is untouched, since `"jython"` is an
actual launcher binary name in a way a bare `"python"` is not guaranteed to be.

An earlier pass through this section claimed `test_dap_client.py` "runs fine"
once the executable was fixed — checked more carefully, that was wrong: the
file tested a fully removed `Client` surface (`wait_for_event()`, dict-style
response access), which the previous `FileNotFoundError` had been masking
before the executable existed to even reach that code. It wasn't a duplicate
of `test_client_events.py` either — that file only exercises the event bus
(death, matching, family subscriptions), never `evaluate`/`scopes`/`variables`/
stepping/`pause`/`exception_info`/function breakpoints/`goto`/`completions`/
etc. Rewritten to the current `Client` API instead of retired; both files now
pass against a real pydevd and cover disjoint ground.

### Fixed: three commands that crashed on their own success path

Found while writing unit tests for `commands/inspection.py` and
`commands/misc.py` (no real pydevd needed — the bug is visible against any
response shaped like the real one): `exception_info()`, `modules()`, and
`pydevd_info()` each read the raw response object `SESSION.client.*()`
returns instead of its `.body`, the way every sibling command in those two
files does. The real response types (`pdvp/schema/pydevd_schema.py`) are
`__slots__`-based `BaseSchema` objects with no `.get()`/`.keys()`, so:

- `exception_info()` did `ExceptionInfo(info)` — `ExceptionInfo` is a `dict`
  subclass, so this raised `TypeError` on every successful request.
- `modules()` did `ModuleList(result.get("modules", []))` — raised
  `AttributeError` on every successful request.
- `pydevd_info()` did `InfoSections(result)` — same `TypeError` as
  `exception_info()`, same reason.

In each case the `try/except DAPError` around the request covered the
*failure* path only; the crash was on the success path, which is a worse bug
than the DAPError-propagation gaps noted above — those degrade a failure into
a raw exception, these turned three commands into ones that could never
return a result at all. Fixed by reading `.body` (and, for the two whose body
itself nests further schema objects — `exceptionId`/`details` on
`ExceptionInfoResponseBody`, `python`/`platform`/`process`/`pydevd` on
`PydevdSystemInfoResponseBody` — `.body.to_dict()`, which recurses through
those refs the way `Client.request()`'s callers elsewhere already rely on).
`modules()`'s `result.body.modules` is already a plain list of dicts, matching
how `breakpoints.py` treats the analogous `set_breakpoints()` response, so no
`.to_dict()` needed there.
