"""VM-only upstream and probes; the manager, sandbox and gateway are all real."""

import asyncio
import base64
import errno
import http.client
import json
import mmap
import os
import re
import shlex
import signal
import socket
import stat
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit

PUBLIC = "opencode.example.com"
BASIC = "Basic " + base64.b64encode(b"opencode:fakepassword").decode()
COOKIE = "__Host-opencode-preview"
RUNTIME = Path("/run/opencode-previews")


def denied(operation, allowed=(errno.EACCES, errno.EPERM, errno.ENOENT)):
    try:
        operation()
    except OSError as error:
        assert error.errno in allowed, error
    else:
        raise AssertionError("Forbidden operation succeeded")


def allocate(memory_max):
    # Host-controlled gates let the test set OOM scores before any pressure.
    # Keep both allocation and waiting bounded even if the host assertion fails.
    signal.alarm(90)
    prefix = Path("pool-allocator")
    prefix.with_suffix(".ready").touch()
    chunks = []
    chunk_size = 8 * 1024 * 1024
    for gate, target in (
        ("hold", memory_max // 2 + 64 * 1024 * 1024),
        ("oom", memory_max * 2),
    ):
        while not prefix.with_suffix("." + gate).exists():
            time.sleep(0.05)
        while len(chunks) * chunk_size < target:
            chunk = mmap.mmap(
                -1, chunk_size, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS
            )
            # Reserving address space alone cannot exercise memory.max.
            chunk[:: mmap.PAGESIZE] = b"x" * (chunk_size // mmap.PAGESIZE)
            chunks.append(chunk)
            time.sleep(0.01)
        prefix.with_suffix("." + gate + "-filled").write_text(
            str(len(chunks) * chunk_size)
        )
    raise AssertionError("Allocator exceeded twice the pool cap without being killed")


def probe(manifest_path):
    manifest = json.loads(Path(manifest_path).read_text())
    status = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
    )
    for key in ("CapEff", "CapPrm", "CapAmb", "CapInh"):
        assert int(status[key], 16) == 0, (key, status[key])
    assert int(status["NoNewPrivs"]) == 1
    assert int(status["Seccomp"]) == 2
    assert status["NSpid"].split() == [str(os.getpid())]

    # Direct argv execution makes the trusted supervisor our immediate parent.
    parent = Path(f"/proc/{os.getppid()}")
    assert parent.exists()
    for fd in (0, 1):
        denied(
            lambda fd=fd: (parent / f"fd/{fd}").readlink(), (errno.EACCES, errno.EPERM)
        )
        denied(
            lambda fd=fd: os.open(parent / f"fd/{fd}", os.O_WRONLY | os.O_NONBLOCK),
            (errno.EACCES, errno.EPERM),
        )

    for name in manifest["hidden"]:
        denied(lambda name=name: Path(name).read_bytes())
    for name in manifest["sockets"]:
        denied(lambda name=name: Path(name).stat())

        # Even a disclosed pathname must not become a control-plane channel.
        def connect(name=name):
            with socket.socket(socket.AF_UNIX) as client:
                client.connect(name)

        denied(connect)

    bundle = Path(os.environ["SSL_CERT_FILE"])
    assert bundle.is_relative_to("/var/tmp"), bundle
    assert b"BEGIN CERTIFICATE" in bundle.read_bytes()
    assert b"PRIVATE KEY" not in bundle.read_bytes()
    denied(lambda: bundle.open("ab"), (errno.EROFS, errno.EACCES, errno.EPERM))
    assert os.environ["GH_TOKEN"] and os.environ["GH_TOKEN"] != "fakegh"
    assert "fakegh" not in json.dumps(dict(os.environ))
    assert "fakepassword" not in json.dumps(dict(os.environ))
    assert "OPENCODE_SERVER_PASSWORD" not in os.environ
    assert os.statvfs("/sys/fs/cgroup").f_flag & os.ST_RDONLY
    # Check the actual delegated child, not just an unrelated cgroup mount.
    group = Path(manifest["cgroup"])
    assert group.is_dir(), group
    denied(
        lambda: os.open(group / "cgroup.procs", os.O_WRONLY),
        (errno.EROFS, errno.EACCES, errno.EPERM),
    )
    print("sandbox invariants verified")


async def serve():
    from aiohttp import web

    async def handle(request):
        if request.path == "/health":
            # Native, headerless requests must reach upstream, not a Basic gate.
            return web.Response(text="fake OpenCode health")
        if request.path == "/pty/test/connect":
            if request.query.get("ticket") != "fake-ticket+/=":
                return web.Response(status=401, text="invalid ticket")
            if "Authorization" in request.headers:
                return web.Response(status=400, text="ticket unexpectedly needs Basic")
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_str("ticket accepted")
            async for message in ws:
                await ws.send_str(message.data)
            return ws
        if request.headers.get("Authorization") != BASIC:
            return web.Response(
                status=401,
                text="fake upstream rejected credentials",
                headers={"X-Preview-Fixture": "upstream"},
            )
        return web.json_response(
            {
                "path": request.raw_path,
                "host": request.host,
                "proto": request.headers.get("X-Forwarded-Proto"),
            }
        )

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 4096).start()
    # A live private destination makes proxy-policy failures distinguishable
    # from merely failing to connect to a nonexistent upstream.
    await web.TCPSite(runner, "127.0.0.2", 4096).start()
    await asyncio.Event().wait()


def request(host=PUBLIC, path="/previews", headers=None, method="GET", body=None):
    # The VM acts as the TLS tunnel: test the production loopback HTTP listener
    # with the public Host, without external DNS, certificates or Cloudflare.
    with closing(
        http.client.HTTPConnection("127.0.0.1", 4080, timeout=10)
    ) as connection:
        connection.request(
            method, path, body=body, headers={"Host": host, **(headers or {})}
        )
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode()


def eventually(check, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return check()
        except (AssertionError, OSError, subprocess.TimeoutExpired):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def directory():
    code, headers, body = request(headers={"Authorization": BASIC})
    assert code == 200, (code, body)
    assert headers["Cache-Control"] == "no-store"
    entries = {}
    for article in re.findall(r"<article>(.*?)</article>", body):
        link = re.search(
            r'href="(https://[^\"]+/__preview_login\?token=[^\"]+)"', article
        )[1]
        parsed = urlsplit(link)
        entries[parsed.hostname] = {
            "login": parsed.path + "?" + parsed.query,
            "runtime": re.search(r'action="/previews/stop/([a-f0-9]+)"', article)[1],
            "csrf": re.search(r'name="csrf" value="([^\"]+)"', article)[1],
        }
    assert all(
        not host.endswith(("-1080.example.com", "-3128.example.com"))
        for host in entries
    )
    return entries


def login(host, entry):
    code, headers, _ = request(host, entry["login"])
    assert code == 303, code
    assert headers["Location"] == "/"
    assert headers["Cache-Control"] == "no-store"
    assert headers["Referrer-Policy"] == "no-referrer"
    cookies = SimpleCookie(headers["Set-Cookie"])
    cookie = cookies[COOKIE]
    assert cookie["secure"] and cookie["httponly"]
    assert cookie["samesite"].lower() == "lax"
    assert cookie["path"] == "/" and not cookie["domain"]
    return {"Cookie": f"{COOKIE}={cookie.value}"}


async def websocket():
    from aiohttp import ClientSession, WSServerHandshakeError

    async with ClientSession() as session:
        url = "http://127.0.0.1:4080/pty/test/connect?ticket=fake-ticket%2B%2F%3D"
        async with session.ws_connect(url, headers={"Host": PUBLIC}, timeout=10) as ws:
            assert (await ws.receive(timeout=10)).data == "ticket accepted"
            await ws.send_str("native websocket round trip")
            assert (await ws.receive(timeout=10)).data == "native websocket round trip"
        try:
            await session.ws_connect(url + "bad", headers={"Host": PUBLIC})
        except WSServerHandshakeError as error:
            assert error.status == 401, error.status
        else:
            raise AssertionError("Invalid WebSocket ticket accepted")


def integration(wrapper, config_path, task):
    config = json.loads(Path(config_path).read_text())
    root = Path(config["workspaces_root"])
    assert config["public_host"] == PUBLIC and config["preview_domain"] == "example.com"
    assert config["runtime_root"] == str(RUNTIME)
    assert Path(config["playwright_mcp"]).is_file()
    assert "cpu_quota" not in config
    assert 0 < config["memory_max"] <= 1024 * 1024 * 1024
    assert config["max_execs"] == 2

    def client(workspace, session, *args, expected=0):
        result = subprocess.run(
            [wrapper, *args],
            cwd=root / workspace,
            user="test-bot",
            group="agent-workspaces",
            extra_groups=[],
            env={
                **os.environ,
                "HOME": "/home/test-bot",
                "OPENCODE_SESSION_ID": session,
            },
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == expected, (
            result.returncode,
            result.stdout,
            result.stderr,
        )
        return result.stdout

    def shell(workspace, session, command, **kwargs):
        return client(workspace, session, "-c", command, **kwargs)

    @contextmanager
    def held(name, code=0):
        # A real foreground shell, released only by a host-controlled gate.
        program = (
            "import os, signal, sys, time; from pathlib import Path; "
            f"signal.alarm(90); p = Path('parallel-{name}'); "
            "p.with_suffix('.ready').write_text(str(os.getpid()))\n"
            "while not p.with_suffix('.release').exists(): time.sleep(0.05)\n"
            f"print('{name}-out'); print('{name}-err', file=sys.stderr); sys.exit({code})"
        )
        process = subprocess.Popen(
            [wrapper, "-c", "exec python3 -c " + shlex.quote(program)],
            cwd=root / "alpha",
            user="test-bot",
            group="agent-workspaces",
            extra_groups=[],
            env={
                **os.environ,
                "HOME": "/home/test-bot",
                "OPENCODE_SESSION_ID": "ses_alpha",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            eventually(lambda: present(root / f"alpha/parallel-{name}.ready"))
            assert process.poll() is None
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)

    def mcp(workspace, session, name, arguments, expected=200, tool_error=False):
        # Exercise the private manager API, not a separately launched test MCP.
        async def call():
            from aiohttp import ClientSession, ClientTimeout, UnixConnector

            async with (
                ClientSession(
                    connector=UnixConnector(path=str(control)),
                    timeout=ClientTimeout(total=150),
                ) as connection,
                connection.post(
                    "http://localhost/mcp",
                    json={
                        "directory": str(root / workspace),
                        "session_id": session,
                        "request": {
                            "method": "tools/call",
                            "params": {"name": name, "arguments": arguments},
                        },
                    },
                ) as response,
            ):
                body = await response.text()
                assert response.status == expected, (response.status, body)
                return json.loads(body)

        envelope = asyncio.run(call())
        if expected != 200:
            assert set(envelope) == {"error"}, envelope
            assert isinstance(envelope["error"]["code"], int), envelope
            assert envelope["error"]["message"], envelope
            return envelope
        assert set(envelope) == {"result"}, envelope
        result = envelope["result"]
        assert bool(result.get("isError")) == tool_error, result
        return result

    async def native_mcp():
        process = await asyncio.create_subprocess_exec(
            str(Path(wrapper).with_name("opencode-session-mcp")),
            cwd=root / "alpha",
            user="test-bot",
            group="agent-workspaces",
            extra_groups=[],
            env={
                **os.environ,
                "HOME": "/home/test-bot",
                # Routing must use the trusted per-call metadata, not this env.
                "OPENCODE_SESSION_ID": "ses_other",
            },
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=8 * 1024 * 1024,
        )
        sequence = 0

        async def rpc(method, params, error=None):
            nonlocal sequence
            sequence += 1
            process.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": sequence,
                        "method": method,
                        "params": params,
                    }
                ).encode()
                + b"\n"
            )
            await process.stdin.drain()
            line = await asyncio.wait_for(process.stdout.readline(), 150)
            assert line, (process.returncode, await process.stderr.read())
            response = json.loads(line)
            assert response["jsonrpc"] == "2.0" and response["id"] == sequence, response
            if error is not None:
                assert response["error"]["code"] == error, response
                return response["error"]
            assert "error" not in response, response
            assert not response["result"].get("isError"), response
            return response["result"]

        try:
            initialized = await rpc(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "preview-vm", "version": "1"},
                },
            )
            assert initialized["protocolVersion"] == "2024-11-05", initialized
            process.stdin.write(
                b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
            )
            tools = await rpc("tools/list", {})
            assert {"browser_evaluate", "browser_take_screenshot"} <= {
                tool["name"] for tool in tools["tools"]
            }, tools
            params = {
                "name": "browser_evaluate",
                "arguments": {
                    "function": "() => document.body.innerText + ':' + ++window.previewCalls"
                },
            }
            await rpc("tools/call", params, error=-32602)
            params["arguments"]["__opencode_session_id"] = "ses_other"
            await rpc("tools/call", params, error=-32000)
            params["arguments"]["__opencode_session_id"] = "ses_alpha"
            result = await rpc("tools/call", params)
            assert "alpha source:3" in json.dumps(result), result
            screenshot = await rpc(
                "tools/call",
                {
                    "name": "browser_take_screenshot",
                    "arguments": {"type": "png", "__opencode_session_id": "ses_alpha"},
                },
            )
            process.stdin.close()
            assert await asyncio.wait_for(process.wait(), 15) == 0
            assert await process.stderr.read() == b""
            return screenshot
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    def start(workspace, session, marker, port=3000):
        # Ordinary shell backgrounding, no registration or test-only launcher.
        shell(
            workspace,
            session,
            f"printf %s {shlex.quote(marker)} > index.html; "
            f"python3 -m http.server {port} --bind 127.0.0.1 > /tmp/http-{port}.log 2>&1 & "
            f"echo $! > /tmp/http-{port}.pid",
        )

    def ready(host):
        entries = directory()
        assert host in entries, entries
        return entries[host]

    def content(host, cookie, marker):
        code, _, body = request(host, "/", cookie)
        assert (code, body) == (200, marker), (code, body)

    def gone(host):
        assert host not in directory()
        assert request(host, "/")[0] == 404

    def pool_limits():
        assert pool.is_dir(), pool
        assert {path for path in parent.iterdir() if path.is_dir()} == {manager, pool}
        assert not (parent / "cgroup.procs").read_text().strip()
        assert not (pool / "cgroup.procs").read_text().strip()
        for group in (parent, pool):
            assert {"cpu", "memory", "pids"} <= set(
                (group / "cgroup.subtree_control").read_text().split()
            ), group
        for name, value in (
            ("memory.max", str(config["memory_max"])),
            ("memory.high", "max"),
            ("memory.oom.group", "0"),
            ("cpu.max", "max 100000"),
        ):
            assert (pool / name).read_text().strip() == value, (pool, name)

    def snapshot(entry, browser=False):
        pool_limits()
        group = pool / ("runtime-" + entry["runtime"])
        assert group.is_dir(), group
        for name, value in (
            ("memory.max", "max"),
            ("memory.high", "max"),
            ("memory.oom.group", "1"),
            ("pids.max", str(config["tasks_max"])),
            ("cpu.max", "max 100000"),
            ("cpu.weight", "100"),
        ):
            assert (group / name).read_text().strip() == value, name
        pids = {
            int(pid)
            for file in group.rglob("cgroup.procs")
            for pid in file.read_text().split()
        }
        assert any(
            b"http.server" in Path(f"/proc/{pid}/cmdline").read_bytes() for pid in pids
        )
        if browser:
            browsers = {
                pid
                for pid in pids
                if b"chrome-headless-shell" in Path(f"/proc/{pid}/cmdline").read_bytes()
            }
            assert browsers, ("No real Chromium in runtime cgroup", group, pids)
            for pid in browsers:
                for namespace in ("mnt", "net", "pid"):
                    assert (
                        Path(f"/proc/{pid}/ns/{namespace}").readlink()
                        != Path(f"/proc/self/ns/{namespace}").readlink()
                    ), (pid, namespace)
        return group, pids

    def cleaned(snapshot, service_restart=False):
        group, pids = snapshot
        # systemd may rebuild the service cgroup tree on restart. Ordinary
        # runtime teardown, including OOM, must leave the live pool intact.
        if not service_restart:
            pool_limits()
        assert group.parent == pool and pids, snapshot
        assert not group.exists(), group
        assert all(not Path(f"/proc/{pid}").exists() for pid in pids), pids
        runtime_id = group.name.removeprefix("runtime-")
        assert not (RUNTIME / "broker" / runtime_id).exists()
        assert not list((RUNTIME / "u").glob(runtime_id + "-*.sock"))

    print("Checking real systemd delegation and native OpenCode forwarding", flush=True)
    control = RUNTIME / "control.sock"
    assert stat.S_ISSOCK(control.stat().st_mode)
    assert stat.S_IMODE(control.stat().st_mode) == 0o600
    assert stat.S_IMODE(RUNTIME.stat().st_mode) == 0o700
    pid = subprocess.check_output(
        ["systemctl", "show", "-p", "MainPID", "--value", "opencode-previews"],
        text=True,
    ).strip()
    cgroup = Path(f"/proc/{pid}/cgroup").read_text().strip().removeprefix("0::")
    manager = Path("/sys/fs/cgroup") / cgroup.lstrip("/")
    assert manager.name == "manager", manager
    parent = manager.parent
    pool = parent / "workloads"
    assert (
        subprocess.check_output(
            ["systemctl", "show", "-p", "OOMPolicy", "--value", "opencode-previews"],
            text=True,
        ).strip()
        == "continue"
    )
    assert {"cpu", "memory", "pids"} <= set(
        (parent / "cgroup.controllers").read_text().split()
    )
    assert request()[0] == 401
    assert request(headers={"Authorization": "Basic invalid"})[0] == 401
    assert request(path="/health")[2] == "fake OpenCode health"
    code, headers, _ = request(path="/session")
    assert code == 401 and headers["X-Preview-Fixture"] == "upstream"
    code, _, body = request(
        path="/session?directory=%2Fsrv%2Ftest", headers={"Authorization": BASIC}
    )
    assert code == 200
    assert json.loads(body) == {
        "path": "/session?directory=%2Fsrv%2Ftest",
        "host": PUBLIC,
        "proto": "https",
    }
    asyncio.run(websocket())

    print("Checking persistent sessions, discovery and same-port isolation", flush=True)
    a, b = (
        "preview-alpha-3000.example.com",
        "preview-beta-3000.example.com",
    )
    start("alpha", "ses_alpha", "alpha source")
    start("beta", "ses_beta", "beta source")
    entry_a, entry_b = eventually(lambda: ready(a)), eventually(lambda: ready(b))
    pool_limits()
    cookie_a, cookie_b = login(a, entry_a), login(b, entry_b)
    content(a, cookie_a, "alpha source")
    content(b, cookie_b, "beta source")
    assert request(a, "/")[0] == 403
    assert request(a, "/", {"Authorization": BASIC})[0] == 403
    assert request(a, "/__preview_login?token=invalid")[0] == 403
    assert request(b, "/", cookie_a)[0] == 403
    assert request(a, "/", cookie_b)[0] == 403
    for workspace in ("alpha", "beta"):
        assert (
            shell(
                workspace,
                "ses_" + workspace,
                "curl --noproxy '*' -fsS http://127.0.0.1:3000/",
            )
            == workspace + " source"
        )
        shell(workspace, "ses_" + workspace, "echo persistent > /tmp/persist")
        assert (
            shell(workspace, "ses_" + workspace, "cat /tmp/persist").strip()
            == "persistent"
        )
    # A second session cannot share a workspace's existing runtime (HTTP 409).
    shell("alpha", "ses_other", "touch must-not-run", expected=77)
    assert not (root / "alpha/must-not-run").exists()
    result = subprocess.run(
        [
            "curl",
            "--silent",
            "--unix-socket",
            str(control),
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps(
                {
                    "directory": str(root / "alpha"),
                    "session_id": "ses_other",
                    "argv": ["true"],
                }
            ),
            "http://localhost/exec",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "409", result.stdout
    shell("alpha", "ses_alpha", "exit 23", expected=23)
    assert (
        client("alpha", "ses_alpha", "--", "printf", "%s", "argv passthrough")
        == "argv passthrough"
    )
    for port in (3000, 3001):
        result = subprocess.run(
            [
                "curl",
                "--noproxy",
                "*",
                "--silent",
                "--max-time",
                "2",
                f"http://127.0.0.1:{port}/",
            ],
            capture_output=True,
            check=False,
        )
        assert result.returncode == 7, result
    start("alpha", "ses_alpha", "alpha source", 3001)
    eventually(lambda: ready("preview-alpha-3001.example.com"))
    assert (
        shell("alpha", "ses_alpha", "curl --noproxy '*' -fsS http://127.0.0.1:3001/")
        == "alpha source"
    )
    shell(
        "beta", "ses_beta", "curl --noproxy '*' -fsS http://127.0.0.1:3001/", expected=7
    )
    assert set(directory()) == {a, b, "preview-alpha-3001.example.com"}

    print("Checking real session MCP browser, inline PNG and ownership", flush=True)
    browser_homes = {}
    for workspace in ("alpha", "beta"):
        result = mcp(
            workspace,
            "ses_" + workspace,
            "browser_navigate",
            {"url": "http://localhost:3000/"},
        )
        text = "\n".join(
            item["text"] for item in result["content"] if item["type"] == "text"
        )
        link = re.search(r"\[Snapshot\]\(([^)]+)\)", text)
        assert link, result
        assert workspace + " source" in shell(
            workspace, "ses_" + workspace, "cat -- " + shlex.quote(link[1])
        )
        browser_homes[workspace] = Path(
            shell(
                workspace, "ses_" + workspace, "realpath -- " + shlex.quote(link[1])
            ).strip()
        ).parent.parent
        result = mcp(
            workspace,
            "ses_" + workspace,
            "browser_evaluate",
            {"function": "() => document.body.innerText"},
        )
        assert workspace + " source" in json.dumps(result), result
        mcp(
            workspace,
            "ses_" + workspace,
            "browser_evaluate",
            {
                "function": "() => { "
                "if (localStorage.getItem('preview-owner') !== null) "
                "throw new Error('Browser storage leaked between sessions'); "
                "localStorage.setItem('preview-owner', document.body.innerText); "
                "window.previewCalls = 0; return document.body.innerText; }"
            },
        )
    for count in (1, 2):
        result = mcp(
            "alpha",
            "ses_alpha",
            "browser_evaluate",
            {"function": "() => document.body.innerText + ':' + ++window.previewCalls"},
        )
        assert f"alpha source:{count}" in json.dumps(result), result
        assert shell("alpha", "ses_alpha", "cat /tmp/persist").strip() == "persistent"
    mcp(
        "alpha",
        "ses_other",
        "browser_evaluate",
        {
            "function": "() => { window.previewCalls = 999; return document.body.innerText; }"
        },
        expected=409,
    )
    result = mcp(
        "alpha",
        "ses_alpha",
        "browser_evaluate",
        {"function": "() => document.body.innerText + ':' + window.previewCalls"},
    )
    assert "alpha source:2" in json.dumps(result), result
    print(
        "Checking packaged native MCP stdio frontend and session metadata", flush=True
    )
    screenshot = asyncio.run(native_mcp())
    images = [item for item in screenshot["content"] if item["type"] == "image"]
    assert images, screenshot
    assert images[0]["mimeType"] == "image/png", images[0]["mimeType"]
    png = base64.b64decode(images[0]["data"], validate=True)
    assert len(png) > 100 and png[:16] == b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    assert int.from_bytes(png[16:20], "big") > 0
    assert int.from_bytes(png[20:24], "big") > 0
    result = mcp(
        "alpha",
        "ses_alpha",
        "browser_evaluate",
        {"function": "() => document.body.innerText + ':' + window.previewCalls"},
    )
    assert "alpha source:3" in json.dumps(result), result

    print(
        "Checking browser private egress is blocked without external access", flush=True
    )
    with closing(
        http.client.HTTPConnection("127.0.0.2", 4096, timeout=10)
    ) as connection:
        connection.request("GET", "/health")
        response = connection.getresponse()
        assert response.status == 200 and response.read() == b"fake OpenCode health"
    # Also make an incorrect Chromium loopback bypass succeed, so it cannot
    # masquerade as a successful policy rejection through connection refusal.
    shell(
        "alpha",
        "ses_alpha",
        "printf %s private-browser-target > health; "
        "python3 -m http.server 4096 --bind 127.0.0.2 > /tmp/private-http.log 2>&1 & "
        "echo $! > /tmp/private-http.pid",
    )

    def private_ready():
        assert (
            shell(
                "alpha",
                "ses_alpha",
                "curl --noproxy '*' -fsS http://127.0.0.2:4096/health",
            )
            == "private-browser-target"
        )

    eventually(private_ready)
    mcp(
        "alpha",
        "ses_alpha",
        "browser_run_code",
        {
            "code": "async (page) => { "
            "const response = await page.goto('http://127.0.0.2:4096/health', "
            "{ timeout: 15000 }); "
            # Pinned SRT maps the public-unicast policy exception to HTTP 500.
            "if (response?.status() !== 500 || "
            "await page.locator('body').innerText() !== 'Internal Server Error') "
            "throw new Error('Private destination was not rejected by the proxy: ' + "
            "JSON.stringify({status: response?.status(), body: await page.locator('body').innerText()})); "
            "return response.status(); }"
        },
    )
    shell("alpha", "ses_alpha", "kill $(cat /tmp/private-http.pid); rm health")
    mcp("alpha", "ses_alpha", "browser_navigate", {"url": "http://localhost:3000/"})
    result = mcp(
        "alpha",
        "ses_alpha",
        "browser_evaluate",
        {
            "function": "() => document.body.innerText + ':' + localStorage.getItem('preview-owner')"
        },
    )
    assert "alpha source:alpha source" in json.dumps(result), result
    content(a, cookie_a, "alpha source")

    print(
        "Checking tool errors retire only the browser and its private home", flush=True
    )
    home = browser_homes["alpha"]
    browser_pids = {
        pid
        for pid in snapshot(entry_a, browser=True)[1]
        if b"HOME=" + str(home).encode() + b"\0"
        in Path(f"/proc/{pid}/environ").read_bytes()
    }
    assert browser_pids, home
    result = mcp(
        "alpha",
        "ses_alpha",
        "browser_evaluate",
        {
            "function": "() => { throw new Error('intentional browser lifecycle failure'); }"
        },
        tool_error=True,
    )
    assert "intentional browser lifecycle failure" in json.dumps(result), result

    def browser_retired():
        assert all(not Path(f"/proc/{pid}").exists() for pid in browser_pids), (
            browser_pids
        )
        shell("alpha", "ses_alpha", "test ! -e " + shlex.quote(str(home)))

    eventually(browser_retired)
    assert shell("alpha", "ses_alpha", "cat /tmp/persist").strip() == "persistent"
    assert ready(a)["runtime"] == entry_a["runtime"]
    mcp("alpha", "ses_alpha", "browser_navigate", {"url": "http://localhost:3000/"})
    mcp(
        "alpha",
        "ses_alpha",
        "browser_evaluate",
        {
            "function": "() => { "
            "if (localStorage.getItem('preview-owner') !== null) "
            "throw new Error('Failed browser retained storage'); "
            "localStorage.setItem('preview-owner', document.body.innerText); "
            "return document.body.innerText; }"
        },
    )

    print(
        "Checking disclosed control paths, credentials, procfs and cgroups", flush=True
    )
    snap_a, snap_b = snapshot(entry_a, browser=True), snapshot(entry_b, browser=True)
    sockets = [str(path) for path in RUNTIME.rglob("*") if path.is_socket()]
    assert str(control) in sockets and len(sockets) >= 4, sockets
    for workspace, entry in (("alpha", entry_a), ("beta", entry_b)):
        broker = RUNTIME / "broker" / entry["runtime"]
        keys = list(broker.rglob("ca.key"))
        assert keys, broker
        hidden = [str(path) for path in keys]
        hidden += [str(Path("/var/tmp") / path.relative_to(broker)) for path in keys]
        hidden += [str(broker / "settings.json")]
        manifest = root / workspace / "probe.json"
        manifest.write_text(
            json.dumps(
                {
                    "hidden": hidden,
                    "sockets": sockets,
                    "cgroup": str(pool / ("runtime-" + entry["runtime"])),
                }
            )
        )
        client(
            workspace,
            "ses_" + workspace,
            "--",
            "python3",
            __file__,
            "probe",
            str(manifest),
        )

    print("Checking app restart and cancellation preserve the session", flush=True)
    shell("alpha", "ses_alpha", "kill $(cat /tmp/http-3000.pid)")
    eventually(lambda: absent(a))
    assert request(a, "/", cookie_a)[0] == 503
    start("alpha", "ses_alpha", "alpha restarted")
    eventually(lambda: ready(a))
    assert ready(a)["login"] == entry_a["login"]
    assert login(a, entry_a) == cookie_a
    content(a, cookie_a, "alpha restarted")
    print(
        "Checking configured parallel shell slots and independent results", flush=True
    )
    with held("first", 17) as first, held("second", 23) as second:
        assert first.poll() is None and second.poll() is None
        commands = (
            Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            for pid in snapshot(entry_a)[1]
        )
        supervisor = next(
            argv
            for argv in commands
            if argv[1:4] == [b"-I", b"-S", config["supervisor"].encode()]
        )
        assert supervisor[supervisor.index(b"--max-execs") + 1] == b"2"
        shell("alpha", "ses_alpha", "touch parallel-rejected", expected=77)
        assert not (root / "alpha/parallel-rejected").exists()
        result = mcp(
            "alpha",
            "ses_alpha",
            "browser_evaluate",
            {"function": "() => document.body.innerText"},
        )
        assert "alpha source" in json.dumps(result), result
        assert ready(a) == entry_a and login(a, entry_a) == cookie_a
        content(a, cookie_a, "alpha restarted")
        for name, process, code in (("first", first, 17), ("second", second, 23)):
            (root / f"alpha/parallel-{name}.release").touch()
            output = process.communicate(timeout=10)
            assert (process.returncode, *output) == (
                code,
                name + "-out\n",
                name + "-err\n",
            )
            if process is first:
                assert second.poll() is None
        assert not (root / "alpha/parallel-rejected").exists()

    with held("cancel") as child, held("survivor") as survivor:
        child.send_signal(signal.SIGTERM)
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 143, (child.returncode, stdout, stderr)
        # Client exit alone is not proof that the actual workload was reaped.
        eventually(
            lambda: shell(
                "alpha",
                "ses_alpha",
                "! kill -0 $(cat parallel-cancel.ready) 2>/dev/null",
            )
        )
        assert survivor.poll() is None
        with held("replacement") as replacement:
            assert survivor.poll() is None and replacement.poll() is None
            assert ready(a) == entry_a and login(a, entry_a) == cookie_a
            content(a, cookie_a, "alpha restarted")
            print(
                "Checking scoped stop kills multiple foreground calls and background processes",
                flush=True,
            )
            snap_a = snapshot(entry_a, browser=True)
            client(
                "alpha",
                "ses_alpha",
                "stop",
                "--session",
                "ses_alpha",
                "--directory",
                str(root / "beta"),
            )
            content(a, cookie_a, "alpha restarted")
            content(b, cookie_b, "beta source")
            assert survivor.poll() is None and replacement.poll() is None
            client(
                "alpha",
                "ses_alpha",
                "stop",
                "--session",
                "ses_alpha",
                "--directory",
                str(root / "alpha"),
            )
            for process in (survivor, replacement):
                output = process.communicate(timeout=10)
                assert process.returncode != 0, output
            eventually(lambda: cleaned(snap_a))
    gone(a)
    gone("preview-alpha-3001.example.com")
    content(b, cookie_b, "beta source")
    assert shell("beta", "ses_beta", "printf unrelated-exec") == "unrelated-exec"
    # Rename away the workspace root while leaving its open cwd and files alive.
    (root / "beta").rename(root / "beta-deleted")
    eventually(lambda: gone(b))
    eventually(lambda: cleaned(snap_b))
    (root / "beta-deleted").rename(root / "beta")

    print("Checking long task hostnames and authenticated directory stop", flush=True)
    start(task, "ses_task", "long task source", 65535)

    def task_ready():
        entries = directory()
        assert len(entries) == 1, entries
        return next(iter(entries.items()))

    task_host, task_entry = eventually(task_ready)
    assert len(task_host.split(".")[0]) <= 63 and task_host.endswith(
        "-65535.example.com"
    ), task_host
    task_cookie = login(task_host, task_entry)
    content(task_host, task_cookie, "long task source")
    task_snapshot = snapshot(task_entry)
    stop_path = "/previews/stop/" + task_entry["runtime"]
    stop_headers = {
        "Authorization": BASIC,
        "Origin": "https://" + PUBLIC,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    assert (
        request(
            path=stop_path, method="POST", headers=stop_headers, body="csrf=invalid"
        )[0]
        == 403
    )
    assert (
        request(
            path=stop_path,
            method="POST",
            headers=stop_headers,
            body="csrf=" + task_entry["csrf"],
        )[0]
        == 303
    )
    eventually(lambda: cleaned(task_snapshot))
    gone(task_host)

    print("Checking shared pool borrowing and isolated group OOM", flush=True)
    empty()
    pool_limits()
    assert not list(pool.glob("runtime-*"))
    # Anonymous memory must hit the pool limit, not escape into VM swap.
    assert len(Path("/proc/swaps").read_text().splitlines()) == 1
    start("alpha", "ses_poolvictim", "pool victim")
    start("beta", "ses_poolsibling", "pool survivor")
    victim, sibling = eventually(lambda: ready(a)), eventually(lambda: ready(b))
    sibling_cookie = login(b, sibling)
    sibling_snapshot = snapshot(sibling)
    pool_inode = pool.stat().st_ino

    def process_identity(pid):
        # Start time rules out a recycled PID; comm can contain spaces or ')'.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]

    manager_identity = process_identity(pid)
    sibling_identities = {
        child: process_identity(child) for child in sibling_snapshot[1]
    }

    def survivor_healthy():
        assert (
            subprocess.check_output(
                ["systemctl", "show", "-p", "MainPID", "--value", "opencode-previews"],
                text=True,
            ).strip()
            == pid
        )
        assert process_identity(pid) == manager_identity
        assert pid in (manager / "cgroup.procs").read_text().split()
        subprocess.run(
            ["systemctl", "is-active", "--quiet", "opencode-previews"],
            check=True,
            timeout=5,
        )
        assert ready(b) == sibling
        content(b, sibling_cookie, "pool survivor")
        assert snapshot(sibling)[0] == sibling_snapshot[0]
        for child, identity in sibling_identities.items():
            assert process_identity(child) == identity, child
        assert pool.stat().st_ino == pool_inode
        assert request(path="/health")[2] == "fake OpenCode health"

    def pool_events(name="memory.events"):
        return {
            key: int(value)
            for key, value in (
                line.split() for line in (pool / name).read_text().splitlines()
            )
        }

    before_oom = pool_events()
    before_local_oom = pool_events("memory.events.local")["oom"]
    shell(
        "alpha",
        "ses_poolvictim",
        f"python3 {shlex.quote(__file__)} allocate {config['memory_max']} "
        "> pool-allocator.log 2>&1 < /dev/null &",
    )
    allocator = root / "alpha/pool-allocator"
    eventually(lambda: present(allocator.with_suffix(".ready")))
    victim_snapshot = snapshot(victim)
    assert any(
        b"allocate" in Path(f"/proc/{child}/cmdline").read_bytes()
        for child in victim_snapshot[1]
    ), victim_snapshot
    victim_broker = RUNTIME / "broker" / victim["runtime"]
    victim_sockets = list((RUNTIME / "u").glob(victim["runtime"] + "-*.sock"))
    assert victim_broker.is_dir() and victim_sockets
    # Prefer this leaf explicitly, including PID-namespace init and helpers.
    # Never exempt a victim child with -1000: group OOM must kill the whole leaf.
    for child in victim_snapshot[1]:
        score = Path(f"/proc/{child}/oom_score_adj")
        score.write_text("1000")
        assert score.read_text().strip() == "1000", child
    for child in sibling_snapshot[1]:
        assert int(Path(f"/proc/{child}/oom_score_adj").read_text()) < 1000, child
    allocator.with_suffix(".hold").touch()

    def borrowed():
        survivor_healthy()
        held = allocator.with_suffix(".hold-filled").read_text()
        assert held.isdecimal(), held
        assert int(held) > config["memory_max"] // 2
        assert int((victim_snapshot[0] / "memory.current").read_text()) > (
            config["memory_max"] // 2
        )
        assert set(pool.glob("runtime-*")) == {victim_snapshot[0], sibling_snapshot[0]}
        assert pool_events()["oom"] == before_oom["oom"]

    eventually(borrowed)
    allocator.with_suffix(".oom").touch()

    def pool_oom():
        survivor_healthy()
        events = pool_events()
        assert events["oom"] > before_oom["oom"], events
        assert pool_events("memory.events.local")["oom"] > before_local_oom
        assert events["oom_kill"] > before_oom["oom_kill"], events
        assert events["oom_group_kill"] > before_oom["oom_group_kill"], events
        assert not allocator.with_suffix(".oom-filled").exists()

    eventually(pool_oom, timeout=45)
    eventually(lambda: cleaned(victim_snapshot))
    eventually(lambda: gone(a))
    assert all(not path.exists() for path in victim_sockets)
    survivor_healthy()
    assert shell("beta", "ses_poolsibling", "printf sibling-exec") == "sibling-exec"
    survivor_healthy()
    client(
        "beta",
        "ses_poolsibling",
        "stop",
        "--session",
        "ses_poolsibling",
        "--directory",
        str(root / "beta"),
    )
    eventually(lambda: cleaned(sibling_snapshot))
    empty()
    assert pool.stat().st_ino == pool_inode
    assert not list(pool.glob("runtime-*"))

    print(
        "Checking service restart revokes capabilities and removes all binders",
        flush=True,
    )
    start("alpha", "ses_alpha", "before restart alpha")
    start("beta", "ses_beta", "before restart beta")
    old_a, old_b = eventually(lambda: ready(a)), eventually(lambda: ready(b))
    assert old_a["runtime"] != entry_a["runtime"]
    mcp("alpha", "ses_alpha", "browser_navigate", {"url": "http://localhost:3000/"})
    mcp(
        "alpha",
        "ses_alpha",
        "browser_evaluate",
        {
            "function": "() => { "
            "if (localStorage.getItem('preview-owner') !== null || "
            "window.previewCalls !== undefined) "
            "throw new Error('Stopped runtime retained browser state'); "
            "return document.body.innerText; }"
        },
    )
    old_cookie = login(a, old_a)
    old_snapshots = snapshot(old_a, browser=True), snapshot(old_b)
    old_sockets = list((RUNTIME / "u").glob("*.sock"))
    assert set(pool.glob("runtime-*")) == {old[0] for old in old_snapshots}
    assert old_sockets
    subprocess.run(
        ["systemctl", "restart", "opencode-previews"], check=True, timeout=60
    )
    subprocess.run(["systemctl", "is-active", "--quiet", "opencode"], check=True)
    eventually(lambda: empty())
    restarted_pid = subprocess.check_output(
        ["systemctl", "show", "-p", "MainPID", "--value", "opencode-previews"],
        text=True,
    ).strip()
    assert restarted_pid != pid
    assert restarted_pid in (manager / "cgroup.procs").read_text().split()
    for old in old_snapshots:
        eventually(lambda old=old: cleaned(old, service_restart=True))
    if pool.exists():
        pool_limits()
    assert not list(parent.rglob("runtime-*"))
    assert not list((RUNTIME / "u").iterdir())
    assert not list((RUNTIME / "broker").iterdir())
    assert all(not path.exists() for path in old_sockets)
    assert request(a, old_a["login"])[0] == 404
    start("alpha", "ses_alpha", "after restart")
    new_a = eventually(lambda: ready(a))
    assert new_a["runtime"] != old_a["runtime"] and new_a["login"] != old_a["login"]
    assert request(a, old_a["login"])[0] == 403
    assert request(a, "/", old_cookie)[0] == 403
    content(a, login(a, new_a), "after restart")
    final_snapshot = snapshot(new_a)
    client(
        "alpha",
        "ses_alpha",
        "stop",
        "--session",
        "ses_alpha",
        "--directory",
        str(root / "alpha"),
    )
    eventually(lambda: cleaned(final_snapshot))
    empty()
    print("All real preview VM integration assertions passed", flush=True)


def absent(host):
    assert host not in directory()


def present(path):
    assert path.exists(), path


def empty():
    assert directory() == {}


if __name__ == "__main__":
    if sys.argv[1] == "serve":
        asyncio.run(serve())
    elif sys.argv[1] == "probe":
        probe(sys.argv[2])
    elif sys.argv[1] == "allocate":
        allocate(int(sys.argv[2]))
    elif sys.argv[1] == "test":
        integration(*sys.argv[2:])
    else:
        raise SystemExit("unknown fixture mode")
