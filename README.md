# IAC Mission Orchestrator

A working prototype of an orchestration layer that plans, executes, and recovers an "inspect and resolve a suspected tank defect" mission across a simulated robot fleet: Scout A, Scout B, and Mobile Manipulator C.

An LLM proposes a structured mission plan. Deterministic backend code validates it, routes tasks by capability and availability, enforces human approval before the repair, and handles failures, timeouts, and late or duplicate events without losing mission history.

## New here? Take the guided tour

Open this folder in a coding agent and start the **`iac-tour`** skill:

| Agent | How to start the tour |
| --- | --- |
| [Claude Code](https://claude.com/claude-code) | Type **`/iac-tour`** |
| [Codex](https://developers.openai.com/codex) (CLI or IDE extension) | Type **`$iac-tour`**, or pick it from `/skills` |

You can also just ask "I'm new to this repo, where do I start?"; both agents pick the skill from its description. Other agents are pointed to it by [AGENTS.md](AGENTS.md).

The tour onboards the project on your machine:
- installs what is missing, with your agreement;
- runs the tests;
- opens the console;
- optionally connects your OpenAI or Anthropic API key;
- walks you through the demo, the failure scenarios, and the code.

There is one copy of the skill, in [.claude/skills/iac-tour/](.claude/skills/iac-tour/SKILL.md). Codex reads it through the symlink `.agents/skills/iac-tour`. On Windows, clone with `git clone -c core.symlinks=true` (symlinks may also need Developer Mode); otherwise Codex will not see the skill, and AGENTS.md still points the way. The tour is plain Markdown, so you can also read it directly. Its helper, `python3 .claude/skills/iac-tour/scripts/iac.py`, drives a running server from the terminal.

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

Stop the server with Ctrl+C. With the UI open, it takes up to 3 seconds to close the browser's live-update connection; a `CancelledError` traceback printed at that point is harmless.

The database is created at `backend/data/orchestrator.db`, with the simulated tank state next to it in `orchestrator.sim.json`. Delete the database to start from a clean state; the tank state resets with it. Only one server process can use a database file at a time; a second one stops with a clear message.

### Enabling the real LLM planner

Without credentials, only the deterministic **fixture planner** is offered, and it is labelled as not being an LLM everywhere it appears. To use a real model:

```bash
cp backend/.env.example backend/.env    # Windows: copy backend\.env.example backend\.env
```

Set `LLM_API_KEY` and `LLM_MODEL`, and `LLM_BASE_URL` if you are not using the OpenAI endpoint. Any OpenAI-compatible Chat Completions API works:

| Provider | `LLM_BASE_URL` | `LLM_MODEL` | `LLM_REASONING_EFFORT` |
| --- | --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` | `gpt-6-luna` (verified, see `docs/VERIFICATION.md`) | `medium` |
| Anthropic | `https://api.anthropic.com/v1` | `claude-sonnet-5-5` or `claude-opus-5-5` (per Anthropic's OpenAI-compatibility docs; not yet run live here) | leave empty |

`LLM_REASONING_EFFORT` is sent only when set. There is intentionally no default model name. Restart the server; the planner selector then offers the LLM planner.

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
| Operator constraints: wrong target or a disallowed repair is rejected for model, fixture, and edited plans, and blocked again at dispatch | `tests/test_constraints.py` |
| LLM harness: structured request, bounded correction, visible failure, no fixture fallback | `tests/test_planner.py` |
| Duplicate and out-of-order events, restart recovery (including between repair and verification), metrics | `tests/test_reliability.py` |
| Typed fleet config, a new robot type via config only, 520 robots with 40 concurrent missions, 20 types with 1,000 robots | `tests/test_fleet.py` |
| Idempotency keys for operator commands, repeated robot commands | `tests/test_idempotency.py` |
| Thread-safe reads, foreign-thread rejection, one server process per database | `tests/test_concurrency.py` |
| Heartbeat lease: silent robot loss detected while busy or idle, recovery, no false alarms | `tests/test_liveness.py` |
| HTTP API end to end | `tests/test_api.py` |

## Using the console

See [docs/DEMO.md](docs/DEMO.md) for the full walkthrough. In short:

1. Submit a work order with its target asset and whether repairs are allowed. Every plan is checked against those two fields. The plan appears for review with its source (LLM, fixture, or operator edit). Edit it if needed; every edit is validated.
2. Start the mission and watch tasks, attempts, evidence, and the timeline update live.
3. Inject faults from the Fleet panel: take a robot offline (announced) or lose it silently (detected by heartbeat), or make its next command fail, time out, arrive twice, or arrive out of order.
4. Approve or reject the repair when asked.

## What is simulated and what is implemented

| Implemented (real code paths) | Simulated |
| --- | --- |
| LLM planning harness, structured output, validation, bounded correction | Robots: actions are timers that return scripted findings |
| Plan validation for model and operator plans, including the operator's target and repair constraint | Sensor data and images (placeholder values and `sim://` references) |
| Mission/task/attempt state machine, dependencies, conditions | The tanks and the T-101 defect (saved next to the database, so a repair survives a restart; deleting the database resets them) |
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
  DESIGN.md          technical design for review (about 4 pages)
  DESIGN_REFERENCE.md  full design: transition table, idempotency, concurrency, scaling
  DEMO.md            walkthrough script
  VERIFICATION.md    status of every requirement, with evidence
  AI_USAGE.md        AI tool usage
.claude/skills/iac-tour/   guided tour for newcomers (skill and helper script)
.agents/skills/iac-tour    symlink to the tour, where Codex looks for skills
AGENTS.md                  points coding agents to the tour
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

See [docs/DESIGN.md](docs/DESIGN.md#8-known-limitations). Most notably: two missions can target the same asset at the same time, only the target and repair permission are enforced from the work order (other instructions rely on the model), there is no authentication (operator names are self-reported), and an unknown repair outcome always goes to the operator rather than being reconciled automatically.

All documentation and code comments are in English. Credentials stay outside version control (`backend/.env` is ignored).
