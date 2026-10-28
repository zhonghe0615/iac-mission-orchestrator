# Mock Robot Execution

How a business action such as `thermal_inspection` becomes concrete robot operations. Every robot, map, reading, and repair here is simulated.

## Layers

```text
validated Plan (thermal_inspection, visual_inspection, repair, verify_repair)
  → engine: policy, routing, reservation, approval        (engine.py, generic)
  → RobotPort command: one per attempt, with its deadline   (robots.py)
  → integration adapter: recipe over the configured route   (simulator.py)
  → mock navigation / sensor / procedure API                (mock_robot.py)
  → progress and result events → engine audit, evidence, UI
```

The engine never sees maps, waypoints, or operation IDs. It receives normalized `progress` events and one `completed` or `failed` result per command. Walking four stations is one inspection attempt, not four tasks.

## Reference and mapping

The mock contracts are IAC-owned JSON shapes inspired by Boston Dynamics Spot concepts. They are not Spot request bodies, and no SDK is installed or connected. Sources consulted on 2026-10-28; no vendor field names were copied.

| Spot concept | Reference | Mock equivalent |
| --- | --- | --- |
| Navigation commands return an ID; feedback separately reports following the route versus reaching the goal | [GraphNav locomotion](https://dev.bostondynamics.com/docs/concepts/autonomy/graphnav_and_robot_locomotion.html), [protocol reference](https://dev.bostondynamics.com/protos/bosdyn/api/proto_reference.html) | `NavigateRequest` → `Ack` → `NavigationFeedback` (`running`, `reached_goal`, `failed`, `cancelled`). Acceptance is not arrival. |
| Maps of identified waypoints and edges | [GraphNav maps](https://dev.bostondynamics.com/docs/concepts/autonomy/graphnav_map_structure) | `config/inspection_routes.json`: a fictional map with fixed, ordered stations per tank. Arbitrary coordinates are not navigable. |
| Data acquisition is a separate payload service with source and capture time | [Data acquisition services](https://dev.bostondynamics.com/docs/concepts/writing_services_for_data_acquisition.html), [image.proto](https://github.com/boston-dynamics/spot-sdk/blob/master/protos/bosdyn/api/image.proto) | `CaptureRequest` → `CaptureResponse` with `source` (`simulated-thermal-payload` or `simulated-camera`) and `acquired_at`. |

A Unitree Go2 adapter is deferred: the [official sport client](https://github.com/unitreerobotics/unitree_sdk2/blob/main/include/unitree/robot/go2/sport/sport_client.hpp) exposes velocity-level movement, so a mapped inspection route would need extra navigation integration.

No real robot API provides weld sealing or gasket replacement. The manipulator's `run_procedure` is a bounded mock operation limited to the catalog's allowlisted procedures. There are no joint-level commands or model-generated trajectories.

## Waypoints, routes, and coordinates

- A **waypoint** is one navigation destination: ID, label, synthetic `position` `[x, y, z]`, and `yaw_rad` where the robot must face the tank.
- A **route** is an ordered list of waypoint IDs to inspect around one asset. Each asset also names a **service waypoint** for close-range work.
- The map declares `frame_id` (`site`), `units` (`m`), and `synthetic: true`. Positions must be exactly three finite numbers; yaw must be finite and within ±π.
- A position is where the robot **stands**. Sensor mounting height and the physical point being inspected or repaired are separate and not modelled.
- Each `NavigateRequest` carries `frame_id`, `target_position`, and `target_yaw_rad` alongside the waypoint ID. The mock robot rejects a pose that does not match that waypoint. `reached_goal` feedback reports the reached pose, which the adapter forwards in the `arrived` progress event.
- The adapter only names destinations. Travel between them belongs to the robot's navigation layer; the mock has no path planning, obstacle avoidance, or physics.

## Recipes

| Action | Operations (stop and capture) | Result |
| --- | --- | --- |
| `thermal_inspection` | For each route station: navigate, confirm arrival, thermal capture | Ordered samples, peak delta, anomaly location, map/route version, source, capture times |
| `visual_inspection` | Navigate to the service station, confirm arrival, camera capture | Observation with station and source |
| `repair` | Navigate directly to the service waypoint (not along the inspection route), confirm arrival, run the allowlisted procedure | Procedure, location, outcome. The tank state changes only when the procedure completes. |
| `verify_repair` | Navigate to the service station, confirm arrival, fresh thermal capture | `repair_verified` or `defect_persists` from the new reading |

Findings are derived from the captured samples with a **demo-only** threshold (5.0 °C), not read from the world state directly. A capture samples the simulated tank at the moment it runs, so verification sees the result of the repair. The defect location (T-101 east weld seam) is simulator world data, kept apart from the route topology.

## Contract rules

- Requests are strict Pydantic models. Unknown operations, unsupported sensor sources, unknown fields, unknown maps, and waypoints outside the target's route are rejected before anything executes.
- The robot must have the matching capability (`navigation`, `thermal_inspection`, `camera`, `repair`). Execution is keyed by capability, not robot ID or type.
- A capture or procedure is accepted only at the waypoint the robot last confirmed reaching.
- Operation IDs are `<command_id>:<step>:<kind>`. A repeated operation ID returns the original response without repeating the capture or procedure. Correlation IDs are not authorization.
- Each operation is bounded by the attempt's deadline. Progress does not extend it or replace heartbeats.

## Progress and safety

- The adapter emits a `progress` event on each operation change: navigating, arrived, captured, or procedure completed. Each carries a per-command sequence number.
- The engine accepts progress only for the attempt's own robot, only while the attempt is running, and only with a higher sequence number than before. Anything else is recorded as ignored. Accepted progress is written to the audit log and updates the task's status line. It never completes a task or creates evidence.
- A failed navigation stops the route: no capture at that station or later. Partial measurements stay in the audit log under the failed attempt; only a successful attempt's result becomes evidence.
- Cancellation stops the active operation and all remaining ones; no completion follows.
- A retry or reassignment is a new attempt that starts the route from the first station. There is no waypoint resume after a failure or restart.
- No procedure request is sent before policy and a matching approval. An uncertain repair is never repeated automatically.

## Fault injection

- **Fail next:** the recipe's last navigation fails ("path blocked").
- **Time out next:** the robot stalls before its last operation. That operation is rejected at its deadline, and the late failure arrives after the engine has timed out the attempt.
- **Take offline** and **Lose silently:** stop the route mid-operation.

## Not implemented

Real SDKs, continuous capture while walking, map editing, path planning between waypoints, localization error, battery, and durable dedupe of suboperations across restarts. The contracts are Pydantic models exchanged in-process; the Protobuf/gRPC boundary in DESIGN.md is planned, not implemented.
