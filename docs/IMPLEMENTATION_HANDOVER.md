# IAC Mission Orchestrator: Implementation Handover

## 1. Purpose and current state

This document hands implementation to the next coding agent. Build a working, reviewable orchestration prototype from the requirements below. Do not treat proposed implementation details as additional assessment requirements.

- Repository: https://github.com/zhonghe0615/iac-mission-orchestrator (private).
- Local workspace used during planning: `/Users/harvie/Documents/ChatGPT/IAC`.
- Baseline: `5461f8c`, containing only README and ignore rules. No application, dependencies, tests, or model integration exist yet. This handover is added after that baseline.
- Source assessment: `IAC_Orchestration_Assessment.pdf`, three pages. Local source path: `/Users/harvie/Desktop/IAC/IAC_Orchestration_Assessment.pdf`. The PDF has not been uploaded to the repository. A cloud environment may not have this file; the essential requirements are transcribed below.
- No credentials are stored in the repository. The owner will provide model credentials separately.

## 2. Confirmed owner decisions

- Frontend: React and TypeScript. Backend: Python and FastAPI. Database: SQLite.
- All documentation and code comments must be in English. Communicate with the owner in Chinese. Use English for the product UI as proposed during planning.
- The owner's intended model is named **GPT6 Luna**, with reasoning enabled when needed. This is a requested model name, not a verified API model identifier. Confirm provider access, the actual API identifier, and supported reasoning parameters before integration. Do not invent them or silently substitute a different model.
- Credentials will arrive later. Continue independent implementation with an explicitly labeled mock planner, then verify real model integration when credentials are available. A mock-only submission is insufficient.
- Aim for a deliverable the owner is satisfied with, beyond the assessment's suggested 6-8 hours if needed. The owner initially set an approximately 12-hour total budget, then clarified that exact time accounting should not distract from doing each necessary step properly. Keep scope disciplined; do not interpret this as unlimited scope.
- All repository work belongs to personal account **zhonghe0615**. Do not create repositories or publish project material under any other account.

## 3. Assessment requirements

### Business context

IAC coordinates heterogeneous robots in existing industrial facilities. The orchestration layer sits above robot integration, OEM fleet managers, sensors, and facility systems. It owns mission planning, workflow execution, task assignment, resource coordination, approvals, exception handling, and operational visibility.

The key boundary is between AI proposing work and deterministic software deciding what is permitted and what executes next. Robot-local motion control and physical safety mechanisms belong below this layer.

### Required fleet

| Robot | Assessment capabilities |
| --- | --- |
| Scout A | Navigation, camera, thermal inspection |
| Scout B | Navigation, camera, thermal inspection |
| Mobile Manipulator C | Inspection and simulated repair |

Define concrete action schemas, durations, observations, and failure behavior. These are implementation assumptions, not additional facts about real hardware. All robot execution may be simulated; no ROS, hardware, 3D simulator, or complex physics is required.

### Required mission

1. An operator submits a plain-English work order to inspect and resolve a suspected tank defect.
2. A real LLM produces a structured, capability-aware mission plan. The operator can inspect or edit it before execution.
3. The orchestrator selects an eligible inspection robot, executes simulated inspection, and records observations and evidence.
4. Findings can trigger a high-risk repair request. Explicit operator approval is required before repair. Approval and denial must produce different outcomes.
5. Following approval, Mobile Manipulator C performs simulated repair, followed by verification and completion of the mission record.
6. Inject a failure, such as Scout A going offline or an action timing out. Demonstrate appropriate retry, reassignment to Scout B, or escalation without silently losing mission state.

### Required product surface

- Web UI: submit a mission; review its plan; view robot availability; track mission/task status and a live event timeline; approve/reject a step; inject a failure.
- Backend: reusable mission/task model, capability-based routing, explicit execution state machine, and simulated robot action/event adapter.
- Meaningful agent integration: real LLM use for at least one decision-support step, with structured output and validation. Replay/mock mode is useful but cannot be the only planner behavior.
- Basic tests: success, approval denial, and failure/recovery. Document at least one unhandled edge case.
- Agent output must not bypass execution rules or directly issue unrestricted low-level robot commands. Simulated repair must be gated by policy and human approval.

### Required delivery artifacts

- Approximately 2-4 pages of technical design explaining architecture and tradeoffs.
- Source repository and README with setup, launch, and clear simulated-versus-implemented boundaries.
- A 3-5 minute demo video **or** live walkthrough covering planning, execution, approval, failure, and recovery. A repeatable walkthrough and script are the proposed default; a recorded video has not been explicitly selected.
- Brief AI-tool usage disclosure describing what tools accelerated and which choices received human engineering judgment. Record actual use; do not invent a history of human decisions.

### Required design document topics

The PDF poses these sections, each with a key question the design should answer directly:

1. **System design.** Components, data flow, and responsibilities for turning a work order into execution across a mixed fleet. Cover agentic planning (how the LLM uses site context, capabilities, and tools; how output is validated), workflow execution (dependencies, retries, timeouts, cancellation, persistence, resumption), fleet coordination (assignment, shared-resource reservation, availability, conflicting commands), human-in-the-loop (approvals, overrides, escalations, audit), and platform boundaries (orchestration versus Robot Integration Layer, OEM fleet managers, robot-local control). Key question: where should AI reasoning end and deterministic, policy-governed execution begin? Also explain the Temporal/LangGraph/workflow-library/custom tradeoff.
2. **Fit into the bigger picture.** Contracts between enterprise inputs, agent/planning services, orchestration, governance, the Robot Integration Layer, and operator interfaces. What is shared across robot types and what stays robot/OEM-specific. Key question: how does the orchestration core stay generic as robot brands, customer sites, and mission types are added?
3. **Reliability, safety, and agent governance.** Path to production: mission durability, idempotent command handling, timeouts, stale or duplicate events, partial completion, human intervention, auditability, access control, and invalid or unsafe LLM plans. Key question: how does a mission remain understandable and recoverable when robots, networks, external systems, or the agent fail?
4. **Build versus buy.** Under tight resources, what to build and what to source: workflow engines, agent frameworks, OEM fleet managers, robot-ops platforms, customer facility systems. Evaluate cost, integration effort, reliability, control, and lock-in. Key question: what gives IAC durable differentiation, and what commodity infrastructure should it avoid rebuilding?
5. **First 90 days.** Scenario: owning the first production-ready Orchestration Layer for four quadrupeds and five mobile manipulators with varied capabilities; missions include gas detection, visual/thermal inspection, material handling, and welding; operators need approvals, exception handling, and audit logs. Give priorities for days 1-30, 31-60, and 61-90, covering: the one or two workflows to ship first and what to defer; the minimum shared mission, task, capability, resource, and event model; testing via simulation, staged real-world rollout, and operator feedback; dependencies on integration engineers, field teams, customers, and vendors; and success metrics (for example mission success rate, time to recover, operator interventions, task reuse, onboarding time for new robot types or mission templates). Key question: what is the smallest useful orchestration product that proves reliable, repeatable workflows across heterogeneous robots?
6. **Team and customer execution.** First roles or contractors to add, collaboration with sales/solutions teams, and turning customer-specific mission requests into reusable platform primitives rather than one-off implementations.

### Evaluation criteria

The assessment values working software and engineering judgment over extensive prose or sophisticated visuals. Its stated primary signals, in order: (1) end-to-end ownership and ability to ship, (2) agent design with clear safety/validation boundaries, (3) robust state and failure handling, (4) reusable platform abstractions, and (5) product/UI clarity. Reviewers will discuss tradeoffs and what would change for real deployments.

### Bonuses

The assessment lists these as optional: durable restart/replay, concurrent missions with resource contention, explicit policy checks, simulated out-of-order events, and metrics for mission completion and recovery. Basic repair authorization is mandatory even though additional policy sophistication is optional. Section 6 sets which bonuses to pursue and in what order.

## 4. Proposed implementation architecture

Use a single backend service with internal modules, not microservices. This is the planning recommendation; refine details when implementation evidence justifies a simpler or safer approach.

```mermaid
flowchart TD
    UI[React operator interface] --> API[FastAPI endpoints]
    API --> HARNESS[Planning harness: context and model invocation]
    HARNESS <--> LLM[External LLM service]
    HARNESS --> VALIDATE[Plan schema and semantic validation]
    API -->|Edited plans| VALIDATE
    VALIDATE -->|Validated plan, explicit start| ENGINE[Mission and task state machine]
    API -->|Approval, denial, cancellation| ENGINE
    ENGINE --> POLICY[Execution policy checks]
    POLICY --> ROUTER[Capability routing and resource reservation]
    ROUTER --> PORT[Robot action interface]
    PORT --> SIM[Simulated fleet adapter]
    SIM --> EVENTS[Execution event processing]
    EVENTS --> ENGINE
    ENGINE --> DATA[Persistence and audit module]
    EVENTS --> DATA
    DATA --> DB[(SQLite)]
    DATA -->|Status and timeline| API
    API -->|Explicit failure injection| SIM
```

### Ownership and external dependencies

We implement the UI, API, planning harness, domain models, validation, policy enforcement, state transitions, routing, persistence logic, audit trail, and simulator. The LLM service and database engine are dependencies. Frameworks provide infrastructure; they do not define our mission semantics.

Real enterprise work-order systems, Robot Integration Layer implementations, OEM fleet managers, sensors, and hardware remain outside prototype scope. Define their conceptual contracts in the design, not full integrations.

### Planning harness

- Supply a bounded site context, supported action catalog, and robot capabilities.
- Generate a structured candidate plan, validate it, and give bounded correction feedback or a clear error.
- Record planner mode and relevant call outcomes. Never silently replace a failed real LLM call with a successful mock result.
- Initial planning is sufficient; autonomous replanning is not required for the first slice.
- MCP is not required. Passing context directly to the model is sufficient initially. Keep robot execution behind the orchestrator rather than exposing an unrestricted repair tool to the model.

### Execution core

- Validate both model-generated and operator-edited plans. Reject unsupported actions, invalid arguments, missing dependencies, cycles, and unsafe sequencing.
- Recheck runtime eligibility before dispatch; plan-time availability can become stale.
- Enforce repair approval in backend code, independent of model assertions or UI controls. Bind approval to the relevant plan version and action. Relevant edits invalidate prior approval.
- Route through capability requirements rather than hardcoding Scout A into workflow logic. A deterministic preference for A can make the failure demo reproducible when A and B are both eligible.
- Reserve a robot while an action is active. Start with clear behavior for unavailable resources; broad multi-mission scheduling can follow later.
- Maintain explicit timeout, cancellation, and recovery semantics. A cancellation request does not prove the physical action has stopped.
- Persist state and audit events consistently. Basic restart recovery is a proposed quality target, not a claim that full workflow replay is already implemented.

### Robot interface and simulator

Define typed commands and results for navigation, camera inspection, thermal inspection, simulated repair, verification, status queries, and cancellation as needed. Document any capabilities assumed for verification or movement beyond the assessment's explicit catalog.

Use mission/task/attempt identifiers to correlate commands and events. The simulator must receive real backend commands and emit results or failures through the same event-processing path. The UI must not animate a predetermined success story independent of backend execution.

## 5. Minimum domain concepts and invariants

These are suggested concepts, not a requirement for one class or table per row.

| Concept | Minimum information |
| --- | --- |
| Mission and plan version | Work order, target, validated plan, lifecycle status, timestamps |
| Task | Action, arguments, capability requirements, dependencies, condition, status |
| Execution attempt | Task, robot, command correlation ID, attempt number, timeout, outcome |
| Robot | Capabilities, availability, active reservation |
| Approval | Relevant plan version and action, decision, actor, timestamp |
| Evidence | Source task/attempt, observation, simulated provenance |
| Event | Correlation IDs, event type, timestamp, payload, deduplication identity |

Define the transition table before implementation. Distinguish mission, task, and attempt state so that a failed attempt can be retried without erasing history.

Required invariants:

1. No repair dispatch without valid approval for that action and plan version.
2. Denial cannot produce successful repair or a misleading "defect resolved" outcome.
3. Dispatch requires matching capability and available capacity.
4. A task runs only when its dependencies and conditions permit it.
5. Reassignment preserves previous attempts and evidence.
6. A late result from a superseded attempt cannot overwrite the current attempt's outcome.
7. A timeout means the result may be unknown; it does not prove that no physical action occurred.
8. Mission success requires the relevant verification to succeed.

For a repair with an unknown result, query status or pause for intervention. Do not blindly retry a potentially completed physical action. For the first recovery demo, inject a clearly failed inspection attempt that is safe to repeat; describe the distinction from ambiguous network loss.

## 6. Implementation milestones and checks

### Priority tiers

- **Tier 1, required slice:** milestones A through F as checked below. Every Tier 1 check must pass before Tier 2 work starts. The assessment rewards a convincing end-to-end slice over breadth.
- **Tier 2, selected bonuses, in this order:**
  1. Explicit policy checks: show each policy decision (allowed or blocked, with its reason) in the timeline and audit record. This mostly surfaces the repair gate that Tier 1 already requires.
  2. Duplicate and out-of-order events: add a simulator control that redelivers or reorders events, and show that they are ignored safely. This builds on the correlation IDs and deduplication identity that Tier 1 already requires.
  3. Durable restart: restart the backend while a mission waits for approval and recover it from SQLite. Running actions with an ambiguous outcome need reconciliation, not redispatch.
  4. Metrics: mission completion, retries, reassignments, time to recover, and operator interventions, derived from the audit events.
- **Deferred:** concurrent missions with resource contention, automatic replanning, and implementing unknown-outcome repair reconciliation. Describe them in the design document instead.

Stop Tier 2 work at a clean item boundary once the remaining budget is needed for the design document, demo, and README.

### A. Contracts and executable foundation

Create backend/frontend scaffolding, configuration examples without secrets, domain schemas, and a transition table. Define a minimal fixed site and defect scenario. Document assumptions such as target identifiers, inspection findings, and denial outcome.

Check: both apps launch; a valid plan can be represented; invalid actions and dependency cycles are rejected.

### B. Deterministic mission execution

Implement persistence, simulated fleet, dispatch, evidence, repair gating, verification, and terminal outcomes. Use a clearly labeled deterministic planner fixture while credentials are unavailable.

Check: success and denial execute through the backend; direct API requests cannot bypass approval; state and audit records agree.

### C. Failure and recovery

Implement a reproducible Scout A inspection failure, eligible reassignment to B, and explicit waiting/escalation when no replacement is available. Correlate attempt results. Document restart and unknown-action behavior; implementing them is Tier 2 or deferred.

Check: failure recovery preserves the mission and history; late A events cannot incorrectly finish B's active task. A repair timeout pauses for operator intervention instead of retrying.

### D. Real planning integration

Verify provider/model configuration when credentials arrive. Implement structured plan generation, validation, bounded correction, and clear error reporting. Revalidate edited plans. Real and mock modes must be visible.

Check: at least one actual model-generated plan completes the workflow; malformed or unsafe output is blocked. If credentials are still unavailable, explicitly report this as an unmet acceptance criterion rather than claiming completion.

### E. Operator interface

Connect mission submission, plan review/editing, robot availability, status, evidence, approvals, and live timeline to backend state. Include explicit failure controls in the simulator UI. Choose a simple update transport appropriate to this prototype.

Check: the user can explain what is happening, why execution is waiting, and what action is needed without reading backend logs. Refreshing the page preserves the visible mission through backend persistence.

### F. Delivery verification

Run meaningful tests and the walkthrough from documented setup instructions. Finish the short design, known limitations, demo script, and AI-use disclosure. Update README to reflect actual implementation, not promises.

Check: success, denial, and failure/recovery can all be reproduced. Mark each requirement as verified, incomplete, or intentionally deferred with evidence.

## 7. Acceptance scenarios

| Scenario | Tier | Observable acceptance criteria |
| --- | --- | --- |
| Approved repair | 1 | Submit work order; obtain and review a real model plan; run inspection; show evidence; pause for approval; approve; C repairs; verification succeeds; mission completes with timeline |
| Denied repair | 1 | Reject the pending repair; no repair command is dispatched; mission clearly records rejection and unresolved work |
| Inspection recovery | 1 | Inject A failure during inspection; persist failed attempt; choose B if eligible; continue the original mission; retain both attempts |
| No available replacement | 1 | Recovery cannot choose B; show explicit waiting or escalation with a reason; no false completion |
| Invalid or unsafe plan | 1 | Invalid model output or edited plans are blocked before execution; omitting an approval node cannot bypass runtime repair checks |
| Stale result | 1 | A superseded attempt's late result does not corrupt the current attempt's state |
| Visible policy decisions | 2 | Each dispatch and repair gate decision appears in the timeline and audit record with its reason |
| Duplicate or out-of-order event | 2 | Injected redelivered or reordered events do not repeat transitions or move state backward |
| Restart | 2 | Restart backend during approval waiting; recover the mission and decision context. Running ambiguous actions require reconciliation rather than automatic redispatch |
| Metrics | 2 | Completion, retry, reassignment, recovery time, and intervention figures match the audit events |

Keep tests focused on behavior and invariants. The first three scenarios are the assessment's basic test categories. Automated tests must run offline against the deterministic planner fixture. The real model requirement in "Approved repair" is a separate integration check, run manually with credentials and recorded as evidence.

## 8. Scope control and unresolved details

- Do not build real robot drivers, 3D visualization, complex physics, a general-purpose agent framework, or a broad enterprise integration platform.
- Proposed default: a small custom state machine with persisted state. Temporal/LangGraph are optional, not assessment requirements. Explain the prototype tradeoff and what would change for production.
- Defer automatic replanning and complex multi-mission contention until the required vertical slice works.
- Do not claim production-grade safety, authentication, durability, or exactly-once physical execution. Document demo identity assumptions and production access-control requirements.
- Provider credentials, exact model ID, reasoning support, and real integration validation remain pending.
- Exact task states, API routes, action schemas, dependency versions, UI layout, and simulation fixtures are implementation decisions. Resolve routine reversible details without repeatedly asking the owner.
- Ask only for missing credentials or choices that materially change scope, cost, access, or the agreed behavior.

## 9. Working and repository conventions

- Inspect the checkout and applicable instructions before editing; preserve unrelated user changes.
- Keep implementation small and changes traceable to requirements. Avoid speculative abstractions or unrelated cleanup.
- Make focused commits and use `codex/` as the default prefix when creating a branch unless the owner requests another name. Never force-push or modify global Git settings as a shortcut.
- Verify the repository owner before any remote write. Use the personal account, not the work account.
- The current local checkout uses repository-local HTTPS rewrite and credential-helper settings because a global configuration rewrites GitHub HTTPS URLs to SSH. These settings are not carried by a clone; configure authentication appropriately in the execution environment without copying secrets.
- If a pull request is appropriate, report its URL and validation evidence. Do not merge or deploy without authorization.
- Report what was actually tested, what is inferred, and what remains unverified. Include residual limitations in the final handoff.
