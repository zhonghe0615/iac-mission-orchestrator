# AI Tool Usage

This records how AI tools were used. It is limited to what is known from the project history.

## Tools and what they did

- **Requirements and planning:** an earlier AI-assisted planning session transcribed the assessment brief and drafted `docs/IMPLEMENTATION_HANDOVER.md` (architecture, invariants, milestones, acceptance scenarios).
- **Review of the plan:** Claude Code (Claude Opus 5.5) checked the handover against the original assessment PDF. It found that the written-design questions and evaluation criteria had been dropped and that the milestones mixed required work with bonuses, then revised the handover.
- **Implementation:** Claude Code wrote the backend (catalog, validation, planner harness, engine, simulator, persistence, API), the tests, the React console, and these documents. It ran the test suite repeatedly and checked that tests fail when key guards are removed (the stale-result check and the approval gate).

## What the AI accelerated

Boilerplate (API routes, persistence helpers, TypeScript types, CSS), the test harness, and first drafts of documentation.

## Decisions made by the author

Decisions stated by the author during the work:

- Stack: React and TypeScript, FastAPI, SQLite.
- Target model for planning ("GPT6 Luna", reasoning when needed), to be confirmed with the provider rather than substituted.
- Prioritize the required end-to-end slice, then pursue selected bonuses.
- The project must be easy for someone else to install and run, which led to committing the built UI so that only Python is required.

<!-- Author: add the engineering judgments you made yourself, in your own words, before submitting. -->
