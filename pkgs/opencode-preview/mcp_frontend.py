"""Native stdio MCP proxy; only the explicit build-time catalog mode spawns code."""

import asyncio
import contextlib
import json
import math
import os
import re
import signal
import sys
from pathlib import Path

import aiohttp

MAX_REQUEST = 128 * 1024
MAX_RESULT = 8 * 1024 * 1024
MAX_CONCURRENT = 8
HTTP_TIMEOUT = 130
OUTPUT_TIMEOUT = 5
CATALOG_TIMEOUT = 30
PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
SESSION_FIELD = "__opencode_session_id"
SESSION = re.compile(r"ses_[A-Za-z0-9]{1,124}\Z")


def encode(value):
    return json.dumps(
        value, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def decode(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs)
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 32:
            raise ValueError("JSON nesting limit exceeded")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("invalid JSON number")
    return value


def valid_id(value):
    return (isinstance(value, str) and len(value) <= 128) or (
        type(value) is int and abs(value) <= 2**53 - 1
    )


def error(code, message):
    return {"error": {"code": code, "message": message}}


def envelope(value):
    if not isinstance(value, dict) or set(value) not in ({"result"}, {"error"}):
        raise ValueError("invalid manager response")
    if "result" in value:
        if not isinstance(value["result"], dict):
            raise ValueError("invalid result")
    else:
        detail = value["error"]
        if (
            not isinstance(detail, dict)
            or type(detail.get("code")) is not int
            or not isinstance(detail.get("message"), str)
        ):
            raise ValueError("invalid error")
    return value


def validate_catalog(catalog):
    if (
        not isinstance(catalog, dict)
        or not isinstance(catalog.get("tools"), list)
        or "nextCursor" in catalog
        or len(encode(catalog)) > MAX_RESULT - 1024
    ):
        raise ValueError("invalid or incomplete tool catalog")
    names = set()
    for tool in catalog["tools"]:
        if (
            not isinstance(tool, dict)
            or not isinstance(tool.get("name"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tool["name"])
            or tool["name"] in names
            or not isinstance(tool.get("inputSchema"), dict)
        ):
            raise ValueError("invalid tool catalog entry")
        names.add(tool["name"])
    return names


class Frontend:
    def __init__(self, config, catalog):
        root = config["runtime_root"]
        if not isinstance(root, str) or not os.path.isabs(root) or "\0" in root:
            raise ValueError("invalid runtime_root")
        self.socket = str(Path(root) / "control.sock")
        self.catalog = catalog
        self.names = validate_catalog(catalog)
        self.directory = os.getcwd()
        self.initialized = False
        self.pending = {}
        self.output_lock = asyncio.Lock()

    async def send(self, writer, id, body):
        data = encode({"jsonrpc": "2.0", "id": id, **body}) + b"\n"
        if len(data) > MAX_RESULT:
            data = (
                encode(
                    {
                        "jsonrpc": "2.0",
                        "id": id,
                        **error(-32000, "MCP result limit exceeded"),
                    }
                )
                + b"\n"
            )
        # Bound both queued writers and a peer which stops consuming stdout.
        async with asyncio.timeout(OUTPUT_TIMEOUT):
            async with self.output_lock:
                writer.write(data)
                await writer.drain()

    async def call(self, http, params):
        if (
            not isinstance(params, dict)
            or not {"name", "arguments"} <= set(params)
            or set(params) - {"name", "arguments", "_meta"}
            or not isinstance(params["name"], str)
            or params["name"] not in self.names
            or not isinstance(params["arguments"], dict)
            or ("_meta" in params and not isinstance(params["_meta"], dict))
        ):
            return error(-32602, "Invalid tool name or arguments")
        arguments = dict(params["arguments"])
        session_id = arguments.pop(SESSION_FIELD, None)
        # This is routing metadata, not authentication. The managed plugin must
        # overwrite any model-supplied value after schema validation.
        if not isinstance(session_id, str) or not SESSION.fullmatch(session_id):
            return error(-32602, "Missing or invalid OpenCode session metadata")
        if "directory" in arguments:
            return error(-32602, "Directory is fixed by the MCP process")
        request = {
            "method": "tools/call",
            "params": {"name": params["name"], "arguments": arguments},
        }
        try:
            data = encode(request)
            if len(data) > MAX_REQUEST:
                return error(-32602, "MCP request limit exceeded")
            decode(data)
            async with http.post(
                "http://localhost/mcp",
                json={
                    "directory": self.directory,
                    "session_id": session_id,
                    "request": request,
                },
                allow_redirects=False,
            ) as response:
                data = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    data.extend(chunk)
                    if len(data) > MAX_RESULT:
                        raise ValueError("MCP result limit exceeded")
                result = envelope(decode(data))
                if response.status != 200 and "error" not in result:
                    raise ValueError("unexpected HTTP status")
                return result
        except (
            aiohttp.ClientError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
            RecursionError,
        ):
            return error(-32000, "MCP manager transport failed")

    async def accept(self, line, http, writer, stopped):
        try:
            message = decode(line)
        except (ValueError, UnicodeError, RecursionError):
            await self.send(writer, None, error(-32700, "Parse error"))
            return
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
            or set(message) - {"jsonrpc", "id", "method", "params"}
            or ("id" in message and not valid_id(message["id"]))
        ):
            await self.send(writer, None, error(-32600, "Invalid Request"))
            return
        method = message["method"]
        params = message.get("params", {})
        if "id" not in message:
            if method == "notifications/cancelled" and isinstance(params, dict):
                id = params.get("requestId")
                if valid_id(id) and id in self.pending:
                    self.pending[id].cancel()
            return
        id = message["id"]
        if id in self.pending:
            # A duplicate cannot receive an unambiguous response. Close the
            # connection rather than emitting two responses for the same ID.
            raise ValueError("duplicate outstanding request ID")
        if not isinstance(params, dict):
            result = error(-32602, "Invalid params")
        elif method == "initialize":
            if self.initialized or not isinstance(params.get("protocolVersion"), str):
                result = error(-32602, "Invalid initialize")
            else:
                version = params["protocolVersion"]
                self.initialized = True
                result = {
                    "result": {
                        "protocolVersion": (
                            version
                            if version in PROTOCOL_VERSIONS
                            else PROTOCOL_VERSIONS[-1]
                        ),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "opencode-preview", "version": "1"},
                    }
                }
        elif method == "ping":
            result = {"result": {}}
        elif not self.initialized:
            result = error(-32000, "MCP is not initialized")
        elif method == "tools/list":
            result = (
                error(-32602, "Invalid cursor")
                if params.get("cursor")
                else {"result": self.catalog}
            )
        elif method == "tools/call":
            if len(self.pending) >= MAX_CONCURRENT:
                result = error(-32000, "MCP frontend is busy")
            else:

                async def perform():
                    await self.send(writer, id, await self.call(http, params))

                def finished(task):
                    self.pending.pop(id, None)
                    if not task.cancelled() and task.exception() is not None:
                        stopped.set()

                task = asyncio.create_task(perform())
                self.pending[id] = task
                task.add_done_callback(finished)
                return
        else:
            result = error(-32601, "Method not found")
        await self.send(writer, id, result)

    async def run(self, reader, writer):
        stopped = asyncio.Event()
        connector = aiohttp.UnixConnector(
            path=self.socket, limit=MAX_CONCURRENT, force_close=True
        )
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
            auto_decompress=False,
            read_bufsize=16384,
        ) as http:

            async def consume():
                while True:
                    line = await reader.readline()
                    if not line:
                        return
                    if len(line) > MAX_REQUEST:
                        raise ValueError("MCP request limit exceeded")
                    if not line.endswith(b"\n"):
                        return
                    await self.accept(line, http, writer, stopped)

            consuming = asyncio.create_task(consume())
            stopping = asyncio.create_task(stopped.wait())
            try:
                await asyncio.wait(
                    (consuming, stopping), return_when=asyncio.FIRST_COMPLETED
                )
                if consuming.done():
                    consuming.result()
            finally:
                tasks = [consuming, stopping, *self.pending.values()]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self.pending.clear()


async def catalog(executable):
    """Build-time only: initialize and list tools without requesting any tool."""
    if not os.path.isabs(executable) or "\0" in executable:
        raise ValueError("catalog executable must be an absolute trusted path")
    process = await asyncio.create_subprocess_exec(
        executable,
        "--headless",
        "--isolated",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=MAX_RESULT,
        start_new_session=True,
    )
    received = 0

    async def exchange(id, method, params):
        nonlocal received
        process.stdin.write(
            encode({"jsonrpc": "2.0", "id": id, "method": method, "params": params})
            + b"\n"
        )
        await process.stdin.drain()
        while True:
            line = await process.stdout.readline()
            received += len(line)
            if not line or not line.endswith(b"\n") or received > MAX_RESULT:
                raise ValueError("invalid or oversized catalog traffic")
            message = decode(line)
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ValueError("invalid catalog response")
            if "id" not in message and isinstance(message.get("method"), str):
                continue
            if (
                type(message.get("id")) is not int
                or message["id"] != id
                or "method" in message
            ):
                raise ValueError("unexpected catalog response")
            result = envelope(
                {
                    key: value
                    for key, value in message.items()
                    if key not in {"jsonrpc", "id"}
                }
            )
            if "error" in result:
                raise ValueError("upstream catalog request failed")
            return result["result"]

    try:
        async with asyncio.timeout(CATALOG_TIMEOUT):
            initialized = await exchange(
                1,
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSIONS[0],
                    "capabilities": {},
                    "clientInfo": {"name": "opencode-preview-catalog", "version": "1"},
                },
            )
            if (
                initialized.get("protocolVersion") not in PROTOCOL_VERSIONS
                or not isinstance(initialized.get("capabilities"), dict)
                or not isinstance(initialized.get("serverInfo"), dict)
            ):
                raise ValueError("unsupported upstream protocol version")
            process.stdin.write(
                encode({"jsonrpc": "2.0", "method": "notifications/initialized"})
                + b"\n"
            )
            result = await exchange(2, "tools/list", {})
            validate_catalog(result)
            return result
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.stdin.close()
        # Drain after killing: wait() alone can hang if a full StreamReader
        # paused the stdout pipe before the child exited.
        async with asyncio.timeout(3):
            while await process.stdout.read(16384):
                pass
            await process.wait()


async def serve(config, tools):
    frontend = Frontend(config, tools)
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_REQUEST)
    input_transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    output_transport, protocol = await loop.connect_write_pipe(
        lambda: asyncio.streams.FlowControlMixin(loop=loop), sys.stdout.buffer
    )
    writer = asyncio.StreamWriter(output_transport, protocol, None, loop)
    try:
        await frontend.run(reader, writer)
    finally:
        input_transport.close()
        output_transport.abort()


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "catalog":
        result = asyncio.run(catalog(sys.argv[2]))
        sys.stdout.buffer.write(encode(result) + b"\n")
        return 0
    if len(sys.argv) == 4 and sys.argv[1] == "serve":
        config = decode(Path(sys.argv[2]).read_bytes())
        tools = decode(Path(sys.argv[3]).read_bytes())
        asyncio.run(serve(config, tools))
        return 0
    print(
        "usage: mcp_frontend.py catalog EXECUTABLE | serve CONFIG CATALOG",
        file=sys.stderr,
    )
    return 64


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
        TimeoutError,
        aiohttp.ClientError,
    ) as exc:
        print(f"preview MCP: {exc}", file=sys.stderr)
        sys.exit(70)
