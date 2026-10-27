# Demo and failure scenarios

Each scenario lists what to click in the console, the equivalent helper commands, and what to point out. Run the helper from the repository root:

```bash
python3 .claude/skills/iac-tour/scripts/iac.py            # command list
```

Below, `iac` stands for that command. It talks to `http://127.0.0.1:8000`; set `IAC_URL` for another port. `create` waits for planning and prints the mission ID on its first line. Add `--planner fixture` for deterministic plans. Without that flag, the server's default planner is used (the LLM when one is configured).

**Before every scenario except the first,** restore the defect: `iac defect tank-T101 on` (or tick **Simulated defect present on T-101** in the Fleet panel). A successful repair fixes the simulated tank, and the fix survives restarts. Without the defect, the inspection finds nothing and the mission closes as "no action needed".

Normal-speed timings: thermal 4 s, visual 3 s, repair 6 s, verification 4 s. "During the inspection" means within about 4 s of starting.

## 1. Main mission: plan, approve, repair, verify

- **Console:** keep the default work order (T-101, **Allow repairs** ticked), then **Generate plan**, **Start mission**, **Approve repair**.
- **Helper:** `iac create "Inspect and resolve the suspected leak on tank T-101" --planner fixture`, then `iac start MID 1`, `iac wait MID waiting_approval`, `iac events MID policy_decision`, `iac approve MID t3 REQUEST_SEQ` (use the sequence printed by `iac wait`), `iac wait MID resolved`.
- **Point out:**
  - the plan has no robot or approval fields;
  - Scout A is chosen by capability and preference;
  - each dispatch has a policy decision with per-rule results;
  - the repair waits for approval and is based on the t1 finding;
  - the mission is **resolved** only after `verify_repair` reports `repair_verified`.

## 2. Denied repair

- **Helper:** `iac create … --planner fixture`, then `iac start MID 1`, `iac wait MID waiting_approval`, `iac reject MID t3 REQUEST_SEQ "Crew not available"`, `iac wait MID unresolved`.
- **Expect:**
  - t3 is `rejected` and was never sent to a robot (no attempt);
  - t4 is `skipped`, because its dependency was rejected;
  - the mission ends **unresolved**: "the defect on tank-T101 remains open".

## 3. Robot goes offline mid-inspection

- **Console:** start a mission, then click **Take offline** on Scout A during t1.
- **Helper:** `iac start MID 1`, then within about 3 s `iac fault scout-a offline`, then `iac wait MID waiting_approval`.
- **Expect:**
  - t1 shows `attempts=[scout-a:lost, scout-b:succeeded]`: the same task and the same mission, with both attempts kept;
  - "lost" means the outcome is unknown; the inspection is safe to repeat, so it moves to Scout B.
- **Restore:** `iac fault scout-a online`.

## 4. Robot goes silent (heartbeat loss)

- **Console:** **Lose silently** on Scout A during t1.
- **Helper:** `iac fault scout-a lose_silently`, wait about 4 s, then `iac robots`.
- **Expect:**
  - for about 3 s nothing happens, because the orchestrator has not been told;
  - then Scout A shows `unreachable  no heartbeat for 3s`, not "offline", because it may still be acting;
  - its attempt becomes `lost` and Scout B takes over.
- **Restore:** `iac fault scout-a online`.

## 5. Timeout and a late result

- **Helper:** before starting, `iac fault scout-a hang_next`. Then start a mission and `iac wait MID waiting_approval`, then `iac events MID | grep -i -e "timed out" -e stale`.
- **Expect:**
  - after 10 s, attempt 1 is `timed_out` ("outcome unknown") and Scout B takes over;
  - Scout A's late result is then logged as `stale_result_ignored` and changes nothing;
  - Scout A becomes available again once it reports.

## 6. No replacement robot

- **Helper:** `iac fault scout-b offline`, then create and start a mission. During t1, run `iac fault scout-a offline`, then `iac show MID`.
- **Expect:** the mission is **waiting_resource**, with a reason for every robot: "Scout A is offline; Scout B is offline; Mobile manipulator type lacks thermal_inspection".
- **Then:** `iac fault scout-b online`. The mission resumes on Scout B and reaches the approval.
- **Restore:** `iac fault scout-a online`.

## 7. Duplicate and out-of-order events

- **Helper:** `iac fault scout-a duplicate_next`, then run a mission and `iac events MID duplicate_event_ignored`. For reordering, use `reorder_next` and look for `stale_event_ignored`.
- **Expect:** each duplicate or reordered event is logged and ignored. Each task still succeeds exactly once, with one piece of evidence.

## 8. A plan edit that breaks a safety rule

- **Console:** on a ready mission, **Edit plan**, delete the repair task's `condition`, then **Validate and save**.
- **Helper:** `iac plan MID > plan.json`. Remove `condition` from the repair task inside the file's `plan` field, then run `iac edit MID plan.json`. The exported version prevents an old file from overwriting a newer plan.
- **Expect:** `HTTP 422: plan failed validation`, because a repair must be conditioned on `defect_detected`. The same validator checks model output.

## 9. Operator constraints versus the work order

- **Fixture:** `iac create "Do a thermal survey of tank T-102. Do not perform repairs." tank-T102 --no-repair --planner fixture`. Expect `planning_failed`: the fixture planner declines orders that do not fit its single plan.
- **LLM** (if configured): the same order without `--planner fixture` gives a single `thermal_inspection` of T-102 and no repair.
- **Conflict** (LLM): T-101 with `--no-repair` and the text "repair the weld seam if you find a leak". The fields win: the plan has no repair, and the model's summary says why.
- **Console:** the **Survey only, no repairs** example chip sets the target and checkbox for you.

## 10. Approval cannot be bypassed

- **Helper:** start a mission and immediately run `iac approve MID t3 1`. Expect `HTTP 409: task t3 is not awaiting approval`. Starting a running mission again gives 409 too.
- **Point out:** approval is tied to the plan version and the exact action, and is used by exactly one dispatch (`test_missions.py::test_api_requests_cannot_bypass_approval`).

## 11. Restart while waiting for approval

- **Steps:**
  1. Run a mission to `waiting_approval`.
  2. Stop the server, start it again, then `iac show MID`.
  3. It is still waiting, with the same reason and evidence. `iac show MID` prints the approval request sequence; `iac approve MID t3 REQUEST_SEQ` completes it.
- **Point out:** if the server stops between the repair and the verification, the tank stays repaired, and verification passes after the restart (`test_restart_between_repair_and_verification_resolves`).

## 12. Metrics

- **Helper:** `iac metrics`, or the console's Metrics panel.
- **Shows:** retries, reassignments, mean time to recover, escalations, operator actions, and ignored duplicate or stale events, all computed from the audit log.
