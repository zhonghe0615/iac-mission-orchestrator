"""Small client for a running orchestrator, for demos and the guided tour. Standard library only.

Usage: python iac.py <command> [args]. The server URL defaults to http://127.0.0.1:8000
(set IAC_URL to change it). Run with no arguments for the command list.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get("IAC_URL", "http://127.0.0.1:8000").rstrip("/")
# Talk to localhost directly even if a proxy is configured in the environment.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
TERMINAL = {"resolved", "unresolved", "no_action_needed", "cancelled", "planning_failed"}


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + "/api" + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with OPENER.open(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = json.loads(exc.read() or b"{}")
        print(f"HTTP {exc.code}: {detail.get('detail')}")
        for err in detail.get("errors", []):
            print("  -", err)
        sys.exit(1)
    except urllib.error.URLError as exc:
        sys.exit(f"cannot reach {URL}: {exc.reason}. Is the server running?")


def show(mid: str, events: int = 8) -> dict:
    d = call("GET", f"/missions/{mid}")
    m = d["mission"]
    print(f"{m['id']}  {m['status']}  plan_v={m['plan_version']} target={m['target']} repairs={'yes' if m['allow_repair'] else 'no'}")
    if m["status_reason"]:
        print(f"  why: {m['status_reason']}")
    for t in d["tasks"]:
        attempts = ", ".join(f"{a['robot_id']}:{a['status']}" for a in t["attempts"]) or "-"
        finding = f" finding={t['finding']}" if t.get("finding") else ""
        print(f"  {t['task_id']:4} {t['action']:20} {t['status']:18} attempts=[{attempts}]{finding}")
        if t["status"] == "awaiting_approval":
            print(f"       approval request seq={approval_request_seq(d, t['task_id'])}")
    if not d["tasks"] and d["current_plan"]:
        for t in d["current_plan"]["plan"]["tasks"]:
            print(f"  plan {t['id']}: {t['action']} after {t['depends_on'] or '-'}")
    if events:
        print("  recent events:")
        for e in d["events"][-events:]:
            print(f"    {e['kind']:24} {e['message'][:110]}")
    return d


def wait(mid: str, statuses: set[str], timeout: float) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = call("GET", f"/missions/{mid}")
        if d["mission"]["status"] in statuses:
            return show(mid)
        if d["mission"]["status"] in TERMINAL:
            break
        time.sleep(0.5)
    show(mid)
    sys.exit(f"stopped waiting: status is {d['mission']['status']}, wanted {sorted(statuses)}")


def approval_request_seq(detail: dict, task_id: str) -> int:
    request = next(
        (e for e in reversed(detail["events"]) if e["kind"] == "approval_requested" and e["data"].get("task_id") == task_id),
        None,
    )
    if request is None:
        if any(t["task_id"] == task_id and t["status"] == "awaiting_approval" for t in detail["tasks"]):
            sys.exit("approval request is missing; refresh the mission")
        return 1  # Let the server report that this task is not awaiting approval.
    return request["seq"]


def main(argv: list[str]) -> None:
    if not argv:
        print(__doc__ + """
Commands:
  config                                planners available, server settings
  robots                                fleet with availability
  create TEXT [TARGET] [--no-repair] [--planner fixture|llm]
                                        submit a work order (TARGET defaults to tank-T101)
  show MID                              mission status, tasks, attempts, recent events
  wait MID STATUS[,STATUS] [SECONDS]    wait for a status (default 60 s)
  start MID PLAN_VERSION | cancel MID
  approve MID TASK REQUEST_SEQ | reject MID TASK REQUEST_SEQ [COMMENT]
  retry MID TASK
  fault ROBOT FAULT                     offline, online, lose_silently, fail_next, hang_next,
                                        duplicate_next, reorder_next, clear
  defect ASSET on|off                   set the simulated defect on a tank
  plan MID                              print the current plan and its version as JSON
  edit MID PLAN.json                    submit a versioned plan file from 'plan MID'
  events MID [KIND]                     full timeline, optionally one event kind
  metrics""")
        return
    cmd, args = argv[0], argv[1:]
    actor = {"actor": os.environ.get("IAC_ACTOR", "reviewer")}
    if cmd == "config":
        print(json.dumps(call("GET", "/config"), indent=2))
    elif cmd == "robots":
        for r in call("GET", "/robots")["robots"]:
            print(f"  {r['id']:15} {r['type']:22} {r['availability']:12} {r.get('health_reason') or ''}")
    elif cmd == "create":
        planner = args[args.index("--planner") + 1] if "--planner" in args else call("GET", "/config")["default_planner"]
        words = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or args[i - 1] != "--planner")]
        body = {"work_order": words[0], "target": words[1] if len(words) > 1 else "tank-T101",
                "allow_repair": "--no-repair" not in args, "planner": planner, **actor}
        mid = call("POST", "/missions", body)["mission"]["id"]
        print(mid)
        wait(mid, {"ready", "planning_failed"}, 300)
    elif cmd == "show":
        show(args[0])
    elif cmd == "wait":
        wait(args[0], set(args[1].split(",")), float(args[2]) if len(args) > 2 else 60)
    elif cmd in ("start", "cancel"):
        body = dict(actor)
        if cmd == "start":
            if len(args) < 2:
                sys.exit("specify the reviewed plan version shown by 'iac show MID'")
            body["expected_plan_version"] = int(args[1])
        call("POST", f"/missions/{args[0]}/{cmd}", body)
        show(args[0])
    elif cmd in ("approve", "reject"):
        if len(args) < 3:
            sys.exit("specify TASK and REQUEST_SEQ shown by 'iac show MID'")
        task = args[1]
        detail = call("GET", f"/missions/{args[0]}")
        body = {
            "decision": cmd, "comment": args[3] if len(args) > 3 else "",
            "expected_plan_version": detail["mission"]["plan_version"],
            "expected_request_seq": int(args[2]), **actor,
        }
        call("POST", f"/missions/{args[0]}/tasks/{task}/decision", body)
        show(args[0])
    elif cmd == "retry":
        call("POST", f"/missions/{args[0]}/tasks/{args[1]}/retry", actor)
        show(args[0])
    elif cmd == "fault":
        call("POST", f"/sim/robots/{args[0]}/fault", {"fault": args[1], **actor})
        main(["robots"])
    elif cmd == "defect":
        call("POST", f"/sim/assets/{args[0]}", {"defect_present": args[1] == "on"})
        print(f"{args[0]}: defect {'present' if args[1] == 'on' else 'absent'}")
    elif cmd == "plan":
        detail = call("GET", f"/missions/{args[0]}")
        print(json.dumps({
            "expected_plan_version": detail["mission"]["plan_version"],
            "plan": (detail["current_plan"] or {}).get("plan"),
        }, indent=2))
    elif cmd == "edit":
        with open(args[1]) as f:
            draft = json.load(f)
        if not isinstance(draft, dict) or "expected_plan_version" not in draft or "plan" not in draft:
            sys.exit("plan file must come from 'iac plan MID'; edit its plan field")
        call("PUT", f"/missions/{args[0]}/plan", {**draft, **actor})
        show(args[0])
    elif cmd == "events":
        for e in call("GET", f"/missions/{args[0]}")["events"]:
            if len(args) < 2 or e["kind"] == args[1]:
                print(f"  {e['kind']:24} {e['message']}")
                if e["kind"] == "policy_decision":
                    for c in e["data"]["checks"]:
                        print(f"      {'pass' if c['passed'] else 'FAIL'} {c['rule']}: {c['detail']}")
    elif cmd == "metrics":
        print(json.dumps(call("GET", "/metrics"), indent=2))
    else:
        sys.exit(f"unknown command {cmd!r}; run with no arguments for help")


if __name__ == "__main__":
    main(sys.argv[1:])
