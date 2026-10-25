"""Mission execution engine.

All state changes run under one lock and one database transaction, so state
and audit records stay consistent. Commands to robots are collected in an
outbox and sent only after the transaction commits.

Threading: the engine belongs to the event loop it was started on. Calls from
another thread or loop are rejected instead of racing; the store is separately
thread-safe for reads.

State model (see docs/DESIGN.md for the full transition table):

    mission: planning -> ready | planning_failed -> running <-> waiting_approval
             | waiting_resource | needs_intervention -> resolved | unresolved
             | no_action_needed | cancelled
    task:    pending -> awaiting_approval -> pending -> running -> succeeded
             pending -> waiting_resource -> running
             running -> pending (safe retry) | failed | needs_intervention
             pending -> skipped | rejected | cancelled
    attempt: running -> succeeded | failed | timed_out | lost
             running -> cancel_requested -> cancelled | cancel_unconfirmed
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from .catalog import ACTIONS, ASSETS, Fleet
from .plan import Constraints, Plan, validate_plan
from .planner import Planner, PlanningOutcome
from .robots import HEARTBEAT_INTERVAL_S, HEARTBEAT_LEASE_S, Command, DispatchError, RobotEvent, RobotPort
from .store import Store, new_id

log = logging.getLogger(__name__)

PRE_START = {"ready", "planning_failed"}
ACTIVE = {"running", "waiting_approval", "waiting_resource", "needs_intervention"}
TERMINAL = {"resolved", "unresolved", "no_action_needed", "cancelled"}
TERMINAL_TASK = {"succeeded", "skipped", "rejected", "cancelled"}
OPEN_ATTEMPT = {"running", "cancel_requested"}
# Up to this many capable robots, routing reasons name each robot; above it they are counted per type.
SMALL_FLEET = 5


class EngineError(Exception):
    status_code = 409


class NotFound(EngineError):
    status_code = 404


class InvalidRequest(EngineError):
    status_code = 422


def action_hash(action: str, args: dict) -> str:
    return hashlib.sha256(json.dumps({"action": action, "args": args}, sort_keys=True).encode()).hexdigest()[:16]


@dataclass
class _Outbox:
    kind: str  # "dispatch" | "cancel"
    attempt_id: str
    command: Command | None = None
    robot_id: str | None = None


class Engine:
    def __init__(
        self,
        store: Store,
        port: RobotPort,
        fleet: Fleet,
        planners: dict[str, Planner],
        time_scale: float = 1.0,
        heartbeat_lease_s: float = HEARTBEAT_LEASE_S,
    ):
        self.store = store
        self.fleet = fleet
        self.heartbeat_lease_s = heartbeat_lease_s
        self.port = port
        self.planners = planners
        self.time_scale = time_scale
        self.lock = asyncio.Lock()
        self.queue: asyncio.Queue[RobotEvent] = asyncio.Queue()
        self.subscribers: set[asyncio.Queue] = set()
        self._outbox: list[_Outbox] = []
        self._timers: dict[str, asyncio.Task] = {}
        self._worker: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()
        # Liveness is tracked in memory: heartbeats are too frequent to write to the database.
        self._last_seen: dict[str, float] = {}
        self._expired: set[str] = set()
        self._monitor: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Set when a robot may have become available; waiting missions are re-checked before commit.
        self._robots_freed = False

    # Lifecycle

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self._locked(self._recover)
        self._worker = asyncio.create_task(self._consume_events())
        now = time.monotonic()
        self._last_seen = {rid: now for rid in self.fleet.robots}  # grace period after start
        self._monitor = asyncio.create_task(self._watch_heartbeats())

    async def stop(self) -> None:
        # Unfinished planning is marked failed on the next start (see _recover).
        for t in [self._worker, self._monitor, *self._timers.values(), *self._background]:
            if t:
                t.cancel()
        self._timers.clear()

    async def enqueue(self, event: RobotEvent) -> None:
        await self.queue.put(event)

    async def _consume_events(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                await self._locked(self._handle_robot_event, event)
            except Exception:
                log.exception("failed to process robot event %s", event)

    async def _locked(self, fn: Callable, *args: Any) -> Any:
        if asyncio.get_running_loop() is not self._loop:
            # asyncio.Lock only serializes callers on one loop; another thread would race.
            raise RuntimeError("engine commands must run on the event loop the engine was started on")
        async with self.lock:
            result = self._in_tx(fn, *args)
            await self._flush_outbox()
        self._notify()
        return result

    def _in_tx(self, fn: Callable, *args: Any) -> Any:
        with self.store.tx():
            result = fn(*args)
            if self._robots_freed:
                self._robots_freed = False
                # Only missions blocked on a robot need another look.
                for mission_id in self.store.missions_waiting_for_robots():
                    self._advance(mission_id)
        return result

    async def _flush_outbox(self) -> None:
        while self._outbox:
            item = self._outbox.pop(0)
            if item.kind == "dispatch":
                try:
                    await self.port.dispatch(item.command)
                except DispatchError as exc:
                    self._in_tx(self._on_dispatch_error, item.attempt_id, str(exc))
                    continue
                attempt = self.store.attempt(item.attempt_id)
                self._start_timer(item.attempt_id, attempt["deadline_at"])
            elif item.kind == "cancel":
                await self.port.cancel(item.robot_id, item.attempt_id)

    # Liveness

    def heartbeat(self, robot_ids: list[str]) -> None:
        """Record a batch heartbeat from the integration layer."""
        now = time.monotonic()
        for rid in robot_ids:
            self._last_seen[rid] = now
            if rid in self._expired:
                self._expired.discard(rid)
                self._spawn(self._locked(self._on_heartbeat_resumed, rid))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _watch_heartbeats(self) -> None:
        interval = HEARTBEAT_INTERVAL_S * self.time_scale
        lease = self.heartbeat_lease_s * self.time_scale
        last_check = time.monotonic()
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            if now - last_check > lease:
                # This loop itself was stalled, so heartbeats were delayed too. Restart the clock
                # instead of declaring robots dead because of our own pause.
                self._last_seen = {rid: now for rid in self._last_seen}
            last_check = now
            stale = [rid for rid, seen in self._last_seen.items() if now - seen > lease and rid not in self._expired]
            if stale:
                self._expired.update(stale)
                await self._locked(self._on_heartbeat_lost, stale)

    def _on_heartbeat_lost(self, robot_ids: list[str]) -> None:
        rows = self.store.robot_map()
        for rid in robot_ids:
            if rows[rid]["online"]:  # already offline if it announced it
                self._on_robot_status(rid, False, reason=f"no heartbeat for {self.heartbeat_lease_s:g}s")

    def _on_heartbeat_resumed(self, robot_id: str) -> None:
        if not self.store.robot(robot_id)["online"]:
            self._on_robot_status(robot_id, True, reason="heartbeats resumed")

    def _start_timer(self, attempt_id: str, deadline: float) -> None:
        async def fire() -> None:
            await asyncio.sleep(max(0.0, deadline - time.time()))
            self._timers.pop(attempt_id, None)
            await self._locked(self._on_timeout, attempt_id)

        self._timers[attempt_id] = asyncio.create_task(fire())

    def _cancel_timer(self, attempt_id: str) -> None:
        timer = self._timers.pop(attempt_id, None)
        if timer and timer is not asyncio.current_task():
            timer.cancel()

    def _notify(self) -> None:
        for q in list(self.subscribers):
            q.put_nowait({"type": "changed", "ts": time.time()})

    def _event(self, mission_id: str | None, kind: str, message: str, **data: Any) -> None:
        self.store.add_event(mission_id, kind, message, data)

    # Operator commands

    async def create_mission(
        self, work_order: str, planner_mode: str, actor: str, *, target: str, allow_repair: bool
    ) -> str:
        """Create a mission and wait for its plan."""
        constraints = Constraints(target=target, allow_repair=allow_repair)
        mission_id = await self._record_mission(work_order, planner_mode, actor, constraints)
        await self._plan_mission(mission_id, work_order, planner_mode, constraints)
        return mission_id

    async def submit_mission(
        self, work_order: str, planner_mode: str, actor: str, *, target: str, allow_repair: bool
    ) -> str:
        """Create a mission and plan it in the background; the mission starts in "planning"."""
        constraints = Constraints(target=target, allow_repair=allow_repair)
        mission_id = await self._record_mission(work_order, planner_mode, actor, constraints)
        task = asyncio.create_task(self._plan_mission(mission_id, work_order, planner_mode, constraints))
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return mission_id

    async def _record_mission(self, work_order: str, planner_mode: str, actor: str, constraints: Constraints) -> str:
        planner = self.planners.get(planner_mode)
        if planner is None:
            raise EngineError(f"planner mode '{planner_mode}' is not configured; available: {sorted(self.planners)}")
        if constraints.target not in ASSETS:
            raise InvalidRequest(f"unknown target asset '{constraints.target}'; known assets: {', '.join(ASSETS)}")
        mission_id = new_id("M")

        def create() -> None:
            now = time.time()
            self.store.insert_mission(
                {
                    "id": mission_id,
                    "work_order": work_order,
                    "target": constraints.target,
                    "allow_repair": int(constraints.allow_repair),
                    "status": "planning",
                    "planner_mode": planner_mode,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            self._event(
                mission_id,
                "mission_created",
                f"Work order submitted by {actor} ({constraints.describe()})",
                work_order=work_order,
                target=constraints.target,
                allow_repair=constraints.allow_repair,
            )
            self._event(mission_id, "planning_started", f"Planning with {planner.label}", planner_mode=planner_mode)

        await self._locked(create)
        return mission_id

    async def _plan_mission(
        self, mission_id: str, work_order: str, planner_mode: str, constraints: Constraints
    ) -> None:
        planner = self.planners[planner_mode]
        try:
            outcome = await planner.plan(work_order, constraints)
        except Exception as exc:
            # A planner bug must not leave the mission stuck in "planning".
            log.exception("planner %s failed", planner_mode)
            outcome = PlanningOutcome(
                plan=None,
                error=f"planner error: {exc}",
                calls=[{"summary": f"Planner raised {type(exc).__name__}: {exc}", "outcome": "error"}],
            )

        def finish() -> None:
            for call in outcome.calls:
                self._event(mission_id, "planner_call", call["summary"], **call)
            if outcome.plan is None:
                self.store.update_mission(mission_id, status="planning_failed", status_reason=outcome.error or "")
                self._event(mission_id, "planning_failed", f"Planning failed: {outcome.error}")
                return
            version = self.store.add_plan_version(mission_id, outcome.plan.model_dump(), planner_mode, planner.label)
            self.store.update_mission(
                mission_id,
                status="ready",
                status_reason="Plan validated; review it and start the mission",
                plan_version=version,
            )
            self._event(mission_id, "plan_validated", f"Plan v{version} produced by {planner.label} passed validation")

        await self._locked(finish)

    async def update_plan(self, mission_id: str, plan_data: Any, actor: str) -> int:
        def update() -> int:
            mission = self._mission(mission_id)
            if mission["status"] not in PRE_START:
                raise EngineError(f"plan can only be edited before start (mission is {mission['status']})")
            plan = validate_plan(plan_data, self.fleet, self._constraints(mission))
            version = self.store.add_plan_version(mission_id, plan.model_dump(), "operator", actor)
            self.store.update_mission(
                mission_id,
                status="ready",
                status_reason="Edited plan validated; review it and start the mission",
                plan_version=version,
            )
            self._event(mission_id, "plan_edited", f"Plan v{version} edited by {actor} and validated")
            return version

        return await self._locked(update)

    async def start_mission(self, mission_id: str, actor: str) -> None:
        def start() -> None:
            mission = self._mission(mission_id)
            if mission["status"] != "ready":
                raise EngineError(f"mission cannot start from status {mission['status']}")
            version = mission["plan_version"]
            plan = validate_plan(self.store.plan_version(mission_id, version)["plan"], self.fleet, self._constraints(mission))
            self._create_tasks(mission_id, version, plan)
            self.store.update_mission(mission_id, status="running", status_reason="")
            self._event(mission_id, "mission_started", f"Mission started by {actor} with plan v{version}")
            self._advance(mission_id)

        await self._locked(start)

    async def decide_approval(self, mission_id: str, task_id: str, decision: str, actor: str, comment: str) -> None:
        if decision not in ("approve", "reject"):
            raise EngineError("decision must be 'approve' or 'reject'")

        def decide() -> None:
            self._mission(mission_id)
            task = self._task(mission_id, task_id)
            if task["status"] != "awaiting_approval":
                raise EngineError(f"task {task_id} is not awaiting approval (status {task['status']})")
            self.store.insert_approval(
                {
                    "id": new_id("ap"),
                    "mission_id": mission_id,
                    "task_id": task_id,
                    "plan_version": task["plan_version"],
                    "action_hash": action_hash(task["action"], task["args"]),
                    "decision": decision,
                    "actor": actor,
                    "comment": comment,
                    "created_at": time.time(),
                }
            )
            note = f": {comment}" if comment else ""
            if decision == "approve":
                self.store.update_task(mission_id, task_id, status="pending", status_reason=f"Approved by {actor}")
                self._event(mission_id, "approval_granted", f"{actor} approved {task['action']} ({task_id}){note}")
            else:
                self.store.update_task(
                    mission_id, task_id, status="rejected", status_reason=f"Rejected by {actor}{note}"
                )
                self._event(mission_id, "approval_rejected", f"{actor} rejected {task['action']} ({task_id}){note}")
            self._advance(mission_id)

        await self._locked(decide)

    async def retry_task(self, mission_id: str, task_id: str, actor: str) -> None:
        def retry() -> None:
            mission = self._mission(mission_id)
            if mission["status"] not in ACTIVE:
                raise EngineError(f"mission is {mission['status']}")
            task = self._task(mission_id, task_id)
            if task["status"] not in ("failed", "needs_intervention"):
                raise EngineError(f"task {task_id} cannot be retried from status {task['status']}")
            spec = ACTIONS[task["action"]]
            reason = f"Retry requested by {actor}"
            if spec.requires_approval:
                reason += "; a new approval is required"
            self.store.update_task(
                mission_id, task_id, status="pending", status_reason=reason, budget_base=task["attempt_count"]
            )
            self._event(mission_id, "task_retry_requested", f"{actor} requested retry of {task_id} ({task['action']})")
            self._advance(mission_id)

        await self._locked(retry)

    async def cancel_mission(self, mission_id: str, actor: str) -> None:
        def cancel() -> None:
            mission = self._mission(mission_id)
            if mission["status"] in TERMINAL:
                raise EngineError(f"mission is already {mission['status']}")
            for task in self.store.tasks(mission_id):
                if task["status"] not in TERMINAL_TASK:
                    self.store.update_task(
                        mission_id, task["task_id"], status="cancelled", status_reason=f"Mission cancelled by {actor}"
                    )
            for attempt in self.store.attempts(mission_id):
                if attempt["status"] == "running":
                    # Keep the reservation until the robot confirms it stopped.
                    self.store.update_attempt(attempt["id"], status="cancel_requested")
                    self._outbox.append(_Outbox("cancel", attempt["id"], robot_id=attempt["robot_id"]))
                    self._event(
                        mission_id,
                        "cancel_requested",
                        f"Cancellation sent to {self.fleet.name(attempt['robot_id'])}; awaiting confirmation",
                    )
            self.store.update_mission(mission_id, status="cancelled", status_reason=f"Cancelled by {actor}")
            self._event(mission_id, "mission_cancelled", f"Mission cancelled by {actor}")

        await self._locked(cancel)

    async def restore_robot(self, robot_id: str, actor: str) -> None:
        """Operator asserts a robot is healthy again after it was marked unresponsive."""

        def restore() -> None:
            robot = self._robot(robot_id)
            if robot["health"] != "ok":
                self.store.update_robot(robot_id, health="ok", health_reason="")
                self._event(None, "robot_status", f"{self.fleet.name(robot_id)} marked healthy by {actor}", robot_id=robot_id)
                self._robots_freed = True

        await self._locked(restore)

    # Robot events

    def _handle_robot_event(self, ev: RobotEvent) -> None:
        attempt = self.store.attempt(ev.command_id) if ev.command_id else None
        mission_id = attempt["mission_id"] if attempt else None
        if not self.store.record_inbox(ev.event_id):
            self._event(
                mission_id,
                "duplicate_event_ignored",
                f"Ignored duplicate '{ev.type}' event from {self.fleet.name(ev.robot_id)}",
                event_id=ev.event_id,
                attempt_id=ev.command_id,
            )
            return

        if ev.type == "status":
            self._on_robot_status(ev.robot_id, bool(ev.payload["online"]))
            return
        if attempt is None:
            self._event(None, "unknown_command_event", f"Event for unknown command {ev.command_id}", event_id=ev.event_id)
            return

        label = f"{self.fleet.name(ev.robot_id)} attempt {attempt['attempt_no']} of {attempt['task_id']}"
        if ev.type == "accepted":
            if attempt["status"] == "running":
                self._event(mission_id, "command_accepted", f"{label}: command accepted", attempt_id=attempt["id"])
            else:
                self._event(
                    mission_id,
                    "stale_event_ignored",
                    f"Ignored out-of-order 'accepted' from {label} (attempt already {attempt['status']})",
                    attempt_id=attempt["id"],
                )
            return

        if ev.type == "cancelled":
            if attempt["status"] == "cancel_requested":
                self._close_attempt(attempt, "cancelled", "Robot confirmed cancellation")
                self._event(mission_id, "cancel_confirmed", f"{label}: cancellation confirmed")
            return

        if ev.type not in ("completed", "failed"):
            self._event(mission_id, "unknown_event_ignored", f"Ignored unsupported event type '{ev.type}'")
            return

        if attempt["status"] != "running":
            self._on_stale_result(attempt, ev, label)
            return
        if ev.type == "completed":
            self._attempt_succeeded(attempt, ev.payload.get("finding"), ev.payload.get("data", {}))
        else:
            self._attempt_failed(attempt, "failed", ev.payload.get("error", "action failed"), outcome_known=True)

    def _on_robot_status(self, robot_id: str, online: bool, reason: str | None = None) -> None:
        """Announced status events and heartbeat-lease expiry both arrive here.

        A lease expiry (reason given) means "unreachable": the robot stopped reporting and may
        still be acting. Only an announced change is reported as offline.
        """
        name = self.fleet.name(robot_id)
        why = f" ({reason})" if reason else ""
        if online:
            # Reporting in is proof of life: restart its lease, or the monitor could judge it by the
            # last heartbeat from before it went offline and mark it unreachable straight away.
            self._last_seen[robot_id] = time.monotonic()
            self._expired.discard(robot_id)
            was_unreachable = self.store.robot(robot_id)["health"] == "unreachable"
            self.store.update_robot(robot_id, online=1, health="ok", health_reason="")
            state = "is reachable again" if was_unreachable else "is back online"
            self._event(None, "robot_status", f"{name} {state}{why}", robot_id=robot_id, online=True)
        elif reason:
            self.store.update_robot(robot_id, online=0, health="unreachable", health_reason=reason)
            self._event(None, "robot_status", f"{name} is unreachable{why}", robot_id=robot_id, online=False)
        else:
            self.store.update_robot(robot_id, online=0, health="ok", health_reason="")
            self._event(None, "robot_status", f"{name} went offline", robot_id=robot_id, online=False)
        if not online:
            lost = f"{name} stopped sending heartbeats during the action" if reason else f"{name} went offline during the action"
            for attempt in self.store.open_attempts():
                if attempt["robot_id"] != robot_id:
                    continue
                if attempt["status"] == "cancel_requested":
                    self._close_attempt(attempt, "cancel_unconfirmed", "Robot went offline before confirming")
                else:
                    self._attempt_failed(attempt, "lost", f"{lost}; outcome unknown", outcome_known=False)
        self._robots_freed = True

    def _on_stale_result(self, attempt: dict, ev: RobotEvent, label: str) -> None:
        mission_id = attempt["mission_id"]
        self._event(
            mission_id,
            "stale_result_ignored",
            f"Ignored late '{ev.type}' from {label}: attempt is already {attempt['status']}; current state unchanged",
            attempt_id=attempt["id"],
            payload=ev.payload,
        )
        if attempt["status"] == "cancel_requested":
            self._close_attempt(attempt, "cancelled", f"Result '{ev.type}' arrived after the cancellation request")
        robot = self.store.robot(ev.robot_id)
        if robot["health"] == "unresponsive" and attempt["id"] in robot["health_reason"]:
            self.store.update_robot(ev.robot_id, health="ok", health_reason="")
            self._event(None, "robot_status", f"{self.fleet.name(ev.robot_id)} is responsive again", robot_id=ev.robot_id)
            self._robots_freed = True

    def _on_timeout(self, attempt_id: str) -> None:
        attempt = self.store.attempt(attempt_id)
        if attempt is None or attempt["status"] not in OPEN_ATTEMPT:
            return
        robot_id = attempt["robot_id"]
        # The robot may still be acting. Keep it out of routing until it responds
        # or an operator restores it.
        self.store.update_robot(
            robot_id, health="unresponsive", health_reason=f"no response to command {attempt_id}"
        )
        self._event(
            None, "robot_status", f"{self.fleet.name(robot_id)} marked unresponsive after a timeout", robot_id=robot_id
        )
        if attempt["status"] == "cancel_requested":
            self._close_attempt(attempt, "cancel_unconfirmed", "No cancellation confirmation before the deadline")
            return
        self._outbox.append(_Outbox("cancel", attempt_id, robot_id=robot_id))
        timeout = ACTIONS[self._task(attempt["mission_id"], attempt["task_id"])["action"]].timeout_s
        self._attempt_failed(
            attempt, "timed_out", f"no result within {timeout:g}s; outcome unknown", outcome_known=False
        )

    def _on_dispatch_error(self, attempt_id: str, error: str) -> None:
        attempt = self.store.attempt(attempt_id)
        if attempt and attempt["status"] == "running":
            self._attempt_failed(attempt, "failed", f"dispatch refused: {error}", outcome_known=True)

    # Attempt outcomes

    def _close_attempt(self, attempt: dict, status: str, detail: str) -> None:
        self.store.update_attempt(attempt["id"], status=status, detail=detail, ended_at=time.time())
        self._cancel_timer(attempt["id"])
        robot = self.store.robot(attempt["robot_id"])
        if robot["reserved_attempt"] == attempt["id"]:
            self.store.update_robot(attempt["robot_id"], reserved_attempt=None)
            self._robots_freed = True

    def _attempt_succeeded(self, attempt: dict, finding: str | None, data: dict) -> None:
        mission_id, task_id = attempt["mission_id"], attempt["task_id"]
        self._close_attempt(attempt, "succeeded", finding or "")
        self.store.insert_evidence(
            {
                "id": new_id("evd"),
                "mission_id": mission_id,
                "task_id": task_id,
                "attempt_id": attempt["id"],
                "robot_id": attempt["robot_id"],
                "finding": finding,
                "data": data,
                "provenance": f"simulated by {self.fleet.name(attempt['robot_id'])}, command {attempt['id']}",
                "created_at": time.time(),
            }
        )
        task = self._task(mission_id, task_id)
        self.store.update_task(
            mission_id, task_id, status="succeeded", finding=finding, status_reason=f"Finding: {finding}"
        )
        self._event(
            mission_id,
            "task_succeeded",
            f"{self.fleet.name(attempt['robot_id'])} completed {task['action']} ({task_id}): {finding}",
            attempt_id=attempt["id"],
            finding=finding,
            data=data,
        )
        self._advance(mission_id)

    def _attempt_failed(self, attempt: dict, status: str, reason: str, outcome_known: bool) -> None:
        mission_id, task_id = attempt["mission_id"], attempt["task_id"]
        self._close_attempt(attempt, status, reason)
        task = self._task(mission_id, task_id)
        spec = ACTIONS[task["action"]]
        name = self.fleet.name(attempt["robot_id"])
        self._event(
            mission_id,
            "attempt_failed",
            f"{name} attempt {attempt['attempt_no']} of {task_id} {status.replace('_', ' ')}: {reason}",
            attempt_id=attempt["id"],
            status=status,
        )
        if task["status"] != "running":
            return  # Task was cancelled while the attempt was open.
        used = task["attempt_count"] - task["budget_base"]
        if spec.retry_safe and used < spec.max_attempts:
            self.store.update_task(
                mission_id,
                task_id,
                status="pending",
                status_reason=f"Attempt {attempt['attempt_no']} {status.replace('_', ' ')}; retrying ({task['action']} is safe to repeat)",
            )
            self._event(mission_id, "task_retry_scheduled", f"{task_id} will be retried, preferring another eligible robot")
        elif spec.retry_safe:
            self.store.update_task(
                mission_id,
                task_id,
                status="failed",
                status_reason=f"All {spec.max_attempts} attempts failed; operator decision required",
            )
            self._event(mission_id, "escalated", f"{task_id} exhausted its retry budget; escalated to the operator")
        elif outcome_known:
            self.store.update_task(
                mission_id,
                task_id,
                status="failed",
                status_reason=f"{reason}. Retrying requires a new approval",
            )
            self._event(mission_id, "escalated", f"{task['action']} ({task_id}) failed; escalated to the operator")
        else:
            self.store.update_task(
                mission_id,
                task_id,
                status="needs_intervention",
                status_reason=(
                    f"{reason}. {task['action']} is not safe to repeat blindly; "
                    "confirm the physical state before retrying"
                ),
            )
            self._event(
                mission_id,
                "escalated",
                f"{task['action']} ({task_id}) outcome unknown; not redispatched automatically",
            )
        self._advance(mission_id)

    # Scheduling

    def _create_tasks(self, mission_id: str, version: int, plan: Plan) -> None:
        for position, t in enumerate(plan.tasks):
            self.store.insert_task(
                {
                    "mission_id": mission_id,
                    "task_id": t.id,
                    "position": position,
                    "plan_version": version,
                    "action": t.action,
                    "args": t.args,
                    "depends_on": t.depends_on,
                    "condition": t.condition.model_dump() if t.condition else None,
                    "rationale": t.rationale,
                    "status": "pending",
                }
            )

    def _advance_all(self) -> None:
        for mission in self.store.missions()[::-1]:  # oldest first
            if mission["status"] in ACTIVE:
                self._advance(mission["id"])

    def _advance(self, mission_id: str) -> None:
        mission = self.store.mission(mission_id)
        if mission["status"] not in ACTIVE:
            return
        changed = True
        while changed:
            changed = False
            tasks = {t["task_id"]: t for t in self.store.tasks(mission_id)}
            for t in tasks.values():
                if t["status"] not in ("pending", "waiting_resource"):
                    continue
                deps = [tasks[d] for d in t["depends_on"]]
                closed = [d for d in deps if d["status"] in ("skipped", "rejected", "cancelled")]
                if closed:
                    d = closed[0]
                    self._skip(mission_id, t, f"dependency {d['task_id']} was {d['status']}")
                    changed = True
                    continue
                if not all(d["status"] == "succeeded" for d in deps):
                    continue
                cond = t["condition"]
                if cond and tasks[cond["task"]]["finding"] != cond["finding"]:
                    got = tasks[cond["task"]]["finding"]
                    self._skip(mission_id, t, f"condition not met: {cond['task']} reported {got}, not {cond['finding']}")
                    changed = True
                    continue
                spec = ACTIONS[t["action"]]
                if spec.requires_approval and self._approval_for(mission, t) is None:
                    self._request_approval(mission_id, t, tasks)
                    changed = True
                    continue
                changed = self._try_dispatch(mission, t) or changed
        self._refresh_mission(mission_id)

    def _skip(self, mission_id: str, task: dict, reason: str) -> None:
        self.store.update_task(mission_id, task["task_id"], status="skipped", status_reason=reason[0].upper() + reason[1:])
        self._event(mission_id, "task_skipped", f"{task['task_id']} ({task['action']}) skipped: {reason}")

    def _request_approval(self, mission_id: str, task: dict, tasks: dict) -> None:
        cond = task["condition"]
        basis = ""
        if cond:
            src = tasks[cond["task"]]
            basis = f" based on {src['task_id']} finding '{src['finding']}'"
        self.store.update_task(
            mission_id,
            task["task_id"],
            status="awaiting_approval",
            status_reason=f"High-risk action; operator approval required{basis}",
        )
        self._event(
            mission_id,
            "approval_requested",
            f"Approval required for {task['action']} of {task['args'].get('target')} ({task['task_id']}){basis}",
            task_id=task["task_id"],
            args=task["args"],
        )

    def _approval_for(self, mission: dict, task: dict) -> dict | None:
        approval = self.store.unconsumed_approval(mission["id"], task["task_id"])
        if approval is None:
            return None
        if approval["plan_version"] != task["plan_version"] or task["plan_version"] != mission["plan_version"]:
            return None
        if approval["action_hash"] != action_hash(task["action"], task["args"]):
            return None
        return approval

    def _route(self, mission_id: str, task: dict) -> tuple[str | None, list[str]]:
        """Pick an eligible robot by capability and availability; explain why others were passed over.

        Capability is a property of the robot type, so the database is asked only for available
        robots of capable types. Reasons name each robot in small fleets and are counted per type
        in large ones.
        """
        capable = self.fleet.capable(task["action"])
        capable_types = sorted({r.type.id for r in capable})
        failed_here = {
            a["robot_id"] for a in self.store.task_attempts(mission_id, task["task_id"]) if a["status"] != "succeeded"
        }
        chosen = None
        if capable_types:
            available = [self.fleet.robots[rid] for rid in self.store.available_robot_ids(capable_types)]
            if available:
                # Prefer robots that have not already failed this task, then by rank.
                chosen = min(available, key=lambda r: (r.id in failed_here, r.preference_rank, r.id)).id

        reasons: list[str] = []
        if len(capable) <= SMALL_FLEET:
            rows = self.store.robot_map()
            for r in capable:
                row = rows[r.id]
                if not row["online"] and row["health"] == "unreachable":
                    reasons.append(f"{r.name} is unreachable ({row['health_reason']})")
                elif not row["online"]:
                    reasons.append(f"{r.name} is offline")
                elif row["health"] != "ok":
                    reasons.append(f"{r.name} is {row['health']} ({row['health_reason']})")
                elif row["reserved_attempt"]:
                    reasons.append(f"{r.name} is busy")
        elif capable_types:
            counts: dict[str, list[str]] = {}
            for row in self.store.unavailable_by_type(capable_types):
                counts.setdefault(row["type"], []).append(f"{row['n']} {row['why']}")
            reasons.extend(f"{self.fleet.types[t].name}: {', '.join(c)}" for t, c in counts.items())
        for t in self.fleet.types_lacking(task["action"]):
            missing = ACTIONS[task["action"]].required_capabilities - t.capabilities
            reasons.append(f"{t.name} type lacks {', '.join(sorted(missing))}")
        if not capable:
            reasons.append("no robot in the fleet has the required capabilities")
        return chosen, reasons

    def _policy_check(self, mission: dict, task: dict, robot_id: str) -> tuple[bool, list[dict]]:
        spec = ACTIONS[task["action"]]
        robot = self.store.robot(robot_id)
        robot_spec = self.fleet.robots[robot_id]
        checks = [
            {
                "rule": "capability",
                "passed": spec.required_capabilities <= robot_spec.capabilities,
                "detail": f"{robot_spec.type.name} provides {', '.join(sorted(spec.required_capabilities))}",
            },
            {
                "rule": "availability",
                "passed": bool(robot["online"]) and robot["health"] == "ok" and not robot["reserved_attempt"],
                "detail": f"{self.fleet.name(robot_id)} online, healthy, unreserved",
            },
            {
                "rule": "operator_target",
                "passed": task["args"].get("target") == mission["target"],
                "detail": f"target {task['args'].get('target')}, operator chose {mission['target']}",
            },
        ]
        if spec.role == "intervention":
            checks.append(
                {
                    "rule": "repairs_allowed",
                    "passed": bool(mission["allow_repair"]),
                    "detail": "operator allowed repairs" if mission["allow_repair"] else "operator did not allow repairs",
                }
            )
        if spec.requires_approval:
            approval = self._approval_for(mission, task)
            checks.append(
                {
                    "rule": "operator_approval",
                    "passed": approval is not None,
                    "detail": (
                        f"approved by {approval['actor']} for plan v{approval['plan_version']}, unused"
                        if approval
                        else "no unused approval matching this plan version and action"
                    ),
                }
            )
        return all(c["passed"] for c in checks), checks

    def _try_dispatch(self, mission: dict, task: dict) -> bool:
        mission_id, task_id = mission["id"], task["task_id"]
        robot_id, reasons = self._route(mission_id, task)
        if robot_id is None:
            reason = "No eligible robot: " + "; ".join(reasons)
            if task["status"] == "waiting_resource" and task["status_reason"] == reason:
                return False
            self.store.update_task(mission_id, task_id, status="waiting_resource", status_reason=reason)
            self._event(mission_id, "waiting_resource", f"{task_id} ({task['action']}) waiting. {reason}")
            return True

        allowed, checks = self._policy_check(mission, task, robot_id)
        verdict = "allowed" if allowed else "blocked"
        self._event(
            mission_id,
            "policy_decision",
            f"Policy {verdict} {task['action']} ({task_id}) on {self.fleet.name(robot_id)}",
            task_id=task_id,
            robot_id=robot_id,
            allowed=allowed,
            checks=checks,
        )
        if not allowed:
            # Defensive: routing, validation, and approval checks above should prevent this.
            # A policy block does not clear by itself, so the operator decides.
            failed = ", ".join(c["rule"] for c in checks if not c["passed"])
            self.store.update_task(
                mission_id, task_id, status="needs_intervention", status_reason=f"Blocked by policy ({failed})"
            )
            return True

        attempt_no = task["attempt_count"] + 1
        attempt_id = new_id("cmd")
        now = time.time()
        timeout = ACTIONS[task["action"]].timeout_s
        self.store.insert_attempt(
            {
                "id": attempt_id,
                "mission_id": mission_id,
                "task_id": task_id,
                "attempt_no": attempt_no,
                "robot_id": robot_id,
                "robot_type": self.fleet.robots[robot_id].type.id,
                "status": "running",
                "started_at": now,
                "deadline_at": now + timeout * self.time_scale,
            }
        )
        self.store.update_robot(robot_id, reserved_attempt=attempt_id)
        approval = self._approval_for(mission, task) if ACTIONS[task["action"]].requires_approval else None
        if approval:
            self.store.update_approval(approval["id"], consumed_by=attempt_id)
        self.store.update_task(
            mission_id,
            task_id,
            status="running",
            attempt_count=attempt_no,
            status_reason=f"Running on {self.fleet.name(robot_id)} (attempt {attempt_no})",
        )
        self._event(
            mission_id,
            "task_dispatched",
            f"{task['action']} ({task_id}) dispatched to {self.fleet.name(robot_id)}"
            f" ({self.fleet.robots[robot_id].type.name}), attempt {attempt_no}",
            attempt_id=attempt_id,
            robot_id=robot_id,
            robot_type=self.fleet.robots[robot_id].type.id,
            passed_over=reasons,
        )
        self._outbox.append(
            _Outbox("dispatch", attempt_id, Command(attempt_id, robot_id, task["action"], dict(task["args"])))
        )
        return True

    def _refresh_mission(self, mission_id: str) -> None:
        mission = self.store.mission(mission_id)
        if mission["status"] not in ACTIVE:
            return
        tasks = self.store.tasks(mission_id)

        def describe(status: str) -> list[str]:
            return [f"{t['task_id']}: {t['status_reason']}" for t in tasks if t["status"] == status]

        if all(t["status"] in TERMINAL_TASK for t in tasks):
            status, reason = self._outcome(mission, tasks)
            self.store.update_mission(mission_id, status=status, status_reason=reason)
            self._event(mission_id, "mission_closed", f"Mission {status.replace('_', ' ')}: {reason}", outcome=status)
            return
        if blocked := describe("needs_intervention") + describe("failed"):
            status, reason = "needs_intervention", "; ".join(blocked)
        elif waiting := describe("awaiting_approval"):
            status, reason = "waiting_approval", "; ".join(waiting)
        elif running := describe("running"):
            status, reason = "running", "; ".join(running)
        elif waiting := describe("waiting_resource"):
            status, reason = "waiting_resource", "; ".join(waiting)
        else:
            status, reason = "running", ""
        if (status, reason) != (mission["status"], mission["status_reason"]):
            self.store.update_mission(mission_id, status=status, status_reason=reason)
            if status != mission["status"]:
                self._event(mission_id, "mission_status", f"Mission {status.replace('_', ' ')}", status=status)

    @staticmethod
    def _outcome(mission: dict, tasks: list[dict]) -> tuple[str, str]:
        target = mission["target"]
        rejected = [t for t in tasks if t["status"] == "rejected"]
        if rejected:
            return "unresolved", f"{rejected[0]['action']} was rejected by the operator; the defect on {target} remains open"
        verifications = [t for t in tasks if ACTIONS[t["action"]].role == "verification" and t["status"] == "succeeded"]
        if any(v["finding"] != ACTIONS[v["action"]].success_finding for v in verifications):
            return "unresolved", f"Verification found the defect on {target} persists"
        if verifications:
            return "resolved", f"Intervention on {target} applied and verified"
        if any(
            t["status"] == "succeeded" and ACTIONS[t["action"]].role == "inspection"
            and t["finding"] == ACTIONS[t["action"]].defect_finding
            for t in tasks
        ):
            return "unresolved", f"A defect was detected on {target} but no verified repair was completed"
        return "no_action_needed", f"Inspection of {target} found no defect"

    # Restart recovery

    def _recover(self) -> None:
        for robot in self.fleet.robots.values():
            status = self.port.status(robot.id)
            self.store.upsert_robot(robot.id, robot.type.id, status["online"], "ok")
            self.store.update_robot(robot.id, reserved_attempt=None)
        open_attempts = self.store.open_attempts()
        for attempt in open_attempts:
            self._event(
                attempt["mission_id"],
                "recovery",
                f"Backend restarted while attempt {attempt['attempt_no']} of {attempt['task_id']} was open",
                attempt_id=attempt["id"],
            )
            if attempt["status"] == "cancel_requested":
                self._close_attempt(attempt, "cancel_unconfirmed", "Backend restarted before confirmation")
            else:
                self._attempt_failed(attempt, "lost", "backend restarted; outcome unknown", outcome_known=False)
        for mission in self.store.missions():
            if mission["status"] == "planning":
                reason = "Backend restarted while planning; submit the work order again or enter a plan manually"
                self.store.update_mission(mission["id"], status="planning_failed", status_reason=reason)
                self._event(mission["id"], "planning_failed", reason)
            if mission["status"] in ACTIVE:
                self._event(mission["id"], "recovery", f"Mission recovered from storage in status {mission['status']}")
        self._advance_all()

    # Lookups

    def _mission(self, mission_id: str) -> dict:
        mission = self.store.mission(mission_id)
        if mission is None:
            raise NotFound(f"mission {mission_id} not found")
        return mission

    @staticmethod
    def _constraints(mission: dict) -> Constraints:
        return Constraints(target=mission["target"], allow_repair=bool(mission["allow_repair"]))

    def _task(self, mission_id: str, task_id: str) -> dict:
        task = self.store.task(mission_id, task_id)
        if task is None:
            raise NotFound(f"task {task_id} not found in mission {mission_id}")
        return task

    def _robot(self, robot_id: str) -> dict:
        robot = self.store.robot(robot_id)
        if robot is None:
            raise NotFound(f"robot {robot_id} not found")
        return robot

    # Read models

    def mission_detail(self, mission_id: str) -> dict:
        # One transaction gives a consistent snapshot even if read from another thread.
        with self.store.tx():
            return self._mission_detail(mission_id)

    def _mission_detail(self, mission_id: str) -> dict:
        mission = self._mission(mission_id)
        attempts = self.store.attempts(mission_id)
        tasks = self.store.tasks(mission_id)
        for t in tasks:
            spec = ACTIONS[t["action"]]
            t["requires_approval"] = spec.requires_approval
            t["risk"] = spec.risk
            t["role"] = spec.role
            t["attempts"] = [a for a in attempts if a["task_id"] == t["task_id"]]
        versions = self.store.plan_versions(mission_id)
        return {
            "mission": mission,
            "plan_versions": versions,
            "current_plan": next((v for v in versions if v["version"] == mission["plan_version"]), None),
            "tasks": tasks,
            "approvals": self.store.approvals(mission_id),
            "evidence": self.store.evidence(mission_id),
            "events": self.store.events(mission_id),
        }

    def robots_view(self) -> list[dict]:
        with self.store.tx():
            return self._robots_view()

    def _robots_view(self) -> list[dict]:
        rows = self.store.robot_map()
        out = []
        for spec in self.fleet.robots.values():
            row = rows[spec.id]
            reservation = self.store.attempt(row["reserved_attempt"]) if row["reserved_attempt"] else None
            if not row["online"]:
                availability = "unreachable" if row["health"] == "unreachable" else "offline"
            elif row["health"] != "ok":
                availability = row["health"]
            elif reservation:
                availability = "busy"
            else:
                availability = "available"
            out.append(
                {
                    "id": spec.id,
                    "name": spec.name,
                    "type": spec.type.id,
                    "type_name": spec.type.name,
                    "capabilities": sorted(spec.capabilities),
                    "availability": availability,
                    "health_reason": row["health_reason"],
                    "reservation": reservation and {
                        "attempt_id": reservation["id"],
                        "mission_id": reservation["mission_id"],
                        "task_id": reservation["task_id"],
                    },
                }
            )
        return out
