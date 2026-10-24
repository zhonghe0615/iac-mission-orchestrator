# Mission Orchestrator: Technical Design

## 1. Architecture

The prototype is one FastAPI process with internal modules, a React operator console, and SQLite. Robots are simulated behind the same command/event interface a real integration would use.

```
Operator UI ──HTTP/SSE──▶ API ──▶ Planning harness ──▶ LLM (OpenAI-compatible)
                           │            │
                           │            └──▶ Plan validator ◀── operator edits
                           ▼
                     Execution engine ──▶ Policy check ──▶ Router ──▶ Robot port ──▶ Simulated fleet
                           ▲                                                            │
                           └────────────── robot events (inbox, dedupe) ◀───────────────┘
                           │
                       SQLite: missions, plan versions, tasks, attempts,
                               approvals, evidence, audit events
```

| Module | Responsibility |
| --- | --- |
| `catalog.py` | Site assets, action catalog (role, arguments, findings, risk, retry safety, timeouts), and the fleet model. One source of truth for the planner prompt, the validator, and the engine. |
| `config/fleet.json` | Robot types (each with its capabilities) and the robots of each type. |
| `planner.py` | Builds bounded context, calls the model, validates output, feeds errors back for at most two corrections, records every call. Also holds the labelled fixture planner. |
| `plan.py` | Schema and semantic validation for model and operator plans. |
| `engine.py` | Mission/task/attempt state machine, scheduling, routing, policy gate, approvals, timeouts, recovery. |
| `robots.py`, `simulator.py` | Typed command/event contract and the simulated fleet with explicit fault injection. |
| `store.py` | Transactional persistence and the audit log. |

**Where AI reasoning ends.** The model turns a free-text work order into a candidate plan: which inspections, in what order, under which conditions, and which repair procedure. It never picks a robot, never sees a command channel, and cannot express approval: the plan schema has no approval or robot fields, and unknown fields are rejected. Everything after validation is deterministic: dependency evaluation, condition checks, routing, policy, approval enforcement, timeouts, and retry decisions.

**Plan validation** runs on every model output and every operator edit: strict schema; known actions, arguments, and values; a single mission target; known dependencies; no cycles; conditions only on dependencies and only on findings that action can report; at least one inspection; every repair depends on an inspection, is conditioned on `defect_detected`, and is followed by `verify_repair`. A capability check rejects actions no robot in the fleet can perform. Validation errors go back to the model verbatim; after three failed outputs, planning fails visibly. A failed model call is never replaced by the fixture plan.

## 2. Execution model

### State and transitions

Mission, task, and attempt state are separate, so a failed attempt can be retried without erasing history.

| Entity | From | To | Trigger |
| --- | --- | --- | --- |
| Mission | planning | ready / planning_failed | Planner returns a valid plan / fails |
| Mission | ready, planning_failed | ready | Operator saves a valid edited plan (new version) |
| Mission | ready | running | Operator starts |
| Mission | running ↔ | waiting_approval, waiting_resource, needs_intervention | Derived from task states |
| Mission | active | resolved / unresolved / no_action_needed | All tasks terminal; outcome rules below |
| Mission | any non-terminal | cancelled | Operator cancels |
| Task | pending | skipped | A dependency was skipped/rejected, or the condition is not met |
| Task | pending | awaiting_approval | High-risk action without a matching unused approval |
| Task | awaiting_approval | pending / rejected | Operator approves / rejects |
| Task | pending | waiting_resource | No eligible robot (reason lists every robot passed over) |
| Task | pending, waiting_resource | running | Policy allows dispatch; attempt created; robot reserved |
| Task | running | succeeded | Current attempt reports completion |
| Task | running | pending | Attempt failed or timed out, action is retry-safe, budget remains |
| Task | running | failed | Retry budget exhausted, or a non-retry-safe action failed explicitly |
| Task | running | needs_intervention | Non-retry-safe action with unknown outcome (timeout, robot lost, restart) |
| Task | failed, needs_intervention | pending | Operator retry (high-risk actions need a new approval) |
| Attempt | running | succeeded / failed / timed_out / lost | Result, explicit failure, deadline, robot offline or restart |
| Attempt | running | cancel_requested → cancelled / cancel_unconfirmed | Mission cancel; robot confirms, or deadline passes |

Mission outcome: any rejected task → **unresolved**; any verification reporting `defect_persists` → **unresolved**; all verifications report `repair_verified` → **resolved**; a defect detected but nothing verified → **unresolved**; otherwise **no_action_needed**. Success requires verification, not just a repair report.

### Dependencies, retries, timeouts, cancellation

A task runs only when all dependencies succeeded and its condition holds. Each action declares a timeout and whether it is safe to repeat. Inspections are retry-safe with three attempts, and the router prefers a robot that has not already failed that task, which is how Scout A's failure moves work to Scout B. A repair is not retry-safe: an explicit failure escalates; an unknown outcome pauses for intervention and is never re-sent automatically. A timeout marks the robot `unresponsive` and removes it from routing until it reports again or an operator restores it, because a timeout does not prove the robot stopped. Cancellation keeps the robot reserved until the robot confirms.

### Approval

Approval is a property of the action in the catalog, not of the plan, so omitting it from a plan changes nothing. An approval records the plan version and a hash of the action and arguments. Dispatch requires an unused approval matching both, and consumes it, so one approval authorizes exactly one physical attempt. Plans are frozen once a mission starts; edits before start create a new version.

### Persistence and consistency

Each operator command and robot event is handled under one lock inside one SQLite transaction, together with its audit events. Commands to robots are queued in an outbox and sent only after commit, so a crash cannot leave a sent command without a record. If dispatch is refused, a follow-up transaction records the failure.

Robot events carry an event ID and the command (attempt) ID. The inbox drops duplicates. Events for an attempt that is no longer `running` are recorded as stale and change nothing, which covers late results after reassignment and out-of-order delivery.

### Idempotency

Every input that can arrive twice has a defined effect the second time:

| Input | Duplicate handling | Test |
| --- | --- | --- |
| Operator command (create, edit plan, start, approve/reject, retry, cancel) | Optional `Idempotency-Key` header, scoped to method and path. A repeat returns the stored response, errors included, marked `Idempotent-Replayed: true`. The same key with a different body gets 422; a duplicate arriving while the first is still running gets 409. The UI sends one key per action and reuses it when retrying after a dropped connection. | `test_idempotency.py` |
| Robot command | The command ID is the attempt ID. The simulated robot never executes a command ID twice and re-sends the original result, as the Robot Integration Layer contract requires. | `test_robot_executes_a_repeated_command_once` |
| Robot event | Dropped by event ID in the inbox. | `test_duplicate_completion_does_not_repeat_transitions` |
| Late or reordered robot event | Ignored when its attempt is no longer running. | `test_late_result_from_timed_out_attempt_is_ignored` |
| Restart recovery, schema setup | Running them again changes nothing. | restart tests |

Domain rules back this up: an approval is used by exactly one dispatch, and approving a task that is not awaiting approval is refused.

### Concurrency and thread safety

- **One writer path.** All state changes run on the engine's event loop under one lock, inside one SQLite transaction. The engine rejects calls from another thread or event loop instead of racing, because an asyncio lock only serializes callers on its own loop.
- **Thread-safe store.** The shared SQLite connection is guarded by a re-entrant lock held for the whole transaction. Read models (mission detail, robot list) are read inside one transaction, so a reader on any thread gets a consistent snapshot, never a mission state mixed with task states from another moment.
- **One process per database.** The database is opened in SQLite's exclusive locking mode, so a second server process (for example `uvicorn --workers 2`) fails at startup with a clear message instead of double-booking robots. The operating system releases the lock when the process exits, including after a crash.

Tests run six reader threads against a live 35-robot, 15-mission run. They also check that the engine refuses a foreign thread and that a second process is refused. Each protection was disabled in turn to confirm its test fails. The torn-read check is timing-dependent: without snapshot reads it failed in 2 of 3 runs.

**Restart.** On startup, every attempt still open is marked `lost`. Retry-safe tasks are redispatched; others go to `needs_intervention`. Approval waits survive unchanged, because they are only rows.

### Fleet coordination

Every robot belongs to a declared robot type, and the type defines its capabilities. The fleet is loaded from `config/fleet.json` at startup and rejected with every problem listed if a robot has a missing or unknown type, an ID is duplicated, or a type declares no capabilities. Each attempt records the robot type at dispatch time, so history keeps it even if the fleet file changes later.

Routing filters by capability (via the robot's type), then availability (online, healthy, unreserved), then preference rank. The reason for passing over robots is recorded and shown: per robot in small fleets, counted per type in large ones (for example "Inspection quadruped: 300 offline"). A reservation is held for the lifetime of an attempt. With no eligible robot, the task waits with an explicit reason and resumes when a robot becomes available. Every dispatch passes an explicit policy check (capability, availability, known target, approval) whose result is written to the timeline.

### Robot liveness

The engine learns about availability in two ways, and both lead to the same offline handling (open attempts become `lost`, retry-safe work is rerouted, a repair pauses for the operator):

- **Announced changes.** A robot or fleet manager reports going offline or online with a status event. The engine reacts within milliseconds: events are pushed, not polled.
- **Heartbeat lease.** The integration layer reports every live robot in one batch each second. Last-seen times are kept in memory, because heartbeats are too frequent to write to the database. A monitor marks a robot offline after 3 s without one ("no heartbeat for 3s") and back online when heartbeats resume. This catches silent failures, such as power or network loss, that never produce an event, whether the robot was busy or idle. Without it, a busy robot would be noticed only at its command timeout (10–15 s), and an idle one only when a dispatch to it failed.

To avoid false alarms, the monitor restarts its clock if its own loop was stalled longer than the lease, since heartbeats would have been delayed too. No false losses occurred in the 520- and 1,000-robot runs.

### Scaling the fleet

Capability-based routing means plans, the engine, and the UI never name a robot. Two kinds of growth are supported without code changes:

- **More robots of known types:** add entries to `config/fleet.json`.
- **New robot types:** declare the type and its capabilities. A test adds a "thermal drone" type known to no code, and it takes over inspection and verification work.

Actions declare a **role** (inspection, intervention, verification) and which findings mean "defect" or "verified". The validator's safety rules and the mission outcome rules read these roles instead of action names, so new mission types are catalog entries plus simulator or adapter behavior.

Design choices for scale:

- The planner receives robot types and counts, not individual robots, so the prompt does not grow with the fleet.
- Routing asks the database only for available robots of capable types.
- When a robot becomes free, only missions waiting for a robot are re-evaluated.

**Measured** (MacBook, Python 3.12, SQLite in memory): 520 robots (500 quadrupeds, 20 manipulators) with 40 concurrent missions run to completion with no robot double-booked. The engine spent about 0.15 s handling about 480 operations, about 0.3 ms each. The test suite checks the no-double-booking property at that size.

The ceiling is the single process with one lock and SQLite: every event is handled one at a time. Production scaling is described in section 4.

## 3. Fit into the wider system

| Boundary | Contract |
| --- | --- |
| Enterprise intake → orchestration | Work order: free text plus optional asset ID, priority, requester, and external ticket reference. Orchestration returns a mission ID and status callbacks. |
| Planning service ↔ orchestration | Input: work order, bounded site context, capability catalog. Output: a plan document in the schema above. The planner is advisory and replaceable; validation lives in orchestration. |
| Orchestration ↔ governance | Approval requests keyed by mission, task, plan version, and action hash; decisions with actor and comment; the append-only audit log. |
| Orchestration ↔ Robot Integration Layer | Commands `{command_id, robot_id, action, args}` and events `{event_id, command_id, type, payload}`, plus status queries and cancellation. Orchestration never sends motion primitives. |
| Orchestration → operator interfaces | Read models for missions, tasks, attempts, evidence, robot availability, and a change stream. |

**Shared across robot types:** capability names, the action catalog (arguments, findings, risk, retry safety, timeouts), mission/task/attempt state, evidence and provenance, and events. **Robot- or OEM-specific:** how an action is executed, navigation and motion, local safety interlocks, sensor formats, and OEM fleet-manager scheduling. These sit behind the Robot Integration Layer adapter.

**Staying generic.** New robot brands add an adapter and declare capabilities. New sites add assets and context. New mission types add catalog actions and validation rules. The engine, routing, approval, and audit code do not change.

## 4. Reliability, safety, and governance toward production

- **Durability and throughput:** move from one process with SQLite to Postgres with the same transactional outbox, or to a workflow engine (see section 5). Replace the global lock with per-mission and per-robot locking (row locks or a reservation table with compare-and-set), run several engine workers sharing missions, and consume robot events from a message bus. Persist robot and asset state from the integration layer instead of the simulator's memory. Robots should register through the integration layer instead of a static file, and routing should consider location, battery, and load rather than a fixed preference rank.
- **Idempotent commands:** command IDs already identify attempts, and the simulated robot already deduplicates by them. A real integration layer must do the same, durably, so a resend cannot repeat a physical action. Idempotency keys should expire after a retention period; the prototype keeps them forever.
- **Unknown outcomes:** add a status query or reconciliation step before escalating; keep the rule that a non-repeatable action is never retried blindly.
- **Stale or duplicate events:** keep the inbox and attempt-state checks; add per-robot sequence numbers to detect gaps.
- **Liveness:** tune the heartbeat lease per robot type and network; subscribe to OEM fleet-manager status where available; after a lease expiry, query the robot's actual state when it returns before trusting it with new work.
- **Partial completion and intervention:** today an operator can retry or cancel. Production needs explicit "mark done manually" and "replan from here" actions, both audited.
- **Access control:** the prototype has no authentication; the actor name is self-reported. Production needs SSO identities, role-based permissions (approver vs. operator vs. viewer), approval scoped by site and action risk, and possibly two-person approval for some actions.
- **Invalid or unsafe LLM plans:** already blocked by validation, with bounded correction and visible failure. Add red-team test suites for prompts, evaluation of plan quality over time, and policy rules that are versioned and reviewed like code.
- **Understandable failure:** every state carries a reason string, the timeline records why each decision was made, and the UI states what the mission is waiting for and what the operator can do.

## 5. Build versus buy

| Area | Choice | Reasoning |
| --- | --- | --- |
| Workflow durability | Custom state machine now; evaluate Temporal for production | The mission semantics (approval binding, retry safety, reconciliation) are the product and belong in our code. Temporal would provide durable timers, retries, and history and remove infrastructure work, but it costs an operations dependency and a programming model that is harder to inspect. LangGraph targets agent loops, not physical execution guarantees. |
| Agent framework | None; direct API calls | One structured call with validation does not need a framework. Lock-in is limited to the request format. |
| LLM | Rented | Commodity; keep the provider configurable and the planner advisory. |
| OEM fleet managers, robot drivers | Integrate, do not rebuild | Vendors own motion and safety. Orchestration should coordinate across them. |
| Facility systems (CMMS, work-order tools) | Integrate through adapters | Customer-specific; build thin connectors. |

Evaluate options on integration effort, failure modes and observability, cost at expected volume, control over safety-relevant behavior, and exit cost. IAC's durable differentiation is the cross-fleet capability model, safe execution semantics, governance, and site onboarding; queues, databases, workflow runtimes, and model hosting are commodity.

## 6. First 90 days

Scope: four quadrupeds and five mobile manipulators; gas detection, visual/thermal inspection, material handling, and welding; approvals, exception handling, and audit logs.

- **Days 1–30:** ship visual/thermal inspection with evidence capture end to end on one site in simulation, then on one real robot class. Standardize the mission, task, capability, resource, and event model and the command/event contract with integration engineers. Defer welding: highest risk, most site-specific.
- **Days 31–60:** add gas detection, reusing inspection primitives, and approval-gated material handling. Add durable execution, reconciliation for unknown outcomes, and role-based approvals. Staged rollout: simulation, shadow mode next to manual procedures, then supervised runs with field teams.
- **Days 61–90:** second site and a second robot vendor to prove the model stays generic; mission templates; operator feedback loop on the console; begin a welding pilot behind two-person approval.

Dependencies: robot integration engineers for adapters and command idempotency; field teams for site maps and safety rules; customers for work-order integration and approval policy; vendors for API access and status semantics.

Success metrics: mission success rate, mean time to recover from a failed attempt, operator interventions per mission, share of tasks reused across mission types, and onboarding time for a new robot type or mission template. The prototype already computes several of these from its audit data.

The smallest useful product: one repeatable inspection-and-escalation workflow running reliably across two robot types, with complete audit records.

## 7. Team and customer execution

First additions: a robot integration engineer (adapters, command idempotency, status semantics) and a product-minded full-stack engineer for the operator console; a contractor for safety and compliance review before any physical repair action. With sales and solutions teams: a shared intake template that maps customer requests to catalog actions. Requests that do not fit become catalog or capability proposals with an owner, not one-off code paths. A request becomes a platform primitive only once it generalizes across at least two customers or sites; until then it lives in site configuration.

## 8. Known limitations and unhandled edge cases

- **Concurrent missions on the same asset are not prevented.** Two missions can each request a repair of T-101. Robots are reserved, but assets are not. Production needs asset-level locks or mission de-duplication.
- **Simulator state is in memory.** After a backend restart, the simulated tank is reset, so a mission restarted between repair and verification would report `defect_persists`. A real integration layer would report actual asset state.
- **No automatic reconciliation.** An unknown repair outcome always goes to the operator; there is no status query step.
- **No replanning.** Plans are frozen at start; changing course means cancelling and creating a new mission.
- **Single process.** The engine's lock is in memory; running two backend processes against one database is unsupported.
- **Static fleet.** Robots are read from the fleet file at startup; adding one requires a restart. Routing ignores location and battery.
- **No authentication.** Actor names are recorded for audit but not verified.
