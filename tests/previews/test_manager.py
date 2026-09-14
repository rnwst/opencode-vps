"""Unprivileged manager tests. Cgroup and sandbox entry are explicitly faked."""

import asyncio
import base64
import json
import os
import signal
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from aiohttp import web

SOURCE = Path(__file__).resolve().parents[2] / "pkgs" / "opencode-preview"
sys.path.insert(0, str(SOURCE))
import client
import launch
import manager as module


class FakeProcess:
    def __init__(self):
        self.stdout = asyncio.StreamReader(limit=module.MAX_FRAME)
        self.stdin = self
        self.returncode = None
        self.frames = []
        self.exited = asyncio.Event()
        self.tcp = {}
        self.auto_credit = True
        self.ignore_close = False
        self.emit(event="ready")
        self.emit(event="ports", ports=[])

    def emit(self, **frame):
        self.stdout.feed_data(module.frame_bytes(frame))

    def write(self, data):
        frame = json.loads(data)
        self.frames.append(frame)
        request_id = frame["id"]
        op = frame["op"]
        if op == "exec":
            self.emit(
                event="data",
                id=request_id,
                stream="stdout",
                data=base64.b64encode(b"hello\x00\xff\n").decode(),
            )
            if frame["argv"] != ["hold"]:
                self.emit(
                    event="data",
                    id=request_id,
                    stream="stderr",
                    data=base64.b64encode(b"diagnostic\n").decode(),
                )
                self.emit(event="exit", id=request_id, code=23)
        elif op == "connect":
            self.tcp[request_id] = bytearray()
            self.emit(event="connected", id=request_id)
        elif op == "data":
            decoded = base64.b64decode(frame["data"])
            self.tcp[request_id].extend(decoded)
            if self.auto_credit:
                for offset in range(0, len(decoded), 16384):
                    self.emit(
                        event="written",
                        id=request_id,
                        size=min(16384, len(decoded) - offset),
                    )
        elif op == "end":
            # Respond only after client write-half-close, exercising true half-close.
            response = bytes(self.tcp[request_id]) + b"!"
            for offset in range(0, len(response), 16384):
                self.emit(
                    event="data",
                    id=request_id,
                    data=base64.b64encode(response[offset : offset + 16384]).decode(),
                )
            self.emit(event="end", id=request_id)
            self.emit(event="closed", id=request_id)
        elif op == "cancel":
            self.emit(event="exit", id=request_id, code=-15)

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        if not self.ignore_close:
            self.returncode = 0
            self.stdout.feed_eof()
            self.exited.set()

    def kill(self):
        self.returncode = -9
        self.stdout.feed_eof()
        self.exited.set()

    async def wait(self):
        await self.exited.wait()
        return self.returncode


class FakeManager(module.Manager):
    def __init__(self, config):
        super().__init__(config)
        self.spawned = []
        self.killed = []

    async def _spawn(self, runtime):
        runtime.process = FakeProcess()
        self.spawned.append(runtime)

    async def _kill_cgroup(self, runtime):
        self.killed.append(runtime.runtime_id)
        if runtime.process:
            runtime.process.kill()
            await runtime.process.wait()


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="preview-")
        self.base = Path(self.temporary.name)
        self.workspaces = self.base / "w"
        self.tmp = self.base / "t"
        self.workspaces.mkdir()
        self.tmp.mkdir()
        self.root = self.workspace("manual")
        self.config = {
            "public_host": "opencode.example.test",
            "preview_domain": "preview.example.test",
            "opencode_port": 4096,
            "listen_port": 4097,
            "runtime_root": str(self.base / "r"),
            "workspaces_root": str(self.workspaces),
            "workspaces_tmp_root": str(self.tmp),
            "sandbox_exec": "/fixed/sandbox",
            "supervisor": str(SOURCE / "supervisor.py"),
            "python": sys.executable,
            "shell": "/bin/sh",
            "launch": str(SOURCE / "launch.py"),
            "memory_max": 1024 * 1024,
            "tasks_max": 128,
        }
        self.manager = FakeManager(self.config)
        await self.manager.start()
        self.runner = web.AppRunner(
            self.manager.create_control_app(), handler_cancellation=True
        )
        await self.runner.setup()
        socket_path = self.base / "r" / "control.sock"
        await web.UnixSite(self.runner, str(socket_path)).start()
        socket_path.chmod(0o600)
        connector = aiohttp.UnixConnector(path=str(self.base / "r" / "control.sock"))
        self.http = aiohttp.ClientSession(connector=connector)

    async def asyncTearDown(self):
        await self.http.close()
        await self.manager.close()
        await self.runner.cleanup()
        self.temporary.cleanup()

    def workspace(self, name):
        root = self.workspaces / name
        root.mkdir(parents=True)
        (self.tmp / name).mkdir(parents=True)
        return root

    async def execute(self, **changes):
        body = {"directory": str(self.root), "session_id": "ses_one", "argv": ["echo"]}
        body.update(changes)
        return await self.http.post("http://localhost/exec", json=body)

    async def runtime(self):
        runtime, _ = await self.manager._get_runtime(str(self.root), "ses_one")
        return runtime

    async def eventually(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.01)

    async def heartbeat(self, runtime, **fields):
        previous = runtime.last_heartbeat
        runtime.process.emit(event="ports", ports=fields.pop("ports", []), **fields)
        await self.eventually(lambda: runtime.last_heartbeat > previous)

    def test_config_validation(self):
        for key, value in (
            ("memory_max", True),
            ("cpu_quota", 200),
            ("tasks_max", "128"),
            ("launch", "relative.py"),
            ("preview_domain", "bad..test"),
            ("preview_domain", "a" * 190),
            ("max_ports", 0),
            ("max_ports", True),
            ("idle_timeout_seconds", 0),
            ("idle_timeout_seconds", True),
            ("idle_timeout_seconds", "300"),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                module.Manager({**self.config, key: value})
        self.assertEqual(self.manager.config["max_runtimes"], 4)
        self.assertEqual(self.manager.config["max_ports"], 128)
        self.assertEqual(self.manager.config["idle_timeout_seconds"], 300)

    def test_hostname_lengths_and_collision_namespace(self):
        names = [
            "manual",
            "a" * 40,
            "a" * 41,
            "A Name!",
            ".tasks/manual",
            ".tasks/A Name!",
        ]
        slugs = [module.workspace_slug(name) for name in names]
        slugs.append(module.workspace_slug(slugs[-1]))
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertEqual(slugs[0], "manual")
        self.assertEqual(slugs[1], "a" * 40)
        for slug in slugs:
            self.assertLessEqual(len(slug), 40)
            self.assertRegex(slug, r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
            self.assertLessEqual(len(f"preview-{slug}-65535"), 63)

    async def test_workspace_and_schema_validation(self):
        sub = self.root / "sub"
        sub.mkdir()
        task = self.workspace(".tasks/task")
        self.assertEqual(self.manager._workspace(str(sub), "ses_one")[0], self.root)
        self.assertEqual(
            self.manager._workspace(str(task), "ses_one")[1], ".tasks/task"
        )
        alias = self.workspaces / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        external = self.root / "outside"
        external.symlink_to(self.base, target_is_directory=True)
        for directory in (
            str(self.workspaces),
            str(self.base),
            str(alias),
            str(external),
            str(self.workspaces / ".tasks"),
            "relative",
            "bad\x00path",
        ):
            response = await self.execute(directory=directory)
            self.assertIn(response.status, (400, 403), directory)
            await response.read()
        for session in (None, "ses_bad-id", "ses_", "../ses_one", "ses_" + "a" * 200):
            response = await self.execute(session_id=session)
            self.assertEqual(response.status, 400)
            await response.read()
        for argv in ([], "echo", [True], [""], ["a\x00b"], ["x" * (128 * 1024)]):
            response = await self.execute(argv=argv)
            self.assertEqual(response.status, 400)
            await response.read()
        response = await self.execute(environment={"GH_TOKEN": "no"})
        self.assertEqual(response.status, 400)
        await response.read()
        self.assertFalse(self.manager.spawned)

    async def test_persistent_session_and_conflict(self):
        for _ in range(2):
            async with await self.execute() as response:
                self.assertEqual(response.status, 200)
                frames = [
                    json.loads(line) for line in (await response.read()).splitlines()
                ]
                self.assertEqual(frames[-1]["code"], 23)
                self.assertEqual(
                    base64.b64decode(frames[0]["data"]), b"hello\x00\xff\n"
                )
        self.assertEqual(len(self.manager.spawned), 1)
        async with await self.execute(session_id="ses_other") as response:
            self.assertEqual(response.status, 409)
            self.assertNotIn("ses_one", await response.text())
        runtime = self.manager.spawned[0]
        self.assertEqual({frame["op"] for frame in runtime.process.frames}, {"exec"})
        self.assertEqual(
            stat.S_IMODE((self.base / "r" / "control.sock").stat().st_mode), 0o600
        )

    async def test_disconnect_cancels_only_current_exec(self):
        response = await self.execute(argv=["hold"])
        await response.content.readline()
        runtime = self.manager.spawned[0]
        response.close()
        await self.eventually(
            lambda: any(f["op"] == "cancel" for f in runtime.process.frames)
        )
        self.assertFalse(runtime.stopped)
        self.assertFalse(self.manager.killed)
        async with await self.execute() as response:
            self.assertEqual(response.status, 200)
            await response.read()

    async def test_concurrent_exec_refused(self):
        first = await self.execute(argv=["hold"])
        await first.content.readline()
        async with await self.execute() as second:
            self.assertEqual(second.status, 409)
            await second.read()
        first.close()

    async def test_ports_tokens_and_half_close(self):
        runtime = await self.runtime()
        runtime.process.emit(event="ports", ports=[1080, 3128, 8080])
        await self.eventually(lambda: bool(self.manager.list_previews()))
        mapping = self.manager.list_previews()[0]
        self.assertEqual(mapping["port"], 8080)
        self.assertEqual(
            set(mapping),
            {
                "runtime_id",
                "directory",
                "workspace",
                "session_id",
                "slug",
                "port",
                "hostname",
                "socket_path",
                "token",
                "available",
            },
        )
        self.assertEqual(
            stat.S_IMODE(Path(mapping["socket_path"]).stat().st_mode), 0o600
        )
        reader, writer = await asyncio.open_unix_connection(mapping["socket_path"])
        writer.write(b"request")
        await writer.drain()
        writer.write_eof()
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"request!")
        writer.close()
        await writer.wait_closed()
        runtime.process.emit(event="ports", ports=[])
        await self.eventually(lambda: not self.manager.list_previews())
        self.assertFalse(self.manager.lookup(mapping["hostname"])["available"])
        self.assertFalse(Path(mapping["socket_path"]).exists())
        runtime.process.emit(event="ports", ports=[8080])
        await self.eventually(lambda: bool(self.manager.list_previews()))
        self.assertEqual(self.manager.lookup(mapping["hostname"]), mapping)
        mapping["token"] = "tampered"
        self.assertNotEqual(
            self.manager.lookup(mapping["hostname"])["token"], "tampered"
        )
        await self.manager.stop(runtime.runtime_id)
        self.assertIsNone(self.manager.lookup(mapping["hostname"]))
        self.assertFalse(Path(mapping["socket_path"]).exists())

    async def test_runtime_and_connection_limits(self):
        self.manager.config["max_runtimes"] = 1
        runtime = await self.runtime()
        other = self.workspace("other")
        async with await self.execute(directory=str(other)) as response:
            self.assertEqual(response.status, 503)
            await response.read()
        self.manager.config["max_connections"] = 1
        runtime.channel("tcp")
        with self.assertRaises(web.HTTPServiceUnavailable):
            runtime.channel("tcp")

    async def test_idle_timeout_preserves_session_temporary_data(self):
        async with await self.execute() as response:
            await response.read()
        runtime = self.manager.spawned[0]
        temporary = runtime.session_tmp / "keep"
        temporary.write_text("session data")
        self.assertIsNone(runtime.idle_since)
        await self.heartbeat(runtime, processes=0)
        self.assertTrue(self.manager._idle(runtime))
        self.assertIsNotNone(runtime.idle_since)
        first_idle = runtime.idle_since
        await self.heartbeat(runtime, processes=0)
        self.assertEqual(runtime.idle_since, first_idle)
        self.assertFalse(runtime.stopped)
        await self.heartbeat(runtime)
        self.assertIsNone(runtime.idle_since)
        self.assertFalse(self.manager._idle(runtime))
        await self.heartbeat(runtime, processes=0)
        self.assertGreater(runtime.idle_since, first_idle)
        runtime.idle_since -= self.manager.config["idle_timeout_seconds"]
        await self.eventually(lambda: runtime.runtime_id not in self.manager.runtimes)
        self.assertEqual(temporary.read_text(), "session data")
        async with await self.execute() as response:
            self.assertEqual(response.status, 200)
            await response.read()
        self.assertNotEqual(self.manager.spawned[-1].runtime_id, runtime.runtime_id)
        self.assertEqual(temporary.read_text(), "session data")

    async def test_idle_requires_post_completion_heartbeat(self):
        self.manager.config["max_runtimes"] = 1
        runtime = await self.runtime()
        other = self.workspace("other")
        await self.heartbeat(runtime, processes=0)
        self.assertTrue(self.manager._idle(runtime))
        async with await self.execute() as response:
            await response.read()
        self.assertIsNone(runtime.active_exec)
        self.assertIsNone(runtime.idle_since)
        self.assertFalse(self.manager._idle(runtime))
        async with await self.execute(directory=str(other)) as response:
            self.assertEqual(response.status, 503)
            await response.read()
        await self.heartbeat(runtime, processes=0)
        async with await self.execute(directory=str(other)) as response:
            self.assertEqual(response.status, 200)
            await response.read()
        self.assertNotIn(runtime.runtime_id, self.manager.runtimes)

    async def test_empty_runtime_does_not_transfer_workspace_session(self):
        runtime = await self.runtime()
        await self.heartbeat(runtime, processes=0)
        async with await self.execute(session_id="ses_other") as response:
            self.assertEqual(response.status, 409)
            await response.read()
        self.assertIn(runtime.runtime_id, self.manager.runtimes)
        async with await self.execute() as response:
            self.assertEqual(response.status, 200)
            await response.read()
        self.assertEqual(len(self.manager.spawned), 1)

    async def test_background_work_ports_and_unknown_counts_prevent_eviction(self):
        self.manager.config["max_runtimes"] = 1
        runtime = await self.runtime()
        other = self.workspace("other")
        for fields in ({"processes": 1}, {"processes": 0, "ports": [8080]}, {}):
            with self.subTest(fields=fields):
                await self.heartbeat(runtime, **fields)
                self.assertFalse(self.manager._idle(runtime))
                self.assertIsNone(runtime.idle_since)
                async with await self.execute(directory=str(other)) as response:
                    self.assertEqual(response.status, 503)
                    await response.read()
                self.assertFalse(runtime.stopped)
        self.assertIsNone(runtime.processes)
        await self.heartbeat(runtime, processes=0, ports=[8080])
        token = self.manager.list_previews()[0]["token"]
        runtime.idle_since = module.time.monotonic() - 301
        await asyncio.sleep(1.1)
        self.assertFalse(runtime.stopped)
        self.assertEqual(self.manager.list_previews()[0]["token"], token)
        await self.heartbeat(runtime, processes=1)
        runtime.idle_since = module.time.monotonic() - 301
        await asyncio.sleep(1.1)
        self.assertFalse(runtime.stopped)

    async def test_starting_runtime_is_not_idle(self):
        self.manager.config["max_runtimes"] = 1
        runtime = await self.runtime()
        runtime.started = False
        await self.heartbeat(runtime, processes=0)
        self.assertFalse(self.manager._idle(runtime))
        other = self.workspace("other")
        async with await self.execute(directory=str(other)) as response:
            self.assertEqual(response.status, 503)
            await response.read()

    async def test_cancelled_exec_remains_busy_until_exit_and_new_heartbeat(self):
        self.manager.config["max_runtimes"] = 1
        response = await self.execute(argv=["hold"])
        await response.content.readline()
        runtime = self.manager.spawned[0]
        request_id = runtime.active_exec
        await self.heartbeat(runtime, processes=0)
        self.assertFalse(self.manager._idle(runtime))
        original_write = runtime.process.write

        def delayed_cancel(data):
            frame = json.loads(data)
            if frame["op"] == "cancel":
                runtime.process.frames.append(frame)
            else:
                original_write(data)

        with patch.object(runtime.process, "write", delayed_cancel):
            response.close()
            await self.eventually(lambda: request_id not in runtime.channels)
            await self.heartbeat(runtime, processes=0)
            self.assertEqual(runtime.active_exec, request_id)
            self.assertFalse(self.manager._idle(runtime))
            async with await self.execute() as busy:
                self.assertEqual(busy.status, 409)
                await busy.read()
            runtime.process.emit(event="exit", id=request_id, code=-15)
            await self.eventually(lambda: runtime.active_exec is None)
            self.assertFalse(self.manager._idle(runtime))
            await self.heartbeat(runtime, processes=0)
            self.assertTrue(self.manager._idle(runtime))

    async def test_capacity_evicts_oldest_empty_runtime_after_cleanup(self):
        self.manager.config["max_runtimes"] = 2
        first = await self.runtime()
        second_root = self.workspace("second")
        second, _ = await self.manager._get_runtime(str(second_root), "ses_two")
        await self.heartbeat(first, processes=0)
        await self.heartbeat(second, processes=0)
        third_root = self.workspace("third")
        entered, release = asyncio.Event(), asyncio.Event()
        original_kill = self.manager._kill_cgroup

        async def delayed_cleanup(runtime):
            self.assertIs(runtime, first)
            entered.set()
            await release.wait()
            await original_kill(runtime)

        with patch.object(self.manager, "_kill_cgroup", delayed_cleanup):
            pending = asyncio.create_task(
                self.manager._get_runtime(str(third_root), "ses_three")
            )
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertTrue(first.stopped)
                self.assertEqual(len(self.manager.spawned), 2)
                self.assertFalse(pending.done())
            finally:
                release.set()
            third, _ = await asyncio.wait_for(pending, 2)
        self.assertEqual(self.manager.killed, [first.runtime_id])
        self.assertEqual(
            set(self.manager.runtimes), {second.runtime_id, third.runtime_id}
        )

    async def test_capacity_eviction_cleanup_failure_does_not_spawn(self):
        self.manager.config["max_runtimes"] = 1
        runtime = await self.runtime()
        await self.heartbeat(runtime, processes=0)
        other = self.workspace("other")
        with patch.object(
            self.manager, "_kill_cgroup", side_effect=PermissionError("cgroup busy")
        ):
            async with await self.execute(directory=str(other)) as response:
                self.assertEqual(response.status, 503)
                self.assertEqual(await response.text(), "idle runtime cleanup failed")
        self.assertEqual(len(self.manager.spawned), 1)
        self.assertIn(runtime.runtime_id, self.manager.runtimes)

    async def test_invalid_process_count_fails_closed(self):
        for count in (True, "0", -1, None):
            runtime = await self.runtime()
            runtime.process.emit(event="ports", ports=[], processes=count)
            await self.eventually(
                lambda runtime=runtime: runtime.runtime_id not in self.manager.runtimes
            )

    async def test_stop_hook_idempotent_and_session_scoped(self):
        runtime = await self.runtime()
        for session in ("ses_other", "ses_one", "ses_one"):
            async with self.http.post(
                "http://localhost/stop",
                json={
                    "directory": str(self.root),
                    "session_id": session,
                },
            ) as response:
                self.assertEqual(response.status, 200)
                await response.read()
            if session == "ses_other":
                self.assertIn(runtime.runtime_id, self.manager.runtimes)
        self.assertEqual(self.manager.killed, [runtime.runtime_id])

    async def test_inode_and_temporary_deletion_watch(self):
        runtime = await self.runtime()
        self.root.rename(self.workspaces / "old")
        self.root.mkdir()
        await self.eventually(lambda: runtime.runtime_id not in self.manager.runtimes)
        runtime = await self.runtime()
        runtime.session_tmp.rmdir()
        await self.eventually(lambda: runtime.runtime_id not in self.manager.runtimes)

    async def test_lifetime_and_observer_fail_closed(self):
        runtime = await self.runtime()
        runtime.created -= self.manager.config["max_lifetime_seconds"]
        await self.eventually(lambda: runtime.runtime_id not in self.manager.runtimes)
        runtime = await self.runtime()
        runtime.last_ports -= 6
        await self.eventually(lambda: runtime.runtime_id not in self.manager.runtimes)

    async def test_protocol_eof_stops_runtime(self):
        runtime = await self.runtime()
        runtime.process.stdout.feed_eof()
        await self.eventually(lambda: runtime.runtime_id not in self.manager.runtimes)

    async def test_queue_overflow_closes_only_affected_channel(self):
        runtime = await self.runtime()
        for kind, operation in (("tcp", "close"), ("exec", "cancel")):
            request_id, queue = runtime.channel(kind)
            healthy, healthy_queue = runtime.channel("tcp")
            for _ in range(module.QUEUE_SIZE + 1):
                runtime.process.emit(
                    event="data", id=request_id, stream="stdout", data="eA=="
                )
            runtime.process.emit(event="data", id=healthy, data="eQ==")
            frame = await asyncio.wait_for(healthy_queue.get(), 2)
            self.assertEqual(base64.b64decode(frame["data"]), b"y")
            self.assertNotIn(request_id, runtime.channels)
            self.assertEqual(
                queue.get_nowait()["error"], "stream output limit exceeded"
            )
            self.assertIn({"op": operation, "id": request_id}, runtime.process.frames)
            self.assertFalse(runtime.stopped)
            runtime.channels.pop(healthy)

    async def test_port_cap_counts_disappeared_mappings(self):
        self.manager.config["max_ports"] = 2
        runtime = await self.runtime()
        broker = self.base / "r" / "broker" / runtime.runtime_id
        broker.mkdir()
        _, queue = runtime.channel("exec")
        for port in (8080, 8081):
            runtime.process.emit(event="ports", ports=[port])
            await self.eventually(lambda port=port: port in runtime.listeners)
        self.assertEqual(len(self.manager.mappings), 2)
        runtime.process.emit(event="ports", ports=[8082])
        await self.eventually(lambda: runtime.runtime_id not in self.manager.runtimes)
        self.assertEqual(queue.get_nowait()["error"], "runtime port limit exceeded")
        self.assertFalse(self.manager.mappings)
        self.assertFalse(broker.exists())
        self.assertFalse(list((self.base / "r" / "u").iterdir()))

    async def test_tcp_upload_waits_for_all_written_credits(self):
        runtime = await self.runtime()
        runtime.process.auto_credit = False
        runtime.process.emit(event="ports", ports=[8080])
        await self.eventually(lambda: bool(self.manager.list_previews()))
        reader, writer = await asyncio.open_unix_connection(
            self.manager.list_previews()[0]["socket_path"]
        )
        try:
            writer.write(b"a" * (48 * 1024) + b"last")
            await writer.drain()
            writer.write_eof()
            await self.eventually(
                lambda: any(f["op"] == "data" for f in runtime.process.frames)
            )
            first = next(f for f in runtime.process.frames if f["op"] == "data")
            request_id = first["id"]
            self.assertEqual(len(base64.b64decode(first["data"])), 48 * 1024)
            runtime.process.emit(event="written", id=request_id, size=16384)
            runtime.process.emit(event="written", id=request_id, size=16384)
            await asyncio.sleep(0.05)
            self.assertEqual(sum(f["op"] == "data" for f in runtime.process.frames), 1)
            self.assertFalse(any(f["op"] == "end" for f in runtime.process.frames))
            runtime.process.emit(event="written", id=request_id, size=16384)
            await self.eventually(
                lambda: sum(f["op"] == "data" for f in runtime.process.frames) == 2
            )
            self.assertFalse(any(f["op"] == "end" for f in runtime.process.frames))
            runtime.process.emit(event="written", id=request_id, size=4)
            self.assertEqual(
                await asyncio.wait_for(reader.read(), 2), b"a" * (48 * 1024) + b"last!"
            )
            self.assertFalse(runtime.stopped)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_invalid_written_credit_fails_closed(self):
        for size in (0, -1, 16385, True, "1"):
            runtime = await self.runtime()
            request_id, _ = runtime.channel("tcp")
            runtime.process.emit(event="written", id=request_id, size=size)
            await self.eventually(
                lambda runtime=runtime: runtime.runtime_id not in self.manager.runtimes
            )

    async def test_shutdown_allows_traps_and_cleans_only_owned_broker(self):
        runtime = await self.runtime()
        runtime.stopped = True
        runtime.reader_task.cancel()
        await asyncio.gather(runtime.reader_task, return_exceptions=True)
        runtime.stopped = False
        runtime.process.kill()
        broker_root = self.base / "r" / "broker"
        self.assertEqual(stat.S_IMODE(broker_root.stat().st_mode), 0o700)
        owned = broker_root / runtime.runtime_id
        owned.mkdir()
        (owned / "settings").write_text("private")
        sibling = broker_root / "not-owned"
        sibling.mkdir()
        marker = self.base / "trap-ran"
        runtime.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import sys,pathlib; sys.stdin.buffer.read(); "
            "sys.stdout.buffer.write(b'x'*1048576); sys.stdout.buffer.flush(); "
            "pathlib.Path(sys.argv[1]).write_text('graceful')",
            str(marker),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        group = runtime.cgroup = MagicMock(spec=Path)
        group.rglob.return_value = []
        with (
            patch.object(
                self.manager,
                "_kill_cgroup",
                module.Manager._kill_cgroup.__get__(self.manager),
            ),
            patch.object(runtime.process, "kill", wraps=runtime.process.kill) as kill,
        ):
            await self.manager.stop(runtime.runtime_id)
        kill.assert_not_called()
        group.__truediv__.assert_called_once_with("cgroup.kill")
        group.__truediv__.return_value.write_text.assert_called_once_with("1")
        group.rmdir.assert_called_once()
        self.assertIsNone(runtime.cgroup)
        self.assertEqual(marker.read_text(), "graceful")
        self.assertFalse(owned.exists())
        self.assertTrue(sibling.is_dir())

    async def test_shutdown_grace_timeout_still_kills_cgroup(self):
        runtime = await self.runtime()
        runtime.process.ignore_close = True
        group = runtime.cgroup = MagicMock(spec=Path)
        group.rglob.return_value = []
        with (
            patch.object(
                self.manager,
                "_kill_cgroup",
                module.Manager._kill_cgroup.__get__(self.manager),
            ),
            patch.object(
                runtime.process,
                "wait",
                AsyncMock(side_effect=[asyncio.TimeoutError, -9]),
            ),
        ):
            await self.manager.stop(runtime.runtime_id)
        self.assertEqual(runtime.process.returncode, -9)
        group.__truediv__.return_value.write_text.assert_called_once_with("1")
        self.assertNotIn(runtime.runtime_id, self.manager.runtimes)

    async def test_failed_cgroup_cleanup_keeps_broker_until_processes_dead(self):
        runtime = await self.runtime()
        broker = self.base / "r" / "broker" / runtime.runtime_id
        broker.mkdir()
        with (
            patch.object(
                self.manager, "_kill_cgroup", side_effect=PermissionError("not dead")
            ),
            self.assertRaises(PermissionError),
        ):
            await self.manager.stop(runtime.runtime_id)
        self.assertTrue(broker.is_dir())
        self.assertIn(runtime.runtime_id, self.manager.runtimes)
        await self.manager.close()
        self.assertFalse(broker.exists())

    async def test_startup_failure_has_no_unsafe_fallback(self):
        async def fail(runtime):
            (self.base / "r" / "broker" / runtime.runtime_id).mkdir()
            raise PermissionError("cgroup delegation unavailable")

        with patch.object(self.manager, "_spawn", fail):
            async with await self.execute() as response:
                self.assertEqual(response.status, 503)
                self.assertEqual(await response.text(), "sandbox startup failed")
        self.assertFalse(self.manager.runtimes)
        self.assertFalse(list((self.base / "r" / "broker").iterdir()))

    async def test_startup_cancellation_cleans_child(self):
        started = asyncio.Event()

        async def delayed(runtime):
            runtime.process = FakeProcess()
            started.set()
            await asyncio.sleep(0.05)

        with patch.object(self.manager, "_spawn", delayed):
            task = asyncio.create_task(
                self.manager._get_runtime(str(self.root), "ses_one")
            )
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.manager.runtimes)
        self.assertEqual(len(self.manager.killed), 1)

    async def test_launch_uses_only_service_environment(self):
        runtime = await self.runtime()
        fake = FakeProcess()
        with (
            patch.object(
                self.manager, "_create_cgroup", return_value=Path("/fake/cgroup")
            ),
            patch.object(
                module.asyncio, "create_subprocess_exec", return_value=fake
            ) as spawn,
            patch.dict(
                os.environ,
                {
                    "HOME": "/home/bot",
                    "PATH": "/trusted/bin",
                    "CREDENTIALS_DIRECTORY": "/run/credentials/manager",
                    "NODE_EXTRA_CA_CERTS": "/trusted/node-ca.pem",
                    "NIX_SSL_CERT_FILE": "/trusted/nix-ca.pem",
                    "SSL_CERT_FILE": "/trusted/ca.pem",
                    "OPENCODE_PREVIEW_RUNTIME_ID": "untrusted-selector",
                    "GH_TOKEN": "real",
                    "OPENCODE_SERVER_PASSWORD": "secret",
                    "PYTHONPATH": "/workload",
                },
                clear=True,
            ),
        ):
            await module.Manager._spawn(self.manager, runtime)
        environment = spawn.call_args.kwargs["env"]
        self.assertEqual(
            environment,
            {
                "HOME": "/home/bot",
                "PATH": "/trusted/bin",
                "CREDENTIALS_DIRECTORY": "/run/credentials/manager",
                "NODE_EXTRA_CA_CERTS": "/trusted/node-ca.pem",
                "NIX_SSL_CERT_FILE": "/trusted/nix-ca.pem",
                "SSL_CERT_FILE": "/trusted/ca.pem",
                "OPENCODE_PREVIEW_RUNTIME_ID": runtime.runtime_id,
                "OPENCODE_SESSION_ID": "ses_one",
                "OPENCODE_PREVIEW_URL_TEMPLATE": (
                    f"https://preview-{runtime.slug}-{{port}}.{self.config['preview_domain']}/"
                ),
            },
        )
        self.assertEqual(
            spawn.call_args.args[:4],
            (sys.executable, "-I", "-S", self.config["launch"]),
        )
        self.assertNotIn("secret", repr(spawn.call_args))

    async def test_client_binary_output_exit_and_signal(self):
        config = self.base / "config.json"
        config.write_text(json.dumps(self.config))
        environment = {**os.environ, "OPENCODE_SESSION_ID": "ses_one"}
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(SOURCE / "client.py"),
            str(config),
            "--",
            "echo",
            cwd=self.root,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
        self.assertEqual(process.returncode, 23)
        self.assertEqual(stdout, b"hello\x00\xff\n")
        self.assertEqual(stderr, b"diagnostic\n")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(SOURCE / "client.py"),
            str(config),
            "--",
            "hold",
            cwd=self.root,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(process.stdout.readline(), 5)
        process.send_signal(signal.SIGTERM)
        await asyncio.wait_for(process.communicate(), 5)
        self.assertEqual(process.returncode, 143)
        runtime = self.manager.spawned[0]
        await self.eventually(
            lambda: any(f["op"] == "cancel" for f in runtime.process.frames)
        )
        self.assertFalse(runtime.stopped)

    async def test_real_supervisor_background_pipe_and_large_output(self):
        # Exercise the actual protocol implementation without claiming sandbox
        # security: only this test bypasses secure_process and host port discovery.
        script = (
            "import runpy,sys; ns=runpy.run_path(sys.argv[1]); "
            "worker=ns['Supervisor']; "
            "worker.run.__globals__['listening_ports']=lambda: [int(sys.argv[2])]; worker().run()"
        )

        async def consume(reader, writer):
            total = 0
            try:
                while block := await reader.read(16384):
                    total += len(block)
                    await asyncio.sleep(0.002)
                writer.write(f"accepted:{total}".encode())
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(consume, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        async def spawn(runtime):
            runtime.process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-S",
                "-c",
                script,
                str(SOURCE / "supervisor.py"),
                str(port),
                cwd=runtime.directory,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=module.MAX_FRAME,
            )

        async def kill(runtime):
            if runtime.process:
                runtime.process.stdin.close()
                await asyncio.wait_for(runtime.process.wait(), 5)

        with (
            patch.object(self.manager, "_spawn", spawn),
            patch.object(self.manager, "_kill_cgroup", kill),
        ):
            try:
                async with asyncio.timeout(5):
                    async with await self.execute(
                        argv=["/bin/sh", "-c", "sleep 60 & printf foreground; exit 23"]
                    ) as response:
                        frames = [
                            json.loads(line)
                            for line in (await response.read()).splitlines()
                        ]
                        self.assertEqual(frames[-1]["code"], 23)
                        self.assertEqual(
                            base64.b64decode(frames[0]["data"]), b"foreground"
                        )
                async with asyncio.timeout(10):
                    async with await self.execute(
                        argv=[
                            sys.executable,
                            "-I",
                            "-S",
                            "-c",
                            "import sys; sys.stdout.buffer.write(b'x' * 2097152)",
                        ]
                    ) as response:
                        frames = [
                            json.loads(line)
                            for line in (await response.read()).splitlines()
                        ]
                        self.assertEqual(frames[-1]["code"], 0)
                        self.assertEqual(
                            sum(
                                len(base64.b64decode(f["data"]))
                                for f in frames
                                if f["event"] == "data"
                            ),
                            2097152,
                        )
                self.assertEqual(len(self.manager.runtimes), 1)
                await self.eventually(lambda: bool(self.manager.list_previews()))
                runtime = next(iter(self.manager.runtimes.values()))
                await self.eventually(lambda: type(runtime.processes) is int)
                self.assertGreaterEqual(runtime.processes, 1)
                self.assertFalse(self.manager._idle(runtime))
                reader, writer = await asyncio.open_unix_connection(
                    self.manager.list_previews()[0]["socket_path"]
                )
                try:
                    async with asyncio.timeout(10):
                        writer.write(b"x" * 1048576)
                        await writer.drain()
                        writer.write_eof()
                        self.assertEqual(await reader.read(), b"accepted:1048576")
                    self.assertEqual(len(self.manager.runtimes), 1)
                finally:
                    writer.close()
                    await writer.wait_closed()
            finally:
                await self.manager.close()
                server.close()
                await server.wait_closed()

    def test_cgroup_limits_are_required_before_spawn(self):
        root = Path("/sys/fs/cgroup/service")
        pool = root / "workloads"
        writes = {}

        def write(path, value):
            self.assertNotIn(
                path, writes, "Shared limits must not be rewritten while workloads run"
            )
            writes[path] = value

        with (
            patch.object(module, "delegated_root", return_value=root),
            patch.object(Path, "read_text", return_value="cpu memory pids"),
            patch.object(Path, "write_text", write),
            patch.object(Path, "mkdir"),
            patch.object(
                Path, "resolve", autospec=True, side_effect=lambda path, **_: path
            ),
        ):
            group = self.manager._create_cgroup("a" * 24)
            sibling = self.manager._create_cgroup("b" * 24)
        self.assertEqual(group, pool / ("runtime-" + "a" * 24))
        self.assertEqual(sibling.parent, pool)
        self.assertEqual(
            writes,
            {
                root / "cgroup.subtree_control": "+cpu +memory +pids",
                pool / "memory.max": "1048576",
                pool / "memory.high": "max",
                pool / "memory.oom.group": "0",
                pool / "cpu.max": "max 100000",
                pool / "cgroup.subtree_control": "+cpu +memory +pids",
                group / "memory.oom.group": "1",
                group / "pids.max": "128",
                group / "cpu.max": "max 100000",
                group / "cpu.weight": "100",
                sibling / "memory.oom.group": "1",
                sibling / "pids.max": "128",
                sibling / "cpu.max": "max 100000",
                sibling / "cpu.weight": "100",
            },
        )
        with (
            patch.object(module, "delegated_root", return_value=root),
            patch.object(Path, "read_text", return_value="cpu memory"),
            self.assertRaises(RuntimeError),
        ):
            module.Manager(self.config)._create_cgroup("a" * 24)

    def test_cgroup_setup_failure_does_not_remove_shared_pool(self):
        root = Path("/sys/fs/cgroup/service")
        pool = root / "workloads"
        group = pool / ("runtime-" + "a" * 24)
        for failed in (
            pool / "memory.max",
            pool / "cgroup.subtree_control",
            group / "memory.oom.group",
            group / "cpu.weight",
        ):

            def write(path, value):
                if path == failed:
                    raise OSError("cgroup setup failed")

            with (
                self.subTest(failed=failed),
                patch.object(module, "delegated_root", return_value=root),
                patch.object(Path, "read_text", return_value="cpu memory pids"),
                patch.object(Path, "write_text", write),
                patch.object(Path, "mkdir", autospec=True) as mkdir,
                patch.object(Path, "rmdir", autospec=True) as rmdir,
                patch.object(
                    Path, "resolve", autospec=True, side_effect=lambda path, **_: path
                ),
                self.assertRaises(OSError),
            ):
                module.Manager(self.config)._create_cgroup("a" * 24)
            if failed.parent == pool:
                mkdir.assert_called_once_with(pool, exist_ok=True)
                rmdir.assert_not_called()
            else:
                rmdir.assert_called_once_with(group)

    def test_cgroup_pool_alias_rejected_before_setting_limits(self):
        root = Path("/sys/fs/cgroup/service")
        with (
            patch.object(module, "delegated_root", return_value=root),
            patch.object(Path, "read_text", return_value="cpu memory pids"),
            patch.object(Path, "mkdir"),
            patch.object(Path, "resolve", return_value=root / "manager"),
            patch.object(Path, "write_text", autospec=True) as write,
            self.assertRaises(RuntimeError),
        ):
            self.manager._create_cgroup("a" * 24)
        write.assert_called_once_with(
            root / "cgroup.subtree_control", "+cpu +memory +pids"
        )

    def test_launch_enters_cgroup_before_fixed_shell_command(self):
        root = Path("/sys/fs/cgroup/service")
        group = root / "workloads" / ("runtime-" + "a" * 24)
        steps = []

        def write(path, value):
            steps.append((path.name, value))

        def execute(path, argv):
            steps.append((path, argv))
            raise SystemExit

        argv = [
            "launch.py",
            "--cgroup",
            str(group),
            "--sandbox-exec",
            "/fixed/sandbox",
            "--python",
            "/fixed/python",
            "--supervisor",
            "/fixed/supervisor.py",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch.object(launch, "delegated_root", return_value=root),
            patch.object(Path, "resolve", return_value=group),
            patch.object(Path, "is_symlink", return_value=False),
            patch.object(Path, "write_text", write),
            patch.object(launch.os, "execv", execute),
            self.assertRaises(SystemExit),
        ):
            launch.main()
        self.assertEqual(
            steps,
            [
                ("cgroup.procs", str(os.getpid())),
                (
                    "/fixed/sandbox",
                    [
                        "/fixed/sandbox",
                        "-c",
                        "exec /fixed/python -I -S /fixed/supervisor.py",
                    ],
                ),
            ],
        )

    def test_launch_rejects_cgroups_outside_pool_and_aliases(self):
        root = Path("/sys/fs/cgroup/service")
        name = "runtime-" + "a" * 24
        valid = root / "workloads" / name
        for group, resolved, symlink in (
            (root / name, root / name, False),
            (root / "manager" / name, root / "manager" / name, False),
            (valid / name, valid / name, False),
            (valid, root / "other" / name, False),
            (valid, valid, True),
        ):
            with (
                self.subTest(group=group, resolved=resolved, symlink=symlink),
                patch.object(
                    sys,
                    "argv",
                    [
                        "launch.py",
                        "--cgroup",
                        str(group),
                        "--sandbox-exec",
                        "/fixed/sandbox",
                        "--python",
                        "/fixed/python",
                        "--supervisor",
                        "/fixed/supervisor.py",
                    ],
                ),
                patch.object(launch, "delegated_root", return_value=root),
                patch.object(Path, "resolve", return_value=resolved),
                patch.object(Path, "is_symlink", return_value=symlink),
                patch.object(Path, "write_text") as write,
                patch.object(launch.os, "execv") as execute,
                patch.object(sys, "stderr"),
                self.assertRaises(SystemExit),
            ):
                launch.main()
            write.assert_not_called()
            execute.assert_not_called()

    def test_client_fallback_is_configured_not_environment_selected(self):
        config = self.base / "config.json"
        config.write_text(json.dumps(self.config))
        with (
            patch.object(sys, "argv", ["client.py", str(config), "-c", "exit 23"]),
            patch.dict(os.environ, {"OPENCODE_SANDBOX_EXEC": "/attacker"}, clear=True),
            patch.object(client.os, "execv", side_effect=SystemExit) as execute,
        ):
            with self.assertRaises(SystemExit):
                client.main()
            execute.assert_called_once_with(
                "/fixed/sandbox", ["/fixed/sandbox", "-c", "exit 23"]
            )

    def test_client_rejects_present_invalid_session_without_fallback(self):
        config = self.base / "config.json"
        config.write_text(json.dumps(self.config))
        for session in ("", "invalid/session", "ses_", "ses_" + "x" * 129):
            with (
                patch.object(sys, "argv", ["client.py", str(config), "-c", "exit 23"]),
                patch.dict(os.environ, {"OPENCODE_SESSION_ID": session}),
                patch.object(client.os, "execv") as execute,
                patch.object(sys, "stderr"),
            ):
                self.assertEqual(client.main(), 77)
                execute.assert_not_called()

    def test_delegation_unavailable_rejected(self):
        with (
            patch.object(Path, "read_text", return_value="0::/not-delegated\n"),
            self.assertRaises(RuntimeError),
        ):
            launch.delegated_root()
        with patch.object(Path, "read_text", return_value="0::/service/manager\n"):
            self.assertEqual(launch.delegated_root(), Path("/sys/fs/cgroup/service"))


if __name__ == "__main__":
    unittest.main()
