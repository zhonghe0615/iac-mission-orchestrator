# IAC Mission Orchestrator

A mission orchestration prototype for inspecting and resolving a suspected tank defect across a simulated robot fleet.

## Status

Repository initialized. Application implementation has not started.

## Planned scope

- Generate and validate structured mission plans using a real LLM service.
- Review plans before execution and require explicit human approval for repair.
- Route tasks by robot capability and availability.
- Simulate Scout A, Scout B, and Mobile Manipulator C.
- Persist mission state, approvals, evidence, and execution events.
- Demonstrate inspection failure, reassignment, and recovery.
- Provide tests for successful execution, approval denial, and failure recovery.

## Planned stack

- Frontend: React and TypeScript.
- Backend: Python and FastAPI.
- Storage: SQLite.
- Model service: configurable; model availability and reasoning options will be verified during integration.

Robot actions are simulated. Planning and orchestration will be implemented in the application. Setup instructions and a demo walkthrough will be added with the working implementation.

All documentation and code comments are written in English. Credentials must remain outside version control.
