"""Real stdio MCP and Unix HTTP transport, with sandbox entry explicitly faked."""

import asyncio
import base64
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import test_manager
from aiohttp import ClientSession, UnixConnector, web

module = test_manager.module
SOURCE = test_manager.SOURCE
import mcp_transport
import supervisor


class MCPTests(unittest.IsolatedAsyncioTestCase):
    workspace = test_manager.ManagerTests.workspace
    execute = test_manager.ManagerTests.execute
    runtime = test_manager.ManagerTests.runtime
    eventually = test_manager.ManagerTests.eventually
    asyncTearDown = test_manager.ManagerTests.asyncTearDown

    async def asyncSetUp(self):
        with patch.object(tempfile, "tempdir", "/tmp"):
            await test_manager.ManagerTests.asyncSetUp(self)
        self.expected_exit = 0
        launcher = self.base / "fake-mcp"
        launcher.write_text(
            "#!/bin/sh\nexec "
            + shlex.join(
                [
                    sys.executable,
                    "-B",
                    "-I",
                    "-S",
                    str(Path(__file__).with_name("fake_mcp.py")),
                ]
            )
            + "\n"
        )
        launcher.chmod(0o700)
        self.manager.config["playwright_mcp"] = str(launcher)
        # Only tests bypass native security attestation and host port discovery.
        script = """
import os, runpy, sys
from pathlib import Path
ns = runpy.run_path(sys.argv[1])
ns['Supervisor'].run.__globals__['listening_ports'] = lambda: []
# Scope heartbeat counts to this test worker, not unrelated host processes.
ns['Supervisor'].run.__globals__['workload_processes'] = lambda: len(
    Path(f'/proc/self/task/{os.getpid()}/children').read_text().split())
import mcp_transport
mcp_transport.MCP_TIMEOUT = 5
mcp_transport.MCP_STOP_TIMEOUT = 0.4
# Explicitly emulate the production session_tmp -> /tmp bind mount.
mcp_transport.MCP_TMP = Path(sys.argv[3])
try:
    ns['Supervisor'](sys.argv[2]).run()
except RuntimeError:
    sys.exit(70)
"""

        async def spawn(runtime):
            runtime.process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-B",
                "-I",
                "-S",
                "-c",
                script,
                str(SOURCE / "supervisor.py"),
                str(launcher),
                str(runtime.session_tmp),
                cwd=runtime.directory,
                env={
                    "PATH": os.defpath,
                    "MASKED_TEST": "allowed",
                    "OPENCODE_PREVIEW_RUNTIME_ID": runtime.runtime_id,
                },
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=module.MAX_FRAME,
            )
            self.manager.spawned.append(runtime)

        async def kill(runtime):
            if runtime.process:
                runtime.process.stdin.close()
                await asyncio.wait_for(runtime.process.wait(), 5)
                self.assertEqual(await runtime.process.stderr.read(), b"")
                self.assertEqual(runtime.process.returncode, self.expected_exit)
            self.manager.killed.append(runtime.runtime_id)

        self.manager._spawn = spawn
        self.manager._kill_cgroup = kill

    async def call(self, name="state", arguments=None, **changes):
        body = {
            "directory": str(self.root),
            "session_id": "ses_one",
            "request": {
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            },
        }
        body.update(changes)
        return await self.http.post("http://localhost/mcp", json=body)

    async def result(self, name="state", arguments=None, **changes):
        async with await self.call(name, arguments, **changes) as response:
            self.assertEqual(
                response.status,
                200,
                await response.text() if response.status != 200 else "",
            )
            self.assertEqual(response.content_type, "application/json")
            envelope = await response.json()
            self.assertEqual(set(envelope), {"result"})
            return envelope["result"]

    async def hanging(self):
        task = asyncio.create_task(self.call("hang"))
        await self.eventually(lambda: (self.root / "hanging").exists())
        return task, int((self.root / "hanging").read_text())

    @staticmethod
    def alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    async def test_handshake_state_errors_notifications_and_stderr(self):
        first = await self.result(arguments={"url": "http://localhost:3000"})
        self.assertEqual(first["count"], 1)
        for index, name in enumerate(("state", "notify", "stderr"), 2):
            current = await self.result(name, {"url": "https://example.com"})
            self.assertEqual(current["pid"], first["pid"])
            self.assertEqual(current["count"], index)
            self.assertEqual(current["arguments"], {"url": "https://example.com"})
        async with await self.call("error") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(
                await response.json(),
                {
                    "error": {
                        "code": -32602,
                        "message": "fake tool error",
                        "data": {"safe": True},
                    }
                },
            )
        fresh = await self.result()
        self.assertEqual(fresh["count"], 1)
        self.assertNotEqual(fresh["pid"], first["pid"])
        self.assertFalse(Path(first["home"]).exists())
        self.assertEqual(len(self.manager.spawned), 1)

    async def test_large_request_uses_nonblocking_stdin(self):
        arguments = {"code": "x" * 100000, "unicode": "\u00e9\U0001f642"}
        self.assertEqual(
            (await self.result(arguments=arguments))["arguments"], arguments
        )

    async def test_disconnect_without_handler_cancellation_resets_child(self):
        runner = web.AppRunner(
            self.manager.create_control_app(), handler_cancellation=False
        )
        await runner.setup()
        socket = self.base / "r" / "poll-disconnect.sock"
        await web.UnixSite(runner, str(socket)).start()
        original = self.http
        self.http = ClientSession(connector=UnixConnector(path=str(socket)))
        try:
            pending, pid = await self.hanging()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await self.eventually(lambda: not self.alive(pid))
            runtime = await self.runtime()
            await self.eventually(lambda: runtime.active_mcp is None)
            self.assertFalse(runtime.stopped)
            self.assertEqual((await self.result())["count"], 1)
        finally:
            await self.http.close()
            self.http = original
            await runner.cleanup()

    async def test_large_images_chunked_through_bounded_protocol(self):
        runtime = await self.runtime()
        decode = module.decode_data
        chunks = []

        def track(frame):
            data = decode(frame)
            if frame["event"] == "mcp_data":
                chunks.append(len(data))
            return data

        with patch.object(module, "decode_data", track):
            for blocks in (100000, (mcp_transport.MAX_MCP_RESULT - 1024) // 4):
                result = await self.result("image", {"blocks": blocks})
                self.assertEqual(result["content"][0]["data"], "eA==" * blocks)
        self.assertGreater(len(chunks), 16)
        self.assertTrue(all(0 < size <= 16384 for size in chunks))
        self.assertFalse(runtime.stopped)

    async def test_shell_slot_independent_and_mcp_busy(self):
        shell = await self.execute(
            argv=[
                sys.executable,
                "-c",
                "import time; print('ready',flush=True); time.sleep(60)",
            ]
        )
        await shell.content.readline()
        runtime = await self.runtime()
        shell_id = runtime.active_exec
        await self.result()
        pending, pid = await self.hanging()
        try:
            async with await self.call() as response:
                self.assertEqual(response.status, 409)
            self.assertEqual(runtime.active_exec, shell_id)
            shell.close()
            await self.eventually(lambda: runtime.active_exec is None)
            self.assertTrue(self.alive(pid))
            async with await self.execute(
                argv=[sys.executable, "-c", "print('independent')"]
            ) as response:
                frames = [
                    json.loads(line) for line in (await response.read()).splitlines()
                ]
                self.assertEqual(frames[-1]["code"], 0)
                self.assertEqual(base64.b64decode(frames[0]["data"]), b"independent\n")
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            shell.close()
        await self.eventually(lambda: not self.alive(pid))
        await self.eventually(lambda: runtime.active_mcp is None)
        self.assertFalse(runtime.stopped)
        self.assertEqual((await self.result())["count"], 1)

    async def test_timeout_resets_browser_not_shell_runtime(self):
        shell = await self.execute(
            argv=[
                sys.executable,
                "-c",
                "import time; print('ready',flush=True); time.sleep(60)",
            ]
        )
        self.addCleanup(shell.close)
        await shell.content.readline()
        runtime = await self.runtime()
        shell_id = runtime.active_exec
        with patch.object(module, "MCP_TIMEOUT", 0.3):
            pending, pid = await self.hanging()
            async with await pending as response:
                self.assertEqual(response.status, 504)
                self.assertEqual(
                    (await response.json())["error"]["message"], "MCP request timed out"
                )
        await self.eventually(lambda: not self.alive(pid))
        await self.eventually(lambda: runtime.active_mcp is None)
        self.assertFalse(runtime.stopped)
        self.assertEqual(runtime.active_exec, shell_id)
        self.assertEqual((await self.result())["count"], 1)

    async def test_child_failures_only_reset_browser(self):
        runtime = await self.runtime()
        for name in (
            "bad_json",
            "bad_id",
            "bad_result",
            "both",
            "nan",
            "deep",
            "overflow",
            "exit",
        ):
            with self.subTest(name=name):
                old = await self.result()
                async with await self.call(name) as response:
                    self.assertEqual(response.status, 502)
                    self.assertEqual(
                        await response.json(),
                        {"error": {"code": -32000, "message": "MCP transport failed"}},
                    )
                fresh = await self.result()
                await self.eventually(lambda pid=old["pid"]: not self.alive(pid))
                self.assertNotEqual(fresh["pid"], old["pid"])
                self.assertEqual(fresh["count"], 1)
                self.assertFalse(runtime.stopped)
        self.assertEqual(len(self.manager.spawned), 1)

    async def test_bad_initialize_and_missing_executable_fail_closed(self):
        marker = self.root / "bad-initialize"
        marker.touch()
        async with await self.call() as response:
            self.assertEqual(response.status, 502)
        runtime = await self.runtime()
        homes = runtime.session_tmp / mcp_transport.home_directory(runtime.runtime_id)
        await self.eventually(lambda: not list(homes.iterdir()))
        marker.unlink()
        await self.result()
        runtime = await self.runtime()
        await self.manager.stop(runtime.runtime_id)
        Path(self.manager.config["playwright_mcp"]).unlink()
        async with await self.call() as response:
            self.assertEqual(response.status, 502)
        self.assertFalse(self.manager.spawned[-1].stopped)
        runtime = self.manager.spawned[-1]
        homes = runtime.session_tmp / mcp_transport.home_directory(runtime.runtime_id)
        await self.eventually(lambda: not list(homes.iterdir()))

    async def test_stop_reaps_child_and_fails_pending(self):
        first = await self.result()
        pending, pid = await self.hanging()
        runtime = await self.runtime()
        await self.manager.stop(runtime.runtime_id)
        async with await pending as response:
            self.assertEqual(response.status, 502)
        self.assertFalse(self.alive(pid))
        self.assertNotIn(runtime.runtime_id, self.manager.runtimes)
        self.assertFalse(Path(first["home"]).parent.exists())

    async def test_browser_close_delivers_then_cleans_and_allows_idle_eviction(self):
        first = await self.result()
        home = Path(first["home"])
        runtime = await self.runtime()
        unrelated = runtime.session_tmp / "keep-session-data"
        unrelated.write_text("keep")
        self.assertEqual(
            home.parent,
            runtime.session_tmp / mcp_transport.home_directory(runtime.runtime_id),
        )
        self.assertEqual(home.stat().st_mode & 0o777, 0o700)
        self.assertTrue((home / "private-config").exists())
        self.assertFalse(self.manager._idle(runtime))
        closed = await self.result("browser_close")
        self.assertEqual(closed["pid"], first["pid"])
        self.assertEqual(closed["count"], 2)
        await self.eventually(lambda: not home.exists())
        self.assertFalse(self.alive(first["pid"]))
        await self.eventually(lambda: self.manager._idle(runtime))
        runtime.idle_since -= self.manager.config["idle_timeout_seconds"]
        await self.eventually(lambda: runtime.runtime_id not in self.manager.runtimes)
        self.assertFalse(home.parent.exists())
        self.assertEqual(unrelated.read_text(), "keep")

    async def test_tab_close_preserves_other_tabs_but_last_close_retires(self):
        first = await self.result()
        runtime = await self.runtime()
        home = Path(first["home"])
        await self.result("browser_tabs", {"action": "new"})
        await self.result("browser_tabs", {"action": "new"})
        closed = await self.result("browser_tabs", {"action": "close", "index": 1})
        self.assertEqual(
            closed,
            {
                "content": [
                    {"type": "text", "text": "### Result\n- 0: [tab-2](about:blank)"}
                ]
            },
        )
        self.assertEqual((await self.result())["pid"], first["pid"])
        self.assertEqual(await self.result("browser_tabs", {"action": "list"}), closed)
        self.assertTrue(home.exists())
        self.assertFalse(self.manager._idle(runtime))
        closed = await self.result("browser_tabs", {"action": "close"})
        self.assertEqual(
            closed,
            {
                "content": [
                    {
                        "type": "text",
                        "text": "### Result\nNo open tabs. Navigate to a URL to create one.",
                    }
                ]
            },
        )
        await self.eventually(lambda: not home.exists())
        self.assertFalse(self.alive(first["pid"]))
        await self.eventually(lambda: self.manager._idle(runtime))
        fresh = await self.result()
        self.assertNotEqual(fresh["pid"], first["pid"])
        self.assertEqual(fresh["count"], 1)

    async def test_tool_errors_retire_and_preserve_result(self):
        first = await self.result()
        result = await self.result("tool_error")
        self.assertEqual(
            result,
            {
                "isError": True,
                "content": [{"type": "text", "text": "fake tool failure"}],
            },
        )
        fresh = await self.result()
        self.assertEqual(fresh["count"], 1)
        self.assertNotEqual(first["pid"], fresh["pid"])
        self.assertFalse(Path(first["home"]).exists())

    async def test_signaled_waiter_stops_runtime_before_host_home_cleanup(self):
        first = await self.result()
        runtime = await self.runtime()
        home = Path(first["home"])
        entered, release = asyncio.Event(), asyncio.Event()
        kill = self.manager._kill_cgroup

        async def delayed_kill(runtime):
            entered.set()
            await release.wait()
            await kill(runtime)

        self.expected_exit = 70
        self.manager._kill_cgroup = delayed_kill
        try:
            os.kill(first["pid"], signal.SIGKILL)
            await asyncio.wait_for(entered.wait(), 3)
            self.assertTrue(home.exists())
            self.assertTrue(runtime.stopped)
            async with await self.call() as response:
                self.assertEqual(response.status, 409)
            self.assertEqual(len(self.manager.spawned), 1)
        finally:
            release.set()
        await self.eventually(lambda: runtime.runtime_id not in self.manager.runtimes)
        self.assertFalse(home.parent.exists())

    async def test_failed_cgroup_teardown_does_not_clean_home(self):
        first = await self.result()
        runtime = await self.runtime()
        with (
            patch.object(
                self.manager, "_kill_cgroup", side_effect=PermissionError("not empty")
            ),
            self.assertRaises(PermissionError),
        ):
            await self.manager.stop(runtime.runtime_id)
        self.assertTrue(Path(first["home"]).exists())
        self.assertIn(runtime.runtime_id, self.manager.runtimes)
        await self.manager.stop(runtime.runtime_id)
        self.assertFalse(Path(first["home"]).exists())

    async def test_real_namespace_reaps_detached_descendant_before_cleanup(self):
        unshare = shutil.which("unshare")
        if unshare is None:
            self.skipTest("unshare unavailable")
        flags = [
            unshare,
            "--user",
            "--map-current-user",
            "--pid",
            "--fork",
            "--kill-child=SIGKILL",
            "--mount-proc",
            "--",
        ]
        probe = await asyncio.create_subprocess_exec(
            *flags,
            sys.executable,
            "-I",
            "-S",
            "-c",
            "pass",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if await asyncio.wait_for(probe.wait(), 5):
            self.skipTest("nested user/PID namespaces unavailable")
        launcher = Path(self.manager.config["playwright_mcp"])
        launcher.write_text(
            "#!/bin/sh\nexport FAKE_MCP_PARENT=$$\nexec "
            + shlex.join(
                [
                    *flags,
                    sys.executable,
                    "-B",
                    "-I",
                    "-S",
                    str(Path(__file__).with_name("fake_mcp.py")),
                ]
            )
            + "\n"
        )
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                first = await self.result("detached")
                parent = first["pid"]
                init = int(
                    Path(f"/proc/{parent}/task/{parent}/children").read_text().strip()
                )
                descendant = int(
                    Path(f"/proc/{init}/task/{init}/children").read_text().strip()
                )
                self.assertNotEqual(os.getpgid(descendant), os.getpgid(parent))
                self.assertTrue(self.alive(descendant))
                if cancel:
                    pending, _ = await self.hanging()
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                    runtime = await self.runtime()
                    await self.eventually(
                        lambda runtime=runtime: runtime.active_mcp is None
                    )
                else:
                    await self.result("browser_close")
                fresh = await self.result()
                self.assertEqual(fresh["count"], 1)
                self.assertFalse(self.alive(init))
                self.assertFalse(self.alive(descendant))
                self.assertFalse(Path(first["home"]).exists())

    async def test_ownership_and_resource_policy(self):
        first = await self.result()
        async with await self.call(session_id="ses_other") as response:
            self.assertEqual(response.status, 409)
        alias = self.workspaces / "alias"
        alias.symlink_to(self.root)
        escape = self.root / "escape"
        escape.symlink_to(self.base)
        for directory in (self.base, alias, escape):
            async with await self.call(directory=str(directory)) as response:
                self.assertEqual(response.status, 403)
        sub = self.root / "sub"
        sub.mkdir()
        self.assertEqual((await self.result(directory=str(sub)))["pid"], first["pid"])
        other = self.workspace("other")
        self.manager.config["max_runtimes"] = 1
        async with await self.call(
            directory=str(other), session_id="ses_two"
        ) as response:
            self.assertEqual(response.status, 503)
        runtime = await self.runtime()
        self.manager.config["max_connections"] = 1
        id, _ = runtime.channel("tcp")
        async with await self.call() as response:
            self.assertEqual(response.status, 503)
        runtime.channels.pop(id)
        self.manager.config["max_runtimes"] = 2
        second = await self.result(directory=str(other), session_id="ses_two")
        self.assertNotEqual(first["pid"], second["pid"])
        self.assertEqual(second["count"], 1)

    async def test_bad_inputs_before_allocating_runtime(self):
        valid = {"method": "tools/call", "params": {"name": "state", "arguments": {}}}
        invalid = [
            None,
            [],
            {},
            {**valid, "id": 3},
            {**valid, "jsonrpc": "2.0"},
            {**valid, "method": "initialize"},
            {**valid, "method": "tools/list"},
            {**valid, "method": []},
            {**valid, "params": []},
            {**valid, "params": {"name": "state"}},
            {**valid, "params": {"name": "../command", "arguments": {}}},
            {**valid, "params": {"name": "x" * 129, "arguments": {}}},
            {**valid, "params": {"name": "state", "arguments": []}},
            {**valid, "params": {"name": "state", "arguments": {}, "env": {}}},
            {**valid, "params": {"name": "state", "arguments": {"x": float("nan")}}},
            {
                **valid,
                "params": {"name": "state", "arguments": {"x": "a" * (128 * 1024)}},
            },
        ]
        nested = {}
        for _ in range(33):
            nested = {"x": nested}
        invalid.append({**valid, "params": {"name": "state", "arguments": nested}})
        for request in invalid:
            async with await self.call(request=request) as response:
                self.assertEqual(response.status, 400, request)
                self.assertEqual(response.content_type, "application/json")
                self.assertEqual(set(await response.json()), {"error"})
        for changes in (
            {"env": {}},
            {"argv": ["/bin/sh"]},
            {"session_id": "ses_bad/id"},
            {"directory": "relative"},
        ):
            async with await self.call(**changes) as response:
                self.assertEqual(response.status, 400)
        async with await self.call(arguments={"x": "a" * module.MAX_FRAME}) as response:
            self.assertEqual(response.status, 413)
        async with self.http.post("http://localhost/mcp", data=b"{invalid") as response:
            self.assertEqual(response.status, 400)
        self.assertFalse(self.manager.spawned)
        del self.manager.config["playwright_mcp"]
        async with await self.call() as response:
            self.assertEqual(response.status, 503)
        self.assertFalse(self.manager.spawned)

    async def test_child_only_has_stdio_and_masked_environment(self):
        result = await self.result("fds")
        self.assertEqual(set(result["fds"]), {"0", "1", "2"})
        self.assertEqual(result["fds"]["2"], "/dev/null")
        self.assertEqual(result["env"]["MASKED_TEST"], "allowed")
        self.assertLessEqual(
            set(result["env"]),
            {
                "PATH",
                "MASKED_TEST",
                "LC_CTYPE",
                "PWD",
                "SHLVL",
                "PYTHONNOUSERSITE",
                "OPENCODE_PREVIEW_RUNTIME_ID",
                "OPENCODE_PLAYWRIGHT_HOME",
            },
        )
        self.assertEqual(result["cwd"], str(self.root))

    async def test_manager_checks_result_limit_even_if_supervisor_misbehaves(self):
        runtime = await self.runtime()
        with patch.object(module, "MAX_MCP_RESULT", 1024):
            async with await self.call("image", {"blocks": 20000}) as response:
                self.assertEqual(response.status, 502)
        self.assertFalse(runtime.stopped)
        await asyncio.sleep(0.1)
        self.assertEqual((await self.result())["count"], 1)

    async def test_trusted_launcher_config_and_spawn_arguments(self):
        for value in (None, [], "relative", "/bad\0path"):
            with self.assertRaises(ValueError):
                module.Manager({**self.config, "playwright_mcp": value})
        runtime = await self.runtime()
        process = runtime.process
        with (
            patch.object(
                self.manager, "_create_cgroup", return_value=Path("/fixed/group")
            ),
            patch.object(
                module.asyncio,
                "create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ) as spawn,
        ):
            await module.Manager._spawn(self.manager, runtime)
        self.assertEqual(
            spawn.call_args.args[-2:],
            ("--playwright-mcp", self.manager.config["playwright_mcp"]),
        )


class MCPUnitTests(unittest.TestCase):
    def test_last_tab_detection_requires_exact_response_and_close_action(self):
        no_tabs = "### Result\nNo open tabs. Navigate to a URL to create one."
        content = [{"type": "text", "text": no_tabs}]
        cases = [
            ("browser_tabs", {"action": "close"}, content, True),
            ("browser_tabs", {"action": "close", "index": 0}, content, True),
            ("browser_tabs", {"action": "list"}, content, False),
            ("browser_tabs", {"action": "new"}, content, False),
            ("browser_tabs", {"action": "select", "index": 0}, content, False),
            ("browser_evaluate", {}, content, False),
            ("browser_tabs", {"action": "close"}, [], False),
            (
                "browser_tabs",
                {"action": "close"},
                [
                    {
                        "type": "text",
                        "text": "### Result\n- 0: ["
                        + no_tabs
                        + "](https://example.com/)",
                    }
                ],
                False,
            ),
            (
                "browser_tabs",
                {"action": "close"},
                [
                    {
                        "type": "text",
                        "text": no_tabs
                        + "\n### Open tabs\n- 0: [still open](about:blank)",
                    }
                ],
                False,
            ),
            (
                "browser_tabs",
                {"action": "close"},
                content + [{"type": "text", "text": "another tab"}],
                False,
            ),
        ]
        worker = supervisor.Supervisor()
        self.addCleanup(worker.selector.close)
        child = worker.mcp
        for name, arguments, response, retire in cases:
            with self.subTest(name=name, arguments=arguments, response=response):
                child.id = child.rpc_id = "a"
                child.phase = "call"
                child.request = {
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }
                child.message(
                    json.dumps(
                        {"jsonrpc": "2.0", "id": "a", "result": {"content": response}}
                    )
                )
                self.assertEqual(child.retire_after_result, retire)
                self.assertEqual(
                    json.loads(child.result), {"result": {"content": response}}
                )

    def test_reaping_gates_cleanup_and_replacement(self):
        for code in (None, -signal.SIGKILL, -signal.SIGTERM, 0, 1):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                worker = supervisor.Supervisor("/fixed/mcp")
                self.addCleanup(worker.selector.close)
                child = worker.mcp
                home = Path(temporary) / "browser"
                home.mkdir()
                (home / "private-config").write_text("secret")
                child.home = home
                child.home_fd = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
                child.retired = Mock()
                child.retired.poll.return_value = code
                child.retire_deadline = time.monotonic() + 2
                child.id = "a"
                child.deadline = float("inf")
                with patch.object(child, "start") as start:
                    try:
                        if code is None:
                            child.refresh(False)
                            self.assertTrue(home.exists())
                            start.assert_not_called()
                            child.retire_deadline = 0
                            with self.assertRaisesRegex(RuntimeError, "timed out"):
                                child.refresh(False)
                        elif code < 0:
                            with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
                                child.refresh(False)
                        else:
                            child.refresh(False)
                            self.assertFalse(home.exists())
                            start.assert_called_once()
                        if code is None or code < 0:
                            start.assert_not_called()
                            self.assertTrue(home.exists())
                    finally:
                        if child.home_fd is not None:
                            os.close(child.home_fd)

    def test_private_home_cleanup_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "browser"
            home.mkdir()
            keep = root / "keep"
            keep.mkdir()
            (keep / "file").write_text("untouched")
            (home / "escape").symlink_to(keep)
            worker = supervisor.Supervisor()
            self.addCleanup(worker.selector.close)
            child = worker.mcp
            child.home = home
            child.home_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            child.reap()
            self.assertFalse(home.exists())
            self.assertEqual((keep / "file").read_text(), "untouched")

    def test_cleanup_failure_waits_for_completed_response_frame(self):
        worker = supervisor.Supervisor()
        self.addCleanup(worker.selector.close)
        child = worker.mcp
        child.retired = Mock()
        child.retired.poll.return_value = 0
        child.retire_barrier = 100
        child.retire_deadline = float("inf")
        child.home = Path("/unused/browser")
        child.home_fd = 123
        with patch.object(
            mcp_transport.shutil, "rmtree", side_effect=PermissionError
        ) as remove:
            child.reap()
            remove.assert_not_called()
            worker.written = 100
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                child.reap()
            remove.assert_called_once()

    def test_browser_close_emits_result_before_retirement(self):
        worker = supervisor.Supervisor()
        self.addCleanup(worker.selector.close)
        child = worker.mcp
        child.process = Mock()
        child.process.poll.return_value = None
        child.id = child.last_id = "a"
        child.deadline = float("inf")
        child.result = b'{"result":{}}'
        child.retire_after_result = True
        calls = Mock()
        calls.emit.return_value = 0
        with (
            patch.object(worker, "emit", calls.emit),
            patch.object(worker, "signal_group", calls.signal_group),
            patch.object(worker, "watch"),
        ):
            child.refresh(True)
        self.assertEqual(
            [call[0] for call in calls.mock_calls], ["emit", "emit", "signal_group"]
        )
        self.assertEqual(calls.emit.call_args.args, ("mcp_end", "a"))
        self.assertIsNone(child.id)
        self.assertIsNotNone(child.retired)

    def test_launch_quotes_and_passes_only_configured_mcp_path(self):
        launch = test_manager.launch
        root = Path("/sys/fs/cgroup/service")
        group = root / "workloads" / ("runtime-" + "a" * 24)
        executable = "/fixed/path with space/mcp;not-a-command"
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
            "--playwright-mcp",
            executable,
        ]
        with (
            patch.object(sys, "argv", argv),
            patch.object(launch, "delegated_root", return_value=root),
            patch.object(Path, "resolve", return_value=group),
            patch.object(Path, "is_symlink", return_value=False),
            patch.object(Path, "write_text") as write,
            patch.object(launch.os, "execv") as execute,
        ):
            launch.main()
        write.assert_called_once_with(str(os.getpid()))
        path, command = execute.call_args.args
        self.assertEqual(path, "/fixed/sandbox")
        self.assertEqual(command[:2], ["/fixed/sandbox", "-c"])
        self.assertEqual(
            shlex.split(command[2]),
            [
                "exec",
                "/fixed/python",
                "-I",
                "-S",
                "/fixed/supervisor.py",
                "--playwright-mcp",
                executable,
            ],
        )

    def test_supervisor_cancel_timeout_and_backpressure(self):
        worker = supervisor.Supervisor("/fixed/mcp")
        self.addCleanup(worker.selector.close)
        child = worker.mcp
        process = Mock()
        process.poll.return_value = None
        child.process = process
        child.id = child.last_id = "a"
        child.phase = "result"
        child.result = b'{"result":{}}'
        child.deadline = float("inf")
        with patch.object(worker, "watch"), patch.object(worker, "emit") as emit:
            child.refresh(False)
            emit.assert_not_called()
            child.refresh(True)
            self.assertEqual(
                [call.args[0] for call in emit.call_args_list], ["mcp_data", "mcp_end"]
            )
        child.id = child.last_id = "b"
        child.deadline = 0
        with (
            patch.object(worker, "watch"),
            patch.object(worker, "emit") as emit,
            patch.object(worker, "signal_group") as kill,
        ):
            child.cancel("a")
            kill.assert_not_called()
            child.refresh(True)
            kill.assert_called_once_with(process.pid, supervisor.signal.SIGTERM)
            emit.assert_called_once_with("error", "b", error="MCP request timed out")
            self.assertIsNone(child.process)
            process.stdin.close.assert_called_once()
            process.stdout.close.assert_called_once()
            process.poll.return_value = 0
            child.refresh(True)
            self.assertFalse(child.retired)


if __name__ == "__main__":
    unittest.main()
