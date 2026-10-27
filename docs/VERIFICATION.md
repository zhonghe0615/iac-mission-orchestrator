# Requirement Verification

Status of each assessment requirement as of 2026-10-26.

- **Verified:** reproduced, with the evidence named.
- **Incomplete:** not done yet.
- **Deferred:** intentionally out of scope.

Test names refer to `backend/tests/`. "Browser run" means headless Chrome driving the built UI against the real backend, with screenshots checked by hand.

## Design response coverage

The [short submission design](DESIGN_SUBMISSION.md) answers the six written prompts; [DESIGN.md](DESIGN.md) retains the detailed tradeoffs and production plan. Proposed production controls are described as proposals, not counted as implemented features.

| PDF prompt | Submission section | Prototype evidence or boundary |
| --- | --- | --- |
| 1. Architecture and AI-assisted implementation | §1 | Planner, validator, state machine, simulated adapter, policy, approval, and tests. Robot reservations exist; asset/work-zone reservations remain future work. |
| 2. Fit into IAC | §2 | Enterprise, planning, governance, robot, and UI contracts; fleet config shows type reuse. Real OEM adapters and configurable asset/action catalogs remain future work. |
| 3. Reliability, safety, governance | §3 | Persistence, retry and unknown-outcome rules, audit, and invalid-plan handling are tested. Authentication and physical safety are production proposals. |
| 4. Build versus buy | §4 | Compares mission semantics with Temporal, model hosting, OEM/robot-ops platforms, and facility systems using cost, integration, reliability, control, and exit criteria. |
| 5. First 90 days | §5 | First workflows, common models, rollout gates, dependencies, metrics, cloud foundation, RBAC, and operator feedback analysis are proposed milestones. |
| 6. Team and customer execution | §6 | First roles, sales/solutions handoff, acceptance criteria, and path from customer request to reusable platform primitive. |

## Required mission

| Requirement | Status | Evidence |
| --- | --- | --- |
| Fleet matches the specified robot capabilities | Verified | `backend/config/fleet.json`: Scouts A and B have navigation, camera, and thermal inspection; Manipulator C has navigation, camera inspection, and simulated repair capability. |
| Operator submits a plain-English work order | Verified | Browser run; `test_operator_flow_over_http` |
| A real LLM produces a structured, capability-aware plan | Verified | Two recorded `gpt-6-luna` runs (below), both valid on the first call |
| Plan respects the operator's target asset and repair permission | Verified | Enforced by validation for model, fixture, and edited plans, and again by the dispatch policy (`test_constraints.py`; each check was disabled in turn and its test failed); browser run (fixture planner declines a T-102 survey); real runs below |
| Operator can inspect or edit the plan before execution | Verified | Browser run (plan table and editor); `test_edited_plan_is_revalidated_before_start` |
| Orchestrator selects an eligible inspection robot and records evidence | Verified | `test_approved_repair_resolves_mission`; browser run evidence panel |
| Findings trigger a high-risk repair request; explicit approval is required | Verified | `test_api_requests_cannot_bypass_approval`; approval card in browser run |
| Approval and denial lead to different outcomes | Verified | `test_approved_repair_resolves_mission` (resolved), `test_denied_repair_dispatches_no_repair` (unresolved, no repair command) |
| Manipulator C repairs after approval, then verification completes the record | Verified | Same tests; mission is resolved only after `repair_verified` |
| Injected failure is handled by retry, reassignment, or escalation without losing state | Verified | `test_scout_a_offline_during_inspection_reassigns_to_b`, `test_no_replacement_waits_with_reason_then_resumes`, `test_inspection_failure_escalates_after_retry_budget`, `test_unknown_repair_outcome_is_not_redispatched`; browser run (Scout A taken offline, both attempts kept) |

## Product surface

| Requirement | Status | Evidence |
| --- | --- | --- |
| Web UI: submit, review plan, robot availability, status, live timeline, approve/reject, inject failure | Verified | Browser run covers each; live updates over server-sent events; state survives a page reload |
| Backend: reusable mission/task model, capability routing, explicit state machine, simulated adapter | Verified | `engine.py` transition table (DESIGN_REFERENCE.md §2); `test_new_robot_type_needs_only_configuration` |
| Real LLM decision support with structured output and validation; mock is not the only mode | Verified | Real runs below; `test_planner.py` (bounded correction, no fixture fallback) |
| Tests: success, approval denial, failure and recovery | Verified | 78 tests, passing on Python 3.10 and 3.12 |
| At least one unhandled edge case documented | Verified | DESIGN.md §8 (concurrent missions on one asset, among others) |
| Agent cannot bypass execution rules or issue low-level commands | Verified | The plan schema has no robot or approval fields (`test_invalid_plans_are_rejected`); repair is gated at runtime by approval bound to plan version and action |

## Delivery artifacts

| Artifact | Status | Evidence |
| --- | --- | --- |
| Technical design (about 2–4 pages) | Verified | `docs/DESIGN_SUBMISSION.md`, about 1,800 words, answers all six assessment topics; full discussion in `docs/DESIGN.md` and implementation detail in `docs/DESIGN_REFERENCE.md` |
| README with setup, launch, simulated versus implemented | Verified | Followed from a fresh GitHub clone with pip on Python 3.10 |
| Demo video or live walkthrough | Recorded locally; deliver separately | 3:48 captioned video recorded on 2026-10-25 and retained outside the repository. Shows two real-model plans, Scout A failure and reassignment to Scout B, approved simulated repair and verification, and a separate rejection with no repair dispatch. The edit removes idle pauses; the underlying mission records were checked independently. |
| AI tool usage disclosure | Verified | `docs/AI_USAGE.md`: tools used at each stage, decisions made by the author, accepted proposals, and how the work was checked; reviewed by the author |

## Bonuses

| Bonus | Status | Evidence |
| --- | --- | --- |
| Explicit policy checks | Verified | `policy_decision` events with per-rule results, shown in the timeline; a blocked dispatch goes to `needs_intervention` with the failed rules named (`test_policy_gate_blocks_a_repair_the_operator_did_not_allow`) |
| Simulated out-of-order and duplicate events | Verified | `test_duplicate_completion_does_not_repeat_transitions`, `test_out_of_order_accepted_after_completion_is_ignored` |
| Durable restart and recovery | Verified | `test_restart_while_waiting_for_approval_recovers_decision_context`, `test_restart_with_open_inspection_redispatches_safely`, `test_restart_with_open_repair_requires_intervention`, `test_restart_between_repair_and_verification_resolves` (the simulated tank state is saved, so verification after a restart sees the repair) |
| Metrics for completion and recovery | Verified | `test_metrics_reflect_recovery`; metrics panel |
| Concurrent missions with resource contention | Partly verified | Robots are never double-booked across 40 concurrent missions (`test_large_fleet_runs_many_missions_without_double_booking`). Asset-level contention is deferred: two missions can repair the same tank. |

## Additional guarantees

| Property | Evidence |
| --- | --- |
| Typed fleet; scales without code changes | `test_fleet.py`: 520 robots with 40 missions; 20 types with 1,000 robots; a new type through config only |
| Idempotent operator commands and robot commands | `test_idempotency.py` |
| Thread-safe store; one server process per database | `test_concurrency.py` |
| Silent robot failures detected by a heartbeat lease (busy and idle robots, recovery, no false alarms, including right after a robot announces it is back online); shown as unreachable, distinct from an announced offline; lease configurable | `test_liveness.py`; browser runs at real speed: silent loss detected after 2.6–3.1 s and work moved to Scout B, with an announced offline shown separately; zero false losses with 1,000 robots and 40 missions |
| Tests detect regressions | Each key guard was removed in turn and its tests failed (approval gate, stale-result check, freed-robot recheck, store lock, snapshot reads, idempotency claim, command dedupe, exclusive database lock, loop check) |

## Real model runs

Model `gpt-6-luna` (OpenAI Chat Completions), reasoning effort `medium`.

| Run | Plan | Model call | Outcome |
| --- | --- | --- | --- |
| Engine run | 4 tasks: thermal and visual inspection in parallel, conditional `seal_weld_seam` repair, verification | Valid on the first attempt; 17.3 s; 1,920 tokens (395 reasoning) | Resolved |
| Browser run | 3 tasks: thermal inspection, conditional repair, verification | Valid on the first attempt; 10.0 s; 1,753 tokens | Resolved, with Scout A taken offline mid-inspection and work moved to Scout B |

Runs with the operator's intake fields, 2026-10-25, through the HTTP API (each valid on the first model call):

| Work order | Intake fields | Plan | Outcome |
| --- | --- | --- | --- |
| "Do a thermal survey of tank T-102. Do not perform repairs." (the reviewer's case) | T-102, repairs not allowed | One thermal inspection of T-102; 4.8 s, 1,368 tokens | No action needed; one command, to Scout A |
| Default T-101 leak work order | T-101, repairs allowed | Thermal inspection, conditional `seal_weld_seam`, verification; 6.8 s, 1,733 tokens | Approved; resolved after verification |
| "Inspect T-101 and repair the weld seam if you find a leak." (historical run before text/field consistency validation) | T-101, repairs not allowed | One thermal inspection; the summary said the repair was omitted because of the operator constraints; 5.7 s, 1,414 tokens | This conflicting intake is now rejected before planning. |

Earlier operator runs in the UI, before the intake fields existed, read back from the mission records:

| Work order | Plan | Followed the constraints? |
| --- | --- | --- |
| "Do a thermal survey of tank T-102 for the quarterly report. Do not perform any repairs." | One thermal inspection, no repair | Yes: thermal as requested, no repair |
| "The flange gasket on T-102 may be leaking. Inspect it and, if confirmed, replace the gasket." | Visual inspection, then repair with `replace_gasket` conditioned on `defect_detected`, then verification | Yes: correct procedure, repair only if confirmed |
| "…run the thermal scan and the close-range visual inspection in parallel…" | Thermal and visual with no dependency between them; repair depends on both | Yes: ran on Scouts A and B at the same time |

The model chose different but valid plans across runs. Invalid model output was never observed live. Its handling (correction feedback, visible failure, no fallback) is verified only against a fake endpoint.
