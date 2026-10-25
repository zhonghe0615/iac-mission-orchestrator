"""SQLite persistence for missions, tasks, attempts, approvals, evidence, and audit events.

Every engine handler runs inside one transaction so that state changes and the
audit events that describe them are committed together.

Concurrency: one connection is shared and guarded by a re-entrant lock, so the
store is safe to call from several threads; a transaction holds the lock until
it commits, so readers never see partial writes. The database file is opened in
SQLite's exclusive locking mode, so a second server process cannot use the same
file and double-book robots. The operating system releases that lock when the
process exits, including after a crash.
"""

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS missions (
    id TEXT PRIMARY KEY,
    work_order TEXT NOT NULL,
    target TEXT,
    allow_repair INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    status_reason TEXT NOT NULL DEFAULT '',
    planner_mode TEXT NOT NULL,
    plan_version INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_versions (
    mission_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    plan_json TEXT NOT NULL,
    source TEXT NOT NULL,
    author TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (mission_id, version)
);
CREATE TABLE IF NOT EXISTS tasks (
    mission_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    plan_version INTEGER NOT NULL,
    action TEXT NOT NULL,
    args_json TEXT NOT NULL,
    depends_json TEXT NOT NULL,
    condition_json TEXT,
    rationale TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    status_reason TEXT NOT NULL DEFAULT '',
    finding TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    budget_base INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (mission_id, task_id)
);
CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    robot_id TEXT NOT NULL,
    -- Recorded at dispatch so history keeps the type even if the fleet changes.
    robot_type TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL,
    deadline_at REAL NOT NULL,
    ended_at REAL
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    action_hash TEXT NOT NULL,
    decision TEXT NOT NULL,
    actor TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    consumed_by TEXT
);
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    robot_id TEXT NOT NULL,
    finding TEXT,
    data_json TEXT NOT NULL,
    provenance TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_by_status ON tasks (status);
CREATE INDEX IF NOT EXISTS attempts_by_mission ON attempts (mission_id, task_id);
CREATE INDEX IF NOT EXISTS events_by_mission ON events (mission_id, seq);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    response_code INTEGER,
    response_json TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (scope, key)
);
CREATE TABLE IF NOT EXISTS inbox (
    event_id TEXT PRIMARY KEY,
    received_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS robots (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    online INTEGER NOT NULL,
    health TEXT NOT NULL,
    health_reason TEXT NOT NULL DEFAULT '',
    reserved_attempt TEXT
);
CREATE INDEX IF NOT EXISTS robots_by_type ON robots (type, online, health);
"""

TASK_JSON_FIELDS = {"args_json": "args", "depends_json": "depends_on", "condition_json": "condition"}


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class DatabaseInUseError(RuntimeError):
    pass


class Store:
    def __init__(self, path: str):
        self._lock = threading.RLock()
        self._depth = 0
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=1.0)
        self.conn.row_factory = sqlite3.Row
        try:
            if path != ":memory:":
                self.conn.execute("PRAGMA locking_mode=EXCLUSIVE")
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            self._migrate()
        except sqlite3.OperationalError as exc:
            self.conn.close()
            if "locked" in str(exc):
                raise DatabaseInUseError(
                    f"database {path} is in use by another orchestrator process; "
                    "stop it first or set DATABASE_PATH to a different file"
                ) from None
            raise

    def _migrate(self) -> None:
        # Databases created before missions recorded the operator's repair constraint.
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(missions)")}
        if "allow_repair" not in columns:
            self.conn.execute("ALTER TABLE missions ADD COLUMN allow_repair INTEGER NOT NULL DEFAULT 1")

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[None]:
        """Transaction scope; nested calls join the outer transaction. Holds the store lock throughout."""
        with self._lock:
            if self._depth == 0:
                self.conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self.conn.execute("ROLLBACK")
                raise
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("COMMIT")

    # Generic helpers. Every statement runs under the store lock.

    def _execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def _one(self, sql: str, *params: Any) -> dict | None:
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, *params: Any) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def _update(self, table: str, where: dict, values: dict) -> None:
        sets = ", ".join(f"{k} = ?" for k in values)
        cond = " AND ".join(f"{k} = ?" for k in where)
        self._execute(f"UPDATE {table} SET {sets} WHERE {cond}", [*values.values(), *where.values()])

    def _insert(self, table: str, values: dict) -> None:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        self._execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(values.values()))

    # Missions and plans

    def insert_mission(self, values: dict) -> None:
        self._insert("missions", values)

    def mission(self, mission_id: str) -> dict | None:
        return self._one("SELECT * FROM missions WHERE id = ?", mission_id)

    def missions(self) -> list[dict]:
        return self._all("SELECT * FROM missions ORDER BY created_at DESC")

    def update_mission(self, mission_id: str, **values: Any) -> None:
        values["updated_at"] = time.time()
        self._update("missions", {"id": mission_id}, values)

    def add_plan_version(self, mission_id: str, plan: dict, source: str, author: str) -> int:
        row = self._one("SELECT COALESCE(MAX(version), 0) AS v FROM plan_versions WHERE mission_id = ?", mission_id)
        version = row["v"] + 1
        self._insert(
            "plan_versions",
            {
                "mission_id": mission_id,
                "version": version,
                "plan_json": json.dumps(plan),
                "source": source,
                "author": author,
                "created_at": time.time(),
            },
        )
        return version

    def plan_versions(self, mission_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM plan_versions WHERE mission_id = ? ORDER BY version", mission_id)
        for r in rows:
            r["plan"] = json.loads(r.pop("plan_json"))
        return rows

    def plan_version(self, mission_id: str, version: int) -> dict | None:
        row = self._one("SELECT * FROM plan_versions WHERE mission_id = ? AND version = ?", mission_id, version)
        if row:
            row["plan"] = json.loads(row.pop("plan_json"))
        return row

    # Tasks

    def insert_task(self, values: dict) -> None:
        values = dict(values)
        for col, key in TASK_JSON_FIELDS.items():
            values[col] = json.dumps(values.pop(key))
        self._insert("tasks", values)

    @staticmethod
    def _decode_task(row: dict) -> dict:
        for col, key in TASK_JSON_FIELDS.items():
            row[key] = json.loads(row.pop(col))
        return row

    def tasks(self, mission_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM tasks WHERE mission_id = ? ORDER BY position", mission_id)
        return [self._decode_task(r) for r in rows]

    def task(self, mission_id: str, task_id: str) -> dict | None:
        row = self._one("SELECT * FROM tasks WHERE mission_id = ? AND task_id = ?", mission_id, task_id)
        return self._decode_task(row) if row else None

    def missions_waiting_for_robots(self) -> list[str]:
        rows = self._all(
            "SELECT DISTINCT t.mission_id FROM tasks t JOIN missions m ON m.id = t.mission_id"
            " WHERE t.status = 'waiting_resource' ORDER BY m.created_at"
        )
        return [r["mission_id"] for r in rows]

    def update_task(self, mission_id: str, task_id: str, **values: Any) -> None:
        self._update("tasks", {"mission_id": mission_id, "task_id": task_id}, values)

    # Attempts

    def insert_attempt(self, values: dict) -> None:
        self._insert("attempts", values)

    def attempt(self, attempt_id: str) -> dict | None:
        return self._one("SELECT * FROM attempts WHERE id = ?", attempt_id)

    def attempts(self, mission_id: str) -> list[dict]:
        return self._all("SELECT * FROM attempts WHERE mission_id = ? ORDER BY started_at", mission_id)

    def task_attempts(self, mission_id: str, task_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM attempts WHERE mission_id = ? AND task_id = ? ORDER BY attempt_no", mission_id, task_id
        )

    def open_attempts(self) -> list[dict]:
        return self._all("SELECT * FROM attempts WHERE status IN ('running', 'cancel_requested')")

    def update_attempt(self, attempt_id: str, **values: Any) -> None:
        self._update("attempts", {"id": attempt_id}, values)

    # Approvals

    def insert_approval(self, values: dict) -> None:
        self._insert("approvals", values)

    def approvals(self, mission_id: str) -> list[dict]:
        return self._all("SELECT * FROM approvals WHERE mission_id = ? ORDER BY created_at", mission_id)

    def unconsumed_approval(self, mission_id: str, task_id: str) -> dict | None:
        return self._one(
            "SELECT * FROM approvals WHERE mission_id = ? AND task_id = ? AND decision = 'approve'"
            " AND consumed_by IS NULL ORDER BY created_at DESC LIMIT 1",
            mission_id,
            task_id,
        )

    def update_approval(self, approval_id: str, **values: Any) -> None:
        self._update("approvals", {"id": approval_id}, values)

    # Evidence

    def insert_evidence(self, values: dict) -> None:
        values = dict(values)
        values["data_json"] = json.dumps(values.pop("data"))
        self._insert("evidence", values)

    def evidence(self, mission_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM evidence WHERE mission_id = ? ORDER BY created_at", mission_id)
        for r in rows:
            r["data"] = json.loads(r.pop("data_json"))
        return rows

    # Audit events

    def add_event(self, mission_id: str | None, kind: str, message: str, data: dict | None = None) -> int:
        cur = self._execute(
            "INSERT INTO events (mission_id, ts, kind, message, data_json) VALUES (?, ?, ?, ?, ?)",
            (mission_id, time.time(), kind, message, json.dumps(data or {})),
        )
        return cur.lastrowid

    def events(self, mission_id: str | None = None, kinds: tuple[str, ...] | None = None) -> list[dict]:
        sql, params = "SELECT * FROM events WHERE 1 = 1", []
        if mission_id is not None:
            sql += " AND mission_id = ?"
            params.append(mission_id)
        if kinds:
            sql += f" AND kind IN ({', '.join('?' for _ in kinds)})"
            params.extend(kinds)
        rows = self._all(sql + " ORDER BY seq", *params)
        for r in rows:
            r["data"] = json.loads(r.pop("data_json"))
        return rows

    # Robot event inbox for deduplication

    def record_inbox(self, event_id: str) -> bool:
        """Return False when the event was already processed."""
        cur = self._execute(
            "INSERT OR IGNORE INTO inbox (event_id, received_at) VALUES (?, ?)", (event_id, time.time())
        )
        return cur.rowcount == 1

    # Robot availability view

    def upsert_robot(self, robot_id: str, robot_type: str, online: bool, health: str, health_reason: str = "") -> None:
        self._execute(
            "INSERT INTO robots (id, type, online, health, health_reason, reserved_attempt)"
            " VALUES (?, ?, ?, ?, ?, NULL)"
            " ON CONFLICT(id) DO UPDATE SET type = excluded.type, online = excluded.online,"
            " health = excluded.health, health_reason = excluded.health_reason",
            (robot_id, robot_type, int(online), health, health_reason),
        )

    def available_robot_ids(self, robot_types: list[str]) -> list[str]:
        marks = ", ".join("?" for _ in robot_types)
        rows = self._all(
            f"SELECT id FROM robots WHERE type IN ({marks}) AND online = 1 AND health = 'ok'"
            " AND reserved_attempt IS NULL",
            *robot_types,
        )
        return [r["id"] for r in rows]

    def unavailable_by_type(self, robot_types: list[str]) -> list[dict]:
        """Counts of unavailable robots per type and reason, in one grouped query."""
        marks = ", ".join("?" for _ in robot_types)
        return self._all(
            "SELECT type, CASE WHEN online = 0 AND health = 'unreachable' THEN 'unreachable'"
            " WHEN online = 0 THEN 'offline' WHEN health != 'ok' THEN health ELSE 'busy' END AS why,"
            f" COUNT(*) AS n FROM robots WHERE type IN ({marks})"
            " AND (online = 0 OR health != 'ok' OR reserved_attempt IS NOT NULL) GROUP BY type, why",
            *robot_types,
        )

    def robot_map(self) -> dict[str, dict]:
        """All robot rows in one query, keyed by id."""
        return {r["id"]: r for r in self._all("SELECT * FROM robots")}

    def robot(self, robot_id: str) -> dict | None:
        return self._one("SELECT * FROM robots WHERE id = ?", robot_id)

    def robots(self) -> list[dict]:
        return self._all("SELECT * FROM robots ORDER BY id")

    def update_robot(self, robot_id: str, **values: Any) -> None:
        self._update("robots", {"id": robot_id}, values)

    # Idempotency keys for operator requests

    def claim_idempotency_key(self, scope: str, key: str, request_hash: str) -> tuple[str, dict | None]:
        """Return ("new", None) if this caller should execute the request, else the stored state.

        States: "replay" (completed; stored response returned), "in_progress" (another request with
        this key is executing), "mismatch" (key reused with a different request body).
        """
        with self.tx():
            row = self._one("SELECT * FROM idempotency_keys WHERE scope = ? AND key = ?", scope, key)
            if row is None:
                self._insert(
                    "idempotency_keys",
                    {
                        "scope": scope,
                        "key": key,
                        "request_hash": request_hash,
                        "state": "in_progress",
                        "created_at": time.time(),
                    },
                )
                return "new", None
        if row["request_hash"] != request_hash:
            return "mismatch", row
        if row["state"] == "in_progress":
            return "in_progress", row
        row["response"] = json.loads(row.pop("response_json"))
        return "replay", row

    def complete_idempotency_key(self, scope: str, key: str, code: int, response: Any) -> None:
        with self.tx():
            self._update(
                "idempotency_keys",
                {"scope": scope, "key": key},
                {"state": "done", "response_code": code, "response_json": json.dumps(response)},
            )

    def release_idempotency_key(self, scope: str, key: str) -> None:
        """Forget an unfinished key after an unexpected error so the client can retry."""
        with self.tx():
            self._execute("DELETE FROM idempotency_keys WHERE scope = ? AND key = ?", (scope, key))
