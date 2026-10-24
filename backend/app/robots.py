"""Robot action interface used by the engine.

The orchestrator only ever issues the typed commands below, addressed by
robot, and correlates results by command ID (one command per execution
attempt). A real deployment would put a Robot Integration Layer adapter
behind the same interface.
"""

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

# Liveness: the integration layer reports which robots are alive at this interval,
# in one batch, the way a fleet manager would. A robot missing for the lease is
# treated as offline even if no status event ever arrives.
HEARTBEAT_INTERVAL_S = 1.0
HEARTBEAT_LEASE_S = 3.0


@dataclass(frozen=True)
class Command:
    command_id: str
    robot_id: str
    action: str
    args: dict[str, str]


@dataclass(frozen=True)
class RobotEvent:
    event_id: str
    robot_id: str
    # accepted | completed | failed | cancelled | status
    type: str
    command_id: str | None = None
    payload: dict = field(default_factory=dict)


class DispatchError(Exception):
    """The command was not accepted, so no physical action started."""


EventSink = Callable[[RobotEvent], Awaitable[None]]
HeartbeatSink = Callable[[list[str]], None]


class RobotPort(Protocol):
    async def dispatch(self, command: Command) -> None: ...

    async def cancel(self, robot_id: str, command_id: str) -> None: ...

    def status(self, robot_id: str) -> dict: ...
