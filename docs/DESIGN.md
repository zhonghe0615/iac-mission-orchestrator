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

**Communication protocol: demo versus production.** The demo uses HTTP/JSON at its API boundary to simplify setup, inspection, and integration, with Pydantic schemas validating requests. The simulated Robot Integration Layer currently runs in-process rather than over a network. For production service-to-service communication between orchestration and the Robot Integration Layer, the proposed choice is gRPC with Protobuf: explicit versioned schemas and generated clients support integration across languages and services, while binary serialization and streaming support efficient communication. Browser-facing APIs can remain HTTP/JSON. This is a proposed production evolution, not an implemented transport; command IDs, event correlation, approval rules, deduplication, and unknown-outcome handling remain unchanged and must still be enforced by application code.

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

**Recovery contract.** Preserve completed work, evidence, plan versions, and every execution attempt so an operator can see what happened, why progress stopped, and which actions remain available. Distinguish confirmed failure from an unknown outcome: a timeout does not prove that a physical action never occurred. The prototype retries safe inspections and pauses uncertain repairs. Production recovery would query the integration layer using the original command ID, reconcile the actual result, and resume only eligible remaining tasks. Unresolved outcomes stay with an authorized operator; manual completion or replanning must record the reason and supporting evidence rather than overwrite history. Planner failure leaves the mission visibly unplanned; invalid output never authorizes execution.

**External-system failures (production proposal).** Persist pending integration operations with correlation IDs, retry counts, and a visible reason. Deduplicate incoming work orders by their source ID; retry outbound status updates with bounded backoff and a stable idempotency key where the destination supports it. If delivery is ambiguous and the destination cannot deduplicate, query or reconcile before resending. An unavailable approval service leaves dependent actions blocked until a valid decision is recorded. An unavailable evidence store leaves evidence-dependent actions and outcome confirmation blocked until required evidence is durably stored and referenced; retain uploads locally within a defined capacity limit, then pause evidence-producing work if that limit is reached. Status-reporting failures do not erase completed physical work or rerun the mission. Exhausted retries escalate with an owner and a recovery action. These external connectors are not implemented in the prototype.

**Security and independent physical safeguards (production proposal).** Treat work orders, retrieved documents, and robot/tool outputs as untrusted input: none may grant permissions or override execution policy. The prototype already restricts plans to catalog actions and arguments, enforces target and repair constraints, and gates repairs on approval. These controls do not establish protection against compromised identities, adapters, or cloud services.

Before physical deployment, add:
- **Identity and control boundaries:** authenticated users with site-scoped roles, authenticated service/device connections, least-privilege adapter credentials, and auditable control handoffs. The planner must have no direct robot credentials or unrestricted SDK access. Security-relevant policy and asset-map changes require authorized, versioned review.
- **Independent execution checks:** validate the resolved physical target, permitted operating area, action parameters, and resource/control ownership at the integration boundary. Reject stale, unauthorized, or out-of-policy commands even when the cloud sends them. Define command expiry and revocation behavior; do not equate a valid action name with a safe physical action.
- **Local safety authority:** agree with OEMs and field teams on enforceable geofences, collision/person protection, emergency stops, and loss-of-link behavior appropriate to each robot and site. Cloud commands and LLM output must not disable these protections. On uncertainty, use the locally defined safe response; for an aircraft, this may mean hold, return, or controlled landing rather than simply stopping. Human approval is an additional gate, not a substitute for these safeguards.
- **Validation before rollout:** test malicious instructions, forged/replayed commands, altered target mappings, expired credentials, cloud disconnects, and attempts to bypass local restrictions in simulation and vendor test environments, followed by controlled field trials. Verify both blocked commands and the resulting physical behavior with OEM/field specialists. Record residual risks and acceptance criteria before expanding deployment.

These are planned controls, not implemented or validated demo capabilities. Robots are simulated; the prototype's passing tests demonstrate orchestration rules, not end-to-end cybersecurity or physical safety assurance.

## 4. Build versus buy

**Key answer:** IAC's durable differentiation is the cross-fleet capability model, safe execution semantics, governance, and site onboarding. Queues, databases, workflow runtimes, model hosting, and robot motion are commodities.

- **Workflow engine:** build the mission semantics; consider Temporal underneath them later.
- **Agent framework:** none. One structured call with validation does not need one, and lock-in stays limited to the request format.
- **LLM:** rent it, keep the provider configurable, and keep the model advisory.
- **OEM fleet managers and drivers:** integrate, never rebuild. Vendors own motion and safety.

**Robot-ops platforms.** Evaluate existing platforms or partners for device monitoring, remote diagnostics, logs, and software deployment before building these capabilities. Start with the customer's existing tooling where it is adequate. IAC owns mission intent, cross-fleet task assignment, approval, and audit; OEM systems retain local execution and safety authority. If a platform also schedules work or offers remote control, define one control owner per robot and an explicit handoff. Operator takeovers must be visible to IAC and block conflicting dispatches. Select only the operational capabilities needed for the pilot rather than buying an overlapping orchestration stack.

**Customer facility systems.** Keep the customer's CMMS/EAM authoritative for asset records and work orders, and its identity provider authoritative for users and roles. IAC owns mission plans, attempts, execution decisions, and evidence references. Thin connectors map external asset/work-order IDs to IAC IDs, deduplicate intake, and write back outcomes and evidence without duplicating the customer's asset registry or ticket workflow. Agree on field ownership and completion rules: a resolved robot mission does not automatically close a maintenance ticket unless the customer explicitly defines that mapping. Connector outages follow the recovery rules in section 3.

**Selection method.** Run a time-boxed comparison using the same inspection mission and fault scenarios, including disconnects, duplicate delivery, restart, and unknown physical outcomes. Reject options that cannot preserve required approval and command semantics, expose execution state, or meet agreed data-access requirements. Compare the remaining options on the evidence below; choose the smallest option that meets pilot needs. These are proposed procurement criteria, not completed vendor evaluations.

| Criterion | Evidence used to decide |
| --- | --- |
| Cost | Twelve-month total cost at pilot and expected fleet volume: licenses, hosting, storage/egress, integration, support, and on-call effort. Compare with maintaining the same capability internally. |
| Integration effort | Working adapter spike; supported robot/API versions; time to first accepted mission; custom mappings, deployment prerequisites, and vendor support needed. |
| Reliability | Observed recovery under the fault scenarios; durable state and deduplication behavior; actionable diagnostics; support and outage responsibilities. |
| Control | Ability to enforce IAC policy, approval, cancellation, and reconciliation rules without bypass paths or competing dispatch authority. |
| Future lock-in | Exportable mission/evidence history, stable identifiers, documented interfaces, and a replacement exercise showing which adapters or workflows must change. Include contractual data access and migration cost. |

Keep vendor-specific APIs behind adapters and retain IAC-owned schemas and audit records, while recognizing that workflow-runtime migrations can still be substantial. The demo's custom state machine demonstrates mission semantics; it is not a commitment to rebuild a general-purpose durable workflow engine in production.

## 5. First 90 days

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

## 6. Team and customer execution

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

## 9. Evaluation criteria: implementation and evidence

All five evaluation signals are addressed within the prototype's scope. The latest local verification passed 78 backend tests; the frontend production build and browser walkthrough were also verified. These results establish software behavior with simulated robots, not production or physical-safety readiness. See [VERIFICATION.md](VERIFICATION.md) for supporting verification details.

| Evaluation signal | Implemented in the prototype | Evidence | Boundary and next deployment step |
| --- | --- | --- | --- |
| **End-to-end ownership and ability to ship** | Work order → real LLM plan → review → execution → approval/rejection → verification and recorded outcome; packaged UI served by one Python backend. | API end-to-end tests (`test_api.py`), mission tests (`test_missions.py`), and recorded live-LLM walkthrough covering recovery, approved repair, and rejection. | Physical execution is simulated. Next: validate one OEM adapter and a bounded customer-site pilot. |
| **Agent design with clear safety/validation boundaries** | Advisory planner; catalog-restricted plans; structural and semantic validation; enforced target/repair constraints; dispatch policy and single-use approval bound to plan version and action. | `test_plan.py`, `test_planner.py`, `test_constraints.py`, and approval-bypass tests in `test_missions.py`. | Other natural-language constraints are not deterministically enforced. Authentication, device trust, and independent local safety controls remain production work. |
| **Robust state and failure handling** | Separate mission/task/attempt state; persisted history; restart recovery; safe inspection retries/reassignment; uncertain repair escalation; duplicate/stale-event handling and heartbeat detection. | `test_reliability.py`, `test_idempotency.py`, `test_liveness.py`, and failure scenarios in `test_missions.py`. | One engine process; no full event-log replay or automatic physical-outcome reconciliation. Next: real adapter reconciliation and external-system recovery. |
| **Reusable platform abstractions** | Shared action/capability catalog, configurable robot types, command/event interface, and common scheduling, policy, approval, and audit behavior. | `test_fleet.py` verifies a new robot type through configuration and concurrent missions without robot double-booking. | Only the simulator implements the robot interface today; assets/actions remain Python definitions, and assets are not reserved. Next: a second adapter contract test and real-vendor integration. |
| **Product/UI clarity** | Operator console exposes plan source, editable plans, mission/task progress, attempts, evidence, approval decisions, fleet state, and a live audit timeline. | Browser walkthrough and recorded demo; frontend build verification. | No formal operator usability study; plan editing is JSON-based and mobile layout needs improvement. Next: UX-led field observation and usability testing of approval and exception workflows. |

The delivery tradeoff is a small, inspectable system with working execution rules rather than a production platform. Sections 3–6 describe the proposed security, infrastructure, integration, rollout, and team changes; those proposals are not counted as implemented features above.
