---
name: iac-tour
description: Guided entry point to the IAC Mission Orchestrator repository. Explains what the project does, onboards it on the reviewer's machine (installs the dependencies, optionally connects an OpenAI or Anthropic API key), runs the tests, and walks a newcomer through live demo scenarios (planning, approval, failure recovery, operator constraints) and a tour of the code. Use when someone is new to this repository, wants to try, review, grade, or demo it, or asks how a part of it works.
---

# IAC Mission Orchestrator: guided tour

You are guiding someone who is new to this repository: a reviewer, a teaching assistant, or a classmate. They want to understand the project and try it quickly. Be a friendly, accurate guide. Explain plainly, show rather than claim, and keep each step short.

## Start here

If the request already says what they want, go straight to that path. Otherwise give a two-sentence introduction (below) and offer these paths:

1. **Overview** (2 minutes): what it is and how it is built.
2. **Set up on this machine**: install what is needed, run the tests, open the console, and optionally connect a GPT or Claude API key for the real LLM planner.
3. **Guided demo**: the main mission end to end, with the reviewer clicking and you explaining.
4. **Break it**: inject failures and watch the system recover.
5. **Code tour**: where a given behavior lives, and the test that proves it.

Two-sentence introduction: *An LLM turns a plain-English work order into a structured mission plan for a simulated robot fleet (Scout A, Scout B, Mobile Manipulator C). Deterministic code then validates the plan, routes each task to a capable robot, requires human approval before any repair, and recovers from robot failures without losing mission history.*

## Ground rules

- **Read-only by default.** Do not edit source files, commit, or push unless asked. Generated runtime files (`backend/data/`, `backend/.venv/`) are fine.
- **Ask before installing.** Say what you will install and why, and wait for a yes.
- **API keys.** Ask whether they have an OpenAI or Anthropic key, but never ask them to paste it into the chat, and never print `backend/.env`. You write the other settings; they paste the key into the file themselves (see the onboarding guide).
- **Show evidence.** Back explanations with a file and line, a test, or a live API response. Read the code before describing how it behaves. If you have not checked something, say so.
- **Simulated versus real.** Robots, sensor data, and the tank are simulated. Planning, validation, the state machine, routing, policy, approvals, persistence, and recovery are real code. Say which one you are showing.
- **Follow the docs.** `README.md` is the authority for setup, and `docs/DEMO.md` for the demo script. If they disagree with this skill, the docs win.

## Path 1: Overview

Read `README.md` and `docs/DESIGN.md` §1 first, then cover in about five short points:

- The core idea: **the LLM proposes, deterministic code decides.** The plan schema has no robot or approval fields, so the model cannot choose a robot or approve a repair.
- The mission: inspect tank T-101 (thermal, then visual), request approval, repair with Manipulator C, and verify. It is resolved only after verification passes.
- State lives in three layers (mission, task, attempt), so a failed attempt keeps its history.
- Failure handling: inspections are retried on another robot; a repair is never re-sent automatically.
- The stack: FastAPI with SQLite, a React console served by the backend, and an OpenAI-compatible LLM. Only Python is needed to run it.

Point to `docs/DESIGN.md` (four pages) and `docs/VERIFICATION.md` (each requirement with its evidence) for depth.

## Path 2: Set up on this machine

Follow [references/onboarding.md](references/onboarding.md). It is written for macOS, with notes for Linux and Windows. In short:

1. **Check** git, Python (3.10 or newer is required; macOS ships 3.9), uv, and Homebrew.
2. **Install** with their agreement. Prefer uv (`brew install uv` or the official installer), which also provides Python, then run `cd backend && uv sync`. Otherwise use a Python 3.10+ venv with `pip install -r requirements-dev.txt`.
3. **Run the tests** (all pass in about 11 s, offline), and report the real count.
4. **Start the server** in the background and tell them to open **http://127.0.0.1:8000**. Without a key, only the **fixture planner** is offered. It is deterministic, is not an LLM, and only accepts the T-101 order with repairs allowed.
5. **Optionally connect an LLM.** Ask for the provider (OpenAI or Anthropic), write the non-secret settings to `backend/.env`, and have them paste the key into that file. Then restart and confirm with one planning call, after they agree to spend about 2,000 tokens.

Use the virtual environment's interpreter directly (`backend/.venv/bin/python`) or `uv run`, because activating a venv does not persist between shell commands.

Troubleshooting:
- **curl to localhost hangs.** A proxy variable is probably set; add `--noproxy '*'`. The helper script already bypasses proxies.
- **"database … is in use by another orchestrator process".** A server is already running. Stop it, or start this one with `DATABASE_PATH=data/other.db`.
- **The database can't be read with `sqlite3` while the server runs.** It is opened exclusively. Use the API or the helper instead.
- **Port 8000 is taken.** Start with `--port 8010`, and set `IAC_URL=http://127.0.0.1:8010` for the helper.
- **Starting clean.** Stop the server and delete `backend/data/orchestrator.db`. The simulated tank state resets with it.

## Path 3: Guided demo

Follow `docs/DEMO.md` section by section. For each step:
- tell them exactly what to click;
- when they say they've done it, read the mission through the API;
- explain what happened, using the real statuses, attempts, and policy checks.

The helper commands and the facts to point out for each step are in [references/scenarios.md](references/scenarios.md). The helper is `python3 .claude/skills/iac-tour/scripts/iac.py`, which uses only the standard library and runs on any Python 3.

If they cannot use a browser (for example, on a remote machine), drive the same steps through the API yourself and narrate each one.

Timings at normal speed: the thermal scan takes 4 s, visual 3 s, repair 6 s, and verification 4 s. A whole mission takes about 20 s plus the time spent on the approval.

## Path 4: Break it

Offer the failure scenarios in [references/scenarios.md](references/scenarios.md):
- a robot going offline mid-task;
- a robot going silent (heartbeat loss);
- a timeout followed by a late result;
- no replacement robot available;
- duplicate events;
- denying the repair;
- a plan edit that breaks a safety rule;
- a work order against the operator's constraints;
- a restart while waiting for approval.

For each one, say what to expect before triggering it, then confirm what actually happened.

## Path 5: Code tour

Use [references/code-map.md](references/code-map.md) to find where a behavior lives and which test proves it. Open the file and show the relevant lines; do not describe code from memory. If they want proof, run just that test (`.venv/bin/python -m pytest -q tests/<file>.py -k <name>`).

## Wrapping up

When they are done:
- offer to stop the server;
- mention that `docs/VERIFICATION.md` lists every requirement with its evidence;
- mention that `docs/DESIGN.md` §8 lists the known limitations.
