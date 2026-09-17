"""Native MCP frontend against a real Unix HTTP manager and stdio pipes."""

import asyncio
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web

SOURCE = Path(__file__).resolve().parents[2] / "pkgs" / "opencode-preview"
sys.path.insert(0, str(SOURCE))
import mcp_frontend as module

CATALOG = {
    "tools": [
        {
            "name": "browser_navigate",
            "description": "Pinned upstream description",
            "inputSchema": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": False},
        }
    ]
}


class Writer:
    def __init__(self):
        self.messages = asyncio.Queue()
        self.blocked = False

    def write(self, data):
        self.messages.put_nowait(module.decode(data))

    async def drain(self):
        if self.blocked:
            await asyncio.Future()


class FrontendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"runtime_root": str(self.root)}
        self.requests = asyncio.Queue()
        self.disconnected = asyncio.Queue()
        app = web.Application(client_max_size=module.MAX_REQUEST * 2)

        async def manager(request):
            body = await request.json()
            await self.requests.put(body)
            arguments = body["request"]["params"]["arguments"]
            mode = arguments.get("url")
            if mode == "hang":
                try:
                    await asyncio.Future()
                finally:
                    self.disconnected.put_nowait(body["session_id"])
            if mode == "error":
                return web.json_response(
                    {
                        "error": {
                            "code": -32602,
                            "message": "upstream error",
                            "data": {"test": True},
                        }
                    }
                )
            if mode == "forbidden":
                return web.json_response(
                    {"error": {"code": -32000, "message": "denied"}}, status=403
                )
            if mode == "invalid":
                return web.Response(body=b"{bad json")
            if mode == "both":
                return web.json_response(
                    {"result": {}, "error": {"code": 1, "message": "no"}}
                )
            if mode == "bad-error":
                return web.json_response({"error": {"code": True, "message": "no"}})
            if mode == "bad-status":
                return web.json_response({"result": {}}, status=500)
            if mode == "overflow":
                return web.Response(body=b" " * (module.MAX_RESULT + 1))
            if mode == "image":
                return web.json_response(
                    {
                        "result": {
                            "content": [
                                {
                                    "type": "image",
                                    "data": "eA==" * 100000,
                                    "mimeType": "image/png",
                                }
                            ]
                        }
                    }
                )
            return web.json_response(
                {
                    "result": {
                        "content": [{"type": "text", "text": body["session_id"]}],
                        "arguments": arguments,
                    }
                }
            )

        app.router.add_post("/mcp", manager)
        self.runner = web.AppRunner(app, handler_cancellation=True)
        await self.runner.setup()
        await web.UnixSite(self.runner, str(self.root / "control.sock")).start()
        self.frontend = module.Frontend(self.config, copy.deepcopy(CATALOG))
        self.reader = asyncio.StreamReader(limit=module.MAX_REQUEST)
        self.writer = Writer()
        self.running = asyncio.create_task(self.frontend.run(self.reader, self.writer))

    async def asyncTearDown(self):
        self.reader.feed_eof()
        await asyncio.gather(self.running, return_exceptions=True)
        await self.runner.cleanup()

    def feed(self, method, params=None, id=1):
        message = {"jsonrpc": "2.0", "method": method}
        if id is not None:
            message["id"] = id
        if params is not None:
            message["params"] = params
        self.reader.feed_data(module.encode(message) + b"\n")

    async def response(self):
        return await asyncio.wait_for(self.writer.messages.get(), 3)

    async def initialize(self, version="2024-11-05"):
        self.feed(
            "initialize",
            {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
            id="init",
        )
        response = await self.response()
        self.assertEqual(response["result"]["capabilities"], {"tools": {}})
        self.feed("notifications/initialized", id=None)
        return response

    def call(self, id=1, session="ses_one", url="https://example.com", **arguments):
        self.feed(
            "tools/call",
            {
                "name": "browser_navigate",
                "arguments": {module.SESSION_FIELD: session, "url": url, **arguments},
            },
            id=id,
        )

    async def test_handshake_ping_catalog_without_upstream_launch(self):
        with patch.object(
            module.asyncio,
            "create_subprocess_exec",
            side_effect=AssertionError("runtime launch forbidden"),
        ):
            self.feed("tools/list")
            self.assertEqual((await self.response())["error"]["code"], -32000)
            response = await self.initialize()
            self.assertEqual(response["result"]["protocolVersion"], "2024-11-05")
            self.feed("tools/list", id=2)
            self.assertEqual((await self.response())["result"], CATALOG)
            self.assertNotIn(module.SESSION_FIELD, json.dumps(self.frontend.catalog))
            self.feed("ping", id=3)
            self.assertEqual(
                await self.response(), {"jsonrpc": "2.0", "id": 3, "result": {}}
            )
            self.feed("resources/list", id=4)
            self.assertEqual((await self.response())["error"]["code"], -32601)
            self.feed("tools/list", {"cursor": "unknown"}, id=5)
            self.assertEqual((await self.response())["error"]["code"], -32602)
        self.assertTrue(self.requests.empty())

    async def test_protocol_negotiation(self):
        for version in (*module.PROTOCOL_VERSIONS, "future-version"):
            self.frontend.initialized = False
            response = await self.initialize(version)
            self.assertEqual(
                response["result"]["protocolVersion"],
                version if version in module.PROTOCOL_VERSIONS else "2025-06-18",
            )

    async def test_session_routing_stripping_and_pinned_directory(self):
        await self.initialize()
        with patch.object(
            module.os, "getcwd", return_value="/model/cannot/change/cwd"
        ), patch.object(
            module.asyncio,
            "create_subprocess_exec",
            side_effect=AssertionError("runtime launch forbidden"),
        ):
            for index, session in enumerate(("ses_one", "ses_two"), 1):
                self.call(
                    id=index, session=session, code="arbitrary text is only proxied"
                )
                result = await self.response()
                body = await self.requests.get()
                self.assertEqual(
                    body,
                    {
                        "directory": self.frontend.directory,
                        "session_id": session,
                        "request": {
                            "method": "tools/call",
                            "params": {
                                "name": "browser_navigate",
                                "arguments": {
                                    "url": "https://example.com",
                                    "code": "arbitrary text is only proxied",
                                },
                            },
                        },
                    },
                )
                self.assertNotIn(module.SESSION_FIELD, result["result"]["arguments"])
        self.assertEqual(self.frontend.directory, os.getcwd())
        self.assertEqual(self.frontend.catalog, CATALOG)

    async def test_invalid_arguments_metadata_and_names_do_not_reach_manager(self):
        await self.initialize()
        good = {
            "name": "browser_navigate",
            "arguments": {module.SESSION_FIELD: "ses_one"},
        }
        invalid = [
            None,
            [],
            {},
            {**good, "name": []},
            {**good, "name": "browser_unknown"},
            {**good, "arguments": []},
            {**good, "arguments": {}},
            {**good, "directory": "/tmp"},
            {**good, "_meta": []},
            {**good, "arguments": {**good["arguments"], "directory": "/tmp"}},
        ]
        for session in (
            None,
            True,
            42,
            {},
            "",
            "ses_",
            "ses_bad/id",
            "ses_one\n",
            "ses_" + "a" * 125,
        ):
            invalid.append({**good, "arguments": {module.SESSION_FIELD: session}})
        for params in invalid:
            self.feed("tools/call", params)
            self.assertEqual((await self.response())["error"]["code"], -32602, params)
        self.assertTrue(self.requests.empty())

    async def test_malformed_json_and_ids_remain_safe(self):
        for line, code in (
            (b"{", -32700),
            (b"\xff", -32700),
            (b"[]", -32600),
            (b'{"jsonrpc":"2.0","method":"ping","id":true}', -32600),
            (b'{"jsonrpc":"2.0","method":"ping","id":null}', -32600),
            (b'{"jsonrpc":"2.0","method":"ping","id":[]}', -32600),
            (b'{"jsonrpc":"2.0","method":"ping","id":1.5}', -32600),
            (b'{"jsonrpc":"2.0","method":"ping","id":1,"id":2}', -32700),
            (b'{"jsonrpc":"2.0","method":"ping","id":NaN}', -32700),
            (b'{"jsonrpc":"2.0","method":"ping","id":1e999}', -32700),
            (b"[" * 34 + b"0" + b"]" * 34, -32700),
        ):
            self.reader.feed_data(line + b"\n")
            response = await self.response()
            self.assertIsNone(response["id"])
            self.assertEqual(response["error"]["code"], code)
        self.feed("ping", id="still-live")
        self.assertEqual((await self.response())["id"], "still-live")

    async def test_manager_errors_and_images(self):
        await self.initialize()
        for mode in (
            "error",
            "forbidden",
            "invalid",
            "both",
            "bad-error",
            "bad-status",
        ):
            self.call(url=mode)
            response = await self.response()
            self.assertIn("error", response)
            if mode == "error":
                self.assertEqual(
                    response["error"],
                    {
                        "code": -32602,
                        "message": "upstream error",
                        "data": {"test": True},
                    },
                )
            elif mode == "forbidden":
                self.assertEqual(response["error"]["message"], "denied")
            else:
                self.assertEqual(
                    response["error"]["message"], "MCP manager transport failed"
                )
        self.call(url="image")
        response = await self.response()
        self.assertEqual(response["result"]["content"][0]["data"], "eA==" * 100000)

    async def test_response_bound(self):
        await self.initialize()
        with patch.object(module, "MAX_RESULT", 4096):
            self.call(url="overflow")
            self.assertEqual((await self.response())["error"]["code"], -32000)
            await self.frontend.send(
                self.writer, "large", {"result": {"data": "x" * 4096}}
            )
            self.assertEqual(
                (await self.response())["error"]["message"], "MCP result limit exceeded"
            )

    async def test_request_and_unterminated_buffer_bound(self):
        self.reader.feed_data(b"x" * (module.MAX_REQUEST + 1))
        with self.assertRaises(ValueError):
            await asyncio.wait_for(self.running, 3)
        self.assertTrue(self.requests.empty())

    async def test_concurrency_busy_cancellation_and_eof_close_http(self):
        await self.initialize()
        for id in range(module.MAX_CONCURRENT):
            self.call(id=id, session=f"ses_{id}", url="hang")
        for _ in range(module.MAX_CONCURRENT):
            await asyncio.wait_for(self.requests.get(), 3)
        self.call(id="overflow")
        self.assertEqual(
            (await self.response())["error"]["message"], "MCP frontend is busy"
        )
        self.feed("ping", id="independent")
        self.assertEqual((await self.response())["id"], "independent")
        self.feed("notifications/cancelled", {"requestId": 0}, id=None)
        self.assertEqual(await asyncio.wait_for(self.disconnected.get(), 3), "ses_0")
        self.call(id="replacement", session="ses_fresh")
        self.assertEqual((await self.response())["id"], "replacement")
        self.reader.feed_eof()
        await asyncio.wait_for(self.running, 3)
        closed = {
            await asyncio.wait_for(self.disconnected.get(), 3)
            for _ in range(module.MAX_CONCURRENT - 1)
        }
        self.assertEqual(
            closed, {f"ses_{id}" for id in range(1, module.MAX_CONCURRENT)}
        )
        self.assertFalse(self.frontend.pending)
        self.assertTrue(self.writer.messages.empty())

    async def test_independent_calls_complete_out_of_order(self):
        await self.initialize()
        self.call(id=1, url="hang")
        await asyncio.wait_for(self.requests.get(), 3)
        self.call(id=2, session="ses_other")
        self.assertEqual((await self.response())["id"], 2)
        self.feed("notifications/cancelled", {"requestId": []}, id=None)
        self.feed("notifications/cancelled", {"requestId": "unknown"}, id=None)
        self.feed("notifications/cancelled", {"requestId": 1}, id=None)
        self.assertEqual(await asyncio.wait_for(self.disconnected.get(), 3), "ses_one")

    async def test_http_timeout_closes_request(self):
        await self.initialize()
        # The real client timeout is created when run starts.
        self.reader.feed_eof()
        await self.running
        self.reader = asyncio.StreamReader(limit=module.MAX_REQUEST)
        with patch.object(module, "HTTP_TIMEOUT", 0.05):
            self.running = asyncio.create_task(
                self.frontend.run(self.reader, self.writer)
            )
            self.call(url="hang")
            self.assertEqual((await self.response())["error"]["code"], -32000)
        self.assertEqual(await asyncio.wait_for(self.disconnected.get(), 3), "ses_one")

    async def test_duplicate_outstanding_id_closes_connection(self):
        await self.initialize()
        self.call(id=1, url="hang")
        await asyncio.wait_for(self.requests.get(), 3)
        self.feed("ping", id=1)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            await asyncio.wait_for(self.running, 3)
        self.assertEqual(await asyncio.wait_for(self.disconnected.get(), 3), "ses_one")

    async def test_output_backpressure_terminates_and_cancels(self):
        await self.initialize()
        self.call(url="hang")
        await asyncio.wait_for(self.requests.get(), 3)
        self.writer.blocked = True
        with patch.object(module, "OUTPUT_TIMEOUT", 0.05):
            self.call(id=2, session="ses_two")
            await asyncio.wait_for(self.running, 3)
        self.assertEqual(await asyncio.wait_for(self.disconnected.get(), 3), "ses_one")
        self.assertFalse(self.frontend.pending)

    async def test_real_stdio_cli(self):
        config = self.root / "config.json"
        tools = self.root / "tools.json"
        config.write_text(json.dumps(self.config))
        tools.write_text(json.dumps(CATALOG))
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            str(SOURCE / "mcp_frontend.py"),
            "serve",
            str(config),
            str(tools),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            for id, method, params in (
                (1, "initialize", {"protocolVersion": "2025-06-18"}),
                (2, "tools/list", {}),
                (
                    3,
                    "tools/call",
                    {
                        "name": "browser_navigate",
                        "arguments": {module.SESSION_FIELD: "ses_cli", "url": "hang"},
                    },
                ),
            ):
                process.stdin.write(
                    module.encode(
                        {"jsonrpc": "2.0", "id": id, "method": method, "params": params}
                    )
                    + b"\n"
                )
                await process.stdin.drain()
                if method != "tools/call":
                    response = module.decode(
                        await asyncio.wait_for(process.stdout.readline(), 3)
                    )
                    self.assertEqual(response["id"], id)
                    if method == "tools/list":
                        self.assertEqual(response["result"], CATALOG)
            await asyncio.wait_for(self.requests.get(), 3)
            process.stdin.close()
            await asyncio.wait_for(process.wait(), 3)
            self.assertEqual(await process.stderr.read(), b"")
            self.assertEqual(await process.stdout.read(), b"")
            self.assertEqual(process.returncode, 0)
            self.assertEqual(
                await asyncio.wait_for(self.disconnected.get(), 3), "ses_cli"
            )
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def executable(self, body):
        path = self.root / "fake-upstream"
        path.write_text(f"#!{sys.executable}\nimport json,sys,time,os\n" + body)
        path.chmod(0o700)
        return str(path)

    def upstream(self):
        return self.executable(
            "assert sys.argv[1:] == ['--headless', '--isolated']\n"
            "first = json.loads(input())\n"
            "assert first['method'] == 'initialize'\n"
            "assert first['params']['protocolVersion'] == '2024-11-05'\n"
            "print(json.dumps({'jsonrpc':'2.0','id':first['id'],'result':{'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'fake','version':'1'}}}), flush=True)\n"
            "assert json.loads(input()) == {'jsonrpc':'2.0','method':'notifications/initialized'}\n"
            "second = json.loads(input())\n"
            "assert second['method'] == 'tools/list'\n"
            "print(json.dumps({'jsonrpc':'2.0','method':'notifications/message','params':{'data':'not stdout output'}}), flush=True)\n"
            f"print(json.dumps({{'jsonrpc':'2.0','id':second['id'],'result':{CATALOG!r}}}), flush=True)\n"
            "assert not sys.stdin.readline(), 'unexpected upstream operation'\n"
        )

    async def test_catalog_preserves_tools_and_only_initializes_and_lists(self):
        catalog = await module.catalog(self.upstream())
        self.assertEqual(catalog["tools"][:-1], CATALOG["tools"])
        selection = catalog["tools"][-1]
        self.assertEqual(selection["name"], "browser_select")
        self.assertEqual(
            selection["inputSchema"]["properties"]["browser"]["enum"],
            ["chromium", "firefox", "webkit"],
        )
        frontend = module.Frontend({"runtime_root": str(self.root)}, catalog)
        self.assertIn("browser_select", frontend.names)

    async def test_catalog_cli_outputs_only_tools_list_result(self):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            str(SOURCE / "mcp_frontend.py"),
            "catalog",
            self.upstream(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stderr, b"")
        self.assertEqual(module.decode(stdout)["tools"][:-1], CATALOG["tools"])
        self.assertEqual(module.decode(stdout)["tools"][-1]["name"], "browser_select")
        self.assertEqual(len(stdout.splitlines()), 1)

    async def test_catalog_timeout_reaps_child(self):
        pidfile = self.root / "pid"
        executable = self.executable(
            f"open({str(pidfile)!r},'w').write(str(os.getpid()))\ntime.sleep(60)\n"
        )
        with (
            patch.object(module, "CATALOG_TIMEOUT", 0.2),
            self.assertRaises(TimeoutError),
        ):
            await module.catalog(executable)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)

    async def test_catalog_invalid_and_bounded_traffic(self):
        for body in (
            "print('invalid',flush=True)\n",
            "print(json.dumps({'jsonrpc':'2.0','id':True,'result':{}}),flush=True)\n",
            "print(json.dumps({'jsonrpc':'2.0','id':1,'result':{'protocolVersion':'bad'}}),flush=True)\n",
            "print('x'*5000,flush=True)\n",
            "sys.stdout.write('x'*1000000); sys.stdout.flush()\n",
            "for i in range(200): print(json.dumps({'jsonrpc':'2.0','method':'notifications/message'}),flush=True)\n",
        ):
            with (
                patch.object(module, "MAX_RESULT", 4096),
                self.assertRaises((ValueError, BrokenPipeError)),
            ):
                await asyncio.wait_for(module.catalog(self.executable(body)), 5)

    async def test_invalid_catalog_rejected_without_mutation(self):
        for catalog in (
            {},
            {"tools": {}},
            {**CATALOG, "nextCursor": "more"},
            {"tools": CATALOG["tools"] * 2},
            {"tools": [{"name": "x", "inputSchema": []}]},
        ):
            with self.assertRaises(ValueError):
                module.validate_catalog(catalog)
        original = copy.deepcopy(CATALOG)
        self.assertEqual(module.validate_catalog(CATALOG), {"browser_navigate"})
        self.assertEqual(CATALOG, original)


if __name__ == "__main__":
    unittest.main()
