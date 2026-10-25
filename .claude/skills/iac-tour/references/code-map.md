# Code map

Where each behavior lives, and the test that proves it. Paths are relative to `backend/`. Search for the function name rather than trusting line numbers, then show the lines you found.

| Topic | Where | Proved by (`tests/…`) |
| --- | --- | --- |
| Site, assets, action catalog (role, risk, retry safety, timeouts) | `app/catalog.py`: `ASSETS`, `ACTIONS`, `ActionSpec` | `test_plan.py::test_fixture_plan_is_valid` |
| Typed fleet: robot types, capabilities, config errors | `app/catalog.py`: `Fleet`; `config/fleet.json` | `test_fleet.py::test_invalid_fleet_config_lists_every_problem`, `test_new_robot_type_needs_only_configuration` |
| What the LLM is told | `app/planner.py`: `SYSTEM_PROMPT`, `_user_prompt`; `app/catalog.py`: `site_context` | `test_planner.py::test_valid_output_is_accepted_and_request_is_structured` |
| LLM call, bounded correction, no fixture fallback | `app/planner.py`: `LLMPlanner.plan` | `test_planner.py::test_invalid_output_gets_bounded_correction`, `test_failed_llm_planning_never_substitutes_the_fixture` |
| Fixture planner (not an LLM) | `app/planner.py`: `FixturePlanner`, `FIXTURE_PLAN` | `test_constraints.py::test_fixture_planner_declines_incompatible_work_orders` |
| Plan schema (no robot or approval fields) | `app/plan.py`: `Plan`, `PlanTask` (`extra="forbid"`) | `test_plan.py::test_invalid_plans_are_rejected` |
| Plan safety rules | `app/plan.py`: `validate_plan` | `test_plan.py::test_repair_without_prior_inspection_is_rejected`, `test_repair_without_verification_is_rejected` |
| Operator constraints (target, repairs allowed) | `app/plan.py`: `Constraints`; `app/engine.py`: `_constraints` | `test_constraints.py` (whole file) |
| Mission lifecycle commands | `app/engine.py`: `submit_mission`, `update_plan`, `start_mission`, `decide_approval`, `retry_task`, `cancel_mission` | `test_missions.py::test_approved_repair_resolves_mission` |
| Scheduling: dependencies, conditions, approval requests | `app/engine.py`: `_advance`, `_request_approval` | `test_missions.py::test_no_defect_skips_repair`, `test_independent_inspections_run_in_parallel_on_a_and_b` |
| Routing by capability and availability | `app/engine.py`: `_route`; `app/store.py`: `available_robot_ids` | `test_missions.py::test_no_replacement_waits_with_reason_then_resumes`, `test_fleet.py::test_many_robot_types_route_only_to_capable_types` |
| Policy gate at dispatch | `app/engine.py`: `_policy_check`, `_try_dispatch` | `test_constraints.py::test_policy_gate_blocks_a_repair_the_operator_did_not_allow` |
| Approval bound to plan version and action, single use | `app/engine.py`: `_approval_for`, `action_hash` | `test_missions.py::test_api_requests_cannot_bypass_approval`, `test_denied_repair_dispatches_no_repair` |
| Failure handling: retry, reassign, escalate, unknown outcome | `app/engine.py`: `_attempt_failed`, `_on_timeout` | `test_missions.py::test_scout_a_offline_during_inspection_reassigns_to_b`, `test_unknown_repair_outcome_is_not_redispatched`, `test_inspection_failure_escalates_after_retry_budget` |
| Late, duplicate, out-of-order robot events | `app/engine.py`: `_handle_robot_event`, `_on_stale_result` | `test_missions.py::test_late_result_from_timed_out_attempt_is_ignored`, `test_reliability.py::test_duplicate_completion_does_not_repeat_transitions` |
| Heartbeats: unreachable versus offline | `app/engine.py`: `heartbeat`, `_watch_heartbeats`, `_on_robot_status` | `test_liveness.py` |
| Mission outcome rules (resolved needs verification) | `app/engine.py`: `_outcome`, `_refresh_mission` | `test_missions.py::test_approved_repair_resolves_mission` |
| Restart recovery | `app/engine.py`: `_recover`; `app/simulator.py`: `_save_assets` | `test_reliability.py::test_restart_*` |
| Transactions, outbox, inbox, audit log | `app/engine.py`: `_locked`, `_in_tx`, `_flush_outbox`; `app/store.py` | `test_concurrency.py` |
| Idempotency keys on operator commands | `app/main.py`: `idempotent`; `app/store.py`: `claim_idempotency_key` | `test_idempotency.py` |
| Simulated robots and fault injection | `app/simulator.py`: `SimulatedFleet`, `arm_fault`, `lose_silently`, `_apply` | used by most tests |
| HTTP API and live stream | `app/main.py`: `create_app` | `test_api.py::test_operator_flow_over_http` |
| Metrics from the audit log | `app/metrics.py` | `test_reliability.py::test_metrics_reflect_recovery` |
| Operator console | `frontend/src/`: `App.tsx`, `Sidebar.tsx` (work order form, fleet, faults), `MissionView.tsx` (plan, approvals, timeline), `ProgressBar.tsx` | browser walkthrough in `docs/VERIFICATION.md` |

Design reasoning for each area is in `docs/DESIGN.md`, with the full transition table in `docs/DESIGN_REFERENCE.md` §2.
