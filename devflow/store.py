from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Iterable, Optional

from .models import Task, now_iso


class Store:
    """SQLite 存储。整条任务以 JSON 存在 data 列，状态/时间单独成列便于查询。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    data TEXT NOT NULL)"""
            )
            self._conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
            self._conn.commit()

    def save(self, task: Task) -> Task:
        task.updated_at = now_iso()
        with self._lock:
            if task.id is None:
                cur = self._conn.execute(
                    "INSERT INTO tasks(state, created_at, updated_at, data) VALUES (?,?,?,?)",
                    (task.state, task.created_at, task.updated_at, "{}"),
                )
                task.id = cur.lastrowid
            self._conn.execute(
                "UPDATE tasks SET state=?, updated_at=?, data=? WHERE id=?",
                (task.state, task.updated_at, task.model_dump_json(), task.id),
            )
            self._conn.commit()
        return task

    def get(self, task_id: int) -> Optional[Task]:
        with self._lock:
            row = self._conn.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
        return Task.model_validate_json(row["data"]) if row else None

    def list(self, states: Iterable[str] | None = None, limit: int = 500) -> list[Task]:
        with self._lock:
            if states:
                states = list(states)
                marks = ",".join("?" * len(states))
                rows = self._conn.execute(
                    f"SELECT data FROM tasks WHERE state IN ({marks}) ORDER BY updated_at DESC LIMIT ?",
                    (*states, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT data FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [Task.model_validate_json(r["data"]) for r in rows]

    def kv_get(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv(k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, value),
            )
            self._conn.commit()
