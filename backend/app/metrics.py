"""Operational metrics derived from persisted attempts and audit events."""

from collections import Counter

from .engine import TERMINAL
from .store import Store

OPERATOR_EVENTS = ("approval_granted", "approval_rejected", "task_retry_requested", "mission_cancelled", "plan_edited")


def compute_metrics(store: Store) -> dict:
    missions = store.missions()
    by_status = Counter(m["status"] for m in missions)
    closed = [m for m in missions if m["status"] in TERMINAL]
    resolved = by_status.get("resolved", 0)

    attempts = [a for m in missions for a in store.attempts(m["id"])]
    by_task: dict[tuple[str, str], list[dict]] = {}
    for a in attempts:
        by_task.setdefault((a["mission_id"], a["task_id"]), []).append(a)

    retries = reassignments = 0
    recovery_times = []
    for task_attempts in by_task.values():
        task_attempts.sort(key=lambda a: a["attempt_no"])
        for prev, cur in zip(task_attempts, task_attempts[1:]):
            retries += 1
            if cur["robot_id"] != prev["robot_id"]:
                reassignments += 1
        first_failure = next((a for a in task_attempts if a["status"] not in ("succeeded", "running")), None)
        success = next((a for a in task_attempts if a["status"] == "succeeded"), None)
        if first_failure and success and first_failure["ended_at"] and success["ended_at"]:
            recovery_times.append(success["ended_at"] - first_failure["ended_at"])

    operator_actions = len(store.events(kinds=OPERATOR_EVENTS))
    escalations = len(store.events(kinds=("escalated",)))
    durations = [m["updated_at"] - m["created_at"] for m in closed]

    return {
        "missions_total": len(missions),
        "missions_by_status": dict(by_status),
        "completion_rate": round(resolved / len(closed), 3) if closed else None,
        "mean_mission_duration_s": round(sum(durations) / len(durations), 2) if durations else None,
        "attempts_total": len(attempts),
        "attempts_by_status": dict(Counter(a["status"] for a in attempts)),
        "attempts_by_robot_type": dict(Counter(a["robot_type"] for a in attempts)),
        "retries": retries,
        "reassignments": reassignments,
        "escalations": escalations,
        "operator_actions": operator_actions,
        "mean_time_to_recover_s": round(sum(recovery_times) / len(recovery_times), 2) if recovery_times else None,
        "duplicate_events_ignored": len(store.events(kinds=("duplicate_event_ignored",))),
        "stale_events_ignored": len(store.events(kinds=("stale_result_ignored", "stale_event_ignored"))),
    }
