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
place: a helper the command layer calls when the execution mode says to. No
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
                                       Commands            │
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

### Two delivery paths, one producer

Every message the reader parses goes two independent ways:

```
reader thread ──┬─> waiter registry   (wakes whoever is blocked in a command)
                └─> subscriptions     (fan-out to consumer-owned queues)
```

This is the invariant that makes an event consumer calling `cont()` safe: the
wait is resolved by the **reader**, never by any consumer, so a busy consumer
cannot starve it.

It also creates the one deadlock that must be designed out. If the reader can
ever *block* enqueueing to the public queue:

> a consumer blocks in `cont()` → its subscription queue fills → reader blocks
> on `put()` → reader cannot wake `cont()`'s waiter → both stuck forever.

So **the reader thread must never block on anything user code can influence.**
Bounded-with-drop-oldest is a correctness requirement, not a memory-pressure
tweak; a plain blocking bounded queue is a deadlock.

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
send(request)          -> Pending    # allocate seq, register waiter, transmit
expect(*event_names)   -> Pending    # arm before the trigger (P5)
request(request)       -> Response   # send(...).wait(), sugar over the primitive
close()
on_event: Callable     # single sink, invoked on the reader thread
```

`Pending.wait()` blocks until its message arrives or the connection dies.
`Pending` is a context manager that unregisters on exit.

**One registry, not two.** A waiter is a *predicate plus a slot*. A response
waiter matches `seq`; an event waiter matches a name set. This is the single
most important simplification in the design: two registries meant two death
loops, two registration-vs-death races, two lifetimes. One registry means death
walks one list, once.

**`on_event` is one attribute, not a subscriber list.** Client has one consumer
(P2). Multi-subscriber fan-out lives in Layer 3.

Locks:

- `_registry` — covers `seq`, the waiter list, the death record, and the
  deliberate-close flag. Seq allocation and waiter registration must be atomic
  with each other, or the reader can deliver a response before its waiter
  exists. Registration must be atomic with death, or a waiter registered just
  after the death sweep blocks forever (P4).
- `_send` — socket writes only. A DAP message is header plus body across
  multiple writes; interleaving corrupts the stream.

These are deliberately separate. Holding the registry lock across a blocking
`send()` would couple every waiter to socket backpressure. Two sends from
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

`current_thread_id`, and `current_frame` as `(thread_id, epoch, frame_id)`.

The cursor is **context-local**, not global. The REPL has one; each user thread
draining a subscription has its own. `frame()`, `up()`, `down()` and thread
selection change an implicit argument to the *caller's* next command, so a global
cursor would mean one consumer silently changing what another's next `locals()`
means — and two user threads calling `frame()` would simply race.

Context-local is what lets P0 hold without a "may not move the cursor" rule:
every caller gets the full API because every caller has its own cursor.

Switching threads clears the frame cursor unconditionally: a frame handle from
thread A is meaningless once B is selected, independent of running/stopped.

#### Two independent read guards

Handles need **both** of these. Neither implies the other.

**1. Epoch match.** Every handle carries the epoch it was minted in. Using one at
a different epoch is a clean error — "frame is stale, the program has resumed
since" — not a pydevd error and not wrong data.

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
| `continue` response `allThreadsContinued` | reconcile the optimistic bump |
| `continued` event | mark running, bump if not already |
| `thread` started/exited | add/remove from the table |
| `exited` / `terminated` | process-lifetime reset |
| `disconnected` | connection-lifetime reset |

Bump **at send**, not on the `continued` event. An early bump costs a spurious
"stale frame" error on a race; a late bump returns wrong data silently. Fail in
the direction that is visible.

### Layer 3 — Event bus

*Vocabulary:* pdvp events. *Consumer:* user code.

There is **no dispatch thread and no callback registry**. The whole API is a
subscription — a blocking queue the subscriber owns:

```
bus.subscribe(*names) -> Subscription

Subscription:
    get(timeout=None) -> Event    # blocks; woken by close() and by death
    __iter__                      # for event in sub: ...
    close()
    __enter__ / __exit__
```

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
- **Fan-out is non-blocking, drop-oldest, per subscription.** Correctness rule,
  not a memory tweak: see "two delivery paths" above. Scoped per subscriber, so
  a slow consumer only degrades itself.
- **Ordering holds within a subscription**, and is not promised across them.
- **P4 extends here.** A thread blocked in `get()` must be woken by connection
  death and by `close()`, or it hangs at exit — the same totality obligation the
  waiter registry carries.

Double-visibility remains and is now unremarkable: a consumer that calls `cont()`
receives the next `stopped` as its return value *and* finds it in its own queue.
That is the subscriber's own business to drain or ignore.

Synthetic events — those pdvp invents because DAP has no equivalent:

| Event | Why it must be synthetic |
|---|---|
| `disconnected` | the peer cannot report its own death |
| `session_started` / `session_ended` | DAP has no notion of *our* session; `disconnect()` produces none of exited/terminated/disconnected |

The rule: never invent an event that merely renames a DAP one. There is no
`breakpoint_hit` event — that is `stopped` with `reason == "breakpoint"`.

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
| `cont()` | resumes everything, **blocks** until the next stop | resumes one thread, **returns immediately** |
| stepping | one thread steps, others stay parked | same |
| Prompt | reachable only when fully stopped | always reachable |
| Epoch bump | all threads | the resumed thread |

Non-stop is incompatible with a blocking `cont()`: if `cont()` blocks, you cannot
touch the other suspended threads while one runs, which is the entire point of
the mode. gdb resolves this the same way — in non-stop, `continue` returns to the
prompt immediately and stops are announced asynchronously.

This is what makes P1 non-negotiable. The commands themselves are not
synchronous in one of the two modes, so synchrony cannot be built into anything
below them.

### Wiring

Mode is `config.non_stop`, applied at attach:

- `multiThreadsSingleNotification = not non_stop`
- `steppingResumesAllThreads = False` — **always pinned**

The second flag needs pinning because it defaults to `True` and has no
`setDebuggerProperty` path (attach-time only). Left alone it produces a broken
hybrid: breakpoints stop one thread, but any step resumes everything — neither
mode. Pinning it to `False` costs nothing in all-stop, where every thread is
already suspended, so per-thread stepping is indistinguishable from all-thread
stepping.

That leaves `multiThreadsSingleNotification` as the single user-facing switch,
and it *is* toggleable mid-session via `setDebuggerProperty` — better than gdb,
which makes you decide before running.

### The one command-layer decision point

```
resume(thread_id, request) -> StopResult | Resumption
```

All-stop returns a resolved `StopResult`; non-stop returns a `Resumption` the
user can `.wait()` on later. `cont()`, `step()`, `next()`, `finish()`, `until()`
and the post-`configurationDone` initial stop all route through it. One place
knows about the mode.

The wait must match on `threadId`. In non-stop, another thread's breakpoint would
otherwise satisfy a `cont()` on this one, reporting the wrong location and
leaving the intended thread still running.

---

## 4. Concurrent callers and the thread control right

P0 means several callers may hold the command API at once. The cursor is not the
shared resource — that is context-local (§2). The genuinely shared resource is
**the run state of each inferior thread**, and the API partitions by how it
relates to that:

| Class | Operations | Requires |
|---|---|---|
| **state-changing** | `continue`, `step`/`next`/`finish`, `pause`, `goto` | the control right |
| **frame-scoped reads** | `stackTrace`, `scopes`, `variables`, `evaluate`, `exceptionInfo` | the thread stopped |
| **state-independent** | `threads`, `setBreakpoints`, `modules`, `loadedSources`, `disconnect` | nothing |

### Why a right is needed at all

Without one, two callers resuming the same thread produce **one** resumption —
pydevd resumes on the first `continue`, the second is a no-op, and both waiters
match the same `stopped`. Two commands, one run, success reported to both. A
silently swallowed command is the worst outcome available; the other races
degrade acceptably (a `step` against a running thread is a clean pydevd error, and
a concurrent `pause` correctly surfaces as `reason: "pause"` — the Ctrl+C path).

With the right, N continues become N serialized runs.

### Shape

- Acquired by every state-changing operation, held from the send **until the
  resulting stop**.
- **`pause` is exempt.** Measured (§8): pause is idempotent — a second pause on
  an already-suspended thread emits nothing at all — so it cannot corrupt another
  caller's armed waiter. The exemption is what keeps Ctrl+C working and is the
  escape hatch when one caller's long run is making another wait.
- Reads and state-independent operations never touch it.
- No timeout, per P4; released when the stop arrives or the connection dies. No
  deadlock is possible, because the holder is woken by the reader thread (§2,
  two delivery paths) — nothing the right blocks is needed to release it.

**Scope follows the mode**, which is a consequence rather than a special case:

- **non-stop** — a resume affects one thread, so the right is per-thread; and
  since `cont()` does not block there, it is held only across the send.
  Contention is negligible.
- **all-stop** — a resume affects everything, so the right is global and held
  across the entire run. Which is precisely what all-stop means.

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

---

## 5. Threads and locks

Four threads, and one thing that looks like a thread but isn't.

| Thread | Role |
|---|---|
| **main** | REPL: at the prompt, or inside one command |
| **reader** | recv → parse → reduce → wake waiters → enqueue public events |
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

One synthetic `disconnected`, produced at the Client layer, is the sole death
signal. Every registered waiter is woken by it (P4).

- `close()` is idempotent, and safe to call from any caller including the reader
  thread itself (it must not self-join).
- The deliberate-close flag is set **before** shutting the socket down, so the
  reader racing to declare death first still reports "closed locally" rather
  than misreporting a user's `disconnect()` as the peer vanishing.
- The DAP connection and any process we spawned share one lifetime: tearing down
  either tears down both.

**Interrupt is not a teardown path.** Ctrl+C during a blocked `cont()` sends
`pause`; pydevd suspends the thread and emits `stopped` with `reason: "pause"`,
which the already-armed waiter resolves on. The wait ends because what it was
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

- Client is async; `request()` is sugar over `send().wait()`
- One waiter registry keyed by predicate, covering responses and events alike
- `expect()` armed before the trigger is the only correct spelling of "do X then
  await Y"
- No timeouts above Transport
- Reducer on the reader thread; user code sees events only via subscription
  queues it drains on its own threads — no dispatch thread, no callback registry
- Per-thread `(stopped, epoch)` table; single `running` bool deleted
- Epoch-tagged handles, bumped at resume-send
- Both execution modes supported; `steppingResumesAllThreads` pinned false
- Cursor is context-local; every caller has the full API
- `interrupt()` is fire-and-forget; the armed waiter resolves on `stopped`
- Thread control right: implicit per state-changing command, explicitly holdable
  via `control()`, `pause` exempt
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
| `supportsSingleThreadExecutionRequests` | never advertised; the `singleThread` flag is ignored, granularity comes from the mode |
| `stackTrace` on a running thread | **succeeds** with a torn snapshot (observed depths 1, 12, 1 on the same thread) |
| `variables` on a running thread's frame | **succeeds, returns `[]`** — success-shaped garbage, no error |
| Second `pause` while running | one `stopped` event total for two in-flight pauses |
| `pause` on an already-suspended thread | emits **nothing** — fully idempotent |

Spec defaults worth remembering, since they are not uniform:
`StoppedEvent.allThreadsStopped` missing means *only* that thread stopped, but
`ContinueResponse.allThreadsContinued` missing means **all** threads resumed. A
generic "absent is false" helper gets the second one backwards.

## 9. Open

- Does `request()` raise on `success == False`, or return the failed response?
  (Concurrency makes silent attribution of errors worse, which argues for
  raising.)
- What `Resumption` looks like at the prompt in non-stop mode, and whether
  there is an explicit `wait(tid)` command alongside it.
- Reverse requests (adapter→client) currently have no handler; the correct
  behaviour is replying `success: false`, not crashing the reader.
- Whether `before_prompt` becomes a real hook or stays out of the event model.
- Stdin passthrough is still scoped to a blocking resume wait; it needs the
  foreground/background model and a single-owner terminal in Console. `doc/io_model.md`
  still describes the old mechanism.
- `cont(all=...)` has no meaning in all-stop, since pydevd discards the
  `threadId`. It must refuse rather than silently no-op.

### Blocking defects in `client.py`

Both prevent the mode wiring above from working at all:

- `attach(**arguments)` passes the kwargs dict into the `__restart` positional of
  `AttachRequestArguments`, so it ships as `{"restart": {...}}`. No attach
  argument can currently be sent — including `stopAllThreadsOnSuspend` and
  `steppingResumesAllThreads`.
- `set_debugger_property()` takes no parameters (every field hardcoded `None`)
  and returns `self.request(set_debugger_property)`, the method object —
  `NameError` on call. This is the mid-session mode switch.
