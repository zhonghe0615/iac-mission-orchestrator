"""Thread safety and single-writer protection."""

import asyncio
import subprocess
import sys
import threading

import pytest

from app.engine import TERMINAL, TERMINAL_TASK
from app.store import DatabaseInUseError, Store

from .conftest import BACKEND_DIR, close_harness, make_harness
from .test_fleet import fleet_of

pytestmark = pytest.mark.anyio


async def test_reads_from_other_threads_are_safe_and_consistent():
    h = await make_harness(fleet=fleet_of(scouts=30, manipulators=5), time_scale=0.05)
    errors: list[BaseException] = []
    snapshots = 0
    stop = threading.Event()
    missions: list[str] = []

    def reader() -> None:
        nonlocal snapshots
        while not stop.is_set():
            try:
                for mid in list(missions):
                    detail = h.engine.mission_detail(mid)
                    # A snapshot never mixes mission state with task state from another moment.
                    status, tasks = detail["mission"]["status"], [t["status"] for t in detail["tasks"]]
                    if status in TERMINAL:
                        assert all(t in TERMINAL_TASK for t in tasks), (status, tasks)
                    if status == "waiting_approval":
                        assert "awaiting_approval" in tasks, (status, tasks)
                    snapshots += 1
                for r in h.engine.robots_view():
                    assert (r["availability"] == "busy") == (r["reservation"] is not None) or r["availability"] in (
                        "offline",
                        "unresponsive",
                    )
            except BaseException as exc:  # collected and re-raised in the test thread
                errors.append(exc)
                return

    threads = [threading.Thread(target=reader) for _ in range(6)]
    for t in threads:
        t.start()
    try:
        for _ in range(15):
            missions.append(await h.started_mission())
        for mid in missions:
            await h.wait_status(mid, "waiting_approval", "no_action_needed", timeout=15)
        for mid in missions:
            if h.mission(mid)["status"] == "waiting_approval":
                await h.engine.decide_approval(mid, "t3", "approve", "tester", "")
        for mid in missions:
            await h.wait_status(mid, "resolved", "no_action_needed", timeout=15)
    finally:
        stop.set()
        for t in threads:
            t.join()
        await close_harness(h)
    assert not errors, errors[0]
    assert snapshots > 50


async def test_engine_rejects_commands_from_another_thread(h):
    mid = await h.started_mission()
    result: list[BaseException] = []

    def other_thread() -> None:
        try:
            asyncio.run(h.engine.cancel_mission(mid, "intruder"))
        except BaseException as exc:
            result.append(exc)

    t = threading.Thread(target=other_thread)
    t.start()
    t.join()
    assert isinstance(result[0], RuntimeError) and "event loop" in str(result[0])
    assert h.mission(mid)["status"] != "cancelled"


def test_second_process_cannot_open_the_same_database(tmp_path):
    db = str(tmp_path / "shared.db")
    store = Store(db)
    try:
        probe = (
            "import sys; from app.store import Store, DatabaseInUseError\n"
            "try:\n    Store(sys.argv[1])\nexcept DatabaseInUseError as e:\n    print('IN_USE', e)\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", probe, db], cwd=BACKEND_DIR, capture_output=True, text=True, timeout=30
        )
        assert out.stdout.startswith("IN_USE"), out.stdout + out.stderr
        assert "another orchestrator process" in out.stdout
    finally:
        store.close()
    # Once the first process releases the file, it can be opened again.
    Store(db).close()


def test_database_in_use_error_is_raised_in_process(tmp_path):
    db = str(tmp_path / "shared.db")
    store = Store(db)
    try:
        with pytest.raises(DatabaseInUseError):
            Store(db)
    finally:
        store.close()
