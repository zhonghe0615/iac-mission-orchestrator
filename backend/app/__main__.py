"""Start the server: `python -m app` (add `--port 9000` to change the port)."""

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="IAC Mission Orchestrator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    print(f"Mission Orchestrator: open http://{args.host}:{args.port}")
    uvicorn.run("app.main:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
