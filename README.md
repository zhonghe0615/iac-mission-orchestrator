# IAC Mission Orchestrator

A working prototype of an orchestration layer that plans, executes, and recovers an "inspect and resolve a suspected tank defect" mission across a simulated robot fleet: Scout A, Scout B, and Mobile Manipulator C.

An LLM proposes a structured mission plan. Deterministic backend code validates it, routes tasks by capability and availability, enforces human approval before the repair, and handles failures, timeouts, and late or duplicate events without losing mission history.

## Quick start

Requirements: **Python 3.10 or newer**. Nothing else: the operator UI is already built into `frontend/dist` and served by the backend. Node.js is needed only to change the UI.

### With pip (any OS)

```bash
cd backend
python -m venv .venv
# macOS / Linux:
source .venv/bin/activate
# Windows (PowerShell):  .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m app
```

### With uv (optional, if you have it)

```bash
cd backend
uv run python -m app
```

Then open **http://127.0.0.1:8000**. Use `python -m app --port 9000` if port 8000 is taken.

The database is created at `backend/data/orchestrator.db`. Delete that file to start from a clean state. Only one server process can use a database file at a time; a second one stops with a clear message.

### Enabling the real LLM planner

Without credentials, only the deterministic **fixture planner** is offered, and it is labelled as not being an LLM everywhere it appears. To use a real model:

```bash
cp backend/.env.example backend/.env    # Windows: copy backend\.env.example backend\.env
```

Set `LLM_API_KEY` and `LLM_MODEL`, and `LLM_BASE_URL` if you are not using the OpenAI endpoint. Any OpenAI-compatible Chat Completions API works. `LLM_REASONING_EFFORT` is sent only when set. There is intentionally no default model name. Restart the server; the planner selector then offers the LLM planner.

### Changing the fleet

Robots are defined in `backend/config/fleet.json`. Every robot must belong to a declared robot type, and the type defines its capabilities:

```json
{
  "robot_types": {
    "inspection_quadruped": {"name": "Inspection quadruped", "capabilities": ["navigation", "camera", "thermal_inspection"]}
  },
  "robots": [
    {"id": "scout-a", "name": "Scout A", "type": "inspection_quadruped", "preference_rank": 1}
  ]
}
```

Add robots or new types there and restart; no code changes are needed. An invalid file (unknown or missing type, duplicate ID) stops startup with every problem listed. Set `FLEET_FILE` to use a different file.

## Running the tests

```bash
cd backend
pip install -r requirements-dev.txt     # or: uv run pytest
pytest
```

The suite runs offline: robot timings are scaled down, and the LLM planner is tested against a fake HTTP endpoint. It covers:

| Area | Tests |
| --- | --- |
| Approved repair, denied repair, failure and recovery (the required categories) | `tests/test_missions.py` |
| No eligible robot, late result after reassignment, unknown repair outcome, retry budget, cancellation, approval bypass attempts | `tests/test_missions.py` |
| Plan validation: unsupported actions, bad arguments, cycles, unsafe repair sequencing, attempts to set approval or robots in a plan | `tests/test_plan.py` |
| LLM harness: structured request, bounded correction, visible failure, no fixture fallback | `tests/test_planner.py` |
| Duplicate and out-of-order events, restart recovery, metrics | `tests/test_reliability.py` |
| Typed fleet config, a new robot type via config only, 520 robots with 40 concurrent missions, 20 types with 1,000 robots | `tests/test_fleet.py` |
| Idempotency keys for operator commands, repeated robot commands | `tests/test_idempotency.py` |
| Thread-safe reads, foreign-thread rejection, one server process per database | `tests/test_concurrency.py` |
| HTTP API end to end | `tests/test_api.py` |

## Using the console

See [docs/DEMO.md](docs/DEMO.md) for the full walkthrough. In short:

1. Submit a work order. The plan appears for review with its source (LLM, fixture, or operator edit). Edit it if needed; every edit is validated.
2. Start the mission and watch tasks, attempts, evidence, and the timeline update live.
3. Inject faults from the Fleet panel: take a robot offline, make its next command fail, time out, arrive twice, or arrive out of order.
4. Approve or reject the repair when asked.

## What is simulated and what is implemented

| Implemented (real code paths) | Simulated |
| --- | --- |
| LLM planning harness, structured output, validation, bounded correction | Robots: actions are timers that return scripted findings |
| Plan validation for model and operator plans | Sensor data and images (placeholder values and `sim://` references) |
| Mission/task/attempt state machine, dependencies, conditions | The tank and its defect (in memory; reset on restart) |
| Capability routing, reservations, policy checks | Network faults (injected explicitly by the operator) |
| Approval binding to plan version and action, single use | |
| Timeouts, retries, reassignment, escalation, cancellation semantics | |
| Duplicate and stale event handling | |
| SQLite persistence, audit log, restart recovery | |
| Metrics, operator console, live updates (server-sent events) | |

The simulator receives the same commands a real Robot Integration Layer adapter would and reports through the same event path. The UI displays only backend state.

## Project layout

```
backend/
  app/catalog.py     site, fleet, and action catalog (shared by planner, validator, engine)
  app/plan.py        plan schema and validation
  app/planner.py     LLM planning harness and fixture planner
  app/engine.py      execution engine and state machine
  app/simulator.py   simulated fleet with fault injection
  app/robots.py      robot command/event interface
  app/store.py       SQLite persistence and audit log
  app/main.py        HTTP API and live update stream
  tests/
frontend/            React + TypeScript console (built output committed in dist/)
docs/
  DESIGN.md          architecture, tradeoffs, production path
  DEMO.md            walkthrough script
  VERIFICATION.md    status of every requirement, with evidence
  AI_USAGE.md        AI tool usage
  IMPLEMENTATION_HANDOVER.md   planning handover and acceptance criteria
```

## Changing the UI

Requires Node.js 18 or newer.

```bash
cd frontend
npm ci
npm run dev      # http://localhost:5173, proxies /api to the backend on :8000
npm run build    # updates frontend/dist, which the backend serves
```

## Known limitations

See [docs/DESIGN.md](docs/DESIGN.md#8-known-limitations-and-unhandled-edge-cases). Most notably: two missions can target the same asset at the same time, there is no authentication (operator names are self-reported), and an unknown repair outcome always goes to the operator rather than being reconciled automatically.

All documentation and code comments are in English. Credentials stay outside version control (`backend/.env` is ignored).
