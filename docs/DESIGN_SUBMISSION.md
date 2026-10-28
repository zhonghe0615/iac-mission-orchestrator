# IAC Mission Orchestrator: Technical Design

This is the short submission document. [DESIGN.md](DESIGN.md) retains the full design discussion and evaluation-criteria table; [DESIGN_REFERENCE.md](DESIGN_REFERENCE.md) contains transition tables and implementation details. The prototype uses real LLM planning and deterministic orchestration with simulated robots; production changes below are proposals, not implemented claims.

## 1. Orchestration architecture and AI-assisted implementation

```text
Work order + explicit target/repair constraints
    → Planner (LLM or labelled fixture) → validated, reviewable plan
    → Execution engine → policy + approval gate → capability router
    → RobotPort → simulated fleet → correlated events and evidence
    → SQLite mission/task/attempt history + audit → operator console
```

The planner receives bounded site context: assets, robot types and capability counts, and an action catalog with arguments, findings, and risk. It proposes actions, dependencies, and conditions. The plan schema cannot specify a robot, grant approval, or express arbitrary motion commands. Model output and operator edits pass the same structural and semantic checks: known actions and arguments, valid dependencies, supported findings, target consistency, repair permission, inspection before conditional repair, and verification after repair. Invalid output gets at most two correction attempts before visible failure; a failed LLM call never silently falls back to the fixture. **AI reasoning ends at the candidate plan.** Validation, routing, policy, approval, retries, and outcome classification are deterministic.

Mission, task, and attempt have separate states. A failed attempt remains in history when an inspection is retried on another robot. The engine advances tasks only when dependencies and conditions hold. Robot reservations prevent double booking; an unavailable fleet leaves a task waiting with a reason. Each dispatch records policy checks for capability, availability, target, repair permission, and approval. Approval is tied to one action, its arguments, and the current plan version; it authorizes one dispatch. Operators can reject, retry, cancel, or restore a robot; cancellation does not immediately free the robot for new work. Rejection closes the intervention as unresolved without a repair command. A successful simulated repair still requires verification before the mission resolves.

The execution engine persists transitions, evidence, and audit events in SQLite. Commands leave through an outbox after state commits; incoming events are deduplicated and correlated to attempts. Retry-safe inspections can be reassigned after failure or timeout. A repair is never repeated automatically when its outcome is unknown. Restart recovery keeps approval waits, reissues safe work, and escalates uncertain intervention outcomes. The single-process prototype has tests for success, rejection, failure, restart, stale events, concurrency, and policy bypass. The React console shows the plan, fleet, mission/task progress, attempts, evidence, approvals, and a live timeline. A small custom state machine keeps these execution rules inspectable; Temporal is a production option for generic durable workflow mechanics, not a replacement for IAC's mission semantics.

## 2. Fit into the wider IAC system

| Boundary | Shared contract and responsibility |
| --- | --- |
| Enterprise systems → IAC | Work-order text, target asset, repair permission, and source ID; IAC returns mission status, result, and evidence references. The customer CMMS/EAM remains authoritative for assets and tickets. |
| Planner ↔ orchestration | Bounded site/action context in; advisory plan out. The orchestrator validates and owns execution. |
| Governance ↔ orchestration | Policy outcomes, approval requests and decisions tied to plan/action, trusted actor identity in production, and immutable audit history. |
| Orchestration ↔ Robot Integration Layer | Command ID, robot, action and arguments; correlated events, status, heartbeat, cancellation, and evidence. No low-level motion primitives. |
| Orchestration → operator interfaces | Read models and a change stream; the backend remains authoritative for state and permissions. |

The core shares capability names, action contracts, mission/task/attempt states, resource ownership, evidence references, and event semantics across fleets. OEM adapters translate approved commands and results; navigation, motion, sensor formats, and local safety stay with the robot or vendor. New robot types can declare existing capabilities in fleet configuration. New sites need asset mappings and policy configuration; new mission types first compose catalog actions. A genuinely new action or coordination rule requires reviewed code and tests.

The demo deliberately uses Python models, Pydantic validation, JSON-facing APIs, and an in-process simulator for simplicity. The production plan is Protobuf contracts with gRPC between orchestration and an independently deployed integration layer, providing generated types across languages and versioned schema evolution. Browser APIs and site configuration can remain JSON. Coordinate units/frames, state transitions, deduplication, and approval rules still require application validation; Protobuf/gRPC is planned, not implemented. Mock waypoints carry synthetic `[x, y, z]` positions in a declared frame, passed through navigation requests and arrival feedback; see the full design for the spatial contract.

## 3. Reliability, safety, and governance

A mission remains understandable through explicit status reasons, plan versions, retained attempts and evidence, and an audit timeline explaining routing, policy, and human decisions. It remains recoverable by distinguishing **confirmed failure** from **unknown outcome**. Timeouts and network loss do not prove a physical action stopped: only retry-safe work restarts automatically. Production reconciliation would query the robot or integration layer by the original command ID before resuming; unresolved interventions go to an authorized operator. External work-order, approval, and evidence services need durable integration operations, idempotent retries where supported, and visible escalation. Missing valid approval or required evidence blocks dependent actions.

The prototype enforces catalog actions, plan validation, dispatch policy, and simulated repair approval. It does **not** implement real authentication or physical safety. Before field deployment, add tenant/site-scoped identity and roles, authenticated service/device connections, controlled support access, trusted target mapping, and adapter-side rejection of unauthorized or stale commands. OEM and field teams must enforce local geofences, collision/person protection, emergency stops, and loss-of-link behavior independent of cloud and LLM decisions. Test malicious instructions, forged/replayed commands, disconnects, and unsafe target changes in vendor test environments before supervised trials. Approval is an additional gate, not a substitute for local safeguards.

## 4. Build versus buy

IAC should own the cross-fleet capability and mission model, safe execution and recovery rules, governance, audit semantics, and reusable site onboarding. Rent model inference; use mature databases, queues, object storage, identity systems, and possibly Temporal for generic durability. Integrate OEM fleet managers and robot-ops platforms for motion, local safety, device monitoring, diagnostics, and software deployment. Customer facility systems retain their work orders and asset registry; thin connectors translate IDs and return outcomes. Define one control owner per robot during remote takeover to avoid conflicting dispatch.

Compare candidates using the same inspection mission and fault scenarios. Include 12-month license, infrastructure, integration, and on-call cost; time to a working adapter; behavior under disconnect, duplicate delivery, and unknown outcomes; ability to enforce IAC policy and see state; and data export and replacement effort. Reject a platform that bypasses approval or hides physical execution state. The prototype's custom state machine demonstrates product semantics; it is not a commitment to rebuild a general-purpose workflow runtime.

## 5. First 90 days

The target is four quadrupeds and five manipulators. The smallest useful product is one repeatable **inspect-and-escalate** workflow on one site and one real robot type, with evidence, clear exceptions, and complete audit. A second robot type tests reuse after the first pilot is stable. Physical repair and welding require separate safety review.

| Period | Priority and stage gate |
| --- | --- |
| **Days 1–30** | Ship visual/thermal inspection with evidence, first in simulation and shadow mode, then in supervised runs on one robot type. Standardize the contracts below and manual baseline. Build a small cloud pilot foundation with ECS, Aurora PostgreSQL, and Terraform; include CI/CD, secrets, HTTPS, logging, alerts, and tested backup/restore. Keep the execution engine single-active until multi-worker ownership and resource claims are proven. Defer physical intervention and unattended operation. |
| **Days 31–60** | Stabilize inspection. Deliver per-customer deployment/database isolation, tenant/site ownership, trusted identity, and site-scoped RBAC; test cross-tenant and unauthorized operations before multi-user field rollout. Pilot approval-gated material delivery with confirmed pickup/drop-off and unknown-outcome reconciliation. Add gas sensing only after sensor validation and if capacity permits. |
| **Days 61–90** | Reuse the proven workflow with a second vendor/type, then a second site if readiness permits; extract repeated steps into templates. Add structured rejection reasons and an operator feedback loop. Pilot Jev or a comparable typed classifier on comments for issue themes and explicit negative language, validate against reviewed examples, and route uncertain results to humans. Alert product and engineering for safety concerns or recurring failures; report trends periodically. A rejection may be correct safety behavior, not dissatisfaction. Defer welding until qualified procedures and field/OEM sign-off. |

| Shared model | Minimum fields or behavior |
| --- | --- |
| Mission | ID, source work order, target, constraints, plan version, state, outcome. |
| Task and attempt | Action/arguments, dependencies, condition, required capability, risk, state; attempt adds robot, command ID, timestamps, result. |
| Capability | Stable ID and versioned input/result contract; actions declare risk, timeout, and retry safety. |
| Resource | Robot, asset, or work zone; availability, reservation owner, and release rules. |
| Event | Unique ID, source/type/version, occurrence and receipt times, mission/task/attempt/command correlation, payload. |

Simulation covers success, denial, fault, timeout, duplicate/out-of-order events, restart, and robot contention; add OEM adapter contract tests and representative customer work orders. Field progression is shadow mode with no robot commands, then bounded supervised inspection, then wider use after evidence quality, intervention rate, and recovery are reviewed. Unexpected physical behavior or duplicate action pauses the pilot and returns work to manual procedure. Capture edits, rejection reasons, takeovers, and unclear UI states from day one; review weekly with operators. Customer feedback sent to an external classifier needs agreed data-sharing and tenant/site access rules, and never changes execution policy automatically.

Robot integration engineers own adapters, command semantics, and reconciliation; field teams own maps and safe procedures; customers supply work orders, approvers, and acceptance criteria; vendors supply API and status semantics. Measure accepted mission outcomes by workflow, fault-detection-to-recovery time, unplanned operator interventions separately from required approvals, reuse of action/template contracts, and time/effort from prerequisites to an accepted new robot or template run. Establish targets from the manual baseline rather than inventing field results from simulation.

## 6. Team and customer execution

I would own cloud infrastructure, backend architecture, AI planning and integration, and full-stack implementation. Add a robot integration engineer first; engage a UX/product designer for console workflows and operator testing, then a robotics/site safety specialist before physical intervention. Sales owns business outcomes, solutions gathers procedures and integration prerequisites, and engineering assesses feasibility and risk. Agree on pilot scope, acceptance criteria, ownership, exclusions, and dependencies before promising dates. Classify requests as site configuration, reusable template, adapter work, or new platform semantics. New semantics need a versioned contract and regression tests; promote a primitive after validating it with another customer or site.

## Prototype boundary

Robots, sensors, and the tank are simulated. The planner, validation, state machine, routing, approvals, persistence, recovery, metrics, and UI are implemented. Known gaps include no asset-level reservation, only target and repair permission enforced from work-order constraints, no individual authentication or RBAC (the hosted-demo password is shared), one execution process, and no automatic reconciliation of an unknown repair outcome. [VERIFICATION.md](VERIFICATION.md) maps assessment requirements to evidence; [AI_USAGE.md](AI_USAGE.md) discloses tool use and decisions.
