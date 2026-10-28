"""Simulated fleet adapter.

Receives real commands from the engine and reports results through the same
event path a real integration would use. Faults are injected explicitly by the
operator (or tests); nothing here decides mission flow.

Each business command is expanded into a fixed recipe of mock robot operations
(mock_robot.py) over a configured route: navigate, confirm arrival, then capture
or run the procedure. Progress events come from those operations' state changes,
and the final result is computed from what was actually captured.
"""

import asyncio
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .catalog import ACTIONS, ASSETS, Fleet
from .mock_robot import (
    CAMERA_SOURCE,
    DEFAULT_ROUTES_FILE,
    THERMAL_SOURCE,
    CaptureRequest,
    MockRejected,
    MockRobotAPI,
    NavigateRequest,
    NavigationFeedback,
    ProcedureRequest,
    SiteRoutes,
)
from .robots import HEARTBEAT_INTERVAL_S, Command, DispatchError, EventSink, HeartbeatSink, RobotEvent

# offline/online change availability; the rest apply to the robot's next command.
NEXT_COMMAND_FAULTS = ("fail_next", "hang_next", "duplicate_next", "reorder_next")

# Simulated world data, kept apart from the route topology: where each tank's defect is
# when one is present, and the thermal readings the mock payload reports.
DEFECT_WAYPOINTS = {"tank-T101": "T101-east-seam", "tank-T102": "T102-east-seam"}
NORMAL_DELTAS_C = (0.9, 1.2, 1.1, 0.8)  # by station position on the route
ANOMALY_DELTA_C = 18.4
# Demo-only rule for turning samples into a finding; not a validated diagnostic threshold.
ANOMALY_THRESHOLD_C = 5.0
NO_VISIBLE_ANOMALY = "No visible anomaly"


@dataclass
class SimRobot:
    online: bool = True
    # Disconnected without telling anyone: no status event, no heartbeats, commands vanish.
    silent: bool = False
    # Command IDs already accepted, and the final result of each, for idempotent dispatch.
    seen_commands: set[str] = field(default_factory=set)
    results: dict[str, RobotEvent] = field(default_factory=dict)
    pending_faults: set[str] = field(default_factory=set)
    active: dict[str, asyncio.Task] = field(default_factory=dict)
    # Commands that ignore cancellation because the robot is unresponsive.
    unresponsive: set[str] = field(default_factory=set)


@dataclass
class SimAsset:
    defect_present: bool
    repaired: bool = False


class SimulatedFleet:
    def __init__(
        self,
        fleet: Fleet,
        time_scale: float = 1.0,
        state_file: str | Path | None = None,
        routes_file: str | Path = DEFAULT_ROUTES_FILE,
    ):
        self.time_scale = time_scale
        self.routes = SiteRoutes.load(routes_file)
        for asset_id, waypoint in DEFECT_WAYPOINTS.items():
            if self.routes.station(asset_id, waypoint) is None:
                raise ValueError(f"simulated defect waypoint '{waypoint}' is not on the route for {asset_id}")
        self.api = MockRobotAPI(fleet, self.routes, world=self)
        self.robots = {rid: SimRobot() for rid in fleet.robots}
        # The simulated world outlives the orchestrator: a repaired tank stays repaired across
        # a backend restart, as a real one would. Without a state file it lives in memory only.
        self.state_file = Path(state_file) if state_file else None
        self.assets = {aid: SimAsset(defect_present=(aid == "tank-T101")) for aid in ASSETS}
        if self.state_file and self.state_file.exists():
            saved = json.loads(self.state_file.read_text())
            self.assets.update({aid: SimAsset(**saved[aid]) for aid in ASSETS if aid in saved})
        self.command_log: list[Command] = []
        self._sink: EventSink | None = None
        self._heartbeat_sink: HeartbeatSink | None = None
        self._heartbeat_task: asyncio.Task | None = None

    def attach(self, sink: EventSink, heartbeat: HeartbeatSink | None = None) -> None:
        self._sink = sink
        self._heartbeat_sink = heartbeat

    def start(self) -> None:
        """Begin reporting liveness for every robot that is up."""
        if self._heartbeat_sink is not None:
            self._heartbeat_task = asyncio.create_task(self._heartbeats())

    async def _heartbeats(self) -> None:
        while True:
            self._heartbeat_sink([rid for rid, r in self.robots.items() if r.online])
            await asyncio.sleep(HEARTBEAT_INTERVAL_S * self.time_scale)

    async def _emit(self, event: RobotEvent) -> None:
        assert self._sink is not None, "simulator is not attached to an event sink"
        await self._sink(event)

    def _event(self, robot_id: str, type_: str, command_id: str | None = None, **payload) -> RobotEvent:
        return RobotEvent(
            event_id=f"ev-{uuid.uuid4().hex[:12]}",
            robot_id=robot_id,
            type=type_,
            command_id=command_id,
            payload=payload,
        )

    # RobotPort

    async def dispatch(self, command: Command) -> None:
        """Idempotent per command ID: a repeated command is never executed twice."""
        robot = self.robots[command.robot_id]
        if robot.silent:
            return  # The command disappears; only a timeout or the heartbeat lease reveals it.
        if not robot.online:
            raise DispatchError(f"{command.robot_id} is unreachable")
        if command.command_id in robot.seen_commands:
            result = robot.results.get(command.command_id)
            if result is not None:
                # Re-send the original result event; the engine's inbox drops it if already seen.
                await self._emit(result)
            return
        if robot.active:
            raise DispatchError(f"{command.robot_id} is already executing a command")
        robot.seen_commands.add(command.command_id)
        self.command_log.append(command)
        fault = next((f for f in NEXT_COMMAND_FAULTS if f in robot.pending_faults), None)
        if fault:
            robot.pending_faults.discard(fault)
        if fault == "hang_next":
            robot.unresponsive.add(command.command_id)
        robot.active[command.command_id] = asyncio.create_task(self._run(command, fault))

    async def cancel(self, robot_id: str, command_id: str) -> None:
        robot = self.robots[robot_id]
        task = robot.active.get(command_id)
        if task is None or not robot.online or command_id in robot.unresponsive:
            return  # Request is lost; the engine must not assume the action stopped.
        task.cancel()
        robot.active.pop(command_id, None)
        await self._emit(self._event(robot_id, "cancelled", command_id))

    def status(self, robot_id: str) -> dict:
        robot = self.robots[robot_id]
        return {"online": robot.online, "active_commands": sorted(robot.active)}

    # Execution

    def _recipe(self, command: Command, deadline: float) -> list:
        """The mock operations that carry out one business command."""
        target = command.args["target"]
        route = self.routes.assets[target]
        common = {
            "parent_command_id": command.command_id,
            "robot_id": command.robot_id,
            "target": target,
            "map_id": self.routes.map_id,
            "deadline_at": deadline,
        }
        ops: list = []

        def add(model, waypoint: str, **extra) -> None:
            kind = {NavigateRequest: "nav", ProcedureRequest: "procedure"}.get(model) or (
                "thermal" if extra["source"] == THERMAL_SOURCE else "visual"
            )
            if model is NavigateRequest:
                # The adapter names the destination pose; the robot plans its own way there.
                wp = self.routes.waypoint(waypoint)
                extra = {"frame_id": self.routes.frame_id, "target_position": wp.position, "target_yaw_rad": wp.yaw_rad}
            ops.append(model(operation_id=f"{command.command_id}:{len(ops)}:{kind}", waypoint_id=waypoint, **common, **extra))

        if command.action == "thermal_inspection":
            for waypoint in route.stations:  # stop and capture at each station, in route order
                add(NavigateRequest, waypoint)
                add(CaptureRequest, waypoint, source=THERMAL_SOURCE)
        elif command.action in ("visual_inspection", "verify_repair"):
            source = CAMERA_SOURCE if command.action == "visual_inspection" else THERMAL_SOURCE
            add(NavigateRequest, route.service_waypoint)
            add(CaptureRequest, route.service_waypoint, source=source)
        elif command.action == "repair":
            add(NavigateRequest, route.service_waypoint)
            add(ProcedureRequest, route.service_waypoint, procedure=command.args["procedure"])
        else:
            raise ValueError(f"simulator has no recipe for action {command.action}")
        return ops

    async def _run(self, command: Command, fault: str | None) -> None:
        robot = self.robots[command.robot_id]
        spec = ACTIONS[command.action]
        deadline = command.deadline_at or time.time() + spec.timeout_s * self.time_scale
        ops = self._recipe(command, deadline)
        op_s = spec.nominal_duration_s * self.time_scale / len(ops)
        # fail_next: the last navigation of the recipe fails, after any earlier captures.
        fail_at = max(i for i, op in enumerate(ops) if isinstance(op, NavigateRequest)) if fault == "fail_next" else None
        accepted = self._event(command.robot_id, "accepted", command.command_id)
        seq = 0

        async def progress(stage: str, i: int, op, **data) -> None:
            nonlocal seq
            seq += 1
            await self._emit(
                self._event(
                    command.robot_id,
                    "progress",
                    command.command_id,
                    seq=seq,
                    operation_id=op.operation_id,
                    operation=op.operation,
                    stage=stage,
                    step=i + 1,
                    total_steps=len(ops),
                    target=op.target,
                    waypoint_id=op.waypoint_id,
                    location=self.routes.station(op.target, op.waypoint_id).label,
                    **data,
                )
            )

        try:
            if fault != "reorder_next":
                await self._emit(accepted)
            results: list = []
            error = None
            for i, op in enumerate(ops):
                where = self.routes.station(op.target, op.waypoint_id).label
                if fault == "hang_next" and i == len(ops) - 1:
                    # The robot stops responding and resumes only after the engine's deadline.
                    await asyncio.sleep(spec.timeout_s * 1.5 * self.time_scale)
                try:
                    if isinstance(op, NavigateRequest):

                        async def on_feedback(fb: NavigationFeedback, i=i, op=op) -> None:
                            if fb.status == "running":
                                await progress("navigating", i, op, frame_id=op.frame_id, target_position=list(op.target_position))

                        fail = "path blocked (simulated)" if i == fail_at else None
                        fb = await self.api.navigate(op, op_s, on_feedback, fail=fail)
                        if fb.status != "reached_goal":
                            error = f"navigation to {where} {fb.status}: {fb.detail}"
                            break
                        await progress("arrived", i, op, frame_id=fb.frame_id, position=list(fb.position), yaw_rad=fb.yaw_rad)
                    elif isinstance(op, CaptureRequest):
                        r = await self.api.capture(op, op_s)
                        if r.status != "completed":
                            error = f"capture at {where} failed: {r.detail}"
                            break
                        results.append(r)
                        reading = {"temperature_delta_c": r.temperature_delta_c} if r.source == THERMAL_SOURCE else {"observation": r.observation}
                        await progress("captured", i, op, source=r.source, acquired_at=r.acquired_at, **reading)
                    else:
                        r = await self.api.run_procedure(op, op_s)
                        if r.status != "completed":
                            error = f"{op.procedure} at {where} failed: {r.detail}"
                            break
                        results.append(r)
                        await progress("procedure_completed", i, op, procedure=op.procedure)
                except MockRejected as exc:
                    error = f"robot rejected {op.operation} at {where}: {exc}"
                    break
            if not robot.online:
                return
            if error:
                failed = self._event(command.robot_id, "failed", command.command_id, error=error)
                robot.results[command.command_id] = failed
                await self._emit(failed)
            else:
                finding, data = self._summarize(command, results)
                data["operations"] = self.api.operations_for(command.command_id)
                done = self._event(command.robot_id, "completed", command.command_id, finding=finding, data=data)
                robot.results[command.command_id] = done
                await self._emit(done)
                if fault == "duplicate_next":
                    await self._emit(done)
            if fault == "reorder_next":
                await self._emit(accepted)
        finally:
            robot.active.pop(command.command_id, None)
            robot.unresponsive.discard(command.command_id)

    def _summarize(self, command: Command, results: list) -> tuple[str, dict]:
        """Derive the finding from the captured samples, not from the world state directly."""
        spec = ACTIONS[command.action]
        target = command.args["target"]
        route = self.routes.assets[target]
        provenance = {"map_id": self.routes.map_id, "route_version": self.routes.version, "simulated": True}
        if spec.role == "intervention":
            (r,) = results
            label = self.routes.station(target, r.waypoint_id).label
            return spec.findings[0], {
                "procedure": r.procedure,
                "location": label,
                "waypoint_id": r.waypoint_id,
                "outcome": r.status,
                "completed_at": r.completed_at,
                **provenance,
            }
        if results[0].source == CAMERA_SOURCE:
            (r,) = results
            anomaly = r.observation != NO_VISIBLE_ANOMALY
            data = {
                "observation": r.observation,
                "location": self.routes.station(target, r.waypoint_id).label,
                "waypoint_id": r.waypoint_id,
                "source": r.source,
                "acquired_at": r.acquired_at,
                **provenance,
            }
        else:
            samples = [
                {
                    "waypoint_id": r.waypoint_id,
                    "location": self.routes.station(target, r.waypoint_id).label,
                    "temperature_delta_c": r.temperature_delta_c,
                    "acquired_at": r.acquired_at,
                }
                for r in results
            ]
            peak = max(samples, key=lambda x: x["temperature_delta_c"])
            anomaly = peak["temperature_delta_c"] >= ANOMALY_THRESHOLD_C
            data = {
                "max_temperature_delta_c": peak["temperature_delta_c"],
                **({"location": peak["location"]} if anomaly else {}),
                "anomaly_threshold_c": ANOMALY_THRESHOLD_C,
                "samples": samples,
                "source": THERMAL_SOURCE,
                **({"route_id": route.route_id} if len(samples) > 1 else {}),
                **provenance,
            }
        if spec.role == "inspection":
            return (spec.defect_finding if anomaly else next(f for f in spec.findings if f != spec.defect_finding)), data
        if spec.role == "verification":
            return (next(f for f in spec.findings if f != spec.success_finding) if anomaly else spec.success_finding), data
        raise ValueError(f"simulator has no behavior for role {spec.role}")

    # World: what the mock sensors observe and the mock procedure changes.

    def _open_defect_at(self, target: str, waypoint_id: str) -> bool:
        asset = self.assets[target]
        return asset.defect_present and not asset.repaired and DEFECT_WAYPOINTS.get(target) == waypoint_id

    def thermal_delta(self, target: str, waypoint_id: str) -> float:
        if self._open_defect_at(target, waypoint_id):
            return ANOMALY_DELTA_C
        ids = self.routes.assets[target].stations
        return NORMAL_DELTAS_C[ids.index(waypoint_id) % len(NORMAL_DELTAS_C)]

    def visual_observation(self, target: str, waypoint_id: str) -> str:
        return "Staining and seepage along the weld seam" if self._open_defect_at(target, waypoint_id) else NO_VISIBLE_ANOMALY

    def apply_procedure(self, target: str, procedure: str) -> None:
        self.assets[target].repaired = True
        self._save_assets()

    # Fault injection and scenario controls

    def _drop_connection(self, robot: SimRobot) -> None:
        # Communication is lost. In-flight commands never report back.
        for task in robot.active.values():
            task.cancel()
        robot.active.clear()
        robot.unresponsive.clear()

    async def set_online(self, robot_id: str, online: bool) -> None:
        """Announced change: the robot (or its fleet manager) reports it with a status event."""
        robot = self.robots[robot_id]
        if robot.online == online:
            return
        robot.online = online
        robot.silent = False
        if not online:
            self._drop_connection(robot)
        await self._emit(self._event(robot_id, "status", online=online))

    def lose_silently(self, robot_id: str) -> None:
        """Unannounced loss (power or network failure): no status event, heartbeats stop."""
        robot = self.robots[robot_id]
        if not robot.online:
            return
        robot.online = False
        robot.silent = True
        self._drop_connection(robot)

    def arm_fault(self, robot_id: str, fault: str) -> None:
        if fault not in NEXT_COMMAND_FAULTS:
            raise ValueError(f"unknown fault '{fault}'")
        self.robots[robot_id].pending_faults.add(fault)

    def clear_faults(self, robot_id: str) -> None:
        """Disarm faults that have not been used by a command yet."""
        self.robots[robot_id].pending_faults.clear()

    def set_defect(self, asset_id: str, present: bool) -> None:
        self.assets[asset_id] = SimAsset(defect_present=present)
        self._save_assets()

    def _save_assets(self) -> None:
        if self.state_file is None:
            return
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        tmp.write_text(json.dumps({aid: asdict(a) for aid, a in self.assets.items()}))
        os.replace(tmp, self.state_file)  # atomic: a crash leaves the old or the new state

    def snapshot(self) -> dict:
        return {
            "robots": {
                rid: {
                    "online": r.online,
                    "silent": r.silent,
                    # In the order they apply: one per command.
                    "pending_faults": [f for f in NEXT_COMMAND_FAULTS if f in r.pending_faults],
                    "active_commands": sorted(r.active),
                }
                for rid, r in self.robots.items()
            },
            "assets": {aid: vars(a) for aid, a in self.assets.items()},
        }

    async def shutdown(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        for robot in self.robots.values():
            for task in robot.active.values():
                task.cancel()
            robot.active.clear()
