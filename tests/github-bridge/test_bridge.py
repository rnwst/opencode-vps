#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import http.server
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import datetime as dt
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


SOURCE = Path(os.environ["GITHUB_BRIDGE_SOURCE"])
SPEC = importlib.util.spec_from_file_location("github_bridge", SOURCE)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)


class FakeGitHub:
    def __init__(self) -> None:
        self.pulls: dict[tuple[str, str, int], dict[str, Any]] = {}
        self.lists: dict[str, list[dict[str, Any]]] = {}
        self.responses: dict[str, dict[str, Any]] = {}
        self.graphql_responses: list[dict[str, Any]] = []

    def user(self) -> dict[str, Any]:
        return {"id": 200, "login": "rnwst-bot"}

    def pull(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return self.pulls.get(
            (owner, repo, number), self.rest_subject("pull_request", number)
        )

    def issue(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return self.responses.get(
            f"/repos/{owner}/{repo}/issues/{number}", self.rest_subject("issue", number)
        )

    @staticmethod
    def rest_subject(kind: str, number: int) -> dict[str, Any]:
        value = subject("PR_1" if kind == "pull_request" else "I_1", kind, number)
        return {
            "node_id": value["node_id"],
            "number": number,
            "html_url": value["url"],
            "title": value["title"],
            "body": value["body"],
            "state": "open",
            "updated_at": value["updated_at"],
            "user": {"id": 200},
            "assignees": [{"id": 200}],
            "base": {"ref": "main", "sha": "base-sha"},
            "head": {"ref": "feature", "sha": "head-sha"},
        }

    def get_all(self, path: str) -> list[dict[str, Any]]:
        return self.lists.get(path, [])

    def get(self, path: str) -> tuple[int, dict[str, Any], dict[str, str]]:
        return 200, self.responses.get(path, {}), {}

    def get_optional(self, path: str) -> dict[str, Any] | None:
        if path in self.responses:
            return self.responses[path]
        match = re.fullmatch(r"/repos/owner/repo/issues/(\d+)", path)
        if match:
            return self.rest_subject("issue", int(match.group(1)))
        match = re.fullmatch(r"/repos/owner/repo/pulls/(\d+)", path)
        if match:
            number = int(match.group(1))
            return self.pulls.get(
                ("owner", "repo", number), self.rest_subject("pull_request", number)
            )
        match = re.fullmatch(r"/repos/owner/repo/pulls/(\d+)/reviews/(\d+)", path)
        if match:
            number, review_id = map(int, match.groups())
            reviews = self.lists.get(
                f"/repos/owner/repo/pulls/{number}/reviews?per_page=100", []
            )
            return next(
                (review for review in reviews if int(review["id"]) == review_id), None
            )
        return None

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return self.graphql_responses.pop(0)


class FakeOpenCode:
    def __init__(self) -> None:
        self.next_session = 1
        self.sessions: dict[str, dict[str, Any]] = {}
        self.prompts: list[dict[str, Any]] = []
        self.aborts: list[str] = []
        self.deleted: list[str] = []

    def create_session(self, directory: str, title: str) -> str:
        session_id = f"ses_test_{self.next_session}"
        self.next_session += 1
        self.sessions[session_id] = {
            "id": session_id,
            "directory": directory,
            "title": title,
        }
        return session_id

    def find_session(self, directory: str, title: str) -> str | None:
        for session_id, session in self.sessions.items():
            if session.get("directory") == directory and session.get("title") == title:
                return session_id
        return None

    def validate_model(self, directory: str, provider_id: str, model_id: str) -> None:
        return None

    def get_session(self, session_id: str, directory: str) -> dict[str, Any]:
        return self.sessions[session_id]

    def statuses(self, directory: str) -> dict[str, Any]:
        return {session_id: {"type": "idle"} for session_id in self.sessions}

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
        self.prompts.append(
            {
                "session_id": session_id,
                "directory": directory,
                "message_id": message_id,
                "agent": agent,
                "provider_id": provider_id,
                "model_id": model_id,
                "system": system_text,
                "context": context_text,
            }
        )

    def message_exists(self, session_id: str, directory: str, message_id: str) -> bool:
        return any(item["message_id"] == message_id for item in self.prompts)

    def abort(self, session_id: str, directory: str) -> None:
        self.aborts.append(session_id)

    def delete(self, session_id: str, directory: str) -> None:
        self.deleted.append(session_id)


class FakeWorkspaceManager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.prepared: list[str] = []
        self.removed: list[str] = []

    def prepare(
        self, task_id: str, subject: sqlite3.Row, action: str
    ) -> dict[str, Any]:
        self.prepared.append(task_id)
        path = self.root / ".tasks" / task_id
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "repository": "owner/repo", "repository_id": "300"}

    def restore(self, task_id: str, subject: sqlite3.Row, action: str) -> str:
        path = self.root / ".tasks" / task_id
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def inspect(self, task_id: str) -> dict[str, Any]:
        return {"state": "clean"}

    def head(self, task_id: str) -> str:
        return "head-sha"

    def remove(self, task_id: str) -> None:
        self.removed.append(task_id)


def subject(node_id: str, kind: str = "issue", number: int = 1) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "type": kind,
        "repository_id": "300",
        "owner": "owner",
        "repository": "repo",
        "number": number,
        "url": f"https://github.com/owner/repo/{'pull' if kind == 'pull_request' else 'issues'}/{number}",
        "title": f"Subject {number}",
        "body": "Reference body from GitHub",
        "updated_at": "2026-09-02T00:00:00Z",
    }


class ModelConfigTest(unittest.TestCase):
    def test_default_model_is_regular_astra(self):
        with patch.dict(os.environ, {}, clear=True):
            config = bridge.Config.from_env()
        self.assertEqual((config.provider_id, config.model_id), ("openai", "gpt-6-astra"))

    def test_wrapper_model_selection_overrides_fallback(self):
        with patch.dict(os.environ, {
            "GITHUB_BRIDGE_PROVIDER": "test-provider",
            "GITHUB_BRIDGE_MODEL": "test-model",
        }, clear=True):
            config = bridge.Config.from_env()
        self.assertEqual((config.provider_id, config.model_id), ("test-provider", "test-model"))


class BridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.config = bridge.Config(
            state_root=root / "state",
            workspaces_root=root / "workspaces",
            workspace_manager="/fake/opencode-workspace",
            agent="build",
            provider_id="openai",
            model_id="test-model",
            dry_run=False,
            max_tasks=4,
            retention_days=30,
            opencode_url="http://127.0.0.1:4096",
        )
        self.database = bridge.Database()
        self.github = FakeGitHub()
        self.opencode = FakeOpenCode()
        self.workspaces = FakeWorkspaceManager(self.config.workspaces_root)
        self.instance = bridge.Bridge(
            self.config,
            bridge.Credentials("token", "password", "100"),
            self.database,
            self.github,
            self.opencode,
            self.workspaces,
        )

    def tearDown(self) -> None:
        self.database.connection.close()
        self.temporary.cleanup()

    def save_subject(self, value: dict[str, Any]) -> sqlite3.Row:
        self.database.save_subject(value)
        return self.database.subject(value["node_id"])

    def test_mention_is_valid_anywhere_and_suffix_is_optional(self) -> None:
        self.assertEqual(
            bridge.parse_mention(
                "Context first.\n\n@rnwst-bot implement\nAdd tests.", "rnwst-bot"
            ),
            ("implement", "implement Add tests."),
        )
        self.assertEqual(
            bridge.parse_mention("Please @rnwst-bot answer", "rnwst-bot"),
            ("answer", "answer"),
        )
        self.assertEqual(
            bridge.parse_mention("@rnwst-bot cancel ignored", "rnwst-bot"),
            ("cancel", "cancel"),
        )

    def test_multiple_commands_are_rejected(self) -> None:
        with self.assertRaisesRegex(bridge.BridgeError, "multiple"):
            bridge.parse_mention(
                "@rnwst-bot answer then @rnwst-bot review", "rnwst-bot"
            )

    def test_every_action_is_a_complete_bare_command(self) -> None:
        for action in ("answer", "implement", "review", "continue", "cancel"):
            with self.subTest(action=action):
                self.assertEqual(
                    bridge.parse_mention(f"Context. @rnwst-bot {action}", "rnwst-bot"),
                    (action, action),
                )

    def test_new_action_supersedes_only_its_subject_mapping(self) -> None:
        issue = self.save_subject(subject("I_1"))
        pull = self.save_subject(subject("PR_1", "pull_request", 2))
        self.database.save_task(
            "task-existing",
            "ses_existing",
            "/tmp/existing",
            "300",
            "implement",
            True,
            "active",
        )
        self.database.activate_mapping(issue["node_id"], "ses_existing", "implement")
        self.database.activate_mapping(pull["node_id"], "ses_existing", "implement")

        self.database.save_event(
            "comment:1",
            pull["node_id"],
            "mention",
            "answer",
            "100",
            {"instruction": "answer"},
            "pending",
        )
        self.instance.process_pending_events()

        self.assertEqual(
            self.database.active_mapping(issue["node_id"])["session_id"], "ses_existing"
        )
        self.assertEqual(
            self.database.active_mapping(pull["node_id"])["session_id"], "ses_test_1"
        )

    def test_any_linked_subject_can_continue_and_cancel_shared_session(self) -> None:
        issue = self.save_subject(subject("I_1"))
        pull = self.save_subject(subject("PR_1", "pull_request", 2))
        directory = str(self.config.workspaces_root / ".tasks" / "task-shared")
        self.opencode.sessions["ses_shared"] = {
            "id": "ses_shared",
            "directory": directory,
        }
        self.database.save_task(
            "task-shared", "ses_shared", directory, "300", "implement", True, "active"
        )
        self.database.activate_mapping(issue["node_id"], "ses_shared", "implement")
        self.database.activate_mapping(pull["node_id"], "ses_shared", "implement")

        self.database.save_event(
            "continue:pull",
            pull["node_id"],
            "mention",
            "continue",
            "100",
            {"instruction": "continue address feedback"},
            "pending",
        )
        self.instance.process_pending_events()
        self.instance.dispatch_pending()
        self.assertEqual(self.opencode.prompts[-1]["session_id"], "ses_shared")

        self.instance.cancel(issue)
        self.assertEqual(
            self.database.task_for_session("ses_shared")["state"], "paused"
        )
        self.assertEqual(self.opencode.aborts, ["ses_shared"])
        self.assertEqual(
            self.database.active_mapping(pull["node_id"])["session_id"], "ses_shared"
        )

        self.database.save_event(
            "continue:issue",
            issue["node_id"],
            "mention",
            "continue",
            "100",
            {"instruction": "continue"},
            "pending",
        )
        self.instance.process_pending_events()
        self.instance.dispatch_pending()
        self.assertEqual(
            self.database.task_for_session("ses_shared")["state"], "active"
        )

    def test_unauthorized_mentions_are_ignored(self) -> None:
        value = subject("I_1")
        self.database.save_subject(value)
        self.instance.consider_mention(
            "comment:bad",
            value,
            "999",
            "@rnwst-bot implement malicious change",
            baseline=False,
        )
        self.assertEqual(self.database.event_state("comment:bad"), "ignored")
        self.assertEqual(self.database.active_task_count(), 0)

    def test_closed_subject_rejects_new_commands(self) -> None:
        value = subject("I_1") | {"metadata": {"state": "closed"}}
        self.database.save_subject(value)
        self.instance.consider_mention(
            "comment:closed",
            value,
            "100",
            "@rnwst-bot implement",
            baseline=False,
        )
        self.assertEqual(self.database.event_state("comment:closed"), "ignored")

    def test_closed_subject_still_allows_controller_cancel(self) -> None:
        value = subject("I_1") | {"metadata": {"state": "closed"}}
        issue = self.save_subject(value)
        directory = str(self.config.workspaces_root / ".tasks" / "task-closed")
        self.opencode.sessions["ses_closed"] = {
            "id": "ses_closed",
            "directory": directory,
        }
        self.database.save_task(
            "task-closed", "ses_closed", directory, "300", "answer", True, "active"
        )
        self.database.activate_mapping(issue["node_id"], "ses_closed", "answer")
        self.instance.consider_mention(
            "comment:cancel-closed",
            value,
            "100",
            "@rnwst-bot cancel",
            baseline=False,
        )
        self.instance.process_pending_events()
        self.assertEqual(
            self.database.event_state("comment:cancel-closed"), "delivered"
        )
        self.assertEqual(self.opencode.aborts, ["ses_closed"])

    def test_failed_event_blocks_later_events_for_subject(self) -> None:
        issue = self.save_subject(subject("I_1"))
        for event_id in ("comment:1", "comment:2"):
            self.database.save_event(
                event_id,
                issue["node_id"],
                "mention",
                "answer",
                "100",
                {"instruction": "answer"},
                "pending",
            )
        prepare = self.workspaces.prepare

        def fail_prepare(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise bridge.BridgeError("temporary failure")

        self.workspaces.prepare = fail_prepare
        self.instance.process_pending_events()
        self.workspaces.prepare = prepare
        self.assertEqual(self.database.event_state("comment:1"), "pending")
        self.assertEqual(self.database.event_state("comment:2"), "pending")
        self.assertEqual(self.opencode.sessions, {})

    def test_review_waiting_for_registration_does_not_block_start(self) -> None:
        issue = self.save_subject(subject("I_1"))
        self.database.save_event(
            "review:1",
            issue["node_id"],
            "review",
            "continue",
            "100",
            {"review_body": "early feedback"},
            "pending",
            "2026-09-02T00:00:01Z",
        )
        self.database.save_event(
            "comment:2",
            issue["node_id"],
            "mention",
            "answer",
            "100",
            {"instruction": "answer"},
            "pending",
            "2026-09-02T00:00:02Z",
        )
        self.instance.process_pending_events()
        self.assertEqual(self.database.event_state("review:1"), "pending")
        self.assertEqual(self.database.event_state("comment:2"), "delivered")
        self.instance.process_pending_events()
        self.assertEqual(self.database.event_state("review:1"), "queued")

    def test_pending_start_is_ignored_after_subject_closes(self) -> None:
        value = subject("I_1")
        self.database.save_subject(value)
        self.database.save_event(
            "comment:start",
            value["node_id"],
            "mention",
            "answer",
            "100",
            {"instruction": "answer"},
            "pending",
        )
        self.database.save_subject(value | {"metadata": {"state": "closed"}})
        self.instance.process_pending_events()
        self.assertEqual(self.database.event_state("comment:start"), "ignored")
        self.assertEqual(self.opencode.sessions, {})

    def test_comment_instruction_is_refreshed_before_new_session(self) -> None:
        value = subject("I_1")
        issue = self.save_subject(value)
        self.instance.consider_mention(
            "comment:41",
            value,
            "100",
            "@rnwst-bot answer original",
            baseline=False,
            event_time="2026-09-05T12:00:00Z",
            source_kind="issue_comment",
            source_id="41",
        )
        self.github.responses["/repos/owner/repo/issues/comments/41"] = {
            "id": 41,
            "body": "@rnwst-bot answer edited instruction",
            "user": {"id": 100},
        }
        self.instance.process_pending_events()
        self.assertIn("edited instruction", self.opencode.prompts[0]["context"])
        self.assertNotIn("original", self.opencode.prompts[0]["context"])
        payload = json.loads(self.database.event("comment:41")["payload"])
        self.assertEqual(payload["instruction"], "answer edited instruction")
        self.assertEqual(
            self.database.task_for_session("ses_test_1")["directory"].split("/")[-1],
            bridge.task_id_for(issue, "comment:41"),
        )

    def test_changed_action_reprepares_workspace_before_session(self) -> None:
        value = subject("I_1")
        self.save_subject(value)
        self.instance.consider_mention(
            "comment:42",
            value,
            "100",
            "@rnwst-bot implement original",
            baseline=False,
            source_kind="issue_comment",
            source_id="42",
        )
        self.github.responses["/repos/owner/repo/issues/comments/42"] = {
            "id": 42,
            "body": "@rnwst-bot answer changed",
            "user": {"id": 100},
        }
        self.instance.process_pending_events()
        self.assertEqual(self.opencode.sessions, {})
        self.assertEqual(self.database.event("comment:42")["action"], "answer")
        self.instance.process_pending_events()
        self.assertEqual(len(self.opencode.sessions), 1)
        self.assertIn("answer changed", self.opencode.prompts[0]["context"])

    def test_removed_command_is_ignored_before_session(self) -> None:
        value = subject("I_1")
        self.save_subject(value)
        self.instance.consider_mention(
            "comment:43",
            value,
            "100",
            "@rnwst-bot implement",
            baseline=False,
            source_kind="issue_comment",
            source_id="43",
        )
        self.github.responses["/repos/owner/repo/issues/comments/43"] = {
            "id": 43,
            "body": "No command anymore.",
            "user": {"id": 100},
        }
        self.instance.process_pending_events()
        self.assertEqual(self.database.event_state("comment:43"), "ignored")
        self.assertEqual(self.opencode.sessions, {})

    def test_edit_after_prompt_does_not_create_a_new_event(self) -> None:
        value = subject("I_1")
        self.save_subject(value)
        self.instance.consider_mention(
            "comment:44",
            value,
            "100",
            "@rnwst-bot answer original",
            baseline=False,
        )
        self.database.update_event("comment:44", "delivered")
        self.instance.consider_mention(
            "comment:44",
            value,
            "100",
            "@rnwst-bot implement edited too late",
            baseline=False,
        )
        event = self.database.event("comment:44")
        self.assertEqual(event["state"], "delivered")
        self.assertEqual(event["action"], "answer")

    def test_ignored_comment_can_be_edited_into_a_command(self) -> None:
        value = subject("I_1")
        self.save_subject(value)
        self.instance.consider_mention(
            "comment:45", value, "100", "No command yet.", baseline=False
        )
        self.assertEqual(self.database.event_state("comment:45"), "ignored")
        self.instance.consider_mention(
            "comment:45", value, "100", "@rnwst-bot answer now", baseline=False
        )
        event = self.database.event("comment:45")
        self.assertEqual(event["state"], "pending")
        self.assertEqual(event["action"], "answer")

    def test_informative_task_id_is_safe_and_unique(self) -> None:
        pull = self.save_subject(subject("PR_1", "pull_request", 6))
        first = bridge.task_id_for(pull, "review:1")
        second = bridge.task_id_for(pull, "review:2")
        self.assertRegex(first, r"^task-owner-repo-pr-6-[0-9a-f]{12}$")
        self.assertNotEqual(first, second)

    def test_historical_command_on_new_subject_is_baselined(self) -> None:
        value = subject("I_1")
        self.database.save_subject(value)
        self.instance.event_cutoff = dt.datetime(
            2026, 9, 2, 12, 0, tzinfo=dt.timezone.utc
        )
        self.instance.consider_mention(
            "comment:old",
            value,
            "100",
            "@rnwst-bot implement",
            baseline=False,
            event_time="2026-09-02T11:54:00Z",
        )
        self.assertEqual(self.database.event_state("comment:old"), "baseline")

    def test_notification_propagation_delay_does_not_baseline_fresh_command(
        self,
    ) -> None:
        value = subject("I_1")
        self.database.save_subject(value)
        self.instance.event_cutoff = dt.datetime(
            2026, 9, 2, 12, 0, tzinfo=dt.timezone.utc
        )
        self.instance.consider_mention(
            "comment:delayed",
            value,
            "100",
            "@rnwst-bot implement",
            baseline=False,
            event_time="2026-09-02T11:59:59Z",
        )
        self.assertEqual(self.database.event_state("comment:delayed"), "pending")

    def test_pending_events_follow_source_timestamp(self) -> None:
        value = self.save_subject(subject("I_1"))
        self.database.save_event(
            "comment:later",
            value["node_id"],
            "mention",
            "cancel",
            "100",
            {"instruction": "cancel"},
            "pending",
            "2026-09-02T12:00:02Z",
        )
        self.database.save_event(
            "comment:earlier",
            value["node_id"],
            "mention",
            "continue",
            "100",
            {"instruction": "continue"},
            "pending",
            "2026-09-02T12:00:01Z",
        )
        self.assertEqual(
            [row["id"] for row in self.database.pending_events()],
            ["comment:earlier", "comment:later"],
        )

    def test_same_timestamp_events_follow_numeric_github_id(self) -> None:
        value = self.save_subject(subject("I_1"))
        for event_id in ("issue-event:10", "issue-event:9"):
            self.database.save_event(
                event_id,
                value["node_id"],
                "assignment",
                "implement",
                "100",
                {},
                "pending",
                "2026-09-02T12:00:00Z",
            )
        self.assertEqual(
            [row["id"] for row in self.database.pending_events()],
            ["issue-event:9", "issue-event:10"],
        )

    def test_notification_cursor_does_not_advance_after_partial_failure(self) -> None:
        self.github.notifications = lambda _last_modified: (
            200,
            [{"id": "ok"}, {"id": "bad"}],
            {"last-modified": "new-cursor", "x-poll-interval": "60"},
        )

        def discover(notification: dict[str, Any], baseline: bool) -> None:
            if notification["id"] == "bad":
                raise bridge.BridgeError("transient failure")

        self.instance.discover_notification = discover
        with self.assertRaisesRegex(bridge.BridgeError, "could not be discovered"):
            self.instance.poll_notifications()
        self.assertIsNone(self.database.meta("notifications_last_modified"))
        self.assertIsNone(self.database.meta("notifications_initialized"))

    def test_initial_not_modified_poll_completes_baseline(self) -> None:
        self.github.notifications = lambda _last_modified: (
            304,
            [],
            {"x-poll-interval": "60"},
        )
        self.instance.poll_notifications()
        self.assertEqual(self.database.meta("notifications_initialized"), "1")
        self.assertIsNotNone(self.database.meta("last_event_scan_at"))

    def test_only_controller_assignment_starts_implementation(self) -> None:
        value = subject("I_1")
        self.database.save_subject(value)
        events_path = "/repos/owner/repo/issues/1/events?per_page=100"
        self.github.lists[events_path] = [
            {
                "id": 10,
                "event": "assigned",
                "assigner": {"id": 999},
                "assignee": {"id": 200},
            },
            {
                "id": 11,
                "event": "assigned",
                "assigner": {"id": 100},
                "assignee": {"id": 200},
            },
        ]
        payload = {"assignees": [{"id": 200}]}
        self.instance.discover_issue_or_pr(value, payload, baseline=False, is_pr=False)
        self.assertEqual(self.database.event_state("issue-event:10"), "ignored")
        self.assertEqual(self.database.event_state("issue-event:11"), "pending")
        self.instance.process_pending_events()
        self.assertEqual(len(self.opencode.sessions), 1)
        self.assertIn(
            "Implement the requested change", self.opencode.prompts[0]["system"]
        )

    def test_unassign_reassign_pauses_old_task_before_replacement(self) -> None:
        value = self.save_subject(subject("I_1"))
        directory = str(self.config.workspaces_root / ".tasks" / "task-old")
        self.opencode.sessions["ses_old"] = {"id": "ses_old", "directory": directory}
        self.database.save_task(
            "task-old",
            "ses_old",
            directory,
            "300",
            "implement",
            True,
            "active",
            "assignment",
        )
        self.database.activate_mapping(value["node_id"], "ses_old", "implement")
        self.github.lists["/repos/owner/repo/issues/1/events?per_page=100"] = [
            {
                "id": 20,
                "event": "unassigned",
                "created_at": "2026-09-02T00:00:01Z",
                "assigner": {"id": 100},
                "assignee": {"id": 200},
            },
            {
                "id": 21,
                "event": "assigned",
                "created_at": "2026-09-02T00:00:02Z",
                "assigner": {"id": 100},
                "assignee": {"id": 200},
            },
        ]
        self.instance.discover_issue_or_pr(
            dict(value), {"assignees": [{"id": 200}]}, baseline=False, is_pr=False
        )
        self.instance.process_pending_events()
        self.assertEqual(self.database.task_for_session("ses_old")["state"], "paused")
        self.assertEqual(self.opencode.aborts, ["ses_old"])
        self.assertNotEqual(
            self.database.active_mapping(value["node_id"])["session_id"], "ses_old"
        )

    def test_retry_reuses_prepared_workspace_and_existing_session(self) -> None:
        issue = self.save_subject(subject("I_1"))
        event_id = "comment:recover"
        task_id = bridge.task_id_for(issue, event_id)
        directory = str(self.config.workspaces_root / ".tasks" / task_id)
        title = f"GitHub owner/repo#1 answer [{task_id}]"
        self.opencode.sessions["ses_recovered"] = {
            "id": "ses_recovered",
            "directory": directory,
            "title": title,
        }
        self.database.save_event(
            event_id,
            issue["node_id"],
            "mention",
            "answer",
            "100",
            {"instruction": "answer"},
            "pending",
        )
        self.instance.process_pending_events()
        self.assertEqual(list(self.opencode.sessions), ["ses_recovered"])
        self.assertEqual(
            self.database.active_mapping(issue["node_id"])["session_id"],
            "ses_recovered",
        )

    def test_retry_preserves_prompt_accepted_before_transport_failure(self) -> None:
        issue = self.save_subject(subject("I_1"))
        event_id = "comment:uncertain"
        self.database.save_event(
            event_id,
            issue["node_id"],
            "mention",
            "answer",
            "100",
            {"instruction": "answer"},
            "pending",
        )
        prompt = self.opencode.prompt

        def uncertain_prompt(*args: Any, **kwargs: Any) -> None:
            prompt(*args, **kwargs)
            raise bridge.BridgeError("response lost")

        self.opencode.prompt = uncertain_prompt
        self.instance.process_pending_events()
        task_id = bridge.task_id_for(issue, event_id)
        self.assertEqual(self.database.event_state(event_id), "pending")
        self.assertIsNotNone(self.database.task(task_id))
        self.assertEqual(self.workspaces.removed, [])

        self.opencode.prompt = prompt
        self.instance.process_pending_events()
        self.assertEqual(self.database.event_state(event_id), "delivered")
        self.assertEqual(len(self.opencode.prompts), 1)

    def test_stale_review_recovers_bot_authored_pr_without_mapping(self) -> None:
        pull = self.save_subject(
            subject("PR_1", "pull_request", 1)
            | {"metadata": {"state": "open", "author_id": "200"}}
        )
        event_id = "review:recovery"
        self.database.save_event(
            event_id,
            pull["node_id"],
            "review",
            "continue",
            "100",
            {
                "source_kind": "review",
                "source_id": "61",
                "review_body": "Original feedback.",
            },
            "pending",
        )
        self.github.lists["/repos/owner/repo/pulls/1/reviews?per_page=100"] = [
            {
                "id": 61,
                "submitted_at": "2026-09-05T12:00:00Z",
                "state": "CHANGES_REQUESTED",
                "body": "Please address the edited feedback.",
                "user": {"id": 100},
            }
        ]
        self.github.lists[
            "/repos/owner/repo/pulls/1/reviews/61/comments?per_page=100"
        ] = []
        with self.database.connection:
            self.database.connection.execute(
                "UPDATE events SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?",
                (event_id,),
            )
        self.instance.process_pending_events()
        mapping = self.database.active_mapping(pull["node_id"])
        self.assertIsNotNone(mapping)
        self.assertEqual(self.database.event_state(event_id), "delivered")
        self.assertIn(
            "Please address the edited feedback.", self.opencode.prompts[0]["context"]
        )

    def test_review_feedback_for_two_prs_stays_separate(self) -> None:
        for number in (1, 2):
            value = self.save_subject(subject(f"PR_{number}", "pull_request", number))
            session_id = f"ses_pr_{number}"
            directory = str(
                self.config.workspaces_root / ".tasks" / f"task-pr-{number}"
            )
            self.opencode.sessions[session_id] = {
                "id": session_id,
                "directory": directory,
            }
            self.database.save_task(
                f"task-pr-{number}",
                session_id,
                directory,
                "300",
                "implement",
                True,
                "active",
            )
            self.database.activate_mapping(value["node_id"], session_id, "implement")
            self.github.responses[
                f"/repos/owner/repo/pulls/{number}/requested_reviewers"
            ] = {"users": []}
            self.github.lists[
                f"/repos/owner/repo/pulls/{number}/reviews?per_page=100"
            ] = [
                {
                    "id": 100 + number,
                    "submitted_at": "2026-09-02T00:00:00Z",
                    "user": {"id": 100},
                    "state": "COMMENTED",
                    "body": f"Feedback for PR {number}",
                }
            ]
            self.github.lists[
                f"/repos/owner/repo/pulls/{number}/reviews/{100 + number}/comments?per_page=100"
            ] = [
                {
                    "user": {"id": 100},
                    "html_url": f"https://github.com/owner/repo/pull/{number}#discussion",
                    "path": "file.txt",
                    "line": number,
                    "body": f"Inline {number}",
                }
            ]
            self.instance.discover_issue_or_pr(
                dict(value), {}, baseline=False, is_pr=True
            )

        self.instance.process_pending_events()
        self.instance.dispatch_pending()
        self.assertEqual(
            {item["session_id"] for item in self.opencode.prompts},
            {"ses_pr_1", "ses_pr_2"},
        )
        self.assertIn(
            "Feedback for PR 1",
            next(
                item
                for item in self.opencode.prompts
                if item["session_id"] == "ses_pr_1"
            )["context"],
        )
        self.assertNotIn(
            "Feedback for PR 2",
            next(
                item
                for item in self.opencode.prompts
                if item["session_id"] == "ses_pr_1"
            )["context"],
        )

    def test_reviews_for_one_pr_coalesce_before_dispatch(self) -> None:
        pull = self.save_subject(subject("PR_1", "pull_request", 1))
        directory = str(self.config.workspaces_root / ".tasks" / "task-pr")
        self.opencode.sessions["ses_pr"] = {"id": "ses_pr", "directory": directory}
        self.database.save_task(
            "task-pr", "ses_pr", directory, "300", "implement", True, "active"
        )
        self.database.activate_mapping(pull["node_id"], "ses_pr", "implement")
        for number in (1, 2):
            self.database.save_event(
                f"review:{number}",
                pull["node_id"],
                "review",
                "continue",
                "100",
                {"review_body": f"Review {number}", "inline_comments": []},
                "pending",
            )
        self.instance.process_pending_events()
        pending = self.database.pending_deliveries()
        self.assertEqual(len(pending), 1)
        self.assertIn("Review 1", pending[0]["context_text"])
        self.assertIn("Review 2", pending[0]["context_text"])
        self.instance.dispatch_pending()
        self.assertEqual(self.database.event_state("review:1"), "delivered")
        self.assertEqual(self.database.event_state("review:2"), "delivered")

    def test_review_is_refreshed_before_continuation_dispatch(self) -> None:
        pull = self.save_subject(
            subject("PR_1", "pull_request", 1)
            | {"metadata": {"state": "open", "author_id": "200"}}
        )
        directory = str(self.config.workspaces_root / ".tasks" / "task-pr")
        self.opencode.sessions["ses_pr"] = {"id": "ses_pr", "directory": directory}
        self.database.save_task(
            "task-pr", "ses_pr", directory, "300", "implement", True, "active"
        )
        self.database.activate_mapping(pull["node_id"], "ses_pr", "implement")
        self.database.save_event(
            "review:51",
            pull["node_id"],
            "review",
            "continue",
            "100",
            {
                "source_kind": "review",
                "source_id": "51",
                "review_body": "original",
            },
            "pending",
        )
        self.instance.process_pending_events()
        self.github.lists["/repos/owner/repo/pulls/1/reviews?per_page=100"] = [
            {
                "id": 51,
                "submitted_at": "2026-09-05T12:00:00Z",
                "state": "CHANGES_REQUESTED",
                "body": "edited review",
                "user": {"id": 100},
            }
        ]
        self.github.lists[
            "/repos/owner/repo/pulls/1/reviews/51/comments?per_page=100"
        ] = []
        self.workspaces.head = lambda _task_id: "older-head"
        self.instance.dispatch_pending()
        self.assertIn("edited review", self.opencode.prompts[0]["context"])
        self.assertNotIn("original", self.opencode.prompts[0]["context"])
        self.assertIn(
            "Local checkout HEAD: `older-head`", self.opencode.prompts[0]["context"]
        )
        self.assertIn("PR head changed", self.opencode.prompts[0]["context"])

    def test_existing_message_freezes_delivery_without_refresh(self) -> None:
        pull = self.save_subject(subject("PR_1", "pull_request", 1))
        directory = str(self.config.workspaces_root / ".tasks" / "task-pr")
        self.opencode.sessions["ses_pr"] = {"id": "ses_pr", "directory": directory}
        self.database.save_task(
            "task-pr", "ses_pr", directory, "300", "implement", True, "active"
        )
        self.database.activate_mapping(pull["node_id"], "ses_pr", "implement")
        self.database.save_event(
            "review:52",
            pull["node_id"],
            "review",
            "continue",
            "100",
            {"source_kind": "review", "source_id": "52", "review_body": "frozen"},
            "pending",
        )
        self.instance.process_pending_events()
        delivery = self.database.pending_deliveries()[0]
        self.opencode.prompts.append(
            {
                "message_id": delivery["message_id"],
                "session_id": "ses_pr",
                "context": "frozen",
            }
        )
        self.instance.dispatch_pending()
        self.assertEqual(self.database.event_state("review:52"), "delivered")
        self.assertEqual(len(self.opencode.prompts), 1)

    def test_edited_valid_comment_reactivates_cancelled_delivery(self) -> None:
        value = subject("I_1")
        issue = self.save_subject(value)
        directory = str(self.config.workspaces_root / ".tasks" / "task-existing")
        self.opencode.sessions["ses_existing"] = {
            "id": "ses_existing",
            "directory": directory,
        }
        self.database.save_task(
            "task-existing",
            "ses_existing",
            directory,
            "300",
            "implement",
            True,
            "active",
        )
        self.database.activate_mapping(issue["node_id"], "ses_existing", "implement")
        self.instance.consider_mention(
            "comment:90",
            value,
            "100",
            "@rnwst-bot continue original",
            baseline=False,
            source_kind="issue_comment",
            source_id="90",
        )
        self.github.responses["/repos/owner/repo/issues/comments/90"] = {
            "id": 90,
            "body": "No command currently.",
            "user": {"id": 100},
        }
        self.instance.process_pending_events()
        self.instance.dispatch_pending()
        self.assertEqual(self.database.event_state("comment:90"), "ignored")

        self.instance.consider_mention(
            "comment:90",
            value,
            "100",
            "@rnwst-bot continue edited",
            baseline=False,
            source_kind="issue_comment",
            source_id="90",
        )
        self.github.responses["/repos/owner/repo/issues/comments/90"]["body"] = (
            "@rnwst-bot continue edited"
        )
        self.instance.process_pending_events()
        self.instance.dispatch_pending()
        self.assertEqual(self.database.event_state("comment:90"), "delivered")
        self.assertIn("continue edited", self.opencode.prompts[0]["context"])

    def test_uncertain_delivery_is_sealed_before_new_feedback(self) -> None:
        pull = self.save_subject(subject("PR_1", "pull_request", 1))
        directory = str(self.config.workspaces_root / ".tasks" / "task-pr")
        self.opencode.sessions["ses_pr"] = {"id": "ses_pr", "directory": directory}
        self.database.save_task(
            "task-pr", "ses_pr", directory, "300", "implement", True, "active"
        )
        self.database.activate_mapping(pull["node_id"], "ses_pr", "implement")
        self.database.save_event(
            "review:71",
            pull["node_id"],
            "review",
            "continue",
            "100",
            {"review_body": "first"},
            "pending",
        )
        self.instance.process_pending_events()
        prompt = self.opencode.prompt

        def uncertain_prompt(*args: Any, **kwargs: Any) -> None:
            prompt(*args, **kwargs)
            raise bridge.BridgeError("response lost")

        self.opencode.prompt = uncertain_prompt
        self.instance.dispatch_pending()
        self.assertEqual(self.database.pending_deliveries()[0]["state"], "dispatching")
        self.database.save_event(
            "review:72",
            pull["node_id"],
            "review",
            "continue",
            "100",
            {"review_body": "second"},
            "pending",
        )
        self.instance.process_pending_events()
        self.assertEqual(len(self.database.pending_deliveries()), 2)
        self.opencode.prompt = prompt
        self.instance.dispatch_pending()
        self.assertEqual(self.database.event_state("review:71"), "delivered")
        self.assertEqual(self.database.event_state("review:72"), "delivered")
        self.assertEqual(len(self.opencode.prompts), 2)

    def test_refreshed_coalesced_reviews_are_repartitioned(self) -> None:
        pull = self.save_subject(subject("PR_1", "pull_request", 1))
        directory = str(self.config.workspaces_root / ".tasks" / "task-pr")
        self.opencode.sessions["ses_pr"] = {"id": "ses_pr", "directory": directory}
        self.database.save_task(
            "task-pr", "ses_pr", directory, "300", "implement", True, "active"
        )
        self.database.activate_mapping(pull["node_id"], "ses_pr", "implement")
        for review_id in (81, 82):
            self.database.save_event(
                f"review:{review_id}",
                pull["node_id"],
                "review",
                "continue",
                "100",
                {
                    "source_kind": "review",
                    "source_id": str(review_id),
                    "review_body": "short",
                },
                "pending",
            )
        self.instance.process_pending_events()
        self.assertEqual(len(self.database.pending_deliveries()), 1)
        self.github.lists["/repos/owner/repo/pulls/1/reviews?per_page=100"] = [
            {
                "id": review_id,
                "submitted_at": "2026-09-05T12:00:00Z",
                "state": "COMMENTED",
                "body": str(review_id) * 9000,
                "user": {"id": 100},
            }
            for review_id in (81, 82)
        ]
        for review_id in (81, 82):
            self.github.lists[
                f"/repos/owner/repo/pulls/1/reviews/{review_id}/comments?per_page=100"
            ] = []
        self.instance.dispatch_pending()
        self.assertEqual(len(self.opencode.prompts), 1)
        self.assertEqual(len(self.database.pending_deliveries()), 1)
        self.instance.dispatch_pending()
        self.assertEqual(len(self.opencode.prompts), 2)
        self.assertEqual(self.database.event_state("review:81"), "delivered")
        self.assertEqual(self.database.event_state("review:82"), "delivered")

    def test_review_coalescing_starts_new_bounded_delivery(self) -> None:
        pull = self.save_subject(subject("PR_1", "pull_request", 1))
        directory = str(self.config.workspaces_root / ".tasks" / "task-pr")
        self.database.save_task(
            "task-pr", "ses_pr", directory, "300", "implement", True, "active"
        )
        self.database.activate_mapping(pull["node_id"], "ses_pr", "implement")
        for number in (1, 2):
            event_id = f"review:large-{number}"
            self.database.save_event(
                event_id,
                pull["node_id"],
                "review",
                "continue",
                "100",
                {"review_body": "x" * 20000, "inline_comments": []},
                "pending",
            )
        self.instance.process_pending_events()
        self.assertEqual(len(self.database.pending_deliveries()), 2)

    def test_discussion_reply_commands_are_discovered(self) -> None:
        self.github.graphql_responses.append(
            {
                "repository": {
                    "discussion": {
                        "id": "D_1",
                        "number": 1,
                        "url": "https://github.com/owner/repo/discussions/1",
                        "title": "Discussion",
                        "body": "Body",
                        "updatedAt": "2026-09-02T00:00:00Z",
                        "comments": {
                            "nodes": [
                                {
                                    "id": "DC_1",
                                    "body": "Parent",
                                    "createdAt": "2026-09-02T00:00:00Z",
                                    "updatedAt": "2026-09-02T00:00:00Z",
                                    "author": {"databaseId": 999},
                                    "replies": {
                                        "nodes": [
                                            {
                                                "id": "DCR_1",
                                                "body": "@rnwst-bot answer",
                                                "createdAt": "2026-09-02T00:00:01Z",
                                                "updatedAt": "2026-09-02T00:00:01Z",
                                                "author": {"databaseId": 100},
                                            }
                                        ],
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                    },
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    }
                }
            }
        )
        self.instance.discover_discussion("300", "owner", "repo", 1, baseline=False)
        self.assertEqual(
            self.database.event_state("discussion-comment:DCR_1"),
            "pending",
        )

    def test_registration_maps_bot_pull_to_existing_session(self) -> None:
        directory = self.config.workspaces_root / "manual"
        directory.mkdir(parents=True)
        self.opencode.sessions["ses_manual"] = {
            "id": "ses_manual",
            "directory": str(directory),
        }
        self.github.pulls[("owner", "repo", 4)] = {
            "node_id": "PR_4",
            "number": 4,
            "html_url": "https://github.com/owner/repo/pull/4",
            "title": "Pull",
            "body": "Body",
            "updated_at": "2026-09-02T00:00:00Z",
            "user": {"id": 200},
            "base": {"repo": {"id": 300, "name": "repo", "owner": {"login": "owner"}}},
        }
        result = self.instance.register_pr(
            {
                "request_id": "a" * 32,
                "pr_url": "https://github.com/owner/repo/pull/4",
                "session_id": "ses_manual",
                "directory": str(directory),
                "expires_at": int(bridge.time.time() * 1000) + 60000,
            }
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            self.database.active_mapping("PR_4")["session_id"], "ses_manual"
        )
        self.assertFalse(
            bool(self.database.task_for_session("ses_manual")["automated"])
        )

    def test_registration_rejects_non_bot_pull(self) -> None:
        directory = self.config.workspaces_root / "manual"
        directory.mkdir(parents=True)
        self.opencode.sessions["ses_manual"] = {
            "id": "ses_manual",
            "directory": str(directory),
        }
        self.github.pulls[("owner", "repo", 4)] = {"user": {"id": 999}}
        with self.assertRaisesRegex(bridge.BridgeError, "not authored"):
            self.instance.register_pr(
                {
                    "request_id": "a" * 32,
                    "pr_url": "https://github.com/owner/repo/pull/4",
                    "session_id": "ses_manual",
                    "directory": str(directory),
                    "expires_at": int(bridge.time.time() * 1000) + 60000,
                }
            )

    def test_registration_rejects_pull_from_different_repository(self) -> None:
        directory = str(self.config.workspaces_root / ".tasks" / "task-bound")
        self.opencode.sessions["ses_bound"] = {
            "id": "ses_bound",
            "directory": directory,
        }
        self.database.save_task(
            "task-bound", "ses_bound", directory, "999", "implement", True, "active"
        )
        self.github.pulls[("owner", "repo", 4)] = {
            "node_id": "PR_4",
            "number": 4,
            "html_url": "https://github.com/owner/repo/pull/4",
            "title": "Pull",
            "body": "Body",
            "user": {"id": 200},
            "base": {"repo": {"id": 300, "name": "repo", "owner": {"login": "owner"}}},
        }
        with self.assertRaisesRegex(bridge.BridgeError, "does not match"):
            self.instance.register_pr(
                {
                    "request_id": "a" * 32,
                    "pr_url": "https://github.com/owner/repo/pull/4",
                    "session_id": "ses_bound",
                    "directory": directory,
                    "expires_at": int(bridge.time.time() * 1000) + 60000,
                }
            )

    def test_context_is_minimal(self) -> None:
        issue = self.save_subject(subject("I_1"))
        text = self.instance.context(
            issue,
            "answer",
            {"instruction": "answer", "random_comments": ["poison"]},
            "/workspace",
        )
        self.assertIn("Reference body from GitHub", text)
        self.assertIn("## Verified Controller Instruction", text)
        self.assertIn("\n- Subject:", text)
        self.assertNotIn("poison", text)

    def test_cleanup_blocks_dirty_tasks_and_collects_clean_tasks(self) -> None:
        issue = self.save_subject(subject("I_1"))
        for task_id, session_id in (
            ("task-dirty", "ses_dirty"),
            ("task-clean", "ses_clean"),
        ):
            directory = str(self.config.workspaces_root / ".tasks" / task_id)
            self.opencode.sessions[session_id] = {
                "id": session_id,
                "directory": directory,
            }
            self.database.save_task(
                task_id,
                session_id,
                directory,
                "300",
                "implement",
                True,
                "completed",
            )
            self.database.activate_mapping(issue["node_id"], session_id, "implement")
            with self.database.connection:
                self.database.connection.execute(
                    "UPDATE tasks SET completed_at='2020-01-01T00:00:00+00:00' WHERE id=?",
                    (task_id,),
                )

        original_inspect = self.workspaces.inspect
        self.workspaces.inspect = lambda task_id: {
            "state": "dirty-worktree" if task_id == "task-dirty" else "clean"
        }
        try:
            self.instance.cleanup_tasks()
        finally:
            self.workspaces.inspect = original_inspect

        self.assertEqual(
            self.database.task_for_session("ses_dirty")["state"], "cleanup_blocked"
        )
        self.assertEqual(
            self.database.task_for_session("ses_clean")["state"], "collected"
        )
        self.assertEqual(self.workspaces.removed, ["task-clean"])

    def test_cleanup_recovers_interrupted_collection(self) -> None:
        directory = str(self.config.workspaces_root / ".tasks" / "task-collecting")
        self.database.save_task(
            "task-collecting",
            "ses_collecting",
            directory,
            "300",
            "answer",
            True,
            "collecting",
        )
        with self.database.connection:
            self.database.connection.execute(
                "UPDATE tasks SET completed_at='2020-01-01T00:00:00+00:00' WHERE id='task-collecting'"
            )
        self.instance.cleanup_tasks()
        self.assertEqual(
            self.database.task_for_session("ses_collecting")["state"], "collected"
        )
        self.assertEqual(self.workspaces.removed, ["task-collecting"])

    def test_delete_session_removes_mapping_and_task(self) -> None:
        issue = self.save_subject(subject("I_1"))
        directory = str(self.config.workspaces_root / ".tasks" / "task-delete")
        self.opencode.sessions["ses_delete"] = {
            "id": "ses_delete",
            "directory": directory,
        }
        self.database.save_task(
            "task-delete", "ses_delete", directory, "300", "answer", True, "completed"
        )
        self.database.activate_mapping(issue["node_id"], "ses_delete", "answer")
        self.instance.delete_session("ses_delete")
        self.assertIsNone(self.database.active_mapping(issue["node_id"]))
        self.assertIsNone(self.database.task_for_session("ses_delete"))
        self.assertEqual(self.opencode.deleted, ["ses_delete"])
        self.assertEqual(self.workspaces.removed, ["task-delete"])

    def test_delete_session_rejects_provider_retry(self) -> None:
        directory = str(self.config.workspaces_root / ".tasks" / "task-retry")
        self.opencode.sessions["ses_retry"] = {
            "id": "ses_retry",
            "directory": directory,
        }
        self.database.save_task(
            "task-retry", "ses_retry", directory, "300", "answer", True, "completed"
        )
        self.opencode.statuses = lambda _directory: {
            "ses_retry": {"type": "retry", "attempt": 1}
        }
        with self.assertRaisesRegex(bridge.BridgeError, "non-idle"):
            self.instance.delete_session("ses_retry")

    def test_pr_url_validation(self) -> None:
        self.assertEqual(
            bridge.parse_github_pr_url("https://github.com/owner/repo/pull/12"),
            ("owner", "repo", 12),
        )
        for value in (
            "http://github.com/owner/repo/pull/12",
            "https://evil.example/owner/repo/pull/12",
            "https://github.com/owner/repo/issues/12",
            "https://github.com/owner/repo/pull/12?token=x",
        ):
            with self.subTest(value=value), self.assertRaises(bridge.BridgeError):
                bridge.parse_github_pr_url(value)


class GitHubHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.requests: list[dict[str, str]] = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                owner.requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization", ""),
                        "modified": self.headers.get("If-Modified-Since", ""),
                    }
                )
                if self.path.endswith("page=2"):
                    self.respond([{"id": "second"}])
                    return
                if self.headers.get("If-Modified-Since"):
                    self.send_response(304)
                    self.send_header("X-Poll-Interval", "90")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Last-Modified", "Wed, 02 Sep 2026 00:00:00 GMT")
                self.send_header("X-Poll-Interval", "60")
                self.send_header(
                    "Link",
                    f'<http://127.0.0.1:{self.server.server_port}/notifications?page=2>; rel="next"',
                )
                self.end_headers()
                self.wfile.write(json.dumps([{"id": "first"}]).encode())

            def respond(self, payload: Any) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_conditional_notification_pagination(self) -> None:
        client = bridge.GitHubClient(
            bridge.HttpClient(), "secret", self.base, self.base + "/graphql"
        )
        status, notifications, headers = client.notifications(None)
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in notifications], ["first", "second"])
        self.assertEqual(headers["last-modified"], "Wed, 02 Sep 2026 00:00:00 GMT")
        self.assertTrue(
            all(item["authorization"] == "Bearer secret" for item in self.requests)
        )

        status, notifications, headers = client.notifications(
            "Wed, 02 Sep 2026 00:00:00 GMT"
        )
        self.assertEqual(status, 304)
        self.assertEqual(notifications, [])
        self.assertEqual(headers["x-poll-interval"], "90")
        self.assertEqual(self.requests[-1]["modified"], "Wed, 02 Sep 2026 00:00:00 GMT")


if __name__ == "__main__":
    unittest.main()
