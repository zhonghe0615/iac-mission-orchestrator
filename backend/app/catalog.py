"""Site, action catalog, and fleet model.

These values are implementation assumptions for a simulated scenario, not
facts about real hardware. The planner, validator, engine, and simulator all
read from this module so that the model sees the same contract that execution
enforces.

Robots always belong to a declared robot type; the type defines capabilities.
The fleet itself is data (config/fleet.json), so robots can be added without
code changes.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_FLEET_FILE = Path(__file__).resolve().parent.parent / "config" / "fleet.json"


@dataclass(frozen=True)
class Asset:
    id: str
    name: str
    zone: str
    description: str


@dataclass(frozen=True)
class ActionSpec:
    name: str
    description: str
    # "inspection", "intervention" (changes the physical asset), or "verification".
    role: str
    required_capabilities: frozenset[str]
    # Argument name -> allowed values. Every argument is required.
    args: dict[str, tuple[str, ...]]
    # Findings this action can report, used by plan conditions.
    findings: tuple[str, ...]
    risk: str  # "low" or "high"
    # True when repeating the action is physically safe even if a previous
    # attempt may have partly executed. Inspections are; repairs are not.
    retry_safe: bool
    nominal_duration_s: float
    timeout_s: float
    max_attempts: int = 3
    # inspection: the finding that means a defect is present.
    defect_finding: str | None = None
    # verification: the finding that confirms success, and the interventions it verifies.
    success_finding: str | None = None
    verifies: tuple[str, ...] = ()

    @property
    def requires_approval(self) -> bool:
        return self.risk == "high"


SITE_NAME = "North Tank Farm (simulated)"

ASSETS: dict[str, Asset] = {
    a.id: a
    for a in [
        Asset(
            id="tank-T101",
            name="Storage Tank T-101",
            zone="north-tank-farm",
            description="Crude storage tank; operator reports a suspected leak near the east weld seam.",
        ),
        Asset(
            id="tank-T102",
            name="Storage Tank T-102",
            zone="north-tank-farm",
            description="Adjacent storage tank; no open reports.",
        ),
    ]
}

TARGETS = tuple(ASSETS)

ACTIONS: dict[str, ActionSpec] = {
    a.name: a
    for a in [
        ActionSpec(
            name="thermal_inspection",
            description="Travel to the target and capture a thermal scan of its surface.",
            role="inspection",
            required_capabilities=frozenset({"navigation", "thermal_inspection"}),
            args={"target": TARGETS},
            findings=("defect_detected", "no_defect"),
            risk="low",
            retry_safe=True,
            nominal_duration_s=4.0,
            timeout_s=10.0,
            defect_finding="defect_detected",
        ),
        ActionSpec(
            name="visual_inspection",
            description="Travel to the target and capture close-range images.",
            role="inspection",
            required_capabilities=frozenset({"navigation", "camera"}),
            args={"target": TARGETS},
            findings=("defect_detected", "no_defect"),
            risk="low",
            retry_safe=True,
            nominal_duration_s=3.0,
            timeout_s=8.0,
            defect_finding="defect_detected",
        ),
        ActionSpec(
            name="repair",
            description="Apply a simulated repair procedure to the target. High risk; requires operator approval.",
            role="intervention",
            required_capabilities=frozenset({"navigation", "repair"}),
            args={"target": TARGETS, "procedure": ("seal_weld_seam", "replace_gasket")},
            findings=("repair_applied",),
            risk="high",
            retry_safe=False,
            nominal_duration_s=6.0,
            timeout_s=15.0,
            max_attempts=1,
        ),
        ActionSpec(
            name="verify_repair",
            description="Re-inspect the target after a repair and confirm the defect is gone.",
            role="verification",
            required_capabilities=frozenset({"navigation", "thermal_inspection"}),
            args={"target": TARGETS},
            findings=("repair_verified", "defect_persists"),
            risk="low",
            retry_safe=True,
            nominal_duration_s=4.0,
            timeout_s=10.0,
            success_finding="repair_verified",
            verifies=("repair",),
        ),
    ]
}


def actions_with_role(role: str) -> frozenset[str]:
    return frozenset(name for name, spec in ACTIONS.items() if spec.role == role)


# Fleet


class FleetConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RobotType:
    id: str
    name: str
    capabilities: frozenset[str]


@dataclass(frozen=True)
class RobotSpec:
    id: str
    name: str
    type: RobotType
    # Lower rank is preferred when several robots are eligible. This keeps
    # routing deterministic so the failure demo is reproducible.
    preference_rank: int

    @property
    def capabilities(self) -> frozenset[str]:
        return self.type.capabilities


@dataclass
class Fleet:
    types: dict[str, RobotType]
    robots: dict[str, RobotSpec]  # ordered by preference rank
    _capable: dict[str, list[RobotSpec]] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict) -> "Fleet":
        errors: list[str] = []
        types: dict[str, RobotType] = {}
        for type_id, spec in (data.get("robot_types") or {}).items():
            caps = spec.get("capabilities") or []
            if not caps:
                errors.append(f"robot type '{type_id}' declares no capabilities")
            types[type_id] = RobotType(id=type_id, name=spec.get("name", type_id), capabilities=frozenset(caps))
        if not types:
            errors.append("fleet declares no robot types")

        robots: list[RobotSpec] = []
        seen: set[str] = set()
        for i, r in enumerate(data.get("robots") or []):
            robot_id = r.get("id")
            if not robot_id:
                errors.append(f"robot #{i + 1} has no id")
                continue
            if robot_id in seen:
                errors.append(f"duplicate robot id '{robot_id}'")
            seen.add(robot_id)
            type_id = r.get("type")
            if type_id not in types:
                errors.append(f"robot '{robot_id}' has unknown or missing type '{type_id}'")
                continue
            robots.append(
                RobotSpec(
                    id=robot_id,
                    name=r.get("name", robot_id),
                    type=types[type_id],
                    preference_rank=int(r.get("preference_rank", 1000 + i)),
                )
            )
        if errors:
            raise FleetConfigError("; ".join(errors))
        robots.sort(key=lambda r: (r.preference_rank, r.id))
        return cls(types=types, robots={r.id: r for r in robots})

    @classmethod
    def load(cls, path: str | Path = DEFAULT_FLEET_FILE) -> "Fleet":
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise FleetConfigError(f"cannot read fleet file {path}: {exc}") from None
        return cls.from_dict(data)

    def capable(self, action: str) -> list[RobotSpec]:
        """Robots whose type can perform the action, in preference order (cached)."""
        if action not in self._capable:
            required = ACTIONS[action].required_capabilities
            self._capable[action] = [r for r in self.robots.values() if required <= r.capabilities]
        return self._capable[action]

    def types_lacking(self, action: str) -> list[RobotType]:
        required = ACTIONS[action].required_capabilities
        return [t for t in self.types.values() if not required <= t.capabilities]

    def name(self, robot_id: str) -> str:
        robot = self.robots.get(robot_id)
        return robot.name if robot else robot_id

    def type_counts(self) -> dict[str, int]:
        counts = {t: 0 for t in self.types}
        for r in self.robots.values():
            counts[r.type.id] += 1
        return counts


def site_context(fleet: Fleet) -> dict:
    """Bounded context handed to the planner and shown in the UI.

    The planner gets robot types and counts, not individual robots: it never
    assigns robots, and the prompt stays the same size as the fleet grows.
    """
    counts = fleet.type_counts()
    return {
        "site": SITE_NAME,
        "assets": [vars(a) for a in ASSETS.values()],
        "robot_types": [
            {"type": t.id, "name": t.name, "capabilities": sorted(t.capabilities), "count": counts[t.id]}
            for t in fleet.types.values()
        ],
        "actions": [
            {
                "name": a.name,
                "description": a.description,
                "role": a.role,
                "required_capabilities": sorted(a.required_capabilities),
                "args": {k: list(v) for k, v in a.args.items()},
                "findings": list(a.findings),
                "risk": a.risk,
                "requires_approval": a.requires_approval,
            }
            for a in ACTIONS.values()
        ],
    }
