# Mission Orchestrator: Technical Design Reference

The full design, with tables and measurements. [DESIGN.md](DESIGN.md) is the short version for review.

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

**Operator constraints.** The work order comes with two explicit intake fields: the target asset and whether repairs are allowed. They are form fields, not parsed from the text, so checking them is deterministic. When the text and the fields disagree, the fields win; the model is told the constraints and asked to say so in its summary. The fixture planner, which does not read the text, declines any order whose fields do not fit its single plan (T-101, repairs allowed).

**Plan validation** runs on every model output and every operator edit, and again at start: the operator's target and repair constraint; strict schema; known actions, arguments, and values; a single mission target; known dependencies; no cycles; conditions only on dependencies and only on findings that action can report; at least one inspection; every repair depends on an inspection, is conditioned on `defect_detected`, and is followed by `verify_repair`. A capability check rejects actions no robot in the fleet can perform. Validation errors go back to the model verbatim; after three failed outputs, planning fails visibly. A failed model call is never replaced by the fixture plan.

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

**Restart.** On startup, every attempt still open is marked `lost`. Retry-safe tasks are redispatched; others go to `needs_intervention`. Approval waits survive unchanged, because they are only rows. The simulated world also survives: tank state is saved to a file next to the database, so a tank repaired before a restart is still repaired when verification runs after it. Deleting the database resets both.

### Fleet coordination

Every robot belongs to a declared robot type, and the type defines its capabilities. The fleet is loaded from `config/fleet.json` at startup and rejected with every problem listed if a robot has a missing or unknown type, an ID is duplicated, or a type declares no capabilities. Each attempt records the robot type at dispatch time, so history keeps it even if the fleet file changes later.

Routing filters by capability (via the robot's type), then availability (online, healthy, unreserved), then preference rank. The reason for passing over robots is recorded and shown: per robot in small fleets, counted per type in large ones (for example "Inspection quadruped: 300 offline"). A reservation is held for the lifetime of an attempt. With no eligible robot, the task waits with an explicit reason and resumes when a robot becomes available. Every dispatch passes an explicit policy check (capability, availability, operator target, repairs allowed, approval) whose result is written to the timeline. A failed check cannot clear by itself, so it puts the task in `needs_intervention` with the failed rules named; validation should make this unreachable.

### Robot liveness

The engine learns about availability in two ways, and both lead to the same offline handling (open attempts become `lost`, retry-safe work is rerouted, a repair pauses for the operator):

- **Announced changes.** A robot or fleet manager reports going offline or online with a status event. The engine reacts within milliseconds: events are pushed, not polled.
- **Heartbeat lease.** The integration layer reports every live robot in one batch each second. Last-seen times are kept in memory, because heartbeats are too frequent to write to the database. A monitor marks a robot **unreachable** after the lease ("no heartbeat for 3s") and reachable again when heartbeats resume. It says unreachable, not offline, because the engine only knows the robot stopped reporting: it may still be moving or working, which is why its open command's outcome is treated as unknown. Only an announced change is shown as offline. This catches silent failures, such as power or network loss, that never produce an event, whether the robot was busy or idle. Without it, a busy robot would be noticed only at its command timeout (10–15 s), and an idle one only when a dispatch to it failed.

The lease is a tradeoff between detection speed and false alarms. A false alarm is safe but costly: an inspection is redone elsewhere, a repair pauses for the operator, and the robot returns automatically when heartbeats resume. The lease is configurable (`HEARTBEAT_LEASE_S`, minimum two heartbeat intervals). The demo uses 3 s; deployments on industrial Wi-Fi typically need 5–10 s, tuned per network and robot type. To avoid false alarms from our own pauses, the monitor restarts its clock if its loop was stalled longer than the lease, since heartbeats would have been delayed too. No false losses occurred in the 520- and 1,000-robot runs.

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
| Enterprise intake → orchestration | Work order: free text plus the target asset and whether repairs are allowed (both enforced), and optionally priority, requester, and external ticket reference. Orchestration returns a mission ID and status callbacks. |
| Planning service ↔ orchestration | Input: work order, bounded site context, capability catalog. Output: a plan document in the schema above. The planner is advisory and replaceable; validation lives in orchestration. |
| Orchestration ↔ governance | Approval requests keyed by mission, task, plan version, and action hash; decisions with actor and comment; the append-only audit log. |
| Orchestration ↔ Robot Integration Layer | Commands `{command_id, robot_id, action, args}` and events `{event_id, command_id, type, payload}`, plus status queries and cancellation. Orchestration never sends motion primitives. |
| Orchestration → operator interfaces | Read models for missions, tasks, attempts, evidence, robot availability, and a change stream. |

**Communication protocol: demo versus production.** The demo uses HTTP/JSON at its API boundary to simplify setup, inspection, and integration, with Pydantic schemas validating requests. The simulated Robot Integration Layer currently runs in-process rather than over a network. For production service-to-service communication between orchestration and the Robot Integration Layer, the proposed choice is gRPC with Protobuf: explicit versioned schemas and generated clients support integration across languages and services, while binary serialization and streaming support efficient communication. Browser-facing APIs can remain HTTP/JSON. This is a proposed production evolution, not an implemented transport; command IDs, event correlation, approval rules, deduplication, and unknown-outcome handling remain unchanged and must still be enforced by application code.

**Shared across robot types:** capability names, the action catalog (arguments, findings, risk, retry safety, timeouts), mission/task/attempt state, evidence and provenance, and events. **Robot- or OEM-specific:** how an action is executed, navigation and motion, local safety interlocks, sensor formats, and OEM fleet-manager scheduling. These sit behind the Robot Integration Layer adapter.

**Staying generic.** New robot brands add an adapter and declare capabilities. New sites add assets and context. New mission types add catalog actions and validation rules. The engine, routing, approval, and audit code do not change.

## 4. Reliability, safety, and governance toward production

- **Durability and throughput:** move from one process with SQLite to Postgres with the same transactional outbox, or to a workflow engine (see section 5). Replace the global lock with per-mission and per-robot locking (row locks or a reservation table with compare-and-set), run several engine workers sharing missions, and consume robot events from a message bus. Take asset state from the integration layer and site systems instead of the simulator's state file. Robots should register through the integration layer instead of a static file, and routing should consider location, battery, and load rather than a fixed preference rank.
- **Idempotent commands:** command IDs already identify attempts, and the simulated robot already deduplicates by them. A real integration layer must do the same, durably, so a resend cannot repeat a physical action. Idempotency keys should expire after a retention period; the prototype keeps them forever.
- **Unknown outcomes:** add a status query or reconciliation step before escalating; keep the rule that a non-repeatable action is never retried blindly.
- **Stale or duplicate events:** keep the inbox and attempt-state checks; add per-robot sequence numbers to detect gaps.
- **Liveness:** tune the heartbeat lease per robot type and network; subscribe to OEM fleet-manager status where available; after a lease expiry, query the robot's actual state when it returns before trusting it with new work.
- **Partial completion and intervention:** today an operator can retry or cancel. Production needs explicit "mark done manually" and "replan from here" actions, both audited.
- **Access control:** the hosted demo can require one shared password, but actor names are self-reported and there is no individual authentication or RBAC. Production needs SSO identities, role-based permissions (approver vs. operator vs. viewer), approval scoped by site and action risk, and possibly two-person approval for some actions.
- **Invalid or unsafe LLM plans:** already blocked by validation, with bounded correction and visible failure. Add red-team test suites for prompts, evaluation of plan quality over time, and policy rules that are versioned and reviewed like code.
- **Understandable failure:** every state carries a reason string, the timeline records why each decision was made, and the UI states what the mission is waiting for and what the operator can do.

**Recovery contract.** Preserve completed work, evidence, plan versions, and every execution attempt so an operator can see what happened, why progress stopped, and which actions remain available. Distinguish confirmed failure from an unknown outcome: a timeout does not prove that a physical action never occurred. The prototype retries safe inspections and pauses uncertain repairs. Production recovery would query the integration layer using the original command ID, reconcile the actual result, and resume only eligible remaining tasks. Unresolved outcomes stay with an authorized operator; manual completion or replanning must record the reason and supporting evidence rather than overwrite history. Planner failure leaves the mission visibly unplanned; invalid output never authorizes execution.

**External-system failures (production proposal).** Persist pending integration operations with correlation IDs, retry counts, and a visible reason. Deduplicate incoming work orders by their source ID; retry outbound status updates with bounded backoff and a stable idempotency key where the destination supports it. If delivery is ambiguous and the destination cannot deduplicate, query or reconcile before resending. An unavailable approval service leaves dependent actions blocked until a valid decision is recorded. An unavailable evidence store leaves evidence-dependent actions and outcome confirmation blocked until required evidence is durably stored and referenced; retain uploads locally within a defined capacity limit, then pause evidence-producing work if that limit is reached. Status-reporting failures do not erase completed physical work or rerun the mission. Exhausted retries escalate with an owner and a recovery action. These external connectors are not implemented in the prototype.

**Security and independent physical safeguards (production proposal).** Treat work orders, retrieved documents, and robot/tool outputs as untrusted input: none may grant permissions or override execution policy. The prototype already restricts plans to catalog actions and arguments, enforces target and repair constraints, and gates repairs on approval. These controls do not establish protection against compromised identities, adapters, or cloud services.

Before physical deployment, add:
- **Identity and control boundaries:** authenticated users with site-scoped roles, authenticated service/device connections, least-privilege adapter credentials, and auditable control handoffs. The planner must have no direct robot credentials or unrestricted SDK access. Security-relevant policy and asset-map changes require authorized, versioned review.
- **Independent execution checks:** validate the resolved physical target, permitted operating area, action parameters, and resource/control ownership at the integration boundary. Reject stale, unauthorized, or out-of-policy commands even when the cloud sends them. Define command expiry and revocation behavior; do not equate a valid action name with a safe physical action.
- **Local safety authority:** agree with OEMs and field teams on enforceable geofences, collision/person protection, emergency stops, and loss-of-link behavior appropriate to each robot and site. Cloud commands and LLM output must not disable these protections. On uncertainty, use the locally defined safe response; for an aircraft, this may mean hold, return, or controlled landing rather than simply stopping. Human approval is an additional gate, not a substitute for these safeguards.
- **Validation before rollout:** test malicious instructions, forged/replayed commands, altered target mappings, expired credentials, cloud disconnects, and attempts to bypass local restrictions in simulation and vendor test environments, followed by controlled field trials. Verify both blocked commands and the resulting physical behavior with OEM/field specialists. Record residual risks and acceptance criteria before expanding deployment.

These are planned controls, not implemented or validated demo capabilities. Robots are simulated; the prototype's passing tests demonstrate orchestration rules, not end-to-end cybersecurity or physical safety assurance.

## 5. Build versus buy

| Area | Choice | Reasoning |
| --- | --- | --- |
| Workflow durability | Custom state machine now; evaluate Temporal for production | The mission semantics (approval binding, retry safety, reconciliation) are the product and belong in our code. Temporal would provide durable timers, retries, and history and remove infrastructure work, but it costs an operations dependency and a programming model that is harder to inspect. LangGraph targets agent loops, not physical execution guarantees. |
| Agent framework | None; direct API calls | One structured call with validation does not need a framework. Lock-in is limited to the request format. |
| LLM | Rented | Commodity; keep the provider configurable and the planner advisory. |
| OEM fleet managers, robot drivers | Integrate, do not rebuild | Vendors own motion and safety. Orchestration should coordinate across them. |
| Robot-ops platforms | Reuse existing tooling or buy selected capabilities | Device monitoring, diagnostics, logs, and deployment; preserve one explicit control owner per robot. |
| Facility systems (CMMS, work-order tools) | Integrate through adapters | Preserve customer ownership of assets and work orders; IAC owns mission execution and evidence references. |

Evaluate options on integration effort, failure modes and observability, cost at expected volume, control over safety-relevant behavior, and exit cost. IAC's durable differentiation is the cross-fleet capability model, safe execution semantics, governance, and site onboarding; queues, databases, workflow runtimes, and model hosting are commodity.

**Robot-ops platforms.** Evaluate existing platforms or partners for device monitoring, remote diagnostics, logs, and software deployment before building these capabilities. Start with the customer's existing tooling where it is adequate. IAC owns mission intent, cross-fleet task assignment, approval, and audit; OEM systems retain local execution and safety authority. If a platform also schedules work or offers remote control, define one control owner per robot and an explicit handoff. Operator takeovers must be visible to IAC and block conflicting dispatches. Select only the operational capabilities needed for the pilot rather than buying an overlapping orchestration stack.

**Customer facility systems.** Keep the customer's CMMS/EAM authoritative for asset records and work orders, and its identity provider authoritative for users and roles. IAC owns mission plans, attempts, execution decisions, and evidence references. Thin connectors map external asset/work-order IDs to IAC IDs, deduplicate intake, and write back outcomes and evidence without duplicating the customer's asset registry or ticket workflow. Agree on field ownership and completion rules: a resolved robot mission does not automatically close a maintenance ticket unless the customer explicitly defines that mapping. Connector outages follow the recovery rules in section 4.

**Selection method.** Run a time-boxed comparison using the same inspection mission and fault scenarios, including disconnects, duplicate delivery, restart, and unknown physical outcomes. Reject options that cannot preserve required approval and command semantics, expose execution state, or meet agreed data-access requirements. Compare the remaining options on the evidence below; choose the smallest option that meets pilot needs. These are proposed procurement criteria, not completed vendor evaluations.

| Criterion | Evidence used to decide |
| --- | --- |
| Cost | Twelve-month total cost at pilot and expected fleet volume: licenses, hosting, storage/egress, integration, support, and on-call effort. Compare with maintaining the same capability internally. |
| Integration effort | Working adapter spike; supported robot/API versions; time to first accepted mission; custom mappings, deployment prerequisites, and vendor support needed. |
| Reliability | Observed recovery under the fault scenarios; durable state and deduplication behavior; actionable diagnostics; support and outage responsibilities. |
| Control | Ability to enforce IAC policy, approval, cancellation, and reconciliation rules without bypass paths or competing dispatch authority. |
| Future lock-in | Exportable mission/evidence history, stable identifiers, documented interfaces, and a replacement exercise showing which adapters or workflows must change. Include contractual data access and migration cost. |

Keep vendor-specific APIs behind adapters and retain IAC-owned schemas and audit records, while recognizing that workflow-runtime migrations can still be substantial. The demo's custom state machine demonstrates mission semantics; it is not a commitment to rebuild a general-purpose durable workflow engine in production.

## 6. First 90 days

The target fleet is four quadrupeds and five mobile manipulators. The following is a proposed field-delivery plan; the current prototype validates orchestration with simulated robots, not physical execution.

**Smallest useful product.** One operator can submit an inspection work order, review its plan, run it on one real robot type at one site, and receive evidence with either a clear result or an actionable escalation. Every command and intervention is auditable. Proving the same workflow on a second robot type is the next reuse milestone, not a prerequisite for the first customer pilot.

### Priorities and delivery gates

| Period | Deliver and validate | Defer or gate |
| --- | --- | --- |
| **Days 1–30** | Ship visual/thermal **inspect-and-escalate**: work order → reviewed plan → inspection → evidence → operator handoff. Establish the ECS/Aurora/Terraform cloud foundation below. Agree on the shared contract and tenant/site identity boundaries, establish the manual baseline, and connect one real robot type. Run simulation and shadow evaluation before a supervised inspection pilot. | Defer physical repair, welding, and unattended operation. If vendor or site access is delayed, deliver the tested adapter contract and simulation workflow; do not count that as field validation. |
| **Days 31–60** | Stabilize inspection and deliver tenant isolation and site-scoped RBAC as described below. Pilot **approval-gated material delivery** with pickup/drop-off confirmation, resource reservations, and reconciliation of unknown command outcomes. Add gas sensing only if sensor validation and delivery capacity permit. | Require authorization and isolation tests before multi-user field rollout, and site/integration readiness before material delivery. Keep ambiguous outcomes with an operator; do not automatically repeat a potentially completed physical action. |
| **Days 61–90** | Reuse the proven workflow on a second robot type/vendor, then a second site if the first remains stable. Turn repeated steps into mission templates and measure onboarding effort. Add the feedback analysis and product/engineering alert loop described below. | Welding remains deferred pending a separate safety review, qualified procedures, and vendor/field-team sign-off. Calendar dates alone do not authorize expansion. |

**Days 1–30 cloud infrastructure milestone.** Establish the production architecture early, with a small initial deployment rather than premature fleet-scale capacity:

- **AWS ECS:** containerize the API, planning, and execution components and define their deployment and scaling boundaries. Separate stateless request handling from stateful execution before adding API replicas; do not scale copies of the current combined application indiscriminately.
- **Aurora PostgreSQL:** establish the production persistence foundation, including the migration from SQLite, schema migrations, connection limits, backup/restore procedures, and tenant/site ownership. Validate database capacity and scaling settings against pilot load and cost limits.
- **Terraform:** manage networking, ECS services, Aurora, permissions, secrets infrastructure, and observability through versioned, reusable modules. Use separate environment/customer state and controlled plan/apply workflows to reproduce isolated deployments without manual configuration drift.
- **Delivery and operations:** set up CI/CD with rollback procedures, HTTPS, least-privilege service identities, managed secrets, logs, metrics, alerts, cost monitoring, and an explicit cloud-to-site connectivity boundary. Basic infrastructure access controls are required now, before the broader customer RBAC milestone.
- **Execution scaling gate:** keep the execution engine single-active initially and prevent overlapping owners during deployment or recovery. Design database-backed task claims, atomic resource reservations, durable command delivery, idempotency, and owner-fencing before enabling multiple execution workers. ECS and Aurora alone do not make robot dispatch safe to scale horizontally.

Acceptance requires a repeatable deployment, a tested database restore and service recovery, and observable failures in the pilot environment. Load-test stateless scaling after service separation; validate concurrent ownership and failover before enabling multi-worker execution. These are planned production deliverables; the demo remains a local, single-process SQLite application.

**Days 31–60 security milestone.** Start with a separate deployment and database per customer, with explicit tenant and site ownership for missions, assets, robots, evidence, audit records, and integration credentials. Integrate a trusted identity provider and enforce authorization on the backend for every API operation, event stream, evidence access, and robot dispatch. Roles cover viewer, operator, approver, maintenance, and tenant administrator; permissions are scoped to assigned sites, and approval remains bound to the specific plan and action. Service/device credentials are scoped to their tenant and site. IAC support access requires explicit, time-limited authorization and auditing. Acceptance tests must reject cross-tenant access, unauthorized cross-site access, role escalation, and unauthorized approval or robot control. This is planned work, not a current demo capability. Shared-infrastructure SaaS tenancy is deferred; if capacity is constrained, prioritize these controls over adding another workflow.

### Minimum shared model

Standardize these contracts with integration engineers in the first month. Existing code provides a starting point; asset reservations and versioned external contracts still need field implementation.

| Model | Minimum contract |
| --- | --- |
| **Mission** | ID, source work-order reference, target asset, constraints, plan version, lifecycle state, and business outcome. |
| **Task** | Mission-scoped ID, action and arguments, dependencies/conditions, required capabilities, approval policy, and state. Separate attempts hold robot assignment, command ID, timestamps, and result. |
| **Capability** | Stable identifier and versioned input/result contract. Robot types advertise capabilities; actions declare risk, timeout, and retry safety. |
| **Resource** | Robot, asset, or shared work-zone ID; availability; reservation owner; acquisition and release rules. An uncertain physical outcome must not silently release an occupied resource. |
| **Event** | Unique ID, source, type, schema version, occurrence/receipt times, correlation to mission/task/attempt/command, and payload. Define deduplication and stale-event handling. |

### Testing and operator feedback

**Simulation:** extend the existing success, rejection, fault, timeout, duplicate/out-of-order event, restart, and contention tests with vendor adapter contract tests and representative customer work orders. Check approval enforcement, preserved history, resource exclusivity, and no automatic repetition of an intervention with an unknown outcome. Simulation does not establish sensor accuracy or physical safety.

**Field stages:** shadow mode proposes plans without dispatching commands; operators compare them with approved procedures. Supervised runs then execute one bounded inspection workflow using established robot stop controls. Before expansion, the customer and field team review an agreed sample of runs for evidence completeness, inspection quality, recovery behavior, and intervention rate. Approval bypass, duplicate physical action, or unexpected physical behavior stops the affected pilot; ambiguous outcomes require reconciliation before resuming. Return affected work to the existing manual procedure when a gate fails.

**Feedback from day one:** capture plan edits, rejection reasons, manual takeovers, and unclear UI states. Review missions weekly with operators and integration engineers; assign each issue an owner and turn accepted changes into regression cases, catalog/template updates, or UI fixes. Compare results before and after changes. Feedback never changes execution policy automatically.

**Days 61–90 feedback analysis.** Record every approval rejection with a structured reason and optional operator comment, linked to the mission, action, site, and plan version. A rejection is often a correct safety decision, so it is not itself a negative sentiment signal. Pilot Jev (or a comparable typed classifier) to tag free-text feedback by issue type and explicit negative language; validate it against operator-reviewed examples, including neutral and safety-driven rejections, before using its output for routing. Keep uncertain classifications in a human review queue. Alert product and engineering immediately for explicit safety concerns or recurring execution failures, and send a periodic summary of rejection themes and feedback trends. Link each alert to the underlying evidence and an owner; review whether changes reduced the issue. Restrict feedback access by tenant/site and agree on data-sharing rules before sending customer comments to an external model. This is a proposed analytics workflow, not an implemented feature or an automated judgment of an operator's emotions.

### Dependencies and success measures

Integration engineers own adapter mappings, durable command deduplication, cancellation, and status reconciliation. Vendors supply API/sandbox access and documented command, sensor, and safety semantics. Field teams supply maps, operating boundaries, stop/recovery procedures, and supervised test time. Customers supply representative work orders, approvers, acceptance criteria, and the manual baseline. Missing status semantics blocks automatic reconciliation; missing site approval blocks physical trials.

Agree on numerical targets and sample sizes during days 1–30 using that baseline; do not invent field results from simulator runs. Track:

- **Mission success:** accepted business outcomes / started missions, reported separately by workflow. A correct no-defect result succeeds; a rejected repair is a valid control outcome but does not resolve the defect.
- **Recovery time:** fault detection to resumed execution, with unresolved cases and time to operator escalation reported separately.
- **Operator interventions:** unplanned takeovers per mission, separate from required approvals.
- **Task reuse:** share of task instances using existing action contracts/templates without customer-specific engine changes.
- **Onboarding time:** elapsed time and engineering effort from agreed API/site prerequisites to an accepted supervised run for a new robot type or template; report external waiting time separately.

These are proposed pilot measures. The prototype's audit-derived metrics are useful evidence, but are not yet the complete field scorecard.

## 7. Team and customer execution

**Initial team and ownership.**

1. **My ownership:** cloud infrastructure, backend architecture, AI planning and integration, and full-stack implementation. An additional full-stack engineering hire is not the first priority.
2. **First addition — robot integration engineer:** own OEM adapters, command idempotency, status semantics, and field integration debugging.
3. **Next — UX/product designer:** engage on a project or part-time basis to design the operator console, covering multi-mission situational awareness, evidence-led approvals, exception handling, and clear control handoffs. Validate prototypes with operators through workflow observation and usability tests; the role goes beyond visual styling.
4. **Before physical interventions go live — safety specialist:** engage someone with relevant robotics/site experience to review the deployment with OEM and field teams.

**Working with sales and solutions.**

1. **Joint discovery:** sales owns the business outcome and commercial context; solutions documents the current operating procedure, robot fleet, site constraints, and integration dependencies; engineering assesses feasibility, risk, and reuse potential. Capture these in a shared intake brief.
2. **Scope before commitment:** agree on a bounded pilot, measurable acceptance criteria, customer prerequisites, delivery ownership, and explicit exclusions before committing to dates. Separate supported capabilities from discovery work, and review scope changes against their cost and schedule impact.
3. **Classify implementation work:** map each request to existing configuration, mission templates, OEM/system adapters, or a proposed platform capability. Keep customer-specific identifiers and policies outside the orchestration core. New execution semantics require a design review, a versioned contract, and regression tests.
4. **Promote proven reuse:** validate a candidate abstraction against a second customer or site before making it a shared platform primitive. Until then, isolate it in the appropriate configuration, template, or adapter; do not force behavior that requires code into configuration. Assign an owner and document support and compatibility expectations.
5. **Close the feedback loop:** review pilot evidence and operator feedback regularly with sales and solutions. Prioritize improvements by customer value, operational risk, repeat demand, and ongoing support cost; feed the results into qualification guidance and the platform roadmap.

## 8. Known limitations and unhandled edge cases

- **Concurrent missions on the same asset are not prevented.** Two missions can each request a repair of T-101. Robots are reserved, but assets are not. Production needs asset-level locks or mission de-duplication.
- **Constraints are two fields.** Target asset and "repairs allowed" are enforced. Other instructions in the text (for example "thermal only") rely on the model; a plan that adds a visual inspection is still valid.
- **Simulated robot state is in memory.** Tank state survives a restart, but injected faults and offline flags do not.
- **No automatic reconciliation.** An unknown repair outcome always goes to the operator; there is no status query step.
- **No replanning.** Plans are frozen at start; changing course means cancelling and creating a new mission.
- **Single process.** The engine's lock is in memory; running two backend processes against one database is unsupported.
- **Static fleet.** Robots are read from the fleet file at startup; adding one requires a restart. Routing ignores location and battery.
- **No authentication.** Actor names are recorded for audit but not verified.
