"""Simulated fleet adapter.

Receives real commands from the engine and reports results through the same
event path a real integration would use. Faults are injected explicitly by the
operator (or tests); nothing here decides mission flow.
"""

import asyncio
import uuid
from dataclasses import dataclass, field

from .catalog import ACTIONS, ASSETS, Fleet
from .robots import HEARTBEAT_INTERVAL_S, Command, DispatchError, EventSink, HeartbeatSink, RobotEvent

# offline/online change availability; the rest apply to the robot's next command.
NEXT_COMMAND_FAULTS = ("fail_next", "hang_next", "duplicate_next", "reorder_next")


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
    def __init__(self, fleet: Fleet, time_scale: float = 1.0):
        self.time_scale = time_scale
        self.robots = {rid: SimRobot() for rid in fleet.robots}
        self.assets = {aid: SimAsset(defect_present=(aid == "tank-T101")) for aid in ASSETS}
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

    async def _run(self, command: Command, fault: str | None) -> None:
        robot = self.robots[command.robot_id]
        spec = ACTIONS[command.action]
        accepted = self._event(command.robot_id, "accepted", command.command_id)
        try:
            if fault != "reorder_next":
                await self._emit(accepted)
            if fault == "hang_next":
                # Responds only after the engine's deadline has passed.
                await asyncio.sleep(spec.timeout_s * 1.5 * self.time_scale)
            else:
                await asyncio.sleep(spec.nominal_duration_s * self.time_scale)
            if not robot.online:
                return
            if fault == "fail_next":
                failed = self._event(command.robot_id, "failed", command.command_id, error=f"{spec.name} sensor fault")
                robot.results[command.command_id] = failed
                await self._emit(failed)
            else:
                finding, data = self._apply(command)
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

    def _apply(self, command: Command) -> tuple[str, dict]:
        """Scripted result by action role; the asset model is shared across robot types."""
        spec = ACTIONS[command.action]
        asset = self.assets[command.args["target"]]
        open_defect = asset.defect_present and not asset.repaired
        image = f"sim://{command.action}/{command.command_id}.png"
        if spec.role == "inspection":
            if command.action == "thermal_inspection":
                found = {"max_temperature_delta_c": 18.4, "location": "east weld seam"}
                clear = {"max_temperature_delta_c": 1.1}
            else:
                found = {"observation": "Staining and seepage along the east weld seam"}
                clear = {"observation": "No visible anomaly"}
            if open_defect:
                return spec.defect_finding, {**found, "image": image}
            return next(f for f in spec.findings if f != spec.defect_finding), {**clear, "image": image}
        if spec.role == "intervention":
            asset.repaired = True
            return spec.findings[0], {"procedure": command.args.get("procedure", "")}
        if spec.role == "verification":
            if open_defect:
                failure = next(f for f in spec.findings if f != spec.success_finding)
                return failure, {"max_temperature_delta_c": 17.9, "image": image}
            return spec.success_finding, {"max_temperature_delta_c": 0.8, "image": image}
        raise ValueError(f"simulator has no behavior for role {spec.role}")

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
