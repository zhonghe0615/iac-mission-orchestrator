# Demo Walkthrough

A repeatable 3–5 minute script. Start the app (see README), open http://127.0.0.1:8000, and set an operator name in the top bar. To reset everything between runs, stop the server and delete `backend/data/orchestrator.db`.

If an LLM is configured, choose the LLM planner; otherwise the fixture planner is used and labelled as such. The fixture is a stand-in, not the LLM requirement, and it only accepts T-101 with repairs allowed.

## 1. Planning (about 45 s)

1. Keep the default work order about tank T-101, with target **Storage Tank T-101** and **Allow repairs** ticked, and click **Generate plan**.
2. Point out the plan source next to the version ("LLM planner" or "fixture planner"), and the planner call in the timeline. For the LLM, expand it to show the raw output and latency.
3. Click **Edit plan**, delete the `condition` from the repair task, and click **Validate and save**. The backend rejects it: a repair must be conditioned on `defect_detected`. Click **Reset**.
4. Note that the plan has no robot or approval fields: routing and approval belong to the orchestrator.
5. Optional, with the LLM planner: click the **Survey only, no repairs** example. It sets the target to T-102 and clears **Allow repairs**. The plan is a single inspection of T-102. A plan for another asset, or one with a repair, would be rejected and sent back to the model.

## 2. Execution, failure, and recovery (about 90 s)

1. Click **Start mission**. Scout A is chosen for the thermal inspection: it has the required capabilities and the highest routing preference.
2. While t1 is running, click **Take offline** on Scout A in the Fleet panel.
3. Show the result: attempt 1 is `lost` ("outcome unknown"), the inspection is retry-safe, and attempt 2 goes to Scout B. Both attempts stay in the task row, and the mission is the same mission.
4. Optional: click **Bring online** on Scout A afterwards.

Variation (stale result): before starting, click **Time out next** on Scout A. Attempt 1 times out, Scout A is marked unresponsive, B takes over, and Scout A's late result later appears in the timeline as "stale result ignored" without changing state.

Variation (silent failure): instead of **Take offline**, click **Lose silently** on Scout A while it inspects. For about 3 seconds it still looks busy and the panel notes "the orchestrator has not been told yet". Then the heartbeat lease expires: Scout A shows **unreachable** ("no heartbeat for 3s"), not offline, because it may still be acting. Its attempt becomes `lost`, and Scout B takes over.

Variation (no replacement): take Scout B offline before starting, then Scout A while t1 runs. The banner shows "Waiting for an eligible robot" with a reason for every robot. Bring Scout B online and the mission continues.

## 3. Approval (about 60 s)

1. After the visual inspection, the mission pauses with "Waiting for your approval". The card shows the evidence the request is based on (thermal anomaly, ΔT 18.4 °C).
2. Show the policy decisions in the timeline: each dispatch lists its checks.
3. Click **Approve repair**. Manipulator C repairs, a Scout verifies, and the mission closes as **resolved**.

## 4. Denial (about 30 s)

1. Tick **Simulated defect present on T-101** in the Fleet panel. The first mission repaired the simulated tank; without this the new inspection finds no defect and the mission closes as "no action needed".
2. Submit a second work order and start it.
3. At the approval card, click **Reject** with a comment. No repair command is sent, verification is skipped, and the mission closes as **unresolved**, saying that the defect remains open.

## 5. Wrap-up (about 30 s)

- Refresh the page: everything is read back from the backend.
- The metrics panel shows retries, reassignments, time to recover, and ignored stale/duplicate events.
- Optional restart demo: stop the backend while a mission waits for approval, start it again, and approve. The mission resumes from storage. A restart after the repair also works: the simulated tank stays repaired, and verification passes.
