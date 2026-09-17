"""Test-only executable MCP peer with strict handshake and deliberate failures."""

import json
import os
import signal
import stat
import sys
import time
from pathlib import Path

# The plain unit fixture has no descendants and exits normally on TERM, like a
# trusted waiter after reaping. Namespace tests run this same peer as inner PID 1.
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
home = Path(os.environ["OPENCODE_PLAYWRIGHT_HOME"])
assert home.is_relative_to("/tmp") and home.resolve(strict=True) == home
assert stat.S_IMODE(home.stat().st_mode) == 0o700
assert home.stat().st_uid == os.getuid()
(home / "private-config").write_text("fake-secret")
pid = int(os.environ.get("FAKE_MCP_PARENT", os.getpid()))


def send(message):
    print(json.dumps(message, separators=(",", ":")), flush=True)


initialize = json.loads(sys.stdin.readline())
assert initialize["jsonrpc"] == "2.0"
assert initialize["method"] == "initialize"
assert initialize["params"] == {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "opencode-preview", "version": "1"},
}
send(
    {
        "jsonrpc": "2.0",
        "id": initialize["id"],
        "result": {
            "protocolVersion": (
                "bad" if Path("bad-initialize").exists() else "2024-11-05"
            ),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1"},
        },
    }
)
assert json.loads(sys.stdin.readline()) == {
    "jsonrpc": "2.0",
    "method": "notifications/initialized",
}
count = 0
tabs = []
for line in sys.stdin:
    request = json.loads(line)
    assert request["method"] == "tools/call"
    count += 1
    name, arguments = request["params"]["name"], request["params"]["arguments"]
    response = {"jsonrpc": "2.0", "id": request["id"]}
    result = {
        "pid": pid,
        "count": count,
        "arguments": arguments,
        "home": str(home),
        "browser": sys.argv[1] if len(sys.argv) > 1 else "chromium",
    }
    if name == "browser_tabs":
        action = arguments["action"]
        if action == "new" or (action == "list" and not tabs):
            tabs.append(f"tab-{count}")
        elif action == "close":
            tabs.pop(arguments.get("index", len(tabs) - 1))
        text = "\n".join(
            f"- {index}: [{title}](about:blank)" for index, title in enumerate(tabs)
        )
        result = {
            "content": [
                {
                    "type": "text",
                    "text": "### Result\n"
                    + (text or "No open tabs. Navigate to a URL to create one."),
                }
            ]
        }
    elif name == "hang":
        Path("hanging").write_text(str(pid))
        time.sleep(60)
    elif name == "exit":
        sys.exit(3)
    elif name == "bad_json":
        print("not JSON", flush=True)
        continue
    elif name == "bad_id":
        response["id"] = "not-the-request"
    elif name == "bad_result":
        result = []
    elif name == "both":
        response["error"] = {"code": -1, "message": "wrong"}
    elif name == "nan":
        result = {"invalid": float("nan")}
    elif name == "deep":
        sys.stdout.write(
            '{"jsonrpc":"2.0","id":"'
            + request["id"]
            + '","result":'
            + "[" * 2000
            + "0"
            + "]" * 2000
            + "}\n"
        )
        sys.stdout.flush()
        continue
    elif name == "overflow":
        sys.stdout.write("x" * (8 * 1024 * 1024))
        sys.stdout.flush()
        continue
    elif name == "image":
        result = {
            "content": [
                {
                    "type": "image",
                    "mimeType": "image/png",
                    "data": "eA==" * arguments["blocks"],
                }
            ]
        }
    elif name == "error":
        send(
            {
                **response,
                "error": {
                    "code": -32602,
                    "message": "fake tool error",
                    "data": {"safe": True},
                },
            }
        )
        continue
    elif name == "tool_error":
        result = {
            "isError": True,
            "content": [{"type": "text", "text": "fake tool failure"}],
        }
    elif name == "detached":
        # Only real-namespace tests use this mode. A detached session ignores
        # TERM and keeps a private file open until namespace teardown kills it.
        assert os.getpid() == 1
        child = os.fork()
        if not child:
            os.setsid()
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            with (home / "held-open").open("w"):
                (home / "detached-ready").touch()
                while True:
                    time.sleep(1)
        while not (home / "detached-ready").exists():
            time.sleep(0.01)
    elif name == "stderr":
        os.write(2, b"fake-secret\n" * 100000)
    elif name == "notify":
        send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/message",
                "params": {"data": "not host logs"},
            }
        )
    elif name == "fds":
        result["fds"] = {}
        for fd in os.listdir("/proc/self/fd"):
            try:
                result["fds"][fd] = os.readlink("/proc/self/fd/" + fd)
            except FileNotFoundError:
                pass
        result["env"] = dict(os.environ)
        result["cwd"] = os.getcwd()
    send({**response, "result": result})
