# Mission Orchestrator: Technical Design

This is the short version for review. [DESIGN_REFERENCE.md](DESIGN_REFERENCE.md) has the full state transition table, idempotency and concurrency details, liveness tuning, and scaling measurements. Sections 1–6 follow the six topics of the assessment brief.

## 1. System design

```
Operator UI ──HTTP/SSE──▶ API ──▶ Planning harness ──▶ LLM (OpenAI-compatible)
                           │            └──▶ Plan validator ◀── operator edits
                           ▼
                     Execution engine ──▶ Policy check ──▶ Router ──▶ Robot port ──▶ Simulated fleet
                           ▲                                                            │
                           └──────────── robot events (inbox, dedupe) ◀─────────────────┘
                       SQLite: missions, plan versions, tasks, attempts, approvals, evidence, audit
```

One FastAPI process with internal modules, a React console, and SQLite. Robots are simulated behind the command/event interface a real Robot Integration Layer would expose.

**Where AI reasoning ends.** The model turns free text into a candidate plan: which inspections, in what order, under which conditions, and which repair procedure. Everything after that is deterministic code: validation, dependency and condition evaluation, robot routing, policy, approval, timeouts, and retries. The plan schema has no robot or approval fields and rejects unknown fields, so the model cannot choose a robot, issue a command, or approve anything.

**Agentic planning.** The planner sends a bounded context: assets, robot types with capabilities and counts (not individual robots), and the action catalog with arguments, findings, and risk. The operator also sets two intake fields, the target asset and whether repairs are allowed. These are enforced, not inferred from the text. Every plan, whether from the model or edited by the operator, passes the same validator, and the validator runs again at start:
- the plan targets the operator's asset and contains no repair unless repairs are allowed;
- it uses known actions and argument values, with no cycles;
- conditions depend only on findings their dependency actually reports;
- a repair must follow an inspection, be conditioned on `defect_detected`, and be followed by verification.

Validation errors go back to the model for at most two corrections; then planning fails visibly. A failed model call is never replaced with the fixture plan.

**Workflow execution.** Mission, task, and attempt state are separate, so a retry keeps the history of the failed attempt. A task runs when its dependencies have succeeded and its condition holds. Each action declares a timeout, a retry budget, and whether it is safe to repeat:
- **Inspections** are retried and rerouted, preferring a robot that has not failed the task. This is how Scout A's failure moves to Scout B.
- **A repair** is never re-sent automatically. An explicit failure escalates; an unknown outcome (timeout, lost robot, restart) pauses for the operator.

Each command and event is handled in one SQLite transaction together with its audit events. Robot commands leave through an outbox after commit. On restart, open attempts are marked `lost` and handled by the same rules, and approval waits resume unchanged.

**Fleet coordination.** Each robot has a declared type, and the type defines its capabilities. Routing filters by capability, then availability, then preference. A robot is reserved for the whole life of an attempt, so it is never double-booked. With no eligible robot, the task waits and its reason names each robot passed over. Every dispatch passes a policy check, written to the timeline, covering capability, availability, operator target, repairs allowed, and approval. Robots are tracked in two ways:
- **Announced status events.** Shown as "offline".
- **A one-second heartbeat with a 3 s lease** (configurable). A robot that stops reporting is shown as "unreachable", because it may still be acting.

**Human in the loop.** Approval belongs to the action in the catalog, not to the plan. An approval records the plan version and a hash of the action and arguments. One approval authorizes exactly one dispatch. Plans are versioned and frozen at start. The operator can approve, reject, retry, restore a robot, or cancel. Every decision is recorded with actor, reason, and time.

**Platform boundary.** Orchestration sends `{command_id, robot_id, action, args}` and receives `{event_id, command_id, type, payload}`. Motion, navigation, local safety interlocks, and OEM scheduling stay below the Robot Integration Layer.

**Workflow tool tradeoff.** I chose a small custom state machine over Temporal or LangGraph. The mission semantics (approval binding, retry safety, unknown outcomes) are the product and need to be visible in our code. Temporal would add durable timers and history at the cost of an operations dependency and a harder-to-inspect programming model; it is worth evaluating for production. LangGraph targets agent loops, not physical execution guarantees.

## 2. Fit into the bigger picture

| Boundary | Contract |
| --- | --- |
| Enterprise intake → orchestration | Work order text, target asset, repair permission; later priority, requester, ticket reference. Returns a mission ID and status stream. |
| Planning ↔ orchestration | Work order plus bounded context in; plan document out. The planner is advisory and replaceable; validation lives in orchestration. |
| Orchestration ↔ governance | Approval requests keyed by plan version and action hash; decisions with actor and comment; append-only audit log. |
| Orchestration ↔ Robot Integration Layer | Typed commands and events, status, heartbeats, cancellation. No motion primitives. |
| Orchestration → operator UI | Read models for missions, tasks, attempts, evidence, robots; a change stream. |

**Shared** across robot types: capability names, the action catalog, mission/task/attempt state, evidence, and events. **Robot- or OEM-specific:** how an action is executed, sensor formats, local safety, and fleet-manager scheduling.

**Staying generic.** A new robot brand adds an adapter and declares capabilities. A new site adds assets. A new mission type adds catalog actions with a role (inspection, intervention, verification). The validator's safety rules and the outcome rules read roles, not action names, so the engine, routing, approval, and audit code do not change. In tests, a robot type unknown to any code took over inspection work through configuration alone, and the engine ran 40 concurrent missions on 520 robots with no double-booking.

## 3. Reliability, safety, and agent governance

**Key answer:** a mission stays understandable because every state carries a reason, and every decision (plan validation, routing, policy, approval) is an audit event shown in the timeline. It stays recoverable because outcomes are classified, not guessed: work that is safe to repeat is retried, and work with an unknown outcome is handed to a person.

| Concern | Prototype | Production path |
| --- | --- | --- |
| Durability | SQLite transactions, outbox, restart recovery | Postgres with the same outbox, or a workflow engine; per-mission locks instead of one process lock |
| Idempotent commands | Idempotency keys on operator commands; robot deduplicates by command ID | Durable dedupe in the integration layer; key expiry |
| Timeouts, unknown outcomes | Retry-safe work rerouted; repair pauses for the operator | Status query and reconciliation before escalating |
| Stale or duplicate events | Inbox by event ID; events for closed attempts ignored | Per-robot sequence numbers to detect gaps |
| Robot or network loss | Heartbeat lease; unreachable versus offline | Lease tuned per network (5–10 s on industrial Wi-Fi); state query on return |
| Partial completion, intervention | Retry or cancel with a reason | "Mark done manually" and "replan from here", both audited |
| Access control | None; actor names are self-reported | SSO, roles (operator, approver, viewer), site-scoped approval, two-person approval for welding |
| Invalid or unsafe LLM plans | Strict validation, enforced intake constraints, bounded correction, visible failure | Red-team prompt suites, plan-quality evaluation, versioned policy rules |

## 4. Build versus buy

**Key answer:** IAC's durable differentiation is the cross-fleet capability model, safe execution semantics, governance, and site onboarding. Queues, databases, workflow runtimes, model hosting, and robot motion are commodities.

- **Workflow engine:** build the mission semantics; consider Temporal underneath them later.
- **Agent framework:** none. One structured call with validation does not need one, and lock-in stays limited to the request format.
- **LLM:** rent it, keep the provider configurable, and keep the model advisory.
- **OEM fleet managers and drivers:** integrate, never rebuild. Vendors own motion and safety.
- **Facility systems (CMMS, ticketing):** thin adapters per customer.

Options are judged on integration effort, failure modes, cost at expected volume, control over safety-relevant behavior, and exit cost.

## 5. First 90 days

Scope: four quadrupeds and five mobile manipulators; gas detection, visual and thermal inspection, material handling, and welding.

- **Days 1–30:** ship visual/thermal inspection with evidence end to end, first in simulation and then on one real robot class. Agree on the shared model (mission, task, capability, resource, event) and the command/event contract with integration engineers. Defer welding, the highest-risk and most site-specific workflow.
- **Days 31–60:** add gas detection, which reuses inspection primitives, and approval-gated material handling. Add reconciliation for unknown outcomes and role-based approvals. Roll out from simulation to shadow mode next to manual procedures, then to supervised runs with field teams.
- **Days 61–90:** add a second site and a second robot vendor to prove the model stays generic. Add mission templates and an operator feedback loop. Start a welding pilot behind two-person approval.

**Dependencies:**
- integration engineers for adapters and command idempotency;
- field teams for maps and safety rules;
- customers for work-order integration and approval policy;
- vendors for API access and status semantics.

**Metrics:** mission success rate, mean time to recover, operator interventions per mission, task reuse across mission types, and onboarding time for a new robot type or template. The prototype already computes several of these from its audit data.

**Key answer:** the smallest useful product is one repeatable inspect-and-escalate workflow that runs reliably across two robot types, with complete audit records.

## 6. Team and customer execution

**First hires:**
- a robot integration engineer, for adapters, command idempotency, and status semantics;
- a product-minded full-stack engineer, for the operator console;
- a safety and compliance contractor, to review before any physical repair goes live.

**Working with sales and solutions:** use a shared intake template that maps customer requests onto catalog actions. A request that does not fit becomes a catalog or capability proposal with an owner, not a one-off code path. It becomes a platform primitive once it generalizes across two customers or sites; until then it lives in site configuration.

## 7. Implemented versus simulated

- **Implemented:**
  - LLM planning and validation;
  - the state machine, routing, and policy;
  - approvals;
  - persistence and recovery;
  - heartbeats;
  - metrics;
  - the operator UI.
- **Simulated:**
  - robot execution and findings;
  - the tank and its defect, saved next to the database so they survive a restart;
  - faults, injected from the UI.

## 8. Known limitations

- **Assets are not reserved.** Two missions can repair the same tank at once; production needs asset-level locks.
- **Only two constraints are enforced.** Target and repair permission are enforced; other instructions in the text (for example "thermal only") rely on the model.
- **No automatic reconciliation or replanning.** An unknown repair outcome always goes to a person, and plans are frozen at start.
- **One process with a static fleet.** There is a single engine process per database, and adding a robot needs a restart. Routing ignores location and battery.
- **No authentication.**
