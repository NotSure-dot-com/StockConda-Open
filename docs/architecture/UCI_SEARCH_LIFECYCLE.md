---
document_type: design-guideline
status: accepted
title: UCI Search Lifecycle
decision: accepted-with-architecture-exception
baseline: 9acfd605d8a5edb99d98089a49304c06ed01f1fb
scope: uci-search-lifecycle
created: 2026-09-03
supersedes: null
---

# UCI Search Lifecycle

## Purpose

This document defines the architectural contract for asynchronous UCI search
in StockConda. It prevents blocked command processing, overlapping access to
shared search state, stale or duplicate `bestmove` output, cancellation races,
and premature release of native resources.

This is a durable design guideline. Work-package-specific baselines, sample
sizes, performance thresholds, and statistical decisions belong in the
relevant pre-test gate.

## Context

At baseline commit `9acfd60`, `UCIEngine` starts a persistent worker during
initialization, but the active UCI path never submits a search to it. Every
`go` instead creates a new `search_thread`. The unused worker, result queue,
and result monitor form a second incomplete asynchronous-search design.

Maintaining both paths creates lifecycle complexity without providing a
supported fallback. AP03 evaluates replacing them with one serialized,
task-aware worker.

## Decision

StockConda SHALL use exactly one persistent search worker per `UCIEngine`
instance. The worker SHALL be created during initialization and remain alive
until shutdown.

A `go` command SHALL create an immutable search-task envelope and submit it to
that worker. It SHALL NOT create another worker thread. Search tasks,
cancellation, completion, and result publication SHALL be associated with a
unique monotonically increasing task identifier.

Only one search may execute at a time. A replacement search SHALL NOT overlap
the search it replaces.

This decision remains provisional until the AP03 gate passes.

## Non-goals

This guideline does not authorize changes to:

- Principal Variation Search or quiescence-search semantics;
- evaluation, move ordering, LMR, history, or killer heuristics;
- transposition-table semantics;
- FEN transport or board-copy strategy;
- direct Python search APIs outside the UCI path;
- multi-worker or parallel tree search.

Those changes require separate work packages and independently declared
regression gates.

## Components and ownership

### UCI command thread

The UCI command thread owns standard-input processing, command parsing, the
authoritative `UCIEngine.board`, task creation, mutation barriers, and shutdown
initiation. It remains able to answer `isready` while the worker searches.

### Search coordinator

The coordinator owns task identifiers, the current task reference, lifecycle
state, cancellation barriers, and stale-result prevention. Coordinator state
is protected by one lock or an equivalent synchronization primitive.

### Search worker

The persistent worker executes one accepted task at a time, owns its root-board
snapshot, performs iterative deepening, retains the best move from the last
completed iteration, finalizes the task, and returns to the idle state.

The worker SHALL NOT read a mutable root position from `UCIEngine.board` after
accepting a task.

### Shared search resources

The transposition table, history table, killer table, and related state may be
used by the worker. The UCI thread SHALL NOT clear, resize, replace, or mutate
them until the active task has terminated. `isready` is exempt because it does
not mutate search state.

## Search-task contract

A task contains at least:

- `task_id`;
- immutable root-position data;
- depth limit, time limit, and infinite-search mode;
- a task-specific cancellation event;
- a completion event.

The task queue SHOULD have capacity one. It represents the next UCI search,
not an arbitrary backlog.

Cancellation events are never cleared and reused. A resettable global Boolean
is not sufficient to distinguish task generations.

## Lifecycle

```text
IDLE --go--> QUEUED --worker accepts--> SEARCHING
QUEUED or SEARCHING --cancel--> CANCELLING
CANCELLING or completed SEARCHING --> FINALIZING
FINALIZING --publish once and signal completion--> IDLE
IDLE or active state --quit/EOF--> SHUTTING_DOWN
```

Lifecycle state is represented authoritatively. Independent Boolean fields
such as `searching` and `worker_busy` SHALL NOT be allowed to disagree.

## Command semantics

### `go`

The engine parses the parameters, completes any active task, captures the root
position, allocates a task ID and cancellation event, registers the new task,
and enqueues it. The UCI thread then returns to command processing.

Queue insertion SHALL wake the worker directly; no deliberate polling delay is
introduced.

### `stop`

The engine sets the active task's cancellation event. The worker retains the
best move from the last fully completed iteration and emits exactly one
terminal `bestmove`. Calling `stop` while idle is safe and idempotent.

### Replacement `go`

The old task is cancelled and finalized before the replacement task is
accepted. Its required terminal output is completed first. Shared cancellation
state is never reset while the previous task could still observe it.

### `position`, `ucinewgame`, and `setoption`

These commands form a mutation barrier: cancel the active task, wait for
finalization, verify that mutable search state is no longer in use, then apply
the requested mutation.

### `isready`

`isready` responds with `readyok` without cancelling or awaiting an active
search. It is not an implicit completion barrier.

### `quit` and EOF

Shutdown prevents new tasks, cancels and finalizes the active task, sends one
shutdown sentinel, joins the persistent worker, releases the native
transposition table exactly once, and terminates. A join timeout is not proof
that native resources are unused. Repeated shutdown calls are idempotent.

## Result and output rules

Every accepted search produces exactly one terminal `bestmove`. A result may
be published only when its task ID and root position match the task being
finalized and no result was already published.

The move is validated against the captured root position. If no usable move is
available, the engine selects a legal fallback. It emits `bestmove 0000` only
when the root position has no legal move.

All UCI lines are written atomically through the output lock. Diagnostics use
valid `info string` lines and never replace mandatory protocol output.

## Cancellation inside search

PVS and quiescence search observe the active task's cancellation event.
Cancellation propagates without treating a partial iteration as completed.
Rate-limited checks are permitted only when their maximum stop latency is
covered by regression tests.

## Error handling

A search exception is contained by the worker, produces a UCI diagnostic,
finalizes the affected task, and returns one legal fallback or `bestmove 0000`.
It does not silently terminate the worker.

If native-state integrity cannot be guaranteed, the engine rejects further
searches and shuts down safely instead of continuing in an unknown state.

## Stable requirements

| ID | Requirement |
|---|---|
| `UCI-LC-01` | One persistent worker exists per engine instance. |
| `UCI-LC-02` | At most one search task is active. |
| `UCI-LC-03` | Cancellation is bound to one task generation. |
| `UCI-LC-04` | Every accepted search emits exactly one terminal `bestmove`. |
| `UCI-LC-05` | A stale task cannot publish a result for a newer task. |
| `UCI-LC-06` | State-mutating commands enforce a completion barrier. |
| `UCI-LC-07` | `isready` remains responsive during search. |
| `UCI-LC-08` | Native resources outlive every possible user. |
| `UCI-LC-09` | Shutdown and cleanup are idempotent. |

## Prohibited designs

- Creating a new search thread for each `go`.
- Reusing one resettable global stop flag across task generations.
- Allowing multiple functions to independently publish `bestmove`.
- Starting a result-monitor thread for every search.
- Using an unbounded search-task queue.
- Mutating the authoritative board while the worker reads it.
- Releasing or resizing the TT during active native access.
- Accepting a replacement before its predecessor is finalized.
- Combining lifecycle changes with chess-search optimizations.

## Required verification

Any implementation or later modification verifies all `UCI-LC` requirements,
deterministic fixed-depth behavior, UCI process behavior, external Fastchess
operation, performance, static types in the changed design units, and the
applicable Elo non-inferiority gate.

Exact commands and thresholds are declared before testing in the work-package
pre-test gate.

## Consequences

Positive consequences are explicit ownership, responsive UCI input, removal
of per-move thread creation, task-local cancellation, testable stale-result
handling, and one shutdown lifecycle.

Costs are a more explicit synchronization model, short blocking barriers for
mutating commands, stronger worker tests, and only a small expected absolute
latency improvement. No material Elo gain is assumed.

## Alternatives considered

**Continue creating one thread per `go`:** rejected as the target design. It
works at the baseline but preserves avoidable lifecycle churn and leaves the
persistent worker unused.

**Connect the existing queues unchanged:** rejected. They lack safe task
identity, task-local cancellation, stale-result suppression, and an integrated
exactly-once result contract.

**Combine worker conversion with FEN and search-loop optimization:** deferred
to preserve attribution and limit the AP03 regression surface.

**Use multiple workers:** rejected. Shared search state has no parallel-search
design.

## Verdict

**ADOPTED.** StockConda replaced the active per-`go` thread path with one
serialized, task-aware persistent worker. AP03 completed its correctness and
non-inferiority checks and was accepted for architectural value with the
recorded latency exception; the frozen latency HOLD is not relabeled PASS.

The implementation passed correctness and non-inferiority. It missed the
predeclared latency-benefit threshold, after which the architectural
simplification was explicitly accepted as sufficient value.

The accepted evidence and the explicit architecture exception are recorded in
the AP03 verification report.

## Related documents

- [AP03 pre-test gate](../work-packages/AP03/PRE_GATE.md)
- [AP03 verification report](../work-packages/AP03/RESULT.md)
- [Project structure](../STRUCTURE.md)
