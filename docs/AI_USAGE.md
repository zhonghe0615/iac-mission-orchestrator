# AI Tool Usage

I used AI coding agents throughout development. Below is a summary of their contributions and the design decisions I made.

**Who decided what.** I set the direction at every critical point: scope and priorities, which proposals to accept, which review findings to act on and in what order, and when the work was done. The tools proposed designs and made implementation-level choices within that direction. I judged their work by running the system, reading the evidence, and getting an independent review, not by taking their reports at face value.

## Tools and what they did

- **Analysis and planning:** GPT Astra did the first-round analysis of the assessment brief and formulated the implementation plan in `docs/IMPLEMENTATION_HANDOVER.md` (architecture, invariants, milestones, acceptance scenarios).
- **Implementation:** Claude Code with Claude Opus 5.5 wrote nearly all of the code, tests, and documentation: the backend (plan validation, planning harness, execution engine, simulator, persistence, API), the React console, and the design, demo, and verification documents. It also ran the tests, drove the UI in a headless browser to check each change, and reported what it had and had not verified.
- **Code review:** GPT Astra reviewed the near-final submission against the assessment brief.
- **Inside the product:** `gpt-6-luna` is the planning model the orchestrator calls at runtime. It is part of the system under test, not a development tool.

## What the AI accelerated

Implementation, test suites, UI styling, documentation drafts, and repetitive verification (repeat test runs, mutation checks that disable a guard to confirm its test fails, browser walkthroughs, and fresh-clone install checks). The agents also recorded and edited the demo video following my demonstration plan. That work would otherwise have taken days.

## Decisions I made

These came from me, not from a suggestion by the tools:

- **Stack and model:** React and TypeScript, FastAPI, SQLite. I chose `gpt-6-luna` as the planning model, with its exact ID confirmed from the provider's documentation rather than substituted.
- **Scope:** finish the required end-to-end slice first, then pursue bonuses.
- **Portability:** the reviewer must be able to run the project easily. This led to committing the built UI so that only Python is needed, plus pip and uv setup paths tested from a fresh clone.
- **Scalability:** the system must support hundreds or thousands of robots and new robot types, with every robot's type always known. This shaped the typed fleet configuration and the large-fleet tests.
- **Data integrity:** I required idempotency and thread safety.
- **Robot liveness:** I asked how the system learns that a robot went offline, and required heartbeats. I then questioned whether a 3 s lease was too short. That led to a configurable lease, and to reporting a lease expiry as "unreachable" instead of "offline".
- **Execution realism:** I asked how an abstract LLM plan becomes concrete robot commands, and asked for a test where Scouts A and B work in parallel.
- **Acting on the review:** I chose to address all four review findings, and in which order: work-order constraints first, then the restart fix, then the shorter design document and a final real-model run.
- **Operator experience:** a mission progress bar, labelled fault-injection controls, severity icons in the timeline, auto-selecting a new mission, listing example work orders instead of rotating them, and showing "LLM planner" without the model ID.

## Proposals I reviewed and accepted

The AI tools proposed these, mostly in GPT Astra's implementation plan and some during implementation. I reviewed and accepted them:

- A small custom state machine instead of Temporal or LangGraph, with the production tradeoff described in DESIGN.md.
- Separate mission, task, and attempt state, so that a failed attempt keeps its history.
- Approval bound to the plan version and action, and used by exactly one dispatch.
- Treating "outcome unknown" differently from "failed", so that a repair is never re-sent blindly.
- The tier list for bonuses (policy checks, duplicate and out-of-order events, restart, metrics).

## How I checked the AI's work

- I ran the system end to end in the browser myself: planning with the real model, failure injection, approval and rejection, and the example work orders.
- I did not take the agent's claims at face value, and they were not always right. For example, it once said the model had ignored a "no repairs" instruction. Checking the mission record before acting, it found it had misread which mission my screenshot showed, and it corrected the claim. No change was made on the strength of the wrong claim.
- I had a different model review the implementation. GPT Astra's review of Claude Code's work found a real gap: plans were not checked against the work order's target asset or a "no repairs" instruction. That led to the enforced intake fields, and the fix uncovered a scheduler loop that had never been triggered before.
- I relied on evidence rather than assertions: real-model runs recorded in `docs/VERIFICATION.md`, tests that were shown to fail when their guard was removed, and a fresh-clone install.
