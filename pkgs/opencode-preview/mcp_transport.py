"""Bounded stdio MCP child, driven by the sandbox supervisor's selector."""

import base64
import json
import math
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path

MAX_MCP_REQUEST = 128 * 1024
MAX_MCP_RESULT = 8 * 1024 * 1024
MCP_TIMEOUT = 120
MCP_STOP_TIMEOUT = 2
MCP_TMP = Path("/tmp")
CHUNK = 16 * 1024
PROTOCOL_VERSION = "2024-11-05"


def home_directory(runtime_id):
    if not isinstance(runtime_id, str) or not re.fullmatch(r"[a-f0-9]{24}", runtime_id):
        raise ValueError("invalid MCP runtime identity")
    return "opencode-playwright-" + runtime_id


def validate_request(request):
    if (
        not isinstance(request, dict)
        or set(request) != {"method", "params"}
        or request["method"] != "tools/call"
        or not isinstance(request["params"], dict)
        or set(request["params"]) != {"name", "arguments"}
        or not isinstance(request["params"]["name"], str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request["params"]["name"])
        or not isinstance(request["params"]["arguments"], dict)
    ):
        raise ValueError("invalid MCP request")
    pending = [(request, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 32:
            raise ValueError("MCP nesting limit exceeded")
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise ValueError("invalid JSON key")
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("invalid JSON number")
    encoded = json.dumps(request, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > MAX_MCP_REQUEST:
        raise ValueError("MCP request limit exceeded")


def reject_constant(value):
    raise ValueError("invalid JSON constant")


def result_envelope(message):
    if not isinstance(message, dict) or ("result" in message) == ("error" in message):
        raise ValueError("invalid MCP response")
    if "result" in message:
        if not isinstance(message["result"], dict):
            raise ValueError("invalid MCP result")
        return {"result": message["result"]}
    error = message["error"]
    if (
        not isinstance(error, dict)
        or type(error.get("code")) is not int
        or not isinstance(error.get("message"), str)
    ):
        raise ValueError("invalid MCP error")
    return {"error": error}


class MCPChild:
    def __init__(self, supervisor, executable):
        if executable is not None and (
            not isinstance(executable, str)
            or not os.path.isabs(executable)
            or "\0" in executable
        ):
            raise ValueError("MCP launcher must be an absolute trusted path")
        self.supervisor = supervisor
        self.executable = executable
        self.process = None
        self.retired = None
        self.retire_deadline = 0
        self.retire_barrier = 0
        self.home = None
        self.home_fd = None
        self.retire_after_result = False
        self.id = None
        self.last_id = None
        self.sequence = 0
        self.rpc_id = None
        self.phase = None
        self.input = bytearray()
        self.output = bytearray()
        self.result = None
        self.offset = 0
        self.received = 0
        self.deadline = 0
        self.request = None

    def queue(self, message):
        self.output.extend(
            json.dumps(message, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        )

    def call(self, id, request):
        validate_request(request)
        if not self.executable:
            self.supervisor.error(id, "MCP is not configured")
            return
        if self.id is not None:
            self.supervisor.error(id, "MCP is busy")
            return
        self.id = self.last_id = id
        self.request = request
        self.received = 0
        self.deadline = time.monotonic() + MCP_TIMEOUT
        if self.retired is None and self.home_fd is None:
            self.start()
        elif self.process is not None:
            self.forward()

    def start(self):
        try:
            if self.process is None:
                root = MCP_TMP / home_directory(
                    self.supervisor.environment.get("OPENCODE_PREVIEW_RUNTIME_ID")
                )
                root.mkdir(mode=0o700, exist_ok=True)
                if root.resolve(strict=True) != root or not root.is_relative_to("/tmp"):
                    raise ValueError("invalid MCP private root")
                self.home_fd = os.open(
                    root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                )
                info = os.fstat(self.home_fd)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ValueError("invalid MCP private root")
                self.home = Path(tempfile.mkdtemp(prefix="browser-", dir=root))
                if self.home.resolve(strict=True) != self.home:
                    raise ValueError("invalid MCP private home")
                self.process = subprocess.Popen(
                    [self.executable],
                    cwd=self.supervisor.workspace,
                    env={
                        **self.supervisor.environment,
                        "OPENCODE_PLAYWRIGHT_HOME": str(self.home),
                    },
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                )
                for file in (self.process.stdin, self.process.stdout):
                    os.set_blocking(file.fileno(), False)
                self.phase = "initialize"
                self.sequence += 1
                self.rpc_id = str(self.sequence)
                self.queue(
                    {
                        "jsonrpc": "2.0",
                        "id": self.rpc_id,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": PROTOCOL_VERSION,
                            "capabilities": {},
                            "clientInfo": {"name": "opencode-preview", "version": "1"},
                        },
                    }
                )
            else:
                self.forward()
        except (OSError, ValueError):
            self.reset("MCP startup failed")

    def forward(self):
        self.phase = "call"
        self.sequence += 1
        self.rpc_id = str(self.sequence)
        self.queue({"jsonrpc": "2.0", "id": self.rpc_id, **self.request})

    def message(self, line):
        message = json.loads(line, parse_constant=reject_constant)
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise ValueError("invalid MCP message")
        if "id" not in message:
            # Notifications are not control traffic and never reach host logs.
            if (
                not isinstance(message.get("method"), str)
                or "result" in message
                or "error" in message
                or ("params" in message and not isinstance(message["params"], dict))
            ):
                raise ValueError("invalid MCP notification")
            return
        if (
            self.id is None
            or self.phase not in {"initialize", "call"}
            or message["id"] != self.rpc_id
            or "method" in message
        ):
            raise ValueError("unexpected MCP response")
        envelope = result_envelope(message)
        if self.phase == "initialize":
            result = envelope.get("result", {})
            if (
                result.get("protocolVersion") != PROTOCOL_VERSION
                or not isinstance(result.get("capabilities"), dict)
                or not isinstance(result.get("serverInfo"), dict)
            ):
                raise ValueError("MCP initialization failed")
            self.queue({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.forward()
        else:
            encoded = json.dumps(
                envelope, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ).encode()
            if len(encoded) > MAX_MCP_RESULT:
                raise ValueError("MCP result limit exceeded")
            self.result = encoded
            self.offset = 0
            self.phase = "result"
            # Playwright 1.59.1 backend/{tabs,response}.ts renders this exact
            # content for the last tab; utils/mcp/server.ts strips isClose.
            # Do not query tabs afterward: action=list calls ensureTab(), reopening
            # the browser. Exact matching cannot mistake a tab title for closure.
            self.retire_after_result = (
                self.request["params"]["name"] == "browser_close"
                or (
                    self.request["params"]["name"] == "browser_tabs"
                    and self.request["params"]["arguments"].get("action") == "close"
                    and envelope.get("result", {}).get("content")
                    == [
                        {
                            "type": "text",
                            "text": "### Result\nNo open tabs. Navigate to a URL to create one.",
                        }
                    ]
                )
                or "error" in envelope
                or envelope.get("result", {}).get("isError") is True
            )

    def service(self, file, events):
        if self.process is None or file not in (
            self.process.stdin,
            self.process.stdout,
        ):
            return
        try:
            if events & selectors.EVENT_WRITE and self.output:
                count = os.write(file.fileno(), self.output[:CHUNK])
                del self.output[:count]
            if events & selectors.EVENT_READ:
                data = os.read(file.fileno(), CHUNK)
                if not data:
                    self.reset("MCP child exited")
                    return
                self.received += len(data)
                # Include notifications and initialization in a bounded traffic budget.
                if self.received > MAX_MCP_RESULT + MAX_MCP_REQUEST:
                    raise ValueError("MCP traffic limit exceeded")
                previous = len(self.input)
                self.input.extend(data)
                newline = self.input.find(b"\n", previous)
                while newline >= 0:
                    if newline + 1 > MAX_MCP_RESULT:
                        raise ValueError("MCP line limit exceeded")
                    line = self.input[:newline]
                    del self.input[: newline + 1]
                    self.message(line)
                    newline = self.input.find(b"\n")
                if len(self.input) >= MAX_MCP_RESULT:
                    raise ValueError("MCP line limit exceeded")
        except BlockingIOError:
            pass
        except (OSError, ValueError, TypeError, RecursionError):
            self.reset("MCP protocol failed")

    def refresh(self, writable):
        self.reap()
        if self.id is not None and time.monotonic() >= self.deadline:
            self.reset("MCP request timed out")
            return
        if self.process is None and self.id is not None and self.retired is None:
            self.start()
        if self.process is None:
            return
        if self.process.poll() is not None and self.result is None:
            self.reset("MCP child exited")
            return
        if self.result is not None and writable:
            chunk = self.result[self.offset : self.offset + CHUNK]
            self.supervisor.emit(
                "mcp_data", self.id, data=base64.b64encode(chunk).decode()
            )
            self.offset += len(chunk)
            if self.offset == len(self.result):
                barrier = self.supervisor.emit("mcp_end", self.id)
                self.id = self.request = self.result = None
                self.phase = "ready"
                if self.retire_after_result:
                    # Keep the captured response, including tool errors, ahead of
                    # retirement. No helper remains to pin an otherwise idle runtime.
                    self.reset()
                    self.retire_barrier = barrier
                    return
        self.supervisor.watch(
            self.process.stdin,
            selectors.EVENT_WRITE if self.output else 0,
            ("mcp", self.process.stdin),
        )
        self.supervisor.watch(
            self.process.stdout,
            selectors.EVENT_READ if self.result is None else 0,
            ("mcp", self.process.stdout),
        )

    def cancel(self, id):
        if id == self.last_id:
            self.reset("MCP request cancelled")

    def reset(self, error=None):
        if self.process is not None:
            # The trusted unshare waiter blocks TERM while reaping namespace PID 1.
            # Never KILL the waiter: its death alone cannot prove teardown complete.
            if self.process.poll() is None:
                self.supervisor.signal_group(self.process.pid, signal.SIGTERM)
            for file in (self.process.stdin, self.process.stdout):
                self.supervisor.watch(file, 0)
                file.close()
            self.retired = self.process
            self.retire_deadline = time.monotonic() + MCP_STOP_TIMEOUT
            self.process = None
        if error and self.id is not None:
            self.supervisor.error(self.id, error)
        self.id = self.last_id = self.request = self.result = self.phase = None
        self.input.clear()
        self.output.clear()
        self.retire_after_result = False

    def reap(self):
        # A teardown/cleanup failure must not truncate an already captured close
        # or tool-error response. Wait for its end frame, not unrelated later output.
        if self.supervisor.written < self.retire_barrier:
            if time.monotonic() >= self.retire_deadline:
                raise RuntimeError("MCP response flush timed out")
            return
        self.retire_barrier = 0
        if self.retired is not None:
            code = self.retired.poll()
            if code is None:
                if time.monotonic() >= self.retire_deadline:
                    raise RuntimeError("MCP namespace teardown timed out")
                return
            if code < 0:
                # A signaled waiter may have died before waiting for namespace init.
                # Leave the home intact; host cgroup teardown owns recovery now.
                raise RuntimeError("MCP namespace teardown is unconfirmed")
        if self.process is None and self.home_fd is not None:
            try:
                if self.home is not None:
                    shutil.rmtree(self.home.name, dir_fd=self.home_fd)
            except FileNotFoundError:
                pass
            except OSError:
                raise RuntimeError("MCP private home cleanup failed") from None
            os.close(self.home_fd)
            self.home = self.home_fd = None
        self.retired = None

    def shutdown(self):
        self.retire_barrier = 0
        self.reset()
        while True:
            self.reap()
            if self.retired is None:
                return
            time.sleep(0.01)
