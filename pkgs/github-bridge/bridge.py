#!/usr/bin/env python3
"""GitHub notification bridge for persistent OpenCode sessions."""

from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat as stat_module
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable


API_VERSION = "2022-11-28"
USER_AGENT = "opencode-github-bridge"
FINAL_EVENT_STATES = {"baseline", "delivered", "ignored", "dry-run"}
ACTIVE_TASK_STATES = {"queued", "active"}
REGISTRATION_GRACE = dt.timedelta(minutes=5)
EVENT_PROPAGATION_GRACE = dt.timedelta(minutes=5)
ACTION_SYSTEM = {
    "answer": (
        "Investigate the referenced GitHub subject and post a concise, accurate "
        "response using the appropriate GitHub API."
    ),
    "implement": (
        "Implement the requested change in the prepared checkout, validate it, "
        "push it, open or update a pull request, and call github_track_pr with "
        "the pull request URL before reporting completion."
    ),
    "review": (
        "Review the referenced subject using the local checkout. For a pull "
        "request, submit a COMMENT review only; never approve or formally request "
        "changes."
    ),
    "continue": (
        "Resume the existing task using the verified controller instruction or "
        "trusted controller review feedback."
    ),
}


class BridgeError(RuntimeError):
    pass


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def normalize_event_time(value: str) -> str:
    if not value:
        return iso_now()
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat(
            timespec="microseconds"
        )
    except ValueError:
        return iso_now()


def stable_id(prefix: str, value: str, length: int = 24) -> str:
    return prefix + hashlib.sha256(value.encode()).hexdigest()[:length]


def task_id_for(subject: sqlite3.Row, event_id: str) -> str:
    # Keep the event hash for uniqueness while making OpenCode project names
    # recognizable in clients that display only the workspace basename.
    repository = re.sub(
        r"-+",
        "-",
        f"{subject['owner']}-{subject['repository']}".lower().replace("_", "-"),
    )
    repository = re.sub(r"[^a-z0-9-]", "-", repository).strip("-")[:48]
    kind = {"pull_request": "pr", "discussion": "discussion"}.get(
        subject["type"], "issue"
    )
    digest = hashlib.sha256(event_id.encode()).hexdigest()[:12]
    return f"task-{repository}-{kind}-{subject['number']}-{digest}"


def numeric_id(value: Any, field: str) -> str:
    if isinstance(value, bool):
        raise BridgeError(f"{field} is not a numeric GitHub ID")
    if isinstance(value, int) and value >= 0:
        return str(value)
    if isinstance(value, str) and value.isdigit():
        return value
    raise BridgeError(f"{field} is not a numeric GitHub ID")


def optional_numeric_id(value: Any) -> str | None:
    try:
        return numeric_id(value, "GitHub ID")
    except BridgeError:
        return None


def parse_duration(value: str) -> dt.timedelta:
    match = re.fullmatch(r"([1-9][0-9]*)([dmy])", value)
    if not match:
        raise argparse.ArgumentTypeError(
            "duration must use d, m, or y, for example 30d or 1y"
        )
    count = int(match.group(1))
    unit = match.group(2)
    days = count * {"d": 1, "m": 30, "y": 365}[unit]
    return dt.timedelta(days=days)


def parse_github_pr_url(url: str) -> tuple[str, str, int]:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.port is not None
    ):
        raise BridgeError("pull request URL must use https://github.com")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise BridgeError(
            "pull request URL must not contain credentials, query, or fragment"
        )
    match = re.fullmatch(
        r"/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)/?", parsed.path
    )
    if not match:
        raise BridgeError("invalid GitHub pull request URL")
    return match.group(1), match.group(2), int(match.group(3))


def parse_github_repository(value: str) -> str:
    """Validate repository coordinates before they cross the sudo boundary."""
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}",
        value,
    ):
        raise BridgeError("invalid GitHub repository")
    return value


def parse_mention(body: str, bot_login: str) -> tuple[str, str] | None:
    pattern = re.compile(
        rf"(?i)(?<![A-Za-z0-9-])@{re.escape(bot_login)}\s+"
        r"(answer|implement|review|continue|cancel)\b"
    )
    matches = list(pattern.finditer(body))
    if not matches:
        return None
    if len(matches) != 1:
        raise BridgeError("comment contains multiple bot commands")
    match = matches[0]
    action = match.group(1).lower()
    suffix = body[match.end() :].strip()
    instruction = action if not suffix or action == "cancel" else f"{action} {suffix}"
    return action, instruction


@dataclasses.dataclass(frozen=True)
class Config:
    state_root: Path
    workspaces_root: Path
    workspace_manager: str
    agent: str
    provider_id: str
    model_id: str
    dry_run: bool
    max_tasks: int
    retention_days: int
    opencode_url: str
    github_api_url: str = "https://api.github.com"
    github_graphql_url: str = "https://api.github.com/graphql"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            state_root=Path(
                os.environ.get(
                    "GITHUB_BRIDGE_STATE_ROOT", "/var/lib/opencode-github-bridge"
                )
            ),
            workspaces_root=Path(
                os.environ.get(
                    "GITHUB_BRIDGE_WORKSPACES_ROOT", "/srv/opencode/workspaces"
                )
            ),
            workspace_manager=os.environ.get(
                "GITHUB_BRIDGE_WORKSPACE_MANAGER",
                "/run/current-system/sw/bin/opencode-workspace",
            ),
            agent=os.environ.get("GITHUB_BRIDGE_AGENT", "build"),
            provider_id=os.environ.get("GITHUB_BRIDGE_PROVIDER", "openai"),
            model_id=os.environ.get("GITHUB_BRIDGE_MODEL", "gpt-6-astra"),
            dry_run=os.environ.get("GITHUB_BRIDGE_DRY_RUN", "1") == "1",
            max_tasks=int(os.environ.get("GITHUB_BRIDGE_MAX_TASKS", "4")),
            retention_days=int(os.environ.get("GITHUB_BRIDGE_RETENTION_DAYS", "30")),
            opencode_url=os.environ.get(
                "GITHUB_BRIDGE_OPENCODE_URL", "http://127.0.0.1:4096"
            ),
            github_api_url=os.environ.get(
                "GITHUB_BRIDGE_API_URL", "https://api.github.com"
            ),
            github_graphql_url=os.environ.get(
                "GITHUB_BRIDGE_GRAPHQL_URL", "https://api.github.com/graphql"
            ),
        )


@dataclasses.dataclass(frozen=True)
class Credentials:
    github_token: str
    server_password: str
    controller_id: str

    @classmethod
    def load(cls) -> "Credentials":
        directory = Path(os.environ.get("CREDENTIALS_DIRECTORY", ""))

        def read(name: str, fallback_env: str) -> str:
            if directory and (directory / name).is_file():
                value = (directory / name).read_text().strip()
            else:
                path = os.environ.get(fallback_env)
                if not path:
                    raise BridgeError(f"credential {name} is unavailable")
                value = Path(path).read_text().strip()
            if not value:
                raise BridgeError(f"credential {name} is empty")
            return value

        return cls(
            github_token=read("github-token", "GITHUB_BRIDGE_GITHUB_TOKEN_FILE"),
            server_password=read(
                "server-password", "GITHUB_BRIDGE_SERVER_PASSWORD_FILE"
            ),
            controller_id=numeric_id(
                read("controller-id", "GITHUB_BRIDGE_CONTROLLER_ID_FILE"),
                "controller ID",
            ),
        )


class Database:
    def __init__(self, path: Path | str = ":memory:", readonly: bool = False) -> None:
        if path != ":memory:":
            if readonly:
                uri = f"file:{urllib.parse.quote(str(Path(path).resolve()))}?mode=ro"
                self.connection = sqlite3.connect(uri, uri=True)
            else:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                self.connection = sqlite3.connect(path)
        else:
            self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        if not readonly:
            self.connection.execute("PRAGMA synchronous = FULL")
            self.migrate()

    def migrate(self) -> None:
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            raise BridgeError(f"unsupported bridge database version {version}")
        if version == 0:
            self.connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE metadata (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );
                CREATE TABLE subjects (
                  node_id TEXT PRIMARY KEY,
                  type TEXT NOT NULL CHECK (type IN ('issue', 'pull_request', 'discussion')),
                  repository_id TEXT NOT NULL,
                  owner TEXT NOT NULL,
                  repository TEXT NOT NULL,
                  number INTEGER NOT NULL,
                  url TEXT NOT NULL,
                  title TEXT NOT NULL,
                  body TEXT NOT NULL,
                  metadata TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE tasks (
                  id TEXT PRIMARY KEY,
                  session_id TEXT UNIQUE,
                  directory TEXT NOT NULL UNIQUE,
                  repository_id TEXT NOT NULL,
                  action TEXT NOT NULL,
                  trigger_type TEXT NOT NULL,
                  automated INTEGER NOT NULL CHECK (automated IN (0, 1)),
                  state TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  completed_at TEXT,
                  collected_at TEXT,
                  cleanup_reason TEXT,
                  last_warning_at TEXT
                );
                CREATE TABLE mappings (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  subject_node_id TEXT NOT NULL REFERENCES subjects(node_id) ON DELETE CASCADE,
                  session_id TEXT NOT NULL,
                  action TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  superseded_at TEXT
                );
                CREATE UNIQUE INDEX one_active_mapping_per_subject
                  ON mappings(subject_node_id) WHERE superseded_at IS NULL;
                CREATE INDEX mappings_by_session ON mappings(session_id);
                CREATE TABLE events (
                  id TEXT PRIMARY KEY,
                  subject_node_id TEXT REFERENCES subjects(node_id) ON DELETE CASCADE,
                  type TEXT NOT NULL,
                  action TEXT,
                  actor_id TEXT,
                  payload TEXT NOT NULL,
                  state TEXT NOT NULL,
                  source_at TEXT NOT NULL,
                  source_order INTEGER NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  error TEXT
                );
                CREATE TABLE deliveries (
                  id TEXT PRIMARY KEY,
                  event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                  subject_node_id TEXT NOT NULL REFERENCES subjects(node_id) ON DELETE CASCADE,
                  session_id TEXT NOT NULL,
                  action TEXT NOT NULL,
                  system_text TEXT NOT NULL,
                  context_text TEXT NOT NULL,
                  state TEXT NOT NULL,
                  message_id TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  delivered_at TEXT,
                  error TEXT
                );
                CREATE TABLE delivery_events (
                  delivery_id TEXT NOT NULL REFERENCES deliveries(id) ON DELETE CASCADE,
                  event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                  PRIMARY KEY(delivery_id, event_id)
                );
                PRAGMA user_version = 1;
                COMMIT;
                """
            )

    def meta(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else row[0]

    def set_meta(self, key: str, value: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO metadata(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def save_subject(self, subject: dict[str, Any]) -> None:
        values = subject | {"metadata": json.dumps(subject.get("metadata") or {})}
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO subjects(node_id, type, repository_id, owner, repository,
                  number, url, title, body, metadata, updated_at)
                VALUES(:node_id, :type, :repository_id, :owner, :repository,
                  :number, :url, :title, :body, :metadata, :updated_at)
                ON CONFLICT(node_id) DO UPDATE SET
                  type=excluded.type, repository_id=excluded.repository_id,
                  owner=excluded.owner, repository=excluded.repository,
                  number=excluded.number, url=excluded.url, title=excluded.title,
                  body=excluded.body, metadata=excluded.metadata,
                  updated_at=excluded.updated_at
                """,
                values,
            )

    def event_state(self, event_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT state FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        return None if row is None else row[0]

    def event(self, event_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise BridgeError(f"unknown event {event_id}")
        return row

    def save_event(
        self,
        event_id: str,
        subject_id: str,
        event_type: str,
        action: str | None,
        actor_id: str | None,
        payload: dict[str, Any],
        state: str,
        source_at: str = "",
    ) -> None:
        now = iso_now()
        match = re.match(r"^(?:comment|issue-event|review):(\d+)", event_id)
        source_order = int(match.group(1)) if match else 0
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO events(id, subject_node_id, type, action, actor_id,
                  payload, state, source_at, source_order, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    event_id,
                    subject_id,
                    event_type,
                    action,
                    actor_id,
                    json.dumps(payload),
                    state,
                    normalize_event_time(source_at),
                    source_order,
                    now,
                    now,
                ),
            )

    def update_event(self, event_id: str, state: str, error: str | None = None) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE events SET state = ?, error = ?, updated_at = ? WHERE id = ?",
                (state, error, iso_now(), event_id),
            )

    def update_event_content(
        self, event_id: str, action: str, payload: dict[str, Any]
    ) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE events SET action=?, payload=?, error=NULL, updated_at=? WHERE id=?",
                (action, json.dumps(payload), iso_now(), event_id),
            )

    def replace_ignored_event(
        self,
        event_id: str,
        event_type: str,
        action: str,
        actor_id: str,
        payload: dict[str, Any],
        state: str,
    ) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE events SET type=?, action=?, actor_id=?, payload=?, state=?, "
                "error=NULL, updated_at=? WHERE id=? AND state='ignored'",
                (
                    event_type,
                    action,
                    actor_id,
                    json.dumps(payload),
                    state,
                    iso_now(),
                    event_id,
                ),
            )

    def pending_events(self) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM events WHERE state = 'pending' "
                "ORDER BY source_at, "
                "CASE action WHEN 'answer' THEN 0 WHEN 'implement' THEN 0 "
                "WHEN 'review' THEN 0 WHEN 'continue' THEN 1 "
                "WHEN 'cancel' THEN 2 ELSE 1 END, source_order, id"
            )
        )

    def active_mapping(self, subject_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM mappings WHERE subject_node_id = ? AND superseded_at IS NULL",
            (subject_id,),
        ).fetchone()

    def activate_mapping(self, subject_id: str, session_id: str, action: str) -> None:
        now = iso_now()
        with self.connection:
            self.connection.execute(
                "UPDATE mappings SET superseded_at = ? "
                "WHERE subject_node_id = ? AND superseded_at IS NULL",
                (now, subject_id),
            )
            self.connection.execute(
                "INSERT INTO mappings(subject_node_id, session_id, action, created_at) VALUES(?, ?, ?, ?)",
                (subject_id, session_id, action, now),
            )

    def task_for_session(self, session_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM tasks WHERE session_id = ?", (session_id,)
        ).fetchone()

    def task(self, task_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()

    def save_task(
        self,
        task_id: str,
        session_id: str,
        directory: str,
        repository_id: str,
        action: str,
        automated: bool,
        state: str,
        trigger_type: str = "manual",
    ) -> None:
        now = iso_now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO tasks(id, session_id, directory, repository_id, action, trigger_type,
                  automated, state, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET session_id=excluded.session_id,
                  directory=excluded.directory, repository_id=excluded.repository_id,
                  action=excluded.action,
                  trigger_type=excluded.trigger_type,
                  automated=excluded.automated, state=excluded.state,
                  updated_at=excluded.updated_at
                """,
                (
                    task_id,
                    session_id,
                    directory,
                    repository_id,
                    action,
                    trigger_type,
                    int(automated),
                    state,
                    now,
                    now,
                ),
            )

    def active_task_count(self) -> int:
        placeholders = ",".join("?" for _ in ACTIVE_TASK_STATES)
        return self.connection.execute(
            f"SELECT count(*) FROM tasks WHERE automated = 1 AND state IN ({placeholders})",
            tuple(sorted(ACTIVE_TASK_STATES)),
        ).fetchone()[0]

    def set_task_state(
        self, session_id: str, state: str, reason: str | None = None
    ) -> None:
        now = iso_now()
        completed = now if state == "completed" else None
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET state=?, cleanup_reason=?, updated_at=?, completed_at="
                "CASE WHEN ?='active' THEN NULL WHEN ?='completed' THEN ? ELSE completed_at END "
                "WHERE session_id=?",
                (state, reason, now, state, state, completed, session_id),
            )

    def queue_delivery(
        self,
        event_id: str,
        subject_id: str,
        session_id: str,
        action: str,
        system_text: str,
        context_text: str,
        coalesce: bool = False,
    ) -> str:
        if coalesce:
            existing = self.connection.execute(
                "SELECT id FROM deliveries WHERE subject_node_id=? AND session_id=? "
                "AND action=? AND state='pending' ORDER BY created_at LIMIT 1",
                (subject_id, session_id, action),
            ).fetchone()
            if existing is not None:
                separator = "\n\n---\n\n"
                current_length = self.connection.execute(
                    "SELECT length(context_text) FROM deliveries WHERE id=?",
                    (existing["id"],),
                ).fetchone()[0]
                if current_length + len(separator) + len(context_text) > 32768:
                    existing = None
            if existing is not None:
                with self.connection:
                    self.connection.execute(
                        "UPDATE deliveries SET context_text=context_text || ? WHERE id=?",
                        (separator + context_text, existing["id"]),
                    )
                    self.connection.execute(
                        "INSERT OR IGNORE INTO delivery_events(delivery_id, event_id) VALUES(?, ?)",
                        (existing["id"], event_id),
                    )
                return existing["id"]
        delivery_id = stable_id("delivery-", event_id)
        message_id = stable_id("msg_", delivery_id, 26)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO deliveries(id, event_id, subject_node_id, session_id,
                  action, system_text, context_text, state, message_id, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  event_id=excluded.event_id, subject_node_id=excluded.subject_node_id,
                  session_id=excluded.session_id, action=excluded.action,
                  system_text=excluded.system_text, context_text=excluded.context_text,
                  state='pending', error=NULL, delivered_at=NULL
                WHERE deliveries.state='cancelled'
                """,
                (
                    delivery_id,
                    event_id,
                    subject_id,
                    session_id,
                    action,
                    system_text,
                    context_text,
                    message_id,
                    iso_now(),
                ),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO delivery_events(delivery_id, event_id) VALUES(?, ?)",
                (delivery_id, event_id),
            )
        return delivery_id

    def move_delivery_event(
        self,
        old_delivery_id: str,
        event: sqlite3.Row,
        subject_id: str,
        session_id: str,
        system_text: str,
        context_text: str,
    ) -> None:
        new_delivery_id = stable_id("delivery-", event["id"])
        message_id = stable_id("msg_", new_delivery_id, 26)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO deliveries(id, event_id, subject_node_id, session_id,
                  action, system_text, context_text, state, message_id, created_at)
                VALUES(?, ?, ?, ?, 'continue', ?, ?, 'pending', ?, ?)
                ON CONFLICT(id) DO UPDATE SET context_text=excluded.context_text,
                  state='pending', error=NULL, delivered_at=NULL
                WHERE deliveries.state='cancelled'
                """,
                (
                    new_delivery_id,
                    event["id"],
                    subject_id,
                    session_id,
                    system_text,
                    context_text,
                    message_id,
                    iso_now(),
                ),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO delivery_events(delivery_id, event_id) VALUES(?, ?)",
                (new_delivery_id, event["id"]),
            )
            self.connection.execute(
                "DELETE FROM delivery_events WHERE delivery_id=? AND event_id=?",
                (old_delivery_id, event["id"]),
            )

    def pending_deliveries(self) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM deliveries WHERE state IN ('pending', 'dispatching') "
                "ORDER BY created_at"
            )
        )

    def has_pending_delivery(self, session_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM deliveries WHERE session_id=? "
                "AND state IN ('pending', 'dispatching') LIMIT 1",
                (session_id,),
            ).fetchone()
            is not None
        )

    def mark_delivery(
        self, delivery_id: str, state: str, error: str | None = None
    ) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE deliveries SET state=?, error=?, delivered_at=? WHERE id=?",
                (
                    state,
                    error,
                    iso_now() if state == "delivered" else None,
                    delivery_id,
                ),
            )

    def finalize_delivery(self, delivery_id: str) -> None:
        now = iso_now()
        with self.connection:
            self.connection.execute(
                "UPDATE deliveries SET state='delivered', error=NULL, delivered_at=? "
                "WHERE id=?",
                (now, delivery_id),
            )
            self.connection.execute(
                "UPDATE events SET state='delivered', error=NULL, updated_at=? "
                "WHERE id IN (SELECT event_id FROM delivery_events WHERE delivery_id=?)",
                (now, delivery_id),
            )

    def delivery_events(self, delivery_id: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT events.* FROM events JOIN delivery_events "
                "ON delivery_events.event_id=events.id "
                "WHERE delivery_events.delivery_id=? ORDER BY events.source_at, events.source_order",
                (delivery_id,),
            )
        )

    def update_delivery_content(self, delivery_id: str, context_text: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE deliveries SET context_text=?, error=NULL WHERE id=?",
                (context_text, delivery_id),
            )

    def remove_delivery_event(self, delivery_id: str, event_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM delivery_events WHERE delivery_id=? AND event_id=?",
                (delivery_id, event_id),
            )

    def cancel_session(self, session_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET state='paused', updated_at=? WHERE session_id=?",
                (iso_now(), session_id),
            )
            self.connection.execute(
                "UPDATE deliveries SET state='cancelled' WHERE session_id=? "
                "AND state IN ('pending', 'dispatching')",
                (session_id,),
            )

    def pull_subject_for_session(self, session_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT subjects.* FROM subjects
            JOIN mappings ON mappings.subject_node_id = subjects.node_id
            WHERE mappings.session_id = ? AND subjects.type = 'pull_request'
            ORDER BY mappings.created_at DESC LIMIT 1
            """,
            (session_id,),
        ).fetchone()

    def subject(self, node_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM subjects WHERE node_id=?", (node_id,)
        ).fetchone()
        if row is None:
            raise BridgeError(f"unknown subject {node_id}")
        return row

    def status_rows(self, blocked: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM tasks"
        args: tuple[Any, ...] = ()
        if blocked:
            sql += " WHERE state='cleanup_blocked'"
        return list(self.connection.execute(sql + " ORDER BY updated_at DESC", args))

    def mark_cleanup_blocked(self, task_id: str, reason: str) -> bool:
        row = self.connection.execute(
            "SELECT cleanup_reason, last_warning_at FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        should_warn = (
            row is None
            or row["cleanup_reason"] != reason
            or row["last_warning_at"] is None
        )
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET state='cleanup_blocked', cleanup_reason=?, updated_at=?, "
                "last_warning_at=CASE WHEN cleanup_reason IS NOT ? THEN NULL ELSE last_warning_at END "
                "WHERE id=?",
                (reason, iso_now(), reason, task_id),
            )
            if should_warn:
                self.connection.execute(
                    "UPDATE tasks SET last_warning_at=? WHERE id=?",
                    (iso_now(), task_id),
                )
        return should_warn

    def sessions_older_than(self, cutoff: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM tasks WHERE updated_at < ? ORDER BY updated_at",
                (cutoff,),
            )
        )

    def delete_session_records(self, session_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM deliveries WHERE session_id=?", (session_id,)
            )
            self.connection.execute(
                "DELETE FROM mappings WHERE session_id=?", (session_id,)
            )
            self.connection.execute(
                "DELETE FROM tasks WHERE session_id=?", (session_id,)
            )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


class HttpClient:
    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        allow_status: Iterable[int] = (),
    ) -> tuple[int, Any, dict[str, str]]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            url, data=data, method=method, headers=headers or {}
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read()
                status = response.status
                response_headers = {
                    key.lower(): value for key, value in response.headers.items()
                }
        except urllib.error.HTTPError as error:
            try:
                raw = error.read()
                status = error.code
                response_headers = {
                    key.lower(): value for key, value in error.headers.items()
                }
            finally:
                error.close()
            if status not in allow_status:
                text = raw.decode(errors="replace")[:1000]
                raise BridgeError(f"HTTP {status} from {url}: {text}") from error
        except OSError as error:
            raise BridgeError(f"request to {url} failed: {error}") from error
        if status not in allow_status and not 200 <= status < 300:
            raise BridgeError(f"unexpected HTTP {status} from {url}")
        if not raw:
            payload: Any = None
        else:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = raw.decode(errors="replace")
        return status, payload, response_headers


class GitHubClient:
    def __init__(
        self, http: HttpClient, token: str, api_url: str, graphql_url: str
    ) -> None:
        self.http = http
        self.api_url = api_url.rstrip("/")
        self.graphql_url = graphql_url
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }

    def get(
        self,
        path: str,
        extra_headers: dict[str, str] | None = None,
        allow_304: bool = False,
    ) -> Any:
        headers = self.headers | (extra_headers or {})
        status, payload, response_headers = self.http.request(
            "GET", self.url(path), headers, allow_status=(304,) if allow_304 else ()
        )
        return status, payload, response_headers

    def get_optional(self, path: str) -> dict[str, Any] | None:
        status, payload, _ = self.http.request(
            "GET", self.url(path), self.headers, allow_status=(404,)
        )
        if status == 404:
            return None
        if not isinstance(payload, dict):
            raise BridgeError(f"expected an object from {self.url(path)}")
        return payload

    def get_all(self, path: str) -> list[Any]:
        results: list[Any] = []
        next_url: str | None = self.url(path)
        while next_url:
            self.validate_api_url(next_url)
            _, payload, headers = self.http.request("GET", next_url, self.headers)
            if not isinstance(payload, list):
                raise BridgeError(f"expected a list from {next_url}")
            results.extend(payload)
            next_url = self.next_link(headers.get("link"))
        return results

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        _, payload, _ = self.http.request(
            "POST",
            self.graphql_url,
            self.headers | {"Content-Type": "application/json"},
            {"query": query, "variables": variables},
        )
        if not isinstance(payload, dict) or payload.get("errors"):
            raise BridgeError(f"GitHub GraphQL request failed: {payload}")
        return payload["data"]

    def url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            self.validate_api_url(path)
            return path
        return self.api_url + (path if path.startswith("/") else "/" + path)

    def validate_api_url(self, url: str) -> None:
        base = urllib.parse.urlsplit(self.api_url)
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
            raise BridgeError("refusing non-GitHub API URL")

    @staticmethod
    def next_link(header: str | None) -> str | None:
        if not header:
            return None
        for item in header.split(","):
            match = re.match(r'\s*<([^>]+)>;\s*rel="([^"]+)"', item)
            if match and match.group(2) == "next":
                return match.group(1)
        return None

    def user(self) -> dict[str, Any]:
        _, payload, _ = self.get("/user")
        return payload

    def notifications(
        self, last_modified: str | None
    ) -> tuple[int, list[Any], dict[str, str]]:
        headers = {"If-Modified-Since": last_modified} if last_modified else {}
        status, payload, response_headers = self.get(
            "/notifications?all=false&participating=false&per_page=50",
            headers,
            allow_304=True,
        )
        if status == 304:
            return status, [], response_headers
        if not isinstance(payload, list):
            raise BridgeError("GitHub notifications response is not a list")
        results = list(payload)
        next_url = self.next_link(response_headers.get("link"))
        while next_url:
            self.validate_api_url(next_url)
            _, page, page_headers = self.http.request("GET", next_url, self.headers)
            if not isinstance(page, list):
                raise BridgeError("GitHub notifications page is not a list")
            results.extend(page)
            next_url = self.next_link(page_headers.get("link"))
        return status, results, response_headers

    def pull(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        _, payload, _ = self.get(f"/repos/{owner}/{repo}/pulls/{number}")
        return payload

    def issue(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        _, payload, _ = self.get(f"/repos/{owner}/{repo}/issues/{number}")
        return payload


class OpenCodeClient:
    def __init__(self, http: HttpClient, base_url: str, password: str) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        auth = base64.b64encode(f"opencode:{password}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json",
        }

    def request(
        self,
        method: str,
        path: str,
        directory: str | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        url = self.base_url + path
        if directory:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
                {"directory": directory}
            )
        status, payload, _ = self.http.request(method, url, self.headers, body)
        return status, payload

    def create_session(self, directory: str, title: str) -> str:
        _, payload = self.request("POST", "/session", directory, {"title": title})
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
            raise BridgeError("OpenCode returned an invalid session")
        return payload["id"]

    def find_session(self, directory: str, title: str) -> str | None:
        _, payload = self.request("GET", "/session", directory)
        if not isinstance(payload, list):
            raise BridgeError("OpenCode returned an invalid session list")
        matches = [
            item.get("id")
            for item in payload
            if isinstance(item, dict) and item.get("title") == title
        ]
        if len(matches) > 1:
            raise BridgeError("multiple OpenCode sessions match the task title")
        return matches[0] if matches else None

    def validate_model(self, directory: str, provider_id: str, model_id: str) -> None:
        _, payload = self.request("GET", "/config/providers", directory)
        providers = payload.get("providers") if isinstance(payload, dict) else None
        if not isinstance(providers, list):
            raise BridgeError("OpenCode returned an invalid provider list")
        for provider in providers:
            if provider.get("id") != provider_id:
                continue
            models = provider.get("models") or {}
            if isinstance(models, dict) and model_id in models:
                return
            if isinstance(models, list) and any(
                model.get("id") == model_id for model in models
            ):
                return
        raise BridgeError(
            f"configured OpenCode model is unavailable: {provider_id}/{model_id}"
        )

    def get_session(self, session_id: str, directory: str) -> dict[str, Any]:
        _, payload = self.request(
            "GET", f"/session/{urllib.parse.quote(session_id)}", directory
        )
        if not isinstance(payload, dict):
            raise BridgeError("OpenCode returned an invalid session")
        return payload

    def statuses(self, directory: str) -> dict[str, Any]:
        _, payload = self.request("GET", "/session/status", directory)
        return payload if isinstance(payload, dict) else {}

    def prompt(
        self,
        session_id: str,
        directory: str,
        message_id: str,
        agent: str,
        provider_id: str,
        model_id: str,
        system_text: str,
        context_text: str,
    ) -> None:
        body = {
            "messageID": message_id,
            "agent": agent,
            "model": {"providerID": provider_id, "modelID": model_id},
            "system": system_text,
            "parts": [{"type": "text", "text": context_text}],
        }
        self.request(
            "POST",
            f"/session/{urllib.parse.quote(session_id)}/prompt_async",
            directory,
            body,
        )

    def message_exists(self, session_id: str, directory: str, message_id: str) -> bool:
        _, payload = self.request(
            "GET", f"/session/{urllib.parse.quote(session_id)}/message", directory
        )
        if not isinstance(payload, list):
            return False
        return any(
            item.get("info", {}).get("id") == message_id
            for item in payload
            if isinstance(item, dict)
        )

    def abort(self, session_id: str, directory: str) -> None:
        self.request(
            "POST", f"/session/{urllib.parse.quote(session_id)}/abort", directory, {}
        )

    def delete(self, session_id: str, directory: str) -> None:
        self.request("DELETE", f"/session/{urllib.parse.quote(session_id)}", directory)


class WorkspaceManager:
    def __init__(self, executable: str) -> None:
        self.executable = executable

    def run(self, *arguments: str) -> str:
        command = ["/run/wrappers/bin/sudo", "-n", self.executable, *arguments]
        result = subprocess.run(
            command, check=False, text=True, capture_output=True, timeout=180
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            suffix = f": {detail}" if detail else ""
            raise BridgeError(
                f"workspace manager failed with exit status {result.returncode}{suffix}"
            )
        return result.stdout.strip()

    def prepare(
        self, task_id: str, subject: sqlite3.Row, action: str
    ) -> dict[str, Any]:
        ref = (
            f"pull/{subject['number']}"
            if subject["type"] == "pull_request"
            else "default"
        )
        output = self.run(
            "prepare-task",
            task_id,
            f"{subject['owner']}/{subject['repository']}",
            action,
            str(subject["number"]),
            ref,
        )
        return json.loads(output)

    def restore(self, task_id: str, subject: sqlite3.Row, action: str) -> str:
        ref = (
            f"pull/{subject['number']}"
            if subject["type"] == "pull_request"
            else "default"
        )
        return self.run(
            "restore-task",
            task_id,
            f"{subject['owner']}/{subject['repository']}",
            action,
            str(subject["number"]),
            ref,
        )

    def inspect(self, task_id: str) -> dict[str, Any]:
        return json.loads(self.run("inspect-task", task_id))

    def head(self, task_id: str) -> str:
        return self.run("head-task", task_id)

    def remove(self, task_id: str) -> None:
        self.run("remove-task", task_id)

    def manage_github(
        self,
        kind: str,
        name: str,
        action: str,
        repository: str = "-",
        upstream: str = "-",
        remote: str = "all",
        branch: str = "-",
    ) -> dict[str, Any]:
        output = self.run(
            "manage-github",
            kind,
            name,
            action,
            repository,
            upstream,
            remote,
            branch,
        )
        return json.loads(output)


class Bridge:
    def __init__(
        self,
        config: Config,
        credentials: Credentials,
        database: Database,
        github: GitHubClient,
        opencode: OpenCodeClient,
        workspaces: WorkspaceManager,
    ) -> None:
        self.config = config
        self.credentials = credentials
        self.db = database
        self.github = github
        self.opencode = opencode
        self.workspaces = workspaces
        user = github.user()
        self.bot_id = numeric_id(user.get("id"), "bot ID")
        self.bot_login = str(user.get("login") or "")
        if not self.bot_login:
            raise BridgeError("GitHub returned an empty bot login")
        self.event_cutoff: dt.datetime | None = None

    def run(self) -> None:
        self.process_registrations()
        self.poll_notifications()
        if not self.config.dry_run:
            self.process_pending_events()
            self.dispatch_pending()
            self.update_completed_tasks()
            self.cleanup_tasks()
        self.process_registrations()

    def poll_notifications(self) -> None:
        now = int(time.time())
        next_poll = int(self.db.meta("next_poll_at") or "0")
        if now < next_poll:
            return
        last_modified = self.db.meta("notifications_last_modified")
        status, notifications, response_headers = self.github.notifications(
            last_modified
        )
        interval = max(60, int(response_headers.get("x-poll-interval", "60")))
        self.db.set_meta("next_poll_at", str(now + interval))
        if status == 304:
            self.db.set_meta("notifications_initialized", "1")
            self.db.set_meta("last_event_scan_at", iso_now())
            return
        baseline = self.db.meta("notifications_initialized") != "1"
        cutoff = self.db.meta("last_event_scan_at")
        self.event_cutoff = dt.datetime.fromisoformat(cutoff) if cutoff else None
        failures: list[str] = []
        for notification in notifications:
            try:
                self.discover_notification(notification, baseline)
            except BridgeError as error:
                failures.append(str(error))
        if failures:
            raise BridgeError(
                f"{len(failures)} notification(s) could not be discovered: {failures[0]}"
            )
        if response_headers.get("last-modified"):
            self.db.set_meta(
                "notifications_last_modified", response_headers["last-modified"]
            )
        self.db.set_meta("notifications_initialized", "1")
        self.db.set_meta("last_event_scan_at", iso_now())

    def discover_notification(
        self, notification: dict[str, Any], baseline: bool
    ) -> None:
        repository = notification.get("repository") or {}
        owner = repository.get("owner", {}).get("login")
        repo = repository.get("name")
        repository_id = numeric_id(repository.get("id"), "repository ID")
        subject_hint = notification.get("subject") or {}
        subject_type = subject_hint.get("type")
        api_url = subject_hint.get("url")
        if not owner or not repo:
            raise BridgeError("notification repository is incomplete")

        if subject_type == "PullRequest":
            number = self.number_from_api_url(api_url, "pulls")
            payload = self.github.pull(owner, repo, number)
            subject = self.subject_from_rest(
                payload, "pull_request", repository_id, owner, repo
            )
            self.db.save_subject(subject)
            self.discover_issue_or_pr(subject, payload, baseline, is_pr=True)
            return
        if subject_type == "Issue":
            number = self.number_from_api_url(api_url, "issues")
            payload = self.github.issue(owner, repo, number)
            kind = "pull_request" if payload.get("pull_request") else "issue"
            subject = self.subject_from_rest(payload, kind, repository_id, owner, repo)
            self.db.save_subject(subject)
            self.discover_issue_or_pr(
                subject, payload, baseline, is_pr=kind == "pull_request"
            )
            return
        if subject_type == "Discussion":
            number = self.number_from_discussion_notification(notification)
            self.discover_discussion(repository_id, owner, repo, number, baseline)

    @staticmethod
    def number_from_api_url(url: Any, collection: str) -> int:
        if not isinstance(url, str):
            raise BridgeError("notification subject has no API URL")
        parsed = urllib.parse.urlsplit(url)
        match = re.search(rf"/{collection}/([1-9][0-9]*)$", parsed.path)
        if not match:
            raise BridgeError("notification subject URL has an unexpected shape")
        return int(match.group(1))

    @staticmethod
    def number_from_discussion_notification(notification: dict[str, Any]) -> int:
        subject = notification.get("subject") or {}
        for value in (subject.get("url"), subject.get("latest_comment_url")):
            if isinstance(value, str):
                match = re.search(r"/discussions/([1-9][0-9]*)", value)
                if match:
                    return int(match.group(1))
        raise BridgeError("discussion notification does not expose its number")

    @staticmethod
    def subject_from_rest(
        payload: dict[str, Any], kind: str, repository_id: str, owner: str, repo: str
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "state": payload.get("state"),
            "author_id": optional_numeric_id((payload.get("user") or {}).get("id")),
        }
        if kind == "pull_request":
            metadata |= {
                "base_ref": payload.get("base", {}).get("ref"),
                "base_sha": payload.get("base", {}).get("sha"),
                "head_ref": payload.get("head", {}).get("ref"),
                "head_sha": payload.get("head", {}).get("sha"),
            }
        return {
            "node_id": str(payload["node_id"]),
            "type": kind,
            "repository_id": repository_id,
            "owner": owner,
            "repository": repo,
            "number": int(payload["number"]),
            "url": str(payload["html_url"]),
            "title": str(payload.get("title") or ""),
            "body": str(payload.get("body") or ""),
            "metadata": metadata,
            "updated_at": str(payload.get("updated_at") or iso_now()),
        }

    def discover_issue_or_pr(
        self,
        subject: dict[str, Any],
        payload: dict[str, Any],
        baseline: bool,
        is_pr: bool,
    ) -> None:
        owner, repo, number = subject["owner"], subject["repository"], subject["number"]
        comments = self.github.get_all(
            f"/repos/{owner}/{repo}/issues/{number}/comments?per_page=100"
        )
        for comment in comments:
            # Mutable GitHub objects keep one logical event ID. Their current
            # contents are fetched once more immediately before prompt dispatch.
            event_id = f"comment:{comment['id']}"
            actor = optional_numeric_id(comment.get("user", {}).get("id"))
            self.consider_mention(
                event_id,
                subject,
                actor,
                str(comment.get("body") or ""),
                baseline,
                str(comment.get("created_at") or ""),
                "issue_comment",
                str(comment["id"]),
            )

        events = self.github.get_all(
            f"/repos/{owner}/{repo}/issues/{number}/events?per_page=100"
        )
        assignment_events = [
            event
            for event in events
            if event.get("event") in {"assigned", "unassigned"}
            and optional_numeric_id((event.get("assignee") or {}).get("id"))
            == self.bot_id
        ]
        latest_assignment_id = (
            assignment_events[-1].get("id") if assignment_events else None
        )
        review_request_events = [
            event
            for event in events
            if event.get("event") in {"review_requested", "review_request_removed"}
            and optional_numeric_id((event.get("requested_reviewer") or {}).get("id"))
            == self.bot_id
        ]
        latest_review_request_id = (
            review_request_events[-1].get("id") if review_request_events else None
        )
        assignee_ids = {
            value
            for item in payload.get("assignees") or []
            if (value := optional_numeric_id(item.get("id"))) is not None
        }
        requested_ids: set[str] = set()
        if is_pr:
            _, requested, _ = self.github.get(
                f"/repos/{owner}/{repo}/pulls/{number}/requested_reviewers"
            )
            requested_ids = {
                value
                for item in requested.get("users") or []
                if (value := optional_numeric_id(item.get("id"))) is not None
            }
        for event in events:
            event_id = f"issue-event:{event['id']}"
            if self.db.event_state(event_id) in FINAL_EVENT_STATES:
                continue
            event_name = event.get("event")
            if event_name == "assigned":
                actor = optional_numeric_id(
                    (event.get("assigner") or event.get("actor") or {}).get("id")
                )
                assignee = optional_numeric_id((event.get("assignee") or {}).get("id"))
                accepted = (
                    actor == self.credentials.controller_id
                    and assignee == self.bot_id
                    and self.bot_id in assignee_ids
                    and event.get("id") == latest_assignment_id
                    and payload.get("state") != "closed"
                )
                self.record_action(
                    event_id,
                    subject,
                    "assignment",
                    "implement" if accepted else None,
                    actor,
                    {
                        "source_kind": "assignment",
                        "source_id": str(event["id"]),
                    },
                    baseline,
                    str(event.get("created_at") or ""),
                )
            elif event_name == "unassigned":
                actor = optional_numeric_id(
                    (event.get("assigner") or event.get("actor") or {}).get("id")
                )
                assignee = optional_numeric_id((event.get("assignee") or {}).get("id"))
                mapping = self.db.active_mapping(subject["node_id"])
                task = (
                    self.db.task_for_session(mapping["session_id"]) if mapping else None
                )
                accepted = (
                    actor == self.credentials.controller_id
                    and assignee == self.bot_id
                    and task is not None
                    and task["trigger_type"] == "assignment"
                )
                self.record_action(
                    event_id,
                    subject,
                    "unassignment",
                    "cancel" if accepted else None,
                    actor,
                    {},
                    baseline,
                    str(event.get("created_at") or ""),
                )
            elif event_name == "review_requested" and is_pr:
                actor = optional_numeric_id(
                    (event.get("review_requester") or event.get("actor") or {}).get(
                        "id"
                    )
                )
                reviewer = optional_numeric_id(
                    (event.get("requested_reviewer") or {}).get("id")
                )
                accepted = (
                    actor == self.credentials.controller_id
                    and reviewer == self.bot_id
                    and self.bot_id in requested_ids
                    and event.get("id") == latest_review_request_id
                    and payload.get("state") != "closed"
                )
                self.record_action(
                    event_id,
                    subject,
                    "review_requested",
                    "review" if accepted else None,
                    actor,
                    {
                        "source_kind": "review_requested",
                        "source_id": str(event["id"]),
                    },
                    baseline,
                    str(event.get("created_at") or ""),
                )
            elif event_name == "review_request_removed" and is_pr:
                actor = optional_numeric_id(
                    (event.get("review_requester") or event.get("actor") or {}).get(
                        "id"
                    )
                )
                reviewer = optional_numeric_id(
                    (event.get("requested_reviewer") or {}).get("id")
                )
                mapping = self.db.active_mapping(subject["node_id"])
                task = (
                    self.db.task_for_session(mapping["session_id"]) if mapping else None
                )
                accepted = (
                    actor == self.credentials.controller_id
                    and reviewer == self.bot_id
                    and task is not None
                    and task["trigger_type"] == "review_requested"
                )
                self.record_action(
                    event_id,
                    subject,
                    "review_request_removed",
                    "cancel" if accepted else None,
                    actor,
                    {},
                    baseline,
                    str(event.get("created_at") or ""),
                )
            elif event_name == "closed":
                actor = optional_numeric_id((event.get("actor") or {}).get("id"))
                accepted = actor == self.credentials.controller_id
                self.record_action(
                    event_id,
                    subject,
                    "closed",
                    "cancel" if accepted else None,
                    actor,
                    {},
                    baseline,
                    str(event.get("created_at") or ""),
                )

        if is_pr:
            reviews = self.github.get_all(
                f"/repos/{owner}/{repo}/pulls/{number}/reviews?per_page=100"
            )
            for review in reviews:
                if not review.get("submitted_at"):
                    continue
                event_id = f"review:{review['id']}"
                if self.db.event_state(event_id) in FINAL_EVENT_STATES:
                    continue
                actor = optional_numeric_id(review.get("user", {}).get("id"))
                accepted = actor == self.credentials.controller_id
                inline: list[dict[str, Any]] = []
                if accepted:
                    inline = self.github.get_all(
                        f"/repos/{owner}/{repo}/pulls/{number}/reviews/{review['id']}/comments?per_page=100"
                    )
                    inline = [
                        item
                        for item in inline
                        if optional_numeric_id(item.get("user", {}).get("id"))
                        == self.credentials.controller_id
                    ]
                feedback = {
                    "source_kind": "review",
                    "source_id": str(review["id"]),
                    "review_body": str(review.get("body") or ""),
                    "review_state": str(review.get("state") or ""),
                    "inline_comments": [
                        {
                            "url": item.get("html_url"),
                            "path": item.get("path"),
                            "line": item.get("line") or item.get("original_line"),
                            "body": item.get("body") or "",
                        }
                        for item in inline
                    ],
                }
                self.record_action(
                    event_id,
                    subject,
                    "review",
                    "continue" if accepted else None,
                    actor,
                    feedback,
                    baseline,
                    str(review.get("submitted_at") or ""),
                )

    def discover_discussion(
        self, repository_id: str, owner: str, repo: str, number: int, baseline: bool
    ) -> None:
        query = """
        query($owner: String!, $repo: String!, $number: Int!, $after: String) {
          repository(owner: $owner, name: $repo) {
            discussion(number: $number) {
              id number url title body updatedAt
              comments(first: 100, after: $after) {
                nodes {
                  id body createdAt updatedAt author { login ... on User { databaseId } }
                  replies(first: 100) {
                    nodes { id body createdAt updatedAt author { login ... on User { databaseId } } }
                    pageInfo { hasNextPage endCursor }
                  }
                }
                pageInfo { hasNextPage endCursor }
              }
            }
          }
        }
        """
        cursor: str | None = None
        discussion: dict[str, Any] | None = None
        comments: list[dict[str, Any]] = []
        while True:
            data = self.github.graphql(
                query,
                {"owner": owner, "repo": repo, "number": number, "after": cursor},
            )
            page = data.get("repository", {}).get("discussion")
            if not page:
                raise BridgeError("GitHub Discussion is unavailable")
            discussion = page
            connection = page.get("comments") or {}
            comments.extend(connection.get("nodes") or [])
            page_info = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                raise BridgeError("Discussion pagination omitted its cursor")
        if not discussion:
            raise BridgeError("GitHub Discussion is unavailable")
        reply_query = """
        query($id: ID!, $after: String) {
          node(id: $id) {
            ... on DiscussionComment {
              replies(first: 100, after: $after) {
                nodes { id body createdAt updatedAt author { login ... on User { databaseId } } }
                pageInfo { hasNextPage endCursor }
              }
            }
          }
        }
        """
        replies: list[dict[str, Any]] = []
        for comment in comments:
            connection = comment.get("replies") or {}
            replies.extend(connection.get("nodes") or [])
            page_info = connection.get("pageInfo") or {}
            cursor = page_info.get("endCursor")
            while page_info.get("hasNextPage"):
                if not cursor:
                    raise BridgeError("Discussion reply pagination omitted its cursor")
                data = self.github.graphql(
                    reply_query, {"id": comment["id"], "after": cursor}
                )
                connection = (data.get("node") or {}).get("replies") or {}
                replies.extend(connection.get("nodes") or [])
                page_info = connection.get("pageInfo") or {}
                cursor = page_info.get("endCursor")
        comments.extend(replies)
        subject = {
            "node_id": discussion["id"],
            "type": "discussion",
            "repository_id": repository_id,
            "owner": owner,
            "repository": repo,
            "number": number,
            "url": discussion["url"],
            "title": discussion.get("title") or "",
            "body": discussion.get("body") or "",
            "metadata": {},
            "updated_at": discussion.get("updatedAt") or iso_now(),
        }
        self.db.save_subject(subject)
        for comment in comments:
            event_id = f"discussion-comment:{comment['id']}"
            author = comment.get("author") or {}
            actor = optional_numeric_id(author.get("databaseId"))
            self.consider_mention(
                event_id,
                subject,
                actor,
                str(comment.get("body") or ""),
                baseline,
                str(comment.get("createdAt") or ""),
                "discussion_comment",
                str(comment["id"]),
            )

    def consider_mention(
        self,
        event_id: str,
        subject: dict[str, Any],
        actor: str | None,
        body: str,
        baseline: bool,
        event_time: str = "",
        source_kind: str = "",
        source_id: str = "",
    ) -> None:
        existing_state = self.db.event_state(event_id)
        if existing_state in {"baseline", "delivered", "dry-run"}:
            return
        if actor != self.credentials.controller_id:
            self.record_action(
                event_id,
                subject,
                "comment",
                None,
                actor,
                {"source_kind": source_kind, "source_id": source_id},
                baseline,
                event_time,
            )
            return
        try:
            command = parse_mention(body, self.bot_login)
        except BridgeError as error:
            self.db.save_event(
                event_id,
                subject["node_id"],
                "comment",
                None,
                actor,
                {"source_kind": source_kind, "source_id": source_id},
                "ignored",
                event_time,
            )
            self.db.update_event(event_id, "ignored", str(error))
            return
        if command is None:
            self.record_action(
                event_id,
                subject,
                "comment",
                None,
                actor,
                {"source_kind": source_kind, "source_id": source_id},
                baseline,
                event_time,
            )
            return
        action, instruction = command
        metadata = subject.get("metadata") or {}
        if metadata.get("state") == "closed" and action != "cancel":
            self.record_action(
                event_id,
                subject,
                "comment",
                None,
                actor,
                {"source_kind": source_kind, "source_id": source_id},
                baseline,
                event_time,
            )
            return
        self.record_action(
            event_id,
            subject,
            "mention",
            action,
            actor,
            {
                "instruction": instruction,
                "source_kind": source_kind,
                "source_id": source_id,
            },
            baseline,
            event_time,
        )

    def record_action(
        self,
        event_id: str,
        subject: dict[str, Any],
        event_type: str,
        action: str | None,
        actor: str | None,
        payload: dict[str, Any],
        baseline: bool,
        event_time: str = "",
    ) -> None:
        existing_state = self.db.event_state(event_id)
        if existing_state in {"baseline", "delivered", "dry-run"}:
            return
        state = (
            "baseline"
            if baseline or (existing_state is None and self.is_historical(event_time))
            else "ignored"
            if action is None
            else "dry-run"
            if self.config.dry_run
            else "pending"
        )
        complete = payload | {"action": action, "subject_id": subject["node_id"]}
        if existing_state == "ignored" and action is not None and actor is not None:
            self.db.replace_ignored_event(
                event_id, event_type, action, actor, complete, state
            )
            return
        self.db.save_event(
            event_id,
            subject["node_id"],
            event_type,
            action,
            actor,
            complete,
            state,
            event_time,
        )
        if state == "dry-run":
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "event": event_id,
                        "subject": subject["url"],
                        "action": action,
                    }
                )
            )

    def is_historical(self, event_time: str) -> bool:
        if self.event_cutoff is None or not event_time:
            return False
        try:
            parsed = dt.datetime.fromisoformat(event_time.replace("Z", "+00:00"))
        except ValueError:
            return True
        # GitHub can expose a notification shortly after the underlying event.
        # Keep the initial poll as the hard baseline, but tolerate bounded API
        # propagation delay on later scans so fresh commands are not discarded.
        return parsed < self.event_cutoff - EVENT_PROPAGATION_GRACE

    def refresh_subject(self, subject: sqlite3.Row) -> sqlite3.Row | None:
        if subject["type"] == "pull_request":
            payload = self.github.get_optional(
                f"/repos/{subject['owner']}/{subject['repository']}/pulls/{subject['number']}"
            )
            kind = "pull_request"
        elif subject["type"] == "issue":
            payload = self.github.get_optional(
                f"/repos/{subject['owner']}/{subject['repository']}/issues/{subject['number']}"
            )
            kind = "issue"
        else:
            data = self.github.graphql(
                """
                query($owner: String!, $repo: String!, $number: Int!) {
                  repository(owner: $owner, name: $repo) {
                    discussion(number: $number) { id number url title body updatedAt }
                  }
                }
                """,
                {
                    "owner": subject["owner"],
                    "repo": subject["repository"],
                    "number": subject["number"],
                },
            )
            discussion = (data.get("repository") or {}).get("discussion")
            if not discussion:
                return None
            self.db.save_subject(
                {
                    "node_id": discussion["id"],
                    "type": "discussion",
                    "repository_id": subject["repository_id"],
                    "owner": subject["owner"],
                    "repository": subject["repository"],
                    "number": discussion["number"],
                    "url": discussion["url"],
                    "title": discussion.get("title") or "",
                    "body": discussion.get("body") or "",
                    "metadata": {},
                    "updated_at": discussion.get("updatedAt") or iso_now(),
                }
            )
            return self.db.subject(subject["node_id"])
        if payload is None:
            return None
        self.db.save_subject(
            self.subject_from_rest(
                payload,
                kind,
                subject["repository_id"],
                subject["owner"],
                subject["repository"],
            )
        )
        return self.db.subject(subject["node_id"])

    def refresh_event(
        self, event: sqlite3.Row, subject: sqlite3.Row
    ) -> tuple[sqlite3.Row, str, dict[str, Any]] | None:
        """Refresh mutable GitHub input at the last safe point before prompting.

        Callers must check the deterministic OpenCode message first. Once that
        message exists, the agent may be processing it and later edits are
        intentionally too late.
        """
        payload = json.loads(event["payload"])
        source_kind = payload.get("source_kind")
        source_id = str(payload.get("source_id") or "")
        action = str(event["action"] or "")
        if not source_kind:
            return subject, action, payload
        refreshed_subject = self.refresh_subject(subject)
        if refreshed_subject is None:
            self.db.update_event(event["id"], "ignored", "GitHub subject was deleted")
            return None
        subject = refreshed_subject

        if source_kind == "issue_comment":
            comment = self.github.get_optional(
                f"/repos/{subject['owner']}/{subject['repository']}/issues/comments/{source_id}"
            )
            if comment is None:
                self.db.update_event(
                    event["id"], "ignored", "GitHub comment was deleted"
                )
                return None
            actor = optional_numeric_id((comment.get("user") or {}).get("id"))
            try:
                command = (
                    parse_mention(str(comment.get("body") or ""), self.bot_login)
                    if actor == self.credentials.controller_id
                    else None
                )
            except BridgeError as error:
                self.db.update_event(event["id"], "ignored", str(error))
                return None
            if command is None:
                self.db.update_event(
                    event["id"], "ignored", "current comment has no authorized command"
                )
                return None
            action, instruction = command
            payload = {
                "source_kind": source_kind,
                "source_id": source_id,
                "instruction": instruction,
            }
        elif source_kind == "discussion_comment":
            data = self.github.graphql(
                """
                query($id: ID!) {
                  node(id: $id) {
                    ... on DiscussionComment {
                      id body author { login ... on User { databaseId } }
                    }
                  }
                }
                """,
                {"id": source_id},
            )
            comment = data.get("node")
            actor = optional_numeric_id(
                ((comment or {}).get("author") or {}).get("databaseId")
            )
            try:
                command = (
                    parse_mention(
                        str((comment or {}).get("body") or ""), self.bot_login
                    )
                    if actor == self.credentials.controller_id
                    else None
                )
            except BridgeError as error:
                self.db.update_event(event["id"], "ignored", str(error))
                return None
            if command is None:
                self.db.update_event(
                    event["id"],
                    "ignored",
                    "current Discussion comment has no authorized command",
                )
                return None
            action, instruction = command
            payload = {
                "source_kind": source_kind,
                "source_id": source_id,
                "instruction": instruction,
            }
        elif source_kind == "review":
            review = self.github.get_optional(
                f"/repos/{subject['owner']}/{subject['repository']}/pulls/{subject['number']}/reviews/{source_id}"
            )
            if review is None:
                self.db.update_event(
                    event["id"], "ignored", "GitHub review was deleted"
                )
                return None
            actor = optional_numeric_id((review.get("user") or {}).get("id"))
            if actor != self.credentials.controller_id:
                self.db.update_event(
                    event["id"], "ignored", "review actor is unauthorized"
                )
                return None
            inline = self.github.get_all(
                f"/repos/{subject['owner']}/{subject['repository']}/pulls/{subject['number']}/reviews/{source_id}/comments?per_page=100"
            )
            payload = {
                "source_kind": source_kind,
                "source_id": source_id,
                "review_body": str(review.get("body") or ""),
                "review_state": str(review.get("state") or ""),
                "inline_comments": [
                    {
                        "url": item.get("html_url"),
                        "path": item.get("path"),
                        "line": item.get("line") or item.get("original_line"),
                        "body": item.get("body") or "",
                    }
                    for item in inline
                    if optional_numeric_id((item.get("user") or {}).get("id"))
                    == self.credentials.controller_id
                ],
            }
            action = "continue"
        elif source_kind == "assignment":
            issue = self.github.issue(
                subject["owner"], subject["repository"], subject["number"]
            )
            assigned = {
                optional_numeric_id((item or {}).get("id"))
                for item in issue.get("assignees") or []
            }
            timeline = self.github.get_all(
                f"/repos/{subject['owner']}/{subject['repository']}/issues/{subject['number']}/events?per_page=100"
            )
            transitions = [
                item
                for item in timeline
                if item.get("event") in {"assigned", "unassigned"}
                and optional_numeric_id((item.get("assignee") or {}).get("id"))
                == self.bot_id
            ]
            latest = transitions[-1] if transitions else {}
            actor = optional_numeric_id(
                (latest.get("assigner") or latest.get("actor") or {}).get("id")
            )
            if (
                self.bot_id not in assigned
                or str(latest.get("id") or "") != source_id
                or latest.get("event") != "assigned"
                or actor != self.credentials.controller_id
            ):
                self.db.update_event(
                    event["id"],
                    "ignored",
                    "controller assignment is no longer current",
                )
                return None
            action = "implement"
        elif source_kind == "review_requested":
            _, requested, _ = self.github.get(
                f"/repos/{subject['owner']}/{subject['repository']}/pulls/{subject['number']}/requested_reviewers"
            )
            reviewer_ids = {
                optional_numeric_id((item or {}).get("id"))
                for item in requested.get("users") or []
            }
            timeline = self.github.get_all(
                f"/repos/{subject['owner']}/{subject['repository']}/issues/{subject['number']}/events?per_page=100"
            )
            transitions = [
                item
                for item in timeline
                if item.get("event") in {"review_requested", "review_request_removed"}
                and optional_numeric_id(
                    (item.get("requested_reviewer") or {}).get("id")
                )
                == self.bot_id
            ]
            latest = transitions[-1] if transitions else {}
            actor = optional_numeric_id(
                (latest.get("review_requester") or latest.get("actor") or {}).get("id")
            )
            if (
                self.bot_id not in reviewer_ids
                or str(latest.get("id") or "") != source_id
                or latest.get("event") != "review_requested"
                or actor != self.credentials.controller_id
            ):
                self.db.update_event(
                    event["id"],
                    "ignored",
                    "controller review request is no longer current",
                )
                return None
            action = "review"

        metadata = json.loads(subject["metadata"] or "{}")
        if metadata.get("state") == "closed" and action != "cancel":
            self.db.update_event(event["id"], "ignored", "subject is closed")
            return None
        payload |= {"action": action, "subject_id": subject["node_id"]}
        self.db.update_event_content(event["id"], action, payload)
        return subject, action, payload

    def process_pending_events(self) -> None:
        blocked_subjects: set[str] = set()
        blocked_sessions: set[str] = set()
        for row in self.db.pending_events():
            mapping = self.db.active_mapping(row["subject_node_id"])
            if row["subject_node_id"] in blocked_subjects or (
                mapping is not None and mapping["session_id"] in blocked_sessions
            ):
                continue
            try:
                payload = json.loads(row["payload"])
                action = row["action"]
                subject = self.db.subject(row["subject_node_id"])
                if action == "cancel":
                    if self.db.active_mapping(subject["node_id"]) is not None:
                        self.cancel(subject)
                    self.db.update_event(row["id"], "delivered")
                elif action == "continue":
                    if self.db.active_mapping(subject["node_id"]) is None:
                        if row["type"] == "review":
                            created_at = dt.datetime.fromisoformat(row["created_at"])
                            metadata = json.loads(subject["metadata"] or "{}")
                            if utc_now() - created_at < REGISTRATION_GRACE:
                                continue
                            if (
                                subject["type"] == "pull_request"
                                and metadata.get("author_id") == self.bot_id
                            ):
                                self.start_task(
                                    row["id"], subject, "implement", payload
                                )
                                continue
                            self.db.update_event(
                                row["id"],
                                "ignored",
                                "review has no active bot-authored PR session",
                            )
                            continue
                        self.db.update_event(
                            row["id"], "ignored", "subject has no active session"
                        )
                    else:
                        self.continue_task(row["id"], subject, payload)
                else:
                    metadata = json.loads(subject["metadata"] or "{}")
                    if metadata.get("state") == "closed":
                        self.db.update_event(row["id"], "ignored", "subject is closed")
                    else:
                        self.start_task(row["id"], subject, action, payload)
            except Exception as error:  # noqa: BLE001 - persist operational failures for retry
                self.db.update_event(row["id"], "pending", str(error))
                blocked_subjects.add(row["subject_node_id"])
                if mapping is not None:
                    blocked_sessions.add(mapping["session_id"])
                print(f"event {row['id']} remains pending: {error}", file=sys.stderr)

    def start_task(
        self, event_id: str, subject: sqlite3.Row, action: str, payload: dict[str, Any]
    ) -> None:
        task_id = task_id_for(subject, event_id)
        if (
            self.db.task(task_id) is None
            and self.db.active_task_count() >= self.config.max_tasks
        ):
            raise BridgeError("maximum concurrent automated task count reached")
        self.opencode.validate_model(
            str(self.config.workspaces_root),
            self.config.provider_id,
            self.config.model_id,
        )
        prepared = self.workspaces.prepare(task_id, subject, action)
        if str(prepared["repository_id"]) != subject["repository_id"]:
            raise BridgeError("workspace repository does not match the GitHub subject")
        directory = str(prepared["path"])
        title = (
            f"GitHub {subject['owner']}/{subject['repository']}#{subject['number']} "
            f"{action} [{task_id}]"
        )
        session_id: str | None = None
        try:
            session_id = self.opencode.find_session(directory, title)
            message_id = stable_id("msg_", event_id, 26)
            if session_id is not None and self.opencode.message_exists(
                session_id, directory, message_id
            ):
                self.db.save_task(
                    task_id,
                    session_id,
                    directory,
                    str(prepared["repository_id"]),
                    action,
                    True,
                    "active",
                    self.db.event(event_id)["type"],
                )
                self.db.activate_mapping(subject["node_id"], session_id, action)
                self.db.update_event(event_id, "delivered")
                return

            # Workspace preparation can take long enough for GitHub content or
            # a PR head to change. Refresh at the final pre-session boundary.
            old_metadata = json.loads(subject["metadata"] or "{}")
            refreshed = self.refresh_event(self.db.event(event_id), subject)
            if refreshed is None:
                self.discard_unprompted_task(task_id, session_id, directory)
                return
            subject, refreshed_action, payload = refreshed
            new_metadata = json.loads(subject["metadata"] or "{}")
            action_changed = (
                self.db.event(event_id)["type"] != "review"
                and refreshed_action != action
            )
            head_changed = (
                subject["type"] == "pull_request"
                and old_metadata.get("head_sha")
                and old_metadata.get("head_sha") != new_metadata.get("head_sha")
            )
            if action_changed or head_changed:
                self.discard_unprompted_task(task_id, session_id, directory)
                return

            refreshed_title = (
                f"GitHub {subject['owner']}/{subject['repository']}#{subject['number']} "
                f"{action} [{task_id}]"
            )
            if session_id is not None and refreshed_title != title:
                self.opencode.delete(session_id, directory)
                session_id = None
            title = refreshed_title

            if session_id is None:
                session_id = self.opencode.create_session(directory, title)
            self.db.save_task(
                task_id,
                session_id,
                directory,
                str(prepared["repository_id"]),
                action,
                True,
                "queued",
                self.db.event(event_id)["type"],
            )
            system_text = ACTION_SYSTEM[action]
            context_text = self.context(subject, action, payload, directory)
            if not self.opencode.message_exists(session_id, directory, message_id):
                self.opencode.prompt(
                    session_id,
                    directory,
                    message_id,
                    self.config.agent,
                    self.config.provider_id,
                    self.config.model_id,
                    system_text,
                    context_text,
                )
            self.db.activate_mapping(subject["node_id"], session_id, action)
            self.db.set_task_state(session_id, "active")
            self.db.update_event(event_id, "delivered")
        except Exception:
            # Keep deterministic workspace/session artifacts: retries can discover an
            # accepted prompt by title and message ID without duplicating side effects.
            raise

    def discard_unprompted_task(
        self, task_id: str, session_id: str | None, directory: str
    ) -> None:
        """Remove artifacts only after proving the deterministic prompt is absent."""
        existing = self.db.task(task_id)
        stored_session = existing["session_id"] if existing is not None else None
        for candidate in {session_id, stored_session} - {None}:
            try:
                self.opencode.delete(str(candidate), directory)
            except BridgeError:
                pass
            self.db.delete_session_records(str(candidate))
        self.workspaces.remove(task_id)

    def continue_task(
        self, event_id: str, subject: sqlite3.Row, payload: dict[str, Any]
    ) -> None:
        mapping = self.db.active_mapping(subject["node_id"])
        if mapping is None:
            raise BridgeError("subject has no active OpenCode session")
        task = self.db.task_for_session(mapping["session_id"])
        if task is None:
            raise BridgeError("active session has no bridge task record")
        directory = task["directory"]
        if (
            task["automated"]
            and task["state"] not in ACTIVE_TASK_STATES
            and self.db.active_task_count() >= self.config.max_tasks
        ):
            raise BridgeError("maximum concurrent automated task count reached")
        if task["state"] == "collected":
            restore_subject = (
                self.db.pull_subject_for_session(mapping["session_id"]) or subject
            )
            self.workspaces.restore(task["id"], restore_subject, task["action"])
        self.opencode.validate_model(
            directory, self.config.provider_id, self.config.model_id
        )
        self.db.set_task_state(mapping["session_id"], "active")
        self.db.queue_delivery(
            event_id,
            subject["node_id"],
            mapping["session_id"],
            "continue",
            ACTION_SYSTEM["continue"],
            self.context(subject, "continue", payload, directory),
            coalesce=subject["type"] == "pull_request" and "review_body" in payload,
        )
        self.db.update_event(event_id, "queued")

    def cancel(self, subject: sqlite3.Row) -> None:
        mapping = self.db.active_mapping(subject["node_id"])
        if mapping is None:
            raise BridgeError("subject has no active OpenCode session")
        task = self.db.task_for_session(mapping["session_id"])
        if task is None:
            raise BridgeError("active session has no bridge task record")
        self.db.cancel_session(mapping["session_id"])
        self.opencode.abort(mapping["session_id"], task["directory"])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = self.opencode.statuses(task["directory"]).get(
                mapping["session_id"], {}
            )
            if state.get("type", "idle") == "idle":
                return
            time.sleep(0.5)
        raise BridgeError("OpenCode session did not become idle after cancellation")

    def dispatch_pending(self) -> None:
        for delivery in self.db.pending_deliveries():
            task = self.db.task_for_session(delivery["session_id"])
            if task is None or task["state"] == "paused":
                continue
            status = self.opencode.statuses(task["directory"]).get(
                delivery["session_id"], {}
            )
            if status.get("type", "idle") != "idle":
                continue
            delivery_state = delivery["state"]
            try:
                # An existing deterministic message is the edit cutoff: the
                # agent may already be processing that exact prompt.
                if self.opencode.message_exists(
                    delivery["session_id"], task["directory"], delivery["message_id"]
                ):
                    self.db.finalize_delivery(delivery["id"])
                    continue

                contexts: list[tuple[sqlite3.Row, sqlite3.Row, str]] = []
                for event in self.db.delivery_events(delivery["id"]):
                    subject = self.db.subject(event["subject_node_id"])
                    refreshed = self.refresh_event(event, subject)
                    if refreshed is None:
                        self.db.remove_delivery_event(delivery["id"], event["id"])
                        continue
                    subject, action, payload = refreshed
                    if action == "cancel":
                        self.cancel(subject)
                        self.db.update_event(event["id"], "delivered")
                        self.db.remove_delivery_event(delivery["id"], event["id"])
                        continue
                    if action != "continue":
                        self.db.update_event(event["id"], "pending")
                        self.db.remove_delivery_event(delivery["id"], event["id"])
                        continue
                    if subject["type"] == "pull_request" and task["automated"]:
                        remote_head = json.loads(subject["metadata"] or "{}").get(
                            "head_sha"
                        )
                        local_head = self.workspaces.head(task["id"])
                        payload = payload | {
                            "local_head": local_head,
                            "head_mismatch": bool(
                                remote_head and local_head != remote_head
                            ),
                        }
                    contexts.append(
                        (
                            event,
                            subject,
                            self.context(
                                subject, "continue", payload, task["directory"]
                            ),
                        )
                    )
                if not contexts:
                    self.db.mark_delivery(delivery["id"], "cancelled")
                    continue

                selected: list[str] = []
                size = 0
                separator = "\n\n---\n\n"
                for event, subject, context_text in contexts:
                    added = len(context_text) + (len(separator) if selected else 0)
                    if selected and size + added > 32768:
                        self.db.move_delivery_event(
                            delivery["id"],
                            event,
                            subject["node_id"],
                            delivery["session_id"],
                            ACTION_SYSTEM["continue"],
                            context_text,
                        )
                        continue
                    selected.append(context_text)
                    size += added
                context_text = separator.join(selected)
                self.db.update_delivery_content(delivery["id"], context_text)
                # Seal the batch before the asynchronous request. New feedback
                # must never coalesce into a prompt that may already exist.
                self.db.mark_delivery(delivery["id"], "dispatching")
                delivery_state = "dispatching"
                self.opencode.prompt(
                    delivery["session_id"],
                    task["directory"],
                    delivery["message_id"],
                    self.config.agent,
                    self.config.provider_id,
                    self.config.model_id,
                    delivery["system_text"],
                    context_text,
                )
                self.db.finalize_delivery(delivery["id"])
            except Exception as error:  # noqa: BLE001
                self.db.mark_delivery(delivery["id"], delivery_state, str(error))

    @staticmethod
    def context(
        subject: sqlite3.Row, action: str, payload: dict[str, Any], directory: str
    ) -> str:
        # Clients render prompts as Markdown, where ordinary single newlines are
        # soft breaks. Bullets preserve the field layout in mobile clients.
        lines = [
            "## GitHub Task",
            "",
            f"- Action: `{action}`",
            f"- Subject: {subject['url']}",
            f"- Repository: `{subject['owner']}/{subject['repository']}`",
            f"- Title: {subject['title']}",
            f"- Workspace: `{directory}`",
        ]
        metadata = json.loads(subject["metadata"] or "{}")
        if subject["type"] == "pull_request":
            lines.extend(
                [
                    f"- Base: `{metadata.get('base_ref') or '?'}` ({metadata.get('base_sha') or '?'})",
                    f"- Head: `{metadata.get('head_ref') or '?'}` ({metadata.get('head_sha') or '?'})",
                ]
            )
        if payload.get("local_head"):
            lines.append(f"- Local checkout HEAD: `{payload['local_head']}`")
        if payload.get("head_mismatch"):
            lines.extend(
                [
                    "",
                    "The PR head changed after this workspace was prepared. Fetch and "
                    "reconcile the current remote head before applying feedback.",
                ]
            )
        instruction = payload.get("instruction")
        if instruction:
            lines.extend(
                ["", "## Verified Controller Instruction", "", str(instruction)]
            )
        if payload.get("review_body") or payload.get("inline_comments"):
            lines.extend(["", "## Verified Controller Review Feedback", ""])
            if payload.get("review_body"):
                lines.append(str(payload["review_body"]))
            for comment in payload.get("inline_comments") or []:
                location = (
                    f"{comment.get('path') or 'unknown'}:{comment.get('line') or '?'}"
                )
                lines.append(
                    f"- {location} {comment.get('url') or ''}: {comment.get('body') or ''}"
                )
        if action != "continue":
            lines.extend(
                [
                    "",
                    "## Untrusted GitHub Reference Content",
                    "",
                    str(subject["body"])[:16384],
                ]
            )
        return "\n".join(lines)[:32768]

    def process_registrations(self) -> None:
        inbox = self.config.state_root / "inbox"
        responses = self.config.state_root / "responses"
        inbox.mkdir(parents=True, exist_ok=True)
        responses.mkdir(parents=True, exist_ok=True)
        stale_before = time.time() - 86400
        for response_file in responses.glob("response-*.json"):
            if response_file.stat().st_mtime < stale_before:
                response_file.unlink(missing_ok=True)
        entries: list[tuple[int, Path]] = []
        for entry in inbox.iterdir():
            if not re.fullmatch(r"request-[a-f0-9]{32}\.json", entry.name):
                continue
            try:
                metadata = entry.lstat()
            except FileNotFoundError:
                continue
            if stat_module.S_ISREG(metadata.st_mode):
                entries.append((metadata.st_mtime_ns, entry))
        for _, entry in sorted(entries, key=lambda item: (item[0], item[1].name)):
            request_id = entry.name[8:-5]
            response: dict[str, Any]
            try:
                flags = (
                    os.O_RDONLY
                    | os.O_NONBLOCK
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                descriptor = os.open(entry, flags)
                try:
                    metadata = os.fstat(descriptor)
                    if not stat_module.S_ISREG(metadata.st_mode):
                        raise BridgeError("registration request is not a regular file")
                    if metadata.st_size > 65536:
                        raise BridgeError("registration request is too large")
                    data = json.loads(os.read(descriptor, 65537))
                finally:
                    os.close(descriptor)
                if not isinstance(data, dict):
                    raise BridgeError("managed request must be a JSON object")
                if data.get("request_id") != request_id:
                    raise BridgeError("managed request ID does not match its filename")
                operation = data.get("operation")
                if operation is None and "pr_url" in data:
                    # Accept requests created by the previously deployed plugin.
                    response = self.register_pr(data)
                elif data.get("version") != 1:
                    raise BridgeError("unsupported managed request version")
                elif operation == "track_pr":
                    response = self.register_pr(data)
                elif operation == "manage_github_remote":
                    response = self.manage_github_remote(data)
                else:
                    raise BridgeError("unsupported managed request operation")
            except Exception as error:  # noqa: BLE001
                response = {"ok": False, "request_id": request_id, "error": str(error)}
            temporary = responses / f".{request_id}.{secrets.token_hex(4)}.tmp"
            destination = responses / f"response-{request_id}.json"
            temporary.write_text(json.dumps(response))
            os.chmod(temporary, 0o640)
            os.replace(temporary, destination)
            entry.unlink(missing_ok=True)

    def register_pr(self, data: dict[str, Any]) -> dict[str, Any]:
        request_id = str(data.get("request_id") or "")
        if not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise BridgeError("invalid registration request ID")
        expires_at = data.get("expires_at")
        if not isinstance(expires_at, int) or expires_at < int(time.time() * 1000):
            raise BridgeError("registration request expired")
        session_id = str(data.get("session_id") or "")
        directory = str(Path(str(data.get("directory") or "")).resolve())
        workspace_root = str(self.config.workspaces_root.resolve()) + os.sep
        if not directory.startswith(workspace_root):
            raise BridgeError("registration directory is outside the workspace root")
        arguments = data.get("arguments")
        pr_url = data.get("pr_url")
        if pr_url is None and isinstance(arguments, dict):
            pr_url = arguments.get("pr_url")
        owner, repo, number = parse_github_pr_url(str(pr_url or ""))
        pull = self.github.pull(owner, repo, number)
        if numeric_id(pull.get("user", {}).get("id"), "PR author ID") != self.bot_id:
            raise BridgeError("pull request was not authored by the bot account")
        session = self.opencode.get_session(session_id, directory)
        if str(Path(str(session.get("directory") or "")).resolve()) != directory:
            raise BridgeError("OpenCode session directory does not match registration")
        repository = pull.get("base", {}).get("repo") or {}
        subject = self.subject_from_rest(
            pull,
            "pull_request",
            numeric_id(repository.get("id"), "repository ID"),
            str(repository.get("owner", {}).get("login") or owner),
            str(repository.get("name") or repo),
        )
        self.db.save_subject(subject)
        task = self.db.task_for_session(session_id)
        repository_id = str(subject["repository_id"])
        if task is not None and task["repository_id"] != repository_id:
            raise BridgeError("pull request repository does not match the session task")
        if task is None:
            automated = directory.startswith(
                str(self.config.workspaces_root / ".tasks") + os.sep
            )
            task_id = (
                Path(directory).name if automated else stable_id("manual-", session_id)
            )
            self.db.save_task(
                task_id,
                session_id,
                directory,
                repository_id,
                "implement",
                automated,
                "active",
            )
        self.db.activate_mapping(subject["node_id"], session_id, "implement")
        return {
            "ok": True,
            "request_id": request_id,
            "subject_node_id": subject["node_id"],
        }

    def manage_github_remote(self, data: dict[str, Any]) -> dict[str, Any]:
        """Authorize one constrained Git operation for the requesting session."""
        if self.config.dry_run:
            raise BridgeError("GitHub remote management is disabled in dry-run mode")
        expected_keys = {
            "version",
            "operation",
            "request_id",
            "session_id",
            "directory",
            "expires_at",
            "arguments",
        }
        if set(data) != expected_keys:
            raise BridgeError("managed request has unexpected fields")
        request_id = str(data.get("request_id") or "")
        if not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise BridgeError("invalid managed request ID")
        now = int(time.time() * 1000)
        expires_at = data.get("expires_at")
        if (
            not isinstance(expires_at, int)
            or isinstance(expires_at, bool)
            or expires_at < now
            or expires_at > now + 300000
        ):
            raise BridgeError("managed request has an invalid expiry")

        session_id = str(data.get("session_id") or "")
        if not re.fullmatch(r"ses_[A-Za-z0-9]+", session_id):
            raise BridgeError("invalid OpenCode session ID")
        raw_directory = str(data.get("directory") or "")
        directory = Path(raw_directory).resolve()
        root = self.config.workspaces_root.resolve()
        try:
            relative = directory.relative_to(root)
        except ValueError as error:
            raise BridgeError(
                "managed request directory is outside the workspace root"
            ) from error
        parts = relative.parts
        if len(parts) == 1 and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", parts[0]
        ):
            kind, name = "manual", parts[0]
        elif (
            len(parts) == 2
            and parts[0] == ".tasks"
            and re.fullmatch(r"task-[a-z0-9][a-z0-9-]{7,95}", parts[1])
        ):
            kind, name = "task", parts[1]
        else:
            raise BridgeError("managed request directory is not a workspace root")
        session = self.opencode.get_session(session_id, str(directory))
        if str(Path(str(session.get("directory") or "")).resolve()) != str(directory):
            raise BridgeError(
                "OpenCode session directory does not match managed request"
            )

        arguments = data.get("arguments")
        if not isinstance(arguments, dict):
            raise BridgeError("managed request arguments must be an object")
        action = str(arguments.get("action") or "")
        if action not in {"setup", "set", "fetch", "track"}:
            raise BridgeError("invalid GitHub remote action")
        action_keys = {
            "setup": {"action", "repository", "upstream_repository"},
            "set": {"action", "repository", "remote"},
            "fetch": {"action", "remote"},
            "track": {"action", "remote", "branch"},
        }[action]
        if not set(arguments).issubset(action_keys):
            raise BridgeError("GitHub remote action has unexpected arguments")
        repository = str(arguments.get("repository") or "-")
        upstream = str(arguments.get("upstream_repository") or "-")
        remote = str(
            arguments.get("remote") or ("all" if action == "fetch" else "origin")
        )
        branch = str(arguments.get("branch") or "-")
        if repository != "-":
            repository = parse_github_repository(repository)
        if upstream != "-":
            upstream = parse_github_repository(upstream)
        if remote not in {"origin", "source", "upstream", "all"}:
            raise BridgeError("invalid managed remote")
        if len(branch) > 255 or any(character in branch for character in "\n\r\0"):
            raise BridgeError("invalid branch name")
        if action in {"setup", "set"} and repository == "-":
            raise BridgeError("repository is required for this action")
        if action != "setup" and upstream != "-":
            raise BridgeError("upstream_repository is valid only for setup")
        if action == "set" and remote == "all":
            raise BridgeError("set requires one managed remote")
        if action == "track" and remote == "all":
            raise BridgeError("track requires one managed remote")
        if action == "setup" and remote != "origin":
            raise BridgeError("setup does not accept a remote override")

        result = self.workspaces.manage_github(
            kind, name, action, repository, upstream, remote, branch
        )
        return {"ok": True, "request_id": request_id, "result": result}

    def update_completed_tasks(self) -> None:
        for task in self.db.status_rows():
            if task["state"] != "active":
                continue
            updated = dt.datetime.fromisoformat(task["updated_at"])
            if utc_now() - updated < dt.timedelta(seconds=30):
                continue
            state = self.opencode.statuses(task["directory"]).get(
                task["session_id"], {}
            )
            if state.get("type", "idle") == "idle":
                self.db.set_task_state(task["session_id"], "completed")

    def cleanup_tasks(self) -> None:
        cutoff = utc_now() - dt.timedelta(days=self.config.retention_days)
        for task in self.db.status_rows():
            if (
                not task["automated"]
                or task["state"] not in {"completed", "cleanup_blocked", "collecting"}
                or not task["completed_at"]
            ):
                continue
            completed = dt.datetime.fromisoformat(task["completed_at"])
            if completed > cutoff:
                continue
            try:
                if task["state"] == "collecting":
                    self.workspaces.remove(task["id"])
                    with self.db.connection:
                        self.db.connection.execute(
                            "UPDATE tasks SET state='collected', collected_at=?, updated_at=? WHERE id=?",
                            (iso_now(), iso_now(), task["id"]),
                        )
                    continue
                if self.db.has_pending_delivery(task["session_id"]):
                    if self.db.mark_cleanup_blocked(task["id"], "pending-feedback"):
                        print(
                            f"task={task['id']} cleanup_blocked reason=pending-feedback",
                            file=sys.stderr,
                        )
                    continue
                status = self.opencode.statuses(task["directory"]).get(
                    task["session_id"], {"type": "idle"}
                )
                if status.get("type") != "idle":
                    reason = f"session-{status.get('type', 'unknown')}"
                    if self.db.mark_cleanup_blocked(task["id"], reason):
                        print(
                            f"task={task['id']} cleanup_blocked reason={reason}",
                            file=sys.stderr,
                        )
                    continue
                inspection = self.workspaces.inspect(task["id"])
                if inspection["state"] != "clean":
                    if self.db.mark_cleanup_blocked(task["id"], inspection["state"]):
                        print(
                            f"task={task['id']} cleanup_blocked reason={inspection['state']}",
                            file=sys.stderr,
                        )
                    continue
                with self.db.connection:
                    self.db.connection.execute(
                        "UPDATE tasks SET state='collecting', updated_at=? WHERE id=?",
                        (iso_now(), task["id"]),
                    )
                self.workspaces.remove(task["id"])
                with self.db.connection:
                    self.db.connection.execute(
                        "UPDATE tasks SET state='collected', collected_at=?, updated_at=? WHERE id=?",
                        (iso_now(), iso_now(), task["id"]),
                    )
            except Exception as error:  # noqa: BLE001
                current = self.db.task(task["id"])
                if current is not None and current["state"] == "collecting":
                    print(
                        f"task={task['id']} collection will be retried: {error}",
                        file=sys.stderr,
                    )
                    continue
                if self.db.mark_cleanup_blocked(task["id"], str(error)):
                    print(
                        f"task={task['id']} cleanup_blocked reason={error}",
                        file=sys.stderr,
                    )

    def delete_session(self, session_id: str) -> None:
        task = self.db.task_for_session(session_id)
        if task is None:
            raise BridgeError("session has no bridge task record")
        if task["state"] in {"active", "queued"}:
            raise BridgeError("refusing to delete an active session")
        statuses = self.opencode.statuses(task["directory"])
        status = statuses.get(session_id, {"type": "idle"})
        if status.get("type") != "idle":
            raise BridgeError("refusing to delete a non-idle session")
        if task["automated"] and task["state"] != "collected":
            self.workspaces.remove(task["id"])
        self.opencode.delete(session_id, task["directory"])
        self.db.delete_session_records(session_id)


def build_bridge(config: Config) -> Bridge:
    credentials = Credentials.load()
    database = Database(config.state_root / "bridge.sqlite")
    http = HttpClient()
    github = GitHubClient(
        http, credentials.github_token, config.github_api_url, config.github_graphql_url
    )
    opencode = OpenCodeClient(http, config.opencode_url, credentials.server_password)
    workspaces = WorkspaceManager(config.workspace_manager)
    return Bridge(config, credentials, database, github, opencode, workspaces)


def main() -> int:
    parser = argparse.ArgumentParser(prog="github-bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run")
    status = subparsers.add_parser("status")
    status.add_argument("--blocked", action="store_true")
    sessions = subparsers.add_parser("sessions")
    sessions.add_argument("--older-than", type=parse_duration, required=True)
    delete = subparsers.add_parser("delete-session")
    delete.add_argument("session_id")
    delete_many = subparsers.add_parser("delete-sessions")
    delete_many.add_argument("--older-than", type=parse_duration, required=True)
    delete_many.add_argument("--confirm", action="store_true")
    arguments = parser.parse_args()

    config = Config.from_env()
    if arguments.command in {"status", "sessions"}:
        database_path = config.state_root / "bridge.sqlite"
        if not database_path.exists():
            return 0
        database = Database(database_path, readonly=True)
        if arguments.command == "status":
            for row in database.status_rows(arguments.blocked):
                print(
                    "\t".join(
                        str(row[key] or "")
                        for key in (
                            "id",
                            "session_id",
                            "state",
                            "cleanup_reason",
                            "directory",
                            "updated_at",
                        )
                    )
                )
        else:
            cutoff = (utc_now() - arguments.older_than).isoformat(timespec="seconds")
            for row in database.sessions_older_than(cutoff):
                print(
                    f"{row['session_id']}\t{row['state']}\t{row['updated_at']}\t{row['directory']}"
                )
        database.connection.close()
        return 0

    bridge = build_bridge(config)
    try:
        if arguments.command == "run":
            bridge.run()
        elif arguments.command == "delete-session":
            bridge.delete_session(arguments.session_id)
        elif arguments.command == "delete-sessions":
            if not arguments.confirm:
                raise BridgeError("--confirm is required")
            cutoff = (utc_now() - arguments.older_than).isoformat(timespec="seconds")
            for row in bridge.db.sessions_older_than(cutoff):
                bridge.delete_session(row["session_id"])
    finally:
        bridge.db.connection.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BridgeError as error:
        print(f"github-bridge: {error}", file=sys.stderr)
        raise SystemExit(1) from error
