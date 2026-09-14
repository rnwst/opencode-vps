"""Persistent sandbox worker. Launch immutable source with Python -I -S.

The launcher supplies an already-masked environment and the native PID 1 owns
namespace teardown. There are no workload-accessible control listeners.
"""

import argparse
import array
import base64
import binascii
import collections
import ctypes
import errno
import fcntl
import json
import os
import re
import selectors
import signal
import socket
import subprocess
import sys
import termios
import time
from dataclasses import dataclass, field
from pathlib import Path

# -I excludes the script directory; only add the immutable packaged source.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_transport import MCPChild

MAX_FRAME = 256 * 1024
MAX_COMMAND = 128 * 1024
CHUNK = 16 * 1024
MAX_STREAMS = 128
MAX_PENDING = 256 * 1024
OUTPUT_HIGH = 2 * 1024 * 1024
OUTPUT_LIMIT = 8 * 1024 * 1024
RESERVED_PORTS = {1080, 3128}
ID = re.compile(r"[0-9a-fA-F]{1,128}\Z")


def secure_process():
    """Fail closed before accepting requests, including if procfs is unavailable."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) != 0 or libc.prctl(3, 0, 0, 0, 0) != 0:
        raise RuntimeError("cannot disable dumpability")
    status = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
        if ":" in line
    )
    try:
        valid = (
            all(int(status[key], 16) == 0 for key in ("CapEff", "CapPrm", "CapAmb"))
            and int(status["NoNewPrivs"]) == 1
            and int(status["Seccomp"]) in (1, 2)
            # An ancestor's procfs exposes outer PIDs and multiple NSpid entries.
            and int(status["Pid"]) == os.getpid()
            and status["NSpid"].split() == [str(os.getpid())]
        )
    except (KeyError, ValueError):
        valid = False
    if not valid:
        raise RuntimeError("sandbox security prerequisites missing")


def listening_ports():
    ports = set()
    for name in ("tcp", "tcp6"):
        try:
            with open("/proc/net/" + name, encoding="ascii") as table:
                for line in table:
                    fields = line.split()
                    if len(fields) > 3 and fields[3] == "0A":
                        try:
                            port = int(fields[1].rsplit(":", 1)[1], 16)
                        except (ValueError, IndexError):
                            continue
                        if 1 <= port <= 65535 and port not in RESERVED_PORTS:
                            ports.add(port)
        except OSError:
            continue
    return sorted(ports)


def workload_processes():
    count = 0
    own_pid = str(os.getpid())
    seen_self = False
    try:
        with os.scandir("/proc") as entries:
            for entry in entries:
                if entry.name == own_pid:
                    seen_self = True
                    continue
                if entry.name == "1" or not entry.name.isdecimal():
                    continue
                try:
                    # comm can contain spaces, newlines and parentheses, unlike
                    # the fields after its final closing parenthesis.
                    fields = (
                        Path(entry.path, "stat").read_bytes().rsplit(b")", 1)[1].split()
                    )
                    # A zombie leader can still have live threads. num_threads
                    # is stat field 20, offset 17 after removing pid and comm.
                    if fields[0] not in (b"Z", b"X", b"x") or int(fields[17]) != 1:
                        count += 1
                except (OSError, IndexError, ValueError):
                    # Even an exit/read race should delay eviction, not invent idle.
                    count += 1
    except OSError:
        return max(count, 1)
    return count if seen_self else max(count, 1)


@dataclass(eq=False)
class Command:
    id: str
    process: subprocess.Popen
    finished: bool = False


@dataclass(eq=False)
class Pipe:
    file: object
    command: Command
    stream: str


@dataclass(eq=False)
class Stream:
    id: str
    port: int
    sock: object = None
    family: int = socket.AF_INET
    connecting: bool = True
    deadline: float = 0
    pending: bytearray = field(default_factory=bytearray)
    ended: bool = False
    write_closed: bool = False
    read_closed: bool = False


class Supervisor:
    def __init__(self, playwright_mcp=None):
        self.workspace = Path.cwd().resolve()
        self.environment = dict(os.environ)
        self.selector = selectors.DefaultSelector()
        self.input = bytearray()
        self.output = collections.deque()
        self.output_size = 0
        self.written = 0
        self.streams = {}
        self.pipes = set()
        self.command = None
        self.kills = {}
        self.running = True
        self.mcp = MCPChild(self, playwright_mcp)

    def watch(self, file, events, data=None):
        try:
            self.selector.get_key(file)
        except KeyError:
            if events:
                self.selector.register(file, events, data)
        else:
            if events:
                self.selector.modify(file, events, data)
            else:
                self.selector.unregister(file)

    def emit(self, event, id=None, **fields):
        frame = {"event": event, **fields}
        if id is not None or event == "error":
            frame["id"] = id
        encoded = (
            json.dumps(frame, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
        )
        if len(encoded) > MAX_FRAME or self.output_size + len(encoded) > OUTPUT_LIMIT:
            # A controller that cannot consume bounded control traffic loses its worker.
            raise RuntimeError("control output limit exceeded")
        self.output.append(memoryview(encoded))
        self.output_size += len(encoded)
        self.watch(1, selectors.EVENT_WRITE, ("output", None))
        return self.written + self.output_size

    def error(self, id, message):
        self.emit("error", id, error=message[:256])

    def flush(self):
        if not self.output:
            return
        try:
            size = os.write(1, self.output[0][:CHUNK])
        except BlockingIOError:
            return
        self.output_size -= size
        self.written += size
        self.output[0] = self.output[0][size:]
        if not self.output[0]:
            self.output.popleft()
        if not self.output:
            self.watch(1, 0)

    def receive(self):
        try:
            data = os.read(0, CHUNK)
        except BlockingIOError:
            return
        if not data:
            self.running = False
            return
        self.input.extend(data)
        while b"\n" in self.input:
            line, _, rest = self.input.partition(b"\n")
            self.input = bytearray(rest)
            if len(line) + 1 > MAX_FRAME:
                raise RuntimeError("control frame limit exceeded")
            self.request(line)
        if len(self.input) >= MAX_FRAME:
            raise RuntimeError("control frame limit exceeded")

    def request(self, line):
        id = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise TypeError
            candidate = request.get("id")
            if not isinstance(candidate, str) or not ID.fullmatch(candidate):
                raise ValueError
            id = candidate
            op = request.get("op")
            schemas = {
                "exec": {"op", "id", "argv", "cwd"},
                "cancel": {"op", "id"},
                "connect": {"op", "id", "port"},
                "data": {"op", "id", "data"},
                "end": {"op", "id"},
                "close": {"op", "id"},
                "mcp": {"op", "id", "request"},
                "mcp_cancel": {"op", "id"},
            }
            if (
                not isinstance(op, str)
                or op not in schemas
                or set(request) != schemas[op]
            ):
                raise ValueError
            if op == "mcp":
                if len(line) > MAX_COMMAND:
                    raise ValueError
                if (
                    id in self.streams
                    or id in self.kills
                    or (self.command and self.command.id == id)
                ):
                    self.error(id, "id in use")
                else:
                    self.mcp.call(id, request["request"])
            elif op == "mcp_cancel":
                self.mcp.cancel(id)
            elif op == "exec":
                argv, cwd = request["argv"], request["cwd"]
                if (
                    len(line) > MAX_COMMAND
                    or not isinstance(argv, list)
                    or not argv
                    or not all(isinstance(arg, str) and "\0" not in arg for arg in argv)
                    or not argv[0]
                    or not isinstance(cwd, str)
                    or "\0" in cwd
                ):
                    raise ValueError
                resolved = (self.workspace / cwd).resolve(strict=True)
                if not resolved.is_relative_to(self.workspace) or not resolved.is_dir():
                    self.error(id, "cwd is outside workspace or not a directory")
                    return
                if (
                    self.command is not None
                    or id in self.streams
                    or id in self.kills
                    or id == self.mcp.id
                ):
                    self.error(id, "exec busy or id in use")
                    return
                if len(self.pipes) >= 256:
                    self.error(id, "background pipe limit reached")
                    return
                process = subprocess.Popen(
                    argv,
                    cwd=resolved,
                    env=self.environment,
                    close_fds=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                self.command = Command(id, process)
                for name in ("stdout", "stderr"):
                    file = getattr(process, name)
                    os.set_blocking(file.fileno(), False)
                    self.pipes.add(Pipe(file, self.command, name))
            elif op == "cancel":
                if self.command is None or self.command.id != id:
                    self.error(id, "unknown exec")
                elif id not in self.kills:
                    self.signal_group(self.command.process.pid, signal.SIGTERM)
                    self.kills[id] = (self.command.process.pid, time.monotonic() + 2)
            elif op == "connect":
                port = request["port"]
                if (
                    type(port) is not int
                    or not 1 <= port <= 65535
                    or port in RESERVED_PORTS
                ):
                    raise ValueError
                if (
                    id in self.streams
                    or (self.command and self.command.id == id)
                    or id in self.kills
                    or id == self.mcp.id
                ):
                    self.error(id, "id in use")
                elif len(self.streams) >= MAX_STREAMS:
                    self.error(id, "stream limit reached")
                else:
                    stream = Stream(id, port)
                    self.streams[id] = stream
                    self.dial(stream)
            else:
                stream = self.streams.get(id)
                if stream is None:
                    self.error(id, "unknown stream")
                elif op == "close":
                    self.close_stream(stream)
                elif stream.connecting:
                    self.error(id, "stream not connected")
                elif op == "end":
                    stream.ended = True
                elif stream.ended:
                    self.error(id, "stream already ended")
                else:
                    encoded = request["data"]
                    if not isinstance(encoded, str):
                        raise ValueError
                    data = base64.b64decode(encoded, validate=True)
                    if len(stream.pending) + len(data) > MAX_PENDING:
                        self.close_stream(stream, "stream buffer limit exceeded")
                    else:
                        stream.pending.extend(data)
        except (ValueError, TypeError, RecursionError, UnicodeError, binascii.Error):
            self.error(id, "invalid request")
        except (OSError, RuntimeError):
            # Never reflect argv, cwd, environment, or application error strings.
            self.error(id, "operation failed")

    @staticmethod
    def signal_group(pid, sig):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass

    def close_pipe(self, pipe):
        self.watch(pipe.file, 0)
        pipe.file.close()
        self.pipes.discard(pipe)

    def read_pipe(self, pipe, size=CHUNK):
        try:
            data = os.read(pipe.file.fileno(), min(size, CHUNK))
        except BlockingIOError:
            return 0
        if not data:
            self.close_pipe(pipe)
        elif not pipe.command.finished:
            self.emit(
                "data",
                pipe.command.id,
                stream=pipe.stream,
                data=base64.b64encode(data).decode("ascii"),
            )
        return len(data)

    def poll_command(self):
        if self.command is None:
            return
        command = self.command
        code = command.process.poll()
        if code is None:
            return
        # Snapshot queued bytes: descendants may keep writing indefinitely. Do not
        # wait for EOF or chase new writes after the foreground process exits.
        for pipe in list(self.pipes):
            if pipe.command is not command:
                continue
            queued = array.array("i", [0])
            fcntl.ioctl(pipe.file.fileno(), termios.FIONREAD, queued, True)
            remaining = min(queued[0], 1024 * 1024)
            while remaining:
                count = self.read_pipe(pipe, remaining)
                if not count:
                    break
                remaining -= count
        command.finished = True
        self.command = None
        self.emit("exit", command.id, code=code)

    def dial(self, stream):
        try:
            stream.sock = socket.socket(stream.family, socket.SOCK_STREAM)
            stream.sock.setblocking(False)
            stream.deadline = time.monotonic() + 5
            address = "127.0.0.1" if stream.family == socket.AF_INET else "::1"
            result = stream.sock.connect_ex((address, stream.port))
            if result not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
                self.connect_failed(stream)
        except OSError:
            self.connect_failed(stream)

    def connect_failed(self, stream):
        if stream.sock is not None:
            self.watch(stream.sock, 0)
            stream.sock.close()
            stream.sock = None
        if stream.family == socket.AF_INET:
            stream.family = socket.AF_INET6
            self.dial(stream)
        else:
            self.close_stream(stream, "connection failed")

    def close_stream(self, stream, error=None):
        if stream.id not in self.streams:
            return
        if stream.sock is not None:
            self.watch(stream.sock, 0)
            stream.sock.close()
        del self.streams[stream.id]
        if error:
            self.error(stream.id, error)
        self.emit("closed", stream.id)

    def service_stream(self, stream, events):
        if stream.id not in self.streams:
            return
        try:
            if stream.connecting:
                if stream.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
                    self.connect_failed(stream)
                else:
                    stream.connecting = False
                    self.emit("connected", stream.id)
                return
            if events & selectors.EVENT_READ:
                data = stream.sock.recv(CHUNK)
                if data:
                    self.emit(
                        "data", stream.id, data=base64.b64encode(data).decode("ascii")
                    )
                else:
                    stream.read_closed = True
                    self.emit("end", stream.id)
            if events & selectors.EVENT_WRITE and stream.pending:
                count = stream.sock.send(stream.pending[:CHUNK])
                del stream.pending[:count]
                if count:
                    self.emit("written", stream.id, size=count)
        except BlockingIOError:
            pass
        except OSError:
            self.close_stream(stream, "connection failed")

    def refresh(self):
        now = time.monotonic()
        self.poll_command()
        self.mcp.refresh(self.output_size < OUTPUT_HIGH)
        for id, (pid, deadline) in list(self.kills.items()):
            if now >= deadline:
                self.signal_group(pid, signal.SIGKILL)
                del self.kills[id]
        for pipe in self.pipes:
            events = (
                selectors.EVENT_READ
                if pipe.command.finished or self.output_size < OUTPUT_HIGH
                else 0
            )
            self.watch(pipe.file, events, ("pipe", pipe))
        for stream in list(self.streams.values()):
            if stream.connecting:
                if now >= stream.deadline:
                    self.connect_failed(stream)
                if stream.id in self.streams:
                    self.watch(stream.sock, selectors.EVENT_WRITE, ("stream", stream))
                continue
            if stream.ended and not stream.pending and not stream.write_closed:
                try:
                    stream.sock.shutdown(socket.SHUT_WR)
                    stream.write_closed = True
                except OSError:
                    self.close_stream(stream, "connection failed")
                    continue
            if stream.read_closed and stream.write_closed:
                self.close_stream(stream)
                continue
            events = 0
            if not stream.read_closed and self.output_size < OUTPUT_HIGH:
                events |= selectors.EVENT_READ
            if stream.pending:
                events |= selectors.EVENT_WRITE
            self.watch(stream.sock, events, ("stream", stream))

    def run(self):
        try:
            os.set_blocking(0, False)
            os.set_blocking(1, False)
            self.watch(0, selectors.EVENT_READ, ("input", None))
            self.emit("ready", pid=os.getpid())
            next_ports = 0
            while self.running:
                self.refresh()
                if time.monotonic() >= next_ports:
                    self.emit(
                        "ports", ports=listening_ports(), processes=workload_processes()
                    )
                    next_ports = time.monotonic() + 1
                for key, events in self.selector.select(0.05):
                    kind, item = key.data
                    if kind == "input":
                        self.receive()
                        if not self.running:
                            break
                    elif kind == "output":
                        self.flush()
                    elif kind == "pipe" and item in self.pipes:
                        self.read_pipe(item)
                    elif kind == "stream":
                        self.service_stream(item, events)
                    elif kind == "mcp":
                        self.mcp.service(item, events)
        finally:
            self.shutdown()

    def shutdown(self):
        try:
            self.mcp.shutdown()
        finally:
            groups = {pipe.command.process.pid for pipe in self.pipes}
            groups.update(pid for pid, _ in self.kills.values())
            if self.command:
                groups.add(self.command.process.pid)
            for pid in groups:
                self.signal_group(pid, signal.SIGKILL)
            for pipe in list(self.pipes):
                self.close_pipe(pipe)
            if self.command:
                self.command.process.wait(timeout=3)
            for stream in self.streams.values():
                if stream.sock is not None:
                    stream.sock.close()
            self.streams.clear()
            self.selector.close()


def main():
    try:
        secure_process()
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--playwright-mcp")
        args = parser.parse_args()
        if args.playwright_mcp is not None and (
            not os.path.isabs(args.playwright_mcp) or "\0" in args.playwright_mcp
        ):
            parser.error("MCP launcher must be an absolute trusted path")
        Supervisor(args.playwright_mcp).run()
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        print(
            "sandbox supervisor stopped: security or control failure", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
