#!/usr/bin/env python3

import http.server
import json
import os
import pwd
import grp
import subprocess
import datetime as dt
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path("/srv/git")
EVENTS = Path("/tmp/fake-github-events.jsonl")


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        if path == "/user":
            self.respond({"id": 200, "login": "bot"})
            return
        if path == "/notifications":
            notifications = []
            if Path("/tmp/enable-github-notification").exists():
                notifications = [
                    {
                        "id": "thread-1",
                        "updated_at": now,
                        "repository": {
                            "id": 738,
                            "name": "repo",
                            "owner": {"id": 100, "login": "owner"},
                        },
                        "subject": {
                            "type": "Issue",
                            "url": "http://127.0.0.1:4080/repos/owner/repo/issues/1",
                            "latest_comment_url": None,
                        },
                    }
                ]
            body = json.dumps(notifications).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Last-Modified", "Wed, 02 Sep 2026 00:00:00 GMT")
            self.send_header("X-Poll-Interval", "60")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/repos/owner/repo/issues/1":
            self.respond(
                {
                    "id": 501,
                    "node_id": "I_fixture_1",
                    "number": 1,
                    "html_url": "https://github.example/owner/repo/issues/1",
                    "title": "Implement fixture",
                    "body": "Implement the fixture behavior.",
                    "updated_at": now,
                    "assignees": [{"id": 200, "login": "bot"}],
                }
            )
            return
        if path == "/repos/owner/repo/issues/1/comments":
            self.respond([])
            return
        if path == "/repos/owner/repo/issues/1/events":
            self.respond(
                [
                    {
                        "id": 601,
                        "event": "assigned",
                        "created_at": now,
                        "assigner": {"id": 100, "login": "controller"},
                        "assignee": {"id": 200, "login": "bot"},
                    }
                ]
            )
            return
        if path.startswith("/repos/owner/"):
            name = path.removeprefix("/repos/owner/")
            remote = ROOT / f"{name}.git"
            if not remote.exists():
                self.send_error(404)
                return
            self.respond(
                {
                    "id": 300 + sum(name.encode()),
                    "name": name,
                    "full_name": f"owner/{name}",
                    "default_branch": "main",
                    "clone_url": remote.as_uri(),
                    "permissions": {"push": not Path("/tmp/repo-push-denied").exists()},
                }
            )
            return
        if path == "/repos/bot/repo":
            remote = ROOT / "bot-repo.git"
            if not remote.exists():
                self.send_error(404)
                return
            self.respond(
                {
                    "id": 1738,
                    "name": "repo",
                    "full_name": "bot/repo",
                    "default_branch": "main",
                    "clone_url": remote.as_uri(),
                    "parent": {"id": 738},
                    "permissions": {"push": True},
                }
            )
            return
        self.send_error(404)

    def do_POST(self):
        if self.path == "/repos/owner/repo/forks":
            remote = ROOT / "bot-repo.git"
            if not remote.exists():
                subprocess.run(
                    ["git", "clone", "--bare", str(ROOT / "repo.git"), str(remote)],
                    check=True,
                )
                uid = pwd.getpwnam("test-bot").pw_uid
                gid = grp.getgrnam("agent-workspaces").gr_gid
                for root, directories, files in os.walk(remote):
                    os.chown(root, uid, gid)
                    for name in directories + files:
                        os.chown(Path(root) / name, uid, gid)
            self.respond(
                {
                    "id": 1738,
                    "name": "repo",
                    "full_name": "bot/repo",
                    "clone_url": remote.as_uri(),
                    "parent": {"id": 738},
                },
                status=202,
            )
            return
        if self.path not in {"/user/repos", "/orgs/owner/repos"}:
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        name = body["name"]
        remote = ROOT / f"{name}.git"
        if not remote.exists():
            subprocess.run(["git", "init", "--bare", str(remote)], check=True)
            uid = pwd.getpwnam("test-bot").pw_uid
            gid = grp.getgrnam("agent-workspaces").gr_gid
            for root, directories, files in os.walk(remote):
                os.chown(root, uid, gid)
                for name in directories + files:
                    os.chown(Path(root) / name, uid, gid)
        with EVENTS.open("a") as stream:
            stream.write(json.dumps({"path": self.path, "body": body}) + "\n")
        self.respond(
            {
                "id": 900,
                "name": name,
                "full_name": f"owner/{name}",
                "default_branch": "main",
                "clone_url": remote.as_uri(),
            },
            status=201,
        )

    def respond(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


http.server.ThreadingHTTPServer(("127.0.0.1", 4080), Handler).serve_forever()
