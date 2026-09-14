"""Host-only persistent sandbox manager and private Unix HTTP control service."""

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import time
from contextlib import suppress
from pathlib import Path

from aiohttp import web
from launch import delegated_root
from mcp_transport import (
    MAX_MCP_REQUEST,
    MAX_MCP_RESULT,
    MCP_TIMEOUT,
    home_directory,
    reject_constant,
    result_envelope,
    validate_request,
)

MAX_FRAME = 256 * 1024
SESSION = re.compile(r"ses_[A-Za-z0-9]+\Z")
SAFE_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?\Z")
QUEUE_SIZE = 16


def workspace_slug(relative):
    name = Path(relative).name
    # Reserve double hyphens for hashes so a manual name cannot impersonate
    # the generated slug of a task, truncated name, or sanitized name.
    if "/" not in relative and SAFE_SLUG.fullmatch(name) and "--" not in name:
        return name
    stem = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "workspace"
    stem = stem[:22].rstrip("-")
    return stem + "--" + hashlib.sha256(relative.encode()).hexdigest()[:16]


def identity(path):
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise ValueError("not a real directory")
    return info.st_dev, info.st_ino


def frame_bytes(frame):
    data = json.dumps(frame, separators=(",", ":")).encode() + b"\n"
    if len(data) > MAX_FRAME:
        raise ValueError("protocol frame too large")
    return data


def decode_data(frame):
    value = frame.get("data")
    if not isinstance(value, str):
        raise TypeError("invalid protocol data")
    return base64.b64decode(value, validate=True)


class Runtime:
    def __init__(self, manager, root, relative, session, session_tmp):
        self.manager = manager
        self.runtime_id = secrets.token_hex(12)
        self.directory = str(root)
        self.workspace = relative
        self.slug = workspace_slug(relative)
        self.session_id = session
        self.root = root
        self.root_identity = identity(root)
        self.session_tmp = session_tmp
        self.tmp_identity = identity(session_tmp)
        self.created = time.monotonic()
        self.last_ports = self.created
        self.last_heartbeat = 0
        self.last_exec_activity = self.created
        self.active_exec = None
        self.active_mcp = None
        self.processes = None
        self.reported_ports = set()
        self.idle_since = None
        self.started = False
        self.startup_task = None
        self.process = None
        self.cgroup = None
        self.reader_task = None
        self.ready = asyncio.get_running_loop().create_future()
        self.write_lock = asyncio.Lock()
        self.channels = {}
        self.listeners = {}
        self.known_ports = set()
        self.connections = set()
        self.stopped = False
        self.stop_reason = "runtime stopped"

    async def send(self, frame):
        if self.stopped or self.process is None or self.process.returncode is not None:
            raise ConnectionError("runtime is unavailable")
        data = frame_bytes(frame)
        async with self.write_lock:
            if frame["op"] == "exec":
                self.active_exec = frame["id"]
            elif frame["op"] == "mcp":
                self.active_mcp = frame["id"]
            if frame["op"] in {"exec", "mcp"}:
                self.last_exec_activity = time.monotonic()
                self.idle_since = None
            self.process.stdin.write(data)
            await asyncio.wait_for(self.process.stdin.drain(), 10)

    def channel(self, kind):
        if self.stopped:
            raise web.HTTPServiceUnavailable(text="runtime is unavailable")
        if len(self.channels) >= self.manager.config["max_connections"]:
            raise web.HTTPServiceUnavailable(text="runtime connection limit reached")
        request_id = secrets.token_hex(12)
        queue = asyncio.Queue(QUEUE_SIZE)
        self.channels[request_id] = (kind, queue)
        if kind in {"exec", "mcp"}:
            self.last_exec_activity = time.monotonic()
            self.idle_since = None
        return request_id, queue

    async def read_frames(self):
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line or len(line) > MAX_FRAME or not line.endswith(b"\n"):
                    raise ValueError("supervisor protocol ended")
                frame = json.loads(line)
                if not isinstance(frame, dict):
                    raise TypeError("invalid supervisor frame")
                kind = frame.get("event")
                if kind == "ready":
                    if self.ready.done():
                        raise ValueError("duplicate supervisor ready")
                    self.ready.set_result(None)
                    self.last_ports = time.monotonic()
                elif kind == "ports":
                    if not self.ready.done():
                        raise ValueError("ports before ready")
                    ports = frame.get("ports")
                    if (
                        not isinstance(ports, list)
                        or len(ports) > 65535
                        or any(type(p) is not int or not 1 <= p <= 65535 for p in ports)
                    ):
                        raise ValueError("invalid port list")
                    processes = frame.get("processes")
                    if "processes" in frame and (
                        type(processes) is not int or processes < 0
                    ):
                        raise ValueError("invalid workload process count")
                    self.last_ports = self.last_heartbeat = time.monotonic()
                    self.processes = processes
                    self.reported_ports = set(ports) - {1080, 3128}
                    await self.manager._ports(self, self.reported_ports)
                    if self.manager._idle(self):
                        if self.idle_since is None:
                            self.idle_since = self.last_heartbeat
                    else:
                        self.idle_since = None
                elif kind in {
                    "data",
                    "exit",
                    "connected",
                    "written",
                    "end",
                    "closed",
                    "error",
                    "mcp_data",
                    "mcp_end",
                }:
                    request_id = frame.get("id")
                    if not isinstance(request_id, str):
                        raise ValueError("invalid response id")
                    if kind == "data":
                        decode_data(frame)
                    if kind == "mcp_data" and not 1 <= len(decode_data(frame)) <= 16384:
                        raise ValueError("invalid MCP chunk")
                    if kind == "exit" and (
                        type(frame.get("code")) is not int
                        or not -64 <= frame["code"] <= 255
                    ):
                        raise ValueError("invalid exit code")
                    if kind == "written" and (
                        type(frame.get("size")) is not int
                        or not 1 <= frame["size"] <= 16384
                    ):
                        raise ValueError("invalid write acknowledgment")
                    # Keep tracking a cancelled exec after its HTTP channel has
                    # disappeared; only supervisor completion clears it.
                    if kind in {"exit", "error"} and request_id == self.active_exec:
                        self.active_exec = None
                        self.last_exec_activity = time.monotonic()
                        self.idle_since = None
                    if kind in {"mcp_end", "error"} and request_id == self.active_mcp:
                        self.active_mcp = None
                        self.last_exec_activity = time.monotonic()
                        self.idle_since = None
                    channel = self.channels.get(request_id)
                    if channel:
                        if (
                            channel[0] == "mcp"
                            and kind not in {"mcp_data", "mcp_end", "error"}
                        ) or (channel[0] != "mcp" and kind in {"mcp_data", "mcp_end"}):
                            raise ValueError("invalid MCP channel response")
                        if (
                            kind == "data"
                            and channel[0] == "exec"
                            and frame.get("stream") not in {"stdout", "stderr"}
                        ):
                            raise ValueError("invalid output stream")
                        try:
                            if channel[0] == "exec":
                                await asyncio.wait_for(channel[1].put(frame), 1)
                            else:
                                # Let active consumers run without allowing a slow
                                # TCP peer to block every other protocol channel.
                                await asyncio.sleep(0)
                                channel[1].put_nowait(frame)
                        except (asyncio.QueueFull, asyncio.TimeoutError):
                            self.channels.pop(request_id, None)
                            while not channel[1].empty():
                                channel[1].get_nowait()
                            channel[1].put_nowait(
                                {
                                    "event": "error",
                                    "id": request_id,
                                    "error": "stream output limit exceeded",
                                }
                            )
                            await self.send(
                                {
                                    "op": {
                                        "exec": "cancel",
                                        "mcp": "mcp_cancel",
                                        "tcp": "close",
                                    }[channel[0]],
                                    "id": request_id,
                                }
                            )
                else:
                    raise ValueError("unknown supervisor frame")
        except (
            OSError,
            ValueError,
            TypeError,
            RecursionError,
            binascii.Error,
            asyncio.QueueFull,
            asyncio.TimeoutError,
        ):
            pass
        finally:
            if not self.ready.done():
                self.ready.set_exception(RuntimeError("supervisor failed before ready"))
            if not self.stopped:
                self.manager._background_stop(self.runtime_id)


class Manager:
    def __init__(self, config):
        self.config = dict(config)
        for key, default in (
            ("max_runtimes", 4),
            ("max_connections", 128),
            ("max_ports", 128),
            ("max_lifetime_seconds", 86400),
            ("idle_timeout_seconds", 300),
        ):
            self.config.setdefault(key, default)
        for key in (
            "memory_max",
            "tasks_max",
            "cpu_quota",
            "max_runtimes",
            "max_connections",
            "max_ports",
            "max_lifetime_seconds",
            "idle_timeout_seconds",
        ):
            if type(self.config.get(key)) is not int or self.config[key] <= 0:
                raise ValueError(f"{key} must be a positive integer")
        for key in (
            "runtime_root",
            "workspaces_root",
            "workspaces_tmp_root",
            "sandbox_exec",
            "supervisor",
            "python",
            "shell",
            "launch",
        ):
            value = self.config.get(key)
            if not isinstance(value, str) or not os.path.isabs(value):
                raise ValueError(f"{key} must be an absolute path")
        if "playwright_mcp" in self.config:
            value = self.config["playwright_mcp"]
            if not isinstance(value, str) or not os.path.isabs(value) or "\0" in value:
                raise ValueError("playwright_mcp must be an absolute path")
        domain = self.config.get("preview_domain", "")
        if (
            not isinstance(domain, str)
            or len(domain) > 189
            or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                for part in domain.split(".")
            )
        ):
            raise ValueError("invalid preview domain")
        self.runtime_root = Path(self.config["runtime_root"])
        self.workspaces_root = Path(self.config["workspaces_root"])
        self.tmp_root = Path(self.config["workspaces_tmp_root"])
        self.runtimes = {}
        self.mappings = {}
        self.lock = asyncio.Lock()
        self.watcher = None
        self.stopping = {}
        self.closing = False
        self.control_app = self.create_control_app()

    def create_control_app(self):
        app = web.Application(client_max_size=MAX_FRAME)
        app.router.add_post("/exec", self._exec)
        app.router.add_post("/mcp", self._mcp)
        app.router.add_post("/stop", self._stop_request)
        return app

    def _workspace(self, directory, session):
        if (
            not isinstance(session, str)
            or not SESSION.fullmatch(session)
            or len(session) > 128
        ):
            raise web.HTTPBadRequest(text="invalid session ID")
        if (
            not isinstance(directory, str)
            or not os.path.isabs(directory)
            or "\x00" in directory
        ):
            raise web.HTTPBadRequest(text="directory must be an absolute path")
        try:
            cwd = Path(directory).resolve(strict=True)
            relative = cwd.relative_to(self.workspaces_root)
            parts = relative.parts
            count = 2 if parts and parts[0] == ".tasks" else 1
            if len(parts) < count or (count == 1 and parts[0].startswith(".")):
                raise ValueError("invalid workspace root")
            root = self.workspaces_root.joinpath(*parts[:count])
            if root.resolve(strict=True) != root or not cwd.is_dir():
                raise ValueError("invalid workspace root")
            identity(root)
            # Reject aliases into a different workspace, including symlinked roots.
            lexical = Path(os.path.abspath(directory))
            lexical.relative_to(root)
            relative_root = root.relative_to(self.workspaces_root).as_posix()
            temporary = self.tmp_root / relative_root
            if temporary.resolve(strict=True) != temporary:
                raise ValueError("invalid temporary root")
            identity(temporary)
            session_tmp = temporary / session
            return root, relative_root, cwd, session_tmp
        except (OSError, ValueError, RuntimeError):
            raise web.HTTPForbidden(
                text="directory is not a managed workspace"
            ) from None

    async def start(self):
        if self.watcher:
            return
        for path in (
            self.runtime_root,
            self.runtime_root / "u",
            self.runtime_root / "broker",
        ):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.is_symlink() or path.stat().st_uid != os.getuid():
                raise RuntimeError("runtime directory is not privately owned")
            path.chmod(0o700)
        # The host service binds create_control_app() on control.sock with 0600
        # permissions; the manager itself never opens a TCP control endpoint.
        self.watcher = asyncio.create_task(self._watch())

    def list_previews(self):
        return [
            dict(mapping) for mapping in self.mappings.values() if mapping["available"]
        ]

    def lookup(self, host):
        mapping = self.mappings.get(host)
        return dict(mapping) if mapping is not None else None

    def _create_cgroup(self, runtime_id):
        root = delegated_root()
        required = {"cpu", "memory", "pids"}
        if not required <= set((root / "cgroup.controllers").read_text().split()):
            raise RuntimeError("required cgroup controllers are not delegated")
        (root / "cgroup.subtree_control").write_text("+cpu +memory +pids")
        group = root / ("runtime-" + runtime_id)
        group.mkdir()
        try:
            (group / "memory.max").write_text(str(self.config["memory_max"]))
            (group / "pids.max").write_text(str(self.config["tasks_max"]))
            (group / "cpu.max").write_text(f"{self.config['cpu_quota'] * 1000} 100000")
        except BaseException:
            group.rmdir()
            raise
        return group

    async def _spawn(self, runtime):
        runtime.cgroup = self._create_cgroup(runtime.runtime_id)
        # Only the service environment is trusted. Never inherit request/workload
        # environment, server passwords, language startup hooks, or token values.
        environment = {
            key: os.environ[key]
            for key in (
                "HOME",
                "PATH",
                "CREDENTIALS_DIRECTORY",
                "NODE_EXTRA_CA_CERTS",
                "NIX_SSL_CERT_FILE",
                "SSL_CERT_FILE",
            )
            if key in os.environ
        }
        environment["OPENCODE_SESSION_ID"] = runtime.session_id
        environment["OPENCODE_PREVIEW_URL_TEMPLATE"] = (
            f"https://preview-{runtime.slug}-{{port}}.{self.config['preview_domain']}/"
        )
        environment["OPENCODE_PREVIEW_RUNTIME_ID"] = runtime.runtime_id
        runtime.process = await asyncio.create_subprocess_exec(
            self.config["python"],
            "-I",
            "-S",
            self.config["launch"],
            "--cgroup",
            str(runtime.cgroup),
            "--sandbox-exec",
            self.config["sandbox_exec"],
            "--python",
            self.config["python"],
            "--supervisor",
            self.config["supervisor"],
            *(
                ["--playwright-mcp", self.config["playwright_mcp"]]
                if "playwright_mcp" in self.config
                else []
            ),
            cwd=runtime.directory,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=MAX_FRAME,
        )

    async def _get_runtime(self, directory, session):
        root, relative, cwd, session_tmp = self._workspace(directory, session)
        async with self.lock:
            if self.closing:
                raise web.HTTPServiceUnavailable(text="manager is stopping")
            for runtime in self.runtimes.values():
                if runtime.directory == str(root):
                    if runtime.session_id != session:
                        raise web.HTTPConflict(
                            text="workspace already has an active session"
                        )
                    if runtime.stopped or not self._valid(runtime):
                        self._background_stop(runtime.runtime_id)
                        raise web.HTTPConflict(text="workspace runtime is stopping")
                    return runtime, cwd
            if len(self.runtimes) >= self.config["max_runtimes"]:
                candidate = min(
                    (
                        r
                        for r in self.runtimes.values()
                        if r.idle_since is not None and self._idle(r)
                    ),
                    key=lambda r: r.idle_since,
                    default=None,
                )
                if candidate is None:
                    raise web.HTTPServiceUnavailable(text="runtime limit reached")
                try:
                    await self.stop(candidate.runtime_id)
                except (OSError, RuntimeError, asyncio.TimeoutError):
                    raise web.HTTPServiceUnavailable(
                        text="idle runtime cleanup failed"
                    ) from None
                if self.closing:
                    raise web.HTTPServiceUnavailable(text="manager is stopping")
            slug = workspace_slug(relative)
            if any(r.slug == slug for r in self.runtimes.values()):
                raise web.HTTPConflict(text="workspace hostname collision")
            try:
                session_tmp.mkdir(mode=0o700, exist_ok=True)
                identity(session_tmp)
                if session_tmp.resolve(strict=True) != session_tmp:
                    raise ValueError("invalid session temporary directory")
                session_tmp.chmod(0o700)
                runtime = Runtime(self, root, relative, session, session_tmp)
            except (OSError, ValueError):
                raise web.HTTPForbidden(
                    text="invalid session temporary directory"
                ) from None
            self.runtimes[runtime.runtime_id] = runtime
            try:
                async with asyncio.timeout(30):
                    runtime.startup_task = asyncio.create_task(self._spawn(runtime))
                    await asyncio.shield(runtime.startup_task)
                    runtime.reader_task = asyncio.create_task(runtime.read_frames())
                    await asyncio.shield(runtime.ready)
                if not self._valid(runtime) or runtime.stopped:
                    raise RuntimeError("workspace changed during startup")
                runtime.started = True
            except BaseException as error:
                await asyncio.shield(self.stop(runtime.runtime_id))
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise web.HTTPServiceUnavailable(
                    text="sandbox startup failed"
                ) from None
            return runtime, cwd

    def _valid(self, runtime):
        try:
            return (
                identity(runtime.root) == runtime.root_identity
                and runtime.root.resolve(strict=True) == runtime.root
                and identity(runtime.session_tmp) == runtime.tmp_identity
                and runtime.session_tmp.resolve(strict=True) == runtime.session_tmp
                and time.monotonic() - runtime.last_ports < 5
                and time.monotonic() - runtime.created
                < self.config["max_lifetime_seconds"]
            )
        except (OSError, ValueError, RuntimeError):
            return False

    async def _watch(self):
        while True:
            await asyncio.sleep(1)
            for runtime in list(self.runtimes.values()):
                if (
                    runtime.ready.done()
                    and not self._valid(runtime)
                    or (
                        runtime.idle_since is not None
                        and self._idle(runtime)
                        and time.monotonic() - runtime.idle_since
                        >= self.config["idle_timeout_seconds"]
                    )
                ):
                    self._background_stop(runtime.runtime_id)

    def _idle(self, runtime):
        return (
            runtime.started
            and not runtime.stopped
            and runtime.active_exec is None
            and runtime.active_mcp is None
            and not any(
                kind in {"exec", "mcp"} for kind, _ in runtime.channels.values()
            )
            and not runtime.reported_ports
            and not runtime.listeners
            and runtime.processes == 0
            and runtime.last_heartbeat > runtime.last_exec_activity
            and self._valid(runtime)
        )

    def _background_stop(self, runtime_id):
        if runtime_id not in self.stopping and runtime_id in self.runtimes:
            # Reserve teardown synchronously so a watcher cannot select an idle
            # runtime and then race a new exec before the stop task runs.
            self.runtimes[runtime_id].stopped = True
            task = asyncio.create_task(self._stop_runtime(self.runtimes[runtime_id]))
            self.stopping[runtime_id] = task
            # Retrieve errors even when teardown was triggered by protocol EOF.
            task.add_done_callback(
                lambda done: done.exception() if not done.cancelled() else None
            )
        return self.stopping.get(runtime_id)

    async def stop(self, runtime_id):
        task = self._background_stop(runtime_id)
        if task:
            await asyncio.shield(task)

    async def _kill_cgroup(self, runtime):
        drain = None
        try:
            if runtime.process is not None:
                # The protocol reader is already stopped. Drain remaining output
                # so it cannot prevent the supervisor and wrapper traps exiting.
                async def discard_output():
                    while await runtime.process.stdout.read(64 * 1024):
                        pass

                drain = asyncio.create_task(discard_output())
                with suppress(OSError):
                    runtime.process.stdin.close()
                with suppress(OSError, asyncio.TimeoutError):
                    await asyncio.wait_for(runtime.process.wait(), 3)
                # Kill the host helper before cgroup.kill so a helper still in
                # startup cannot enter the killed group and launch new children.
                if runtime.process.returncode is None:
                    with suppress(ProcessLookupError):
                        runtime.process.kill()
            if runtime.cgroup is not None:
                (runtime.cgroup / "cgroup.kill").write_text("1")
            if runtime.process is not None:
                await asyncio.wait_for(runtime.process.wait(), 10)
        finally:
            if drain:
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)
        if runtime.cgroup is not None:
            for _ in range(100):
                try:
                    for path in sorted(runtime.cgroup.rglob("*"), reverse=True):
                        if path.is_dir():
                            path.rmdir()
                    runtime.cgroup.rmdir()
                    runtime.cgroup = None
                    return
                except OSError:
                    await asyncio.sleep(0.05)
            raise RuntimeError("runtime cgroup did not become empty")

    async def _stop_runtime(self, runtime):
        runtime.stopped = True
        try:
            if runtime.startup_task:
                await asyncio.gather(runtime.startup_task, return_exceptions=True)
            if runtime.reader_task:
                runtime.reader_task.cancel()
                await asyncio.gather(runtime.reader_task, return_exceptions=True)
            for server in runtime.listeners.values():
                server.close()
            for server in runtime.listeners.values():
                await server.wait_closed()
            for host, mapping in list(self.mappings.items()):
                if mapping["runtime_id"] == runtime.runtime_id:
                    with suppress(FileNotFoundError):
                        Path(mapping["socket_path"]).unlink()
                    del self.mappings[host]
            for _, queue in runtime.channels.values():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"event": "error", "error": runtime.stop_reason})
            for task in runtime.connections:
                task.cancel()
            if runtime.connections:
                await asyncio.gather(*runtime.connections, return_exceptions=True)
            if not runtime.ready.done():
                runtime.ready.cancel()
            elif not runtime.ready.cancelled():
                runtime.ready.exception()
            await self._kill_cgroup(runtime)
            # /tmp is backed by session_tmp. A killed/unconfirmed namespace waiter
            # cannot clean safely; only an empty runtime cgroup permits recovery.
            if "playwright_mcp" in self.config:
                with suppress(FileNotFoundError):
                    fd = os.open(
                        runtime.session_tmp,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    )
                    try:
                        info = os.fstat(fd)
                        if (info.st_dev, info.st_ino) == runtime.tmp_identity:
                            shutil.rmtree(home_directory(runtime.runtime_id), dir_fd=fd)
                    finally:
                        os.close(fd)
            broker_root = self.runtime_root / "broker"
            if broker_root.resolve(strict=True) != broker_root:
                raise RuntimeError("invalid runtime broker root")
            with suppress(FileNotFoundError):
                shutil.rmtree(broker_root / runtime.runtime_id)
            self.runtimes.pop(runtime.runtime_id, None)
        finally:
            self.stopping.pop(runtime.runtime_id, None)

    async def close(self):
        self.closing = True
        if self.watcher:
            self.watcher.cancel()
            await asyncio.gather(self.watcher, return_exceptions=True)
        results = await asyncio.gather(
            *(self.stop(key) for key in list(self.runtimes)), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def _ports(self, runtime, ports):
        if runtime.stopped:
            return
        if len(runtime.known_ports | ports) > self.config["max_ports"]:
            runtime.stop_reason = "runtime port limit exceeded"
            raise ValueError(runtime.stop_reason)
        runtime.known_ports.update(ports)
        for port in set(runtime.listeners) - ports:
            server = runtime.listeners.pop(port)
            server.close()
            await server.wait_closed()
            mapping = self.mappings[self._hostname(runtime, port)]
            mapping["available"] = False
            with suppress(FileNotFoundError):
                Path(mapping["socket_path"]).unlink()
        for port in ports - set(runtime.listeners):
            host = self._hostname(runtime, port)
            mapping = self.mappings.get(host)
            if mapping is None:
                mapping = {
                    "runtime_id": runtime.runtime_id,
                    "directory": runtime.directory,
                    "workspace": runtime.workspace,
                    "session_id": runtime.session_id,
                    "slug": runtime.slug,
                    "port": port,
                    "hostname": host,
                    "socket_path": str(
                        self.runtime_root / "u" / f"{runtime.runtime_id}-{port}.sock"
                    ),
                    "token": secrets.token_urlsafe(32),
                    "available": False,
                }
                self.mappings[host] = mapping
            server = await asyncio.start_unix_server(
                lambda reader, writer, p=port: self._accept(runtime, p, reader, writer),
                path=mapping["socket_path"],
                limit=64 * 1024,
                start_serving=False,
            )
            try:
                Path(mapping["socket_path"]).chmod(0o600)
                runtime.listeners[port] = server
                await server.start_serving()
                mapping["available"] = True
            except BaseException:
                server.close()
                raise

    def _hostname(self, runtime, port):
        return f"preview-{runtime.slug}-{port}.{self.config['preview_domain']}"

    def _accept(self, runtime, port, reader, writer):
        if (
            runtime.stopped
            or len(runtime.connections) >= self.config["max_connections"]
        ):
            writer.close()
            return
        task = asyncio.create_task(self._connection(runtime, port, reader, writer))
        runtime.connections.add(task)
        task.add_done_callback(runtime.connections.discard)

    async def _connection(self, runtime, port, reader, writer):
        request_id = None
        upload = None
        try:
            request_id, queue = runtime.channel("tcp")
            await runtime.send({"op": "connect", "id": request_id, "port": port})
            first = await asyncio.wait_for(queue.get(), 10)
            if first["event"] != "connected":
                return

            pending = 0
            credited = asyncio.Event()

            async def upstream():
                nonlocal pending
                try:
                    while data := await reader.read(48 * 1024):
                        pending = len(data)
                        credited.clear()
                        await runtime.send(
                            {
                                "op": "data",
                                "id": request_id,
                                "data": base64.b64encode(data).decode("ascii"),
                            }
                        )
                        await credited.wait()
                    await runtime.send({"op": "end", "id": request_id})
                except (OSError, asyncio.TimeoutError):
                    writer.close()
                    with suppress(asyncio.QueueFull):
                        queue.put_nowait({"event": "closed", "id": request_id})

            upload = asyncio.create_task(upstream())
            while True:
                frame = await queue.get()
                if frame["event"] == "data":
                    writer.write(decode_data(frame))
                    async with asyncio.timeout(30):
                        await writer.drain()
                elif frame["event"] == "written":
                    if frame["size"] > pending:
                        raise ValueError("unexpected write acknowledgment")
                    pending -= frame["size"]
                    if not pending:
                        credited.set()
                elif frame["event"] == "end":
                    if writer.can_write_eof():
                        writer.write_eof()
                elif frame["event"] in {"closed", "error"}:
                    break
                else:
                    raise ValueError("invalid TCP response")
        except (OSError, ValueError, asyncio.TimeoutError, web.HTTPException):
            pass
        finally:
            if upload:
                upload.cancel()
                await asyncio.gather(upload, return_exceptions=True)
            if request_id:
                runtime.channels.pop(request_id, None)
                with suppress(OSError, asyncio.TimeoutError):
                    await runtime.send({"op": "close", "id": request_id})
            writer.close()
            with suppress(OSError, asyncio.TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 5)

    async def _body(self, request, keys):
        try:
            body = await request.json()
        except (ValueError, UnicodeError, RecursionError):
            raise web.HTTPBadRequest(text="invalid JSON") from None
        if not isinstance(body, dict) or set(body) != keys:
            raise web.HTTPBadRequest(text="invalid request fields")
        return body

    async def _stop_request(self, request):
        body = await self._body(request, {"directory", "session_id"})
        # Deletion hooks can arrive after the source directory has been removed.
        session, directory = body["session_id"], body["directory"]
        if (
            not isinstance(session, str)
            or not SESSION.fullmatch(session)
            or len(session) > 128
            or not isinstance(directory, str)
            or not os.path.isabs(directory)
            or "\x00" in directory
        ):
            raise web.HTTPBadRequest(text="invalid stop request")
        directory = os.path.realpath(directory)
        for runtime in list(self.runtimes.values()):
            if runtime.session_id == session and runtime.directory == directory:
                await self.stop(runtime.runtime_id)
        return web.json_response({"stopped": True})

    async def _mcp(self, request):
        try:
            body = await self._body(request, {"directory", "session_id", "request"})
            try:
                validate_request(body["request"])
                encoded = frame_bytes(
                    {"op": "mcp", "id": "0" * 24, "request": body["request"]}
                )
                if len(encoded) > MAX_MCP_REQUEST:
                    raise ValueError("MCP request limit exceeded")
            except (ValueError, TypeError, RecursionError, UnicodeError):
                raise web.HTTPBadRequest(text="invalid MCP request") from None
            if "playwright_mcp" not in self.config:
                raise web.HTTPServiceUnavailable(text="MCP is not configured")
            runtime, _ = await self._get_runtime(body["directory"], body["session_id"])
            if runtime.active_mcp is not None or any(
                kind == "mcp" for kind, _ in runtime.channels.values()
            ):
                raise web.HTTPConflict(text="session already has an active MCP request")
            request_id, queue = runtime.channel("mcp")
        except web.HTTPException as error:
            return web.json_response(
                {
                    "error": {
                        "code": -32600 if error.status in {400, 413} else -32000,
                        "message": error.text,
                    }
                },
                status=error.status,
            )
        finished = False
        result = bytearray()
        try:
            async with asyncio.timeout(MCP_TIMEOUT):
                await runtime.send(
                    {"op": "mcp", "id": request_id, "request": body["request"]}
                )
                while True:
                    if request.transport is None or request.transport.is_closing():
                        raise ConnectionError("MCP caller disconnected")
                    try:
                        frame = await asyncio.wait_for(queue.get(), 0.1)
                    except asyncio.TimeoutError:
                        continue
                    if frame["event"] == "mcp_data":
                        chunk = decode_data(frame)
                        if len(result) + len(chunk) > MAX_MCP_RESULT:
                            raise ValueError("MCP result limit exceeded")
                        result.extend(chunk)
                    elif frame["event"] == "mcp_end":
                        envelope = json.loads(result, parse_constant=reject_constant)
                        if not isinstance(envelope, dict) or set(envelope) not in (
                            {"result"},
                            {"error"},
                        ):
                            raise ValueError("invalid MCP envelope")
                        result_envelope(envelope)
                        # Return the already bounded UTF-8 body, without re-encoding images.
                        result.decode("utf-8")
                        finished = True
                        return web.Response(
                            body=bytes(result), content_type="application/json"
                        )
                    elif (
                        frame["event"] == "error"
                        and frame.get("error") == "MCP request timed out"
                    ):
                        raise asyncio.TimeoutError
                    else:
                        raise ValueError("MCP transport failed")
        except asyncio.TimeoutError:
            return web.json_response(
                {"error": {"code": -32000, "message": "MCP request timed out"}},
                status=504,
            )
        except (OSError, ValueError, TypeError, RecursionError):
            return web.json_response(
                {"error": {"code": -32000, "message": "MCP transport failed"}},
                status=502,
            )
        finally:
            runtime.last_exec_activity = time.monotonic()
            runtime.idle_since = None
            runtime.channels.pop(request_id, None)
            if not finished:
                with suppress(OSError, asyncio.TimeoutError):
                    await runtime.send({"op": "mcp_cancel", "id": request_id})

    async def _exec(self, request):
        body = await self._body(request, {"directory", "session_id", "argv"})
        argv = body["argv"]
        if (
            not isinstance(argv, list)
            or not argv
            or len(argv) > 4096
            or any(not isinstance(arg, str) or "\x00" in arg for arg in argv)
            or not argv[0]
        ):
            raise web.HTTPBadRequest(text="argv must be a nonempty array of strings")
        # Validate the encoded supervisor frame before allocating a sandbox.
        try:
            encoded = frame_bytes(
                {"op": "exec", "id": "0" * 24, "argv": argv, "cwd": body["directory"]}
            )
            if len(encoded) > 128 * 1024:
                raise ValueError("exec frame too large")
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="exec frame too large") from None
        runtime, cwd = await self._get_runtime(body["directory"], body["session_id"])
        if runtime.active_exec is not None or any(
            kind == "exec" for kind, _ in runtime.channels.values()
        ):
            raise web.HTTPConflict(text="session already has an active command")
        request_id, queue = runtime.channel("exec")
        response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        finished = False
        try:
            await response.prepare(request)
            await runtime.send(
                {"op": "exec", "id": request_id, "argv": argv, "cwd": str(cwd)}
            )
            while True:
                if request.transport is None or request.transport.is_closing():
                    break
                try:
                    frame = await asyncio.wait_for(queue.get(), 0.1)
                except asyncio.TimeoutError:
                    continue
                kind = frame["event"]
                if kind == "data":
                    await asyncio.wait_for(response.write(frame_bytes(frame)), 30)
                elif kind == "exit":
                    finished = True
                    await asyncio.wait_for(response.write(frame_bytes(frame)), 30)
                    break
                elif kind == "error":
                    await response.write(
                        frame_bytes(
                            {"event": "error", "error": "runtime execution failed"}
                        )
                    )
                    break
                else:
                    raise ValueError("invalid exec response")
        except (OSError, ValueError, asyncio.TimeoutError):
            pass
        finally:
            runtime.last_exec_activity = time.monotonic()
            runtime.idle_since = None
            runtime.channels.pop(request_id, None)
            if not finished:
                with suppress(OSError, asyncio.TimeoutError):
                    await runtime.send({"op": "cancel", "id": request_id})
        return response
