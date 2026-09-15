"""Run with python -m unittest discover -s tests/previews -p test_gateway.py."""

import asyncio
import base64
import gzip
import importlib.util
import os
import re
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import (
    ClientSession,
    DummyCookieJar,
    UnixConnector,
    WSMsgType,
    WSServerHandshakeError,
    web,
)
from aiohttp.test_utils import TestServer

SOURCE = Path(
    os.environ.get(
        "OPENCODE_PREVIEW_SOURCE",
        Path(__file__).resolve().parents[2] / "pkgs/opencode-preview",
    )
)
SPEC = importlib.util.spec_from_file_location("preview_gateway", SOURCE / "gateway.py")
gateway = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gateway)

PUBLIC = "opencode.example.com"
FIRST = "preview-work-3000.example.com"
SECOND = "preview-work-4000.example.com"
OTHER = "preview-other-3000.example.com"
PASSWORD = "server-secret"
BASIC = "Basic " + base64.b64encode(("opencode:" + PASSWORD).encode()).decode()
APP_POLICY_HEADERS = {
    "NEL": '{"report_to":"collector","max_age":86400,"success_fraction":1}',
    "Report-To": '{"group":"collector","max_age":86400,"endpoints":[{"url":"https://collector.example.com/reports"}]}',
    "Reporting-Endpoints": 'collector="https://collector.example.com/reports"',
    "Alt-Svc": 'h3="collector.example.com:443"; ma=86400',
}


class FakeManager:
    def __init__(self, socket_path):
        self.entries = [
            {
                "runtime_id": runtime,
                "directory": '/work/<script>"&',
                "workspace": "work <unsafe>",
                "session_id": "ses_" + runtime,
                "slug": "work",
                "port": port,
                "hostname": host,
                "socket_path": str(socket_path),
                "token": "capability-" + str(port) + runtime,
                "available": True,
            }
            for host, runtime, port in [
                (FIRST, "one", 3000),
                (SECOND, "one", 4000),
                (OTHER, "two", 3000),
            ]
        ]
        self.stopped = []

    def lookup(self, host):
        return next(
            (entry for entry in self.entries if entry["hostname"] == host), None
        )

    def list_previews(self):
        return self.entries

    async def stop(self, runtime_id):
        self.stopped.append(runtime_id)
        self.entries = [
            entry for entry in self.entries if entry["runtime_id"] != runtime_id
        ]


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.release_stream = asyncio.Event()
        self.requests = []
        self.socket = Path(self.temporary.name) / "app.sock"
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.upstream)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        self.addAsyncCleanup(self.runner.cleanup)
        await web.UnixSite(self.runner, str(self.socket)).start()
        self.public_backend = TestServer(app)
        await self.public_backend.start_server()
        self.addAsyncCleanup(self.public_backend.close)
        self.manager = FakeManager(self.socket)
        self.config = {
            "public_host": PUBLIC,
            "preview_domain": "example.com",
            "opencode_port": self.public_backend.port,
        }
        self.server = TestServer(
            gateway.create_app(self.config, self.manager, PASSWORD)
        )
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.client = ClientSession(cookie_jar=DummyCookieJar())
        self.addAsyncCleanup(self.client.close)
        self.addCleanup(self.release_stream.set)

    async def upstream(self, request):
        self.requests.append((request.path, dict(request.headers)))
        if request.path in {"/ws", "/pty"}:
            if (
                request.path == "/pty"
                and request.query.get("ticket") != "android-ticket"
            ):
                return web.Response(
                    status=401,
                    headers={
                        "WWW-Authenticate": 'Basic realm="OpenCode"',
                        **APP_POLICY_HEADERS,
                    },
                )
            ws = web.WebSocketResponse(autoping=False, protocols=["echo"])
            ws.headers.update(APP_POLICY_HEADERS)
            await ws.prepare(request)
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    if message.data == "close":
                        await ws.close(code=4001, message=b"finished")
                    elif message.data == "ping":
                        await ws.ping(b"upstream")
                    else:
                        await ws.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await ws.send_bytes(message.data)
                elif message.type == WSMsgType.PING:
                    await ws.pong(message.data)
                elif message.type == WSMsgType.PONG:
                    await ws.send_str("pong:" + message.data.decode())
            return ws
        if request.path == "/sse":
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b"data: first\n\n")
            await self.release_stream.wait()
            await response.write(b"data: second\n\n")
            return response
        if request.path == "/cookies":
            response = web.Response(
                text="cookies",
                headers={
                    **APP_POLICY_HEADERS,
                    "Clear-Site-Data": '"cookies", "storage"',
                    "WWW-Authenticate": "Basic",
                    "Service-Worker-Allowed": "/",
                    "Content-Security-Policy": "default-src *",
                    "Cross-Origin-Opener-Policy": "unsafe-none",
                    "Cache-Control": "public, max-age=3600",
                    "Referrer-Policy": "unsafe-url",
                    "Access-Control-Allow-Origin": "*",
                    "Refresh": "0; url=https://opencode.example.com/",
                },
            )
            response.headers.add(
                "Set-Cookie", gateway.COOKIE + "=stolen; Path=/; Secure"
            )
            response.headers.add(
                "Set-Cookie", "app=value; Domain=example.com; Path=/; HttpOnly"
            )
            response.headers.add(
                "Set-Cookie",
                "other=two; domain=.example.com; Expires=Wed, 21 Oct 2037 07:28:00 GMT",
            )
            return response
        if request.path == "/compressed":
            return web.Response(
                body=gzip.compress(b"compressed response"),
                headers={"Content-Encoding": "gzip"},
            )
        if request.path == "/redirect":
            return web.Response(
                status=302, headers={"Location": request.query["location"]}
            )
        if (
            request.path == "/protected"
            and request.headers.get("Authorization") != BASIC
            and request.query.get("auth_token") != "query-secret"
        ):
            return web.Response(
                status=401, headers={"WWW-Authenticate": 'Basic realm="OpenCode"'}
            )
        if request.path in {"/protected", "/slow"}:
            await asyncio.sleep(0.05)
        try:
            body = await request.read()
        except ConnectionResetError:
            return web.Response(status=400)
        return web.json_response(
            {
                "headers": dict(request.headers),
                "path": request.raw_path,
                "body": body.decode(),
            }
        )

    def headers(self, host=FIRST, auth=True, **extra):
        headers = {"Host": host}
        if auth and host != PUBLIC:
            headers["Cookie"] = (
                gateway.COOKIE + "=" + self.manager.lookup(host)["token"]
            )
        headers.update(extra)
        return headers

    async def request(
        self, path="/", *, host=FIRST, auth=True, method="GET", headers=None, **kwargs
    ):
        return await self.client.request(
            method,
            self.server.make_url(path),
            headers=self.headers(host, auth, **(headers or {})),
            allow_redirects=False,
            **kwargs,
        )

    async def test_http_header_timeout_applies_only_to_previews(self):
        self.config["handshake_timeout"] = 0.01
        response = await self.request("/protected", host=PUBLIC)
        self.assertEqual(response.status, 401)
        response = await self.request(
            "/protected", host=PUBLIC, headers={"Authorization": BASIC}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["path"], "/protected")
        response = await self.request("/slow")
        self.assertEqual(response.status, 502)
        self.assertEqual(await response.text(), "Upstream unavailable")

    async def test_app_reporting_and_transport_headers_are_preview_filtered(self):
        for host in (PUBLIC, FIRST):
            with self.subTest(host=host):
                response = await self.request("/cookies", host=host)
                self.assertEqual(response.status, 200)
                async with self.client.ws_connect(
                    self.server.make_url("/ws"),
                    headers=self.headers(host),
                    protocols=["echo"],
                ) as ws:
                    with self.assertRaises(WSServerHandshakeError) as raised:
                        await self.client.ws_connect(
                            self.server.make_url("/pty"), headers=self.headers(host)
                        )
                    self.assertEqual(raised.exception.status, 401)
                    for headers in (
                        response.headers,
                        ws._response.headers,
                        raised.exception.headers,
                    ):
                        for name, value in APP_POLICY_HEADERS.items():
                            if host == FIRST:
                                self.assertNotIn(name, headers)
                            else:
                                self.assertEqual(headers[name], value)

    async def test_directory_basic_auth_escape_and_empty_state(self):
        for headers in (
            {},
            {"Authorization": "Basic ???"},
            {"Authorization": "Bearer " + PASSWORD},
            {
                "Authorization": "Basic "
                + base64.b64encode(("other:" + PASSWORD).encode()).decode()
            },
        ):
            response = await self.request("/previews", host=PUBLIC, headers=headers)
            self.assertEqual(response.status, 401)
            self.assertIn("Basic", response.headers["WWW-Authenticate"])
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        response = await self.request(
            "/previews", host=PUBLIC, headers={"Authorization": BASIC}
        )
        text = await response.text()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Referrer-Policy"], "same-origin")
        self.assertIn("&lt;script&gt;&quot;&amp;", text)
        self.assertNotIn("<unsafe>", text)
        self.assertIn('target="_blank" rel="noopener noreferrer"', text)
        self.assertIn("https://" + FIRST + "/__preview_login?token=", text)
        self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
        self.manager.entries.clear()
        response = await self.request(
            "/previews", host=PUBLIC, headers={"Authorization": BASIC}
        )
        self.assertIn("ordinary server command", await response.text())

    async def test_stop_requires_basic_exact_origin_csrf_and_known_runtime(self):
        response = await self.request(
            "/previews", host=PUBLIC, headers={"Authorization": BASIC}
        )
        csrf = re.search(r'name="csrf" value="([^"]+)"', await response.text()).group(1)
        for headers, token, expected in [
            ({"Origin": "https://" + PUBLIC}, csrf, 401),
            ({"Authorization": BASIC}, csrf, 403),
            ({"Authorization": BASIC, "Origin": "null"}, csrf, 403),
            ({"Authorization": BASIC, "Origin": "https://" + FIRST}, csrf, 403),
            ({"Authorization": BASIC, "Origin": "https://" + PUBLIC}, "wrong", 403),
        ]:
            response = await self.request(
                "/previews/stop/one",
                host=PUBLIC,
                method="POST",
                headers=headers,
                data={"csrf": token},
            )
            self.assertEqual(response.status, expected)
            self.assertEqual(self.manager.stopped, [])
        headers = {"Authorization": BASIC, "Origin": "https://" + PUBLIC}
        response = await self.request(
            "/previews/stop/absent",
            host=PUBLIC,
            method="POST",
            headers=headers,
            data={"csrf": csrf},
        )
        self.assertEqual(response.status, 404)
        response = await self.request(
            "/previews/stop/one",
            host=PUBLIC,
            method="POST",
            headers=headers,
            data={"csrf": csrf},
        )
        self.assertEqual(response.status, 303)
        self.assertEqual(self.manager.stopped, ["one"])
        self.assertEqual(response.headers["Location"], "/previews")

    async def test_login_reusable_capability_flags_and_mapping_invalidation(self):
        token = self.manager.lookup(FIRST)["token"]
        for clock in (time.time(), time.time() + 86400 * 365):
            with patch.object(time, "time", return_value=clock):
                response = await self.request(
                    "/__preview_login?token=" + token,
                    auth=False,
                    headers={"Origin": "https://external.example.com"},
                )
            self.assertEqual(response.status, 303)
            self.assertEqual(response.headers["Location"], "/")
            cookie = response.cookies[gateway.COOKIE]
            self.assertEqual(cookie.value, token)
            self.assertTrue(cookie["secure"])
            self.assertTrue(cookie["httponly"])
            self.assertEqual(cookie["samesite"], "Lax")
            self.assertEqual(cookie["path"], "/")
            self.assertFalse(cookie["domain"])
            self.assertFalse(cookie["max-age"])
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        response = await self.request(
            "/__preview_login?token=" + token, host=SECOND, auth=False
        )
        self.assertEqual(response.status, 403)
        self.assertNotIn(token, await response.text())
        response = await self.request(
            "/__preview_login?token=" + token, method="POST", auth=False
        )
        self.assertEqual(response.status, 403)
        response = await self.request(auth=False)
        self.assertEqual(response.status, 403)
        self.manager.lookup(FIRST)["token"] = "new-runtime-token"
        response = await self.request(headers={"Cookie": gateway.COOKIE + "=" + token})
        self.assertEqual(response.status, 403)
        await self.manager.stop("one")
        response = await self.request("/__preview_login?token=" + token, auth=False)
        self.assertEqual(response.status, 404)

    async def test_preview_strips_credentials_identity_and_forwarding_headers(self):
        token = self.manager.lookup(FIRST)["token"]
        response = await self.request(
            headers={
                "Cookie": "a=1; " + gateway.COOKIE + "=" + token + "; b=two",
                "Authorization": BASIC,
                "Proxy-Authorization": "Basic secret",
                "Cf-Access-Jwt-Assertion": "identity-secret",
                "Cf-Access-Authenticated-User-Email": "private@example.com",
                "Cloudflare-Identity": "secret",
                "Forwarded": "host=bad.example.com",
                "X-Forwarded-For": "secret",
                "X-Forwarded-Host": PUBLIC,
                "X-Forwarded-Proto": "http",
                "X-Real-IP": "secret",
                "Connection": "X-Hop",
                "X-Hop": "secret",
            }
        )
        self.assertEqual(response.status, 200)
        data = await response.json()
        headers = {name.lower(): value for name, value in data["headers"].items()}
        self.assertEqual(headers["cookie"], "a=1; b=two")
        self.assertEqual(headers["host"], FIRST)
        self.assertEqual(headers["x-forwarded-host"], FIRST)
        self.assertEqual(headers["x-forwarded-proto"], "https")
        for name in (
            "authorization",
            "proxy-authorization",
            "cf-access-jwt-assertion",
            "cf-access-authenticated-user-email",
            "cloudflare-identity",
            "forwarded",
            "x-real-ip",
            "x-forwarded-for",
            "x-hop",
        ):
            self.assertNotIn(name, headers)
        self.assertNotIn(token, str(data))

    async def test_app_response_cookies_and_security_headers(self):
        response = await self.request("/cookies")
        cookies = response.headers.getall("Set-Cookie")
        self.assertEqual(len(cookies), 2)
        self.assertTrue(all("domain=" not in cookie.lower() for cookie in cookies))
        self.assertTrue(all(gateway.COOKIE not in cookie for cookie in cookies))
        self.assertIn("Expires=Wed, 21 Oct 2037", cookies[1])
        for name in (
            "Clear-Site-Data",
            "WWW-Authenticate",
            "Service-Worker-Allowed",
            "Refresh",
            "Access-Control-Allow-Origin",
        ):
            self.assertNotIn(name, response.headers)
        self.assertEqual(
            response.headers["Content-Security-Policy"], "frame-ancestors 'none'"
        )
        self.assertEqual(response.headers["Cross-Origin-Opener-Policy"], "same-origin")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_service_worker_denied_including_login(self):
        for path in (
            "/worker.js",
            "/__preview_login?token=" + self.manager.lookup(FIRST)["token"],
        ):
            response = await self.request(path, headers={"Service-Worker": "script"})
            self.assertEqual(response.status, 403)
        self.assertEqual(self.requests, [])

    async def test_public_origin_policy_native_auth_and_safe_navigation(self):
        cases = [
            ({}, 200),
            ({"User-Agent": "Android"}, 200),
            ({"Origin": "https://" + PUBLIC}, 200),
            ({"Sec-Fetch-Site": "same-origin"}, 200),
            ({"Origin": "null"}, 403),
            ({"Origin": "https://" + FIRST}, 403),
            ({"Origin": "https://evil.example.com", "User-Agent": "Android"}, 403),
            ({"Sec-Fetch-Site": "same-site"}, 403),
            (
                {
                    "Sec-Fetch-Site": "cross-site",
                    "Sec-Fetch-Mode": "no-cors",
                    "Sec-Fetch-Dest": "script",
                },
                403,
            ),
            ({"Sec-Fetch-Mode": "cors"}, 403),
        ]
        for headers, expected in cases:
            with self.subTest(headers=headers):
                response = await self.request("/api", host=PUBLIC, headers=headers)
                self.assertEqual(response.status, expected)
        nav = {
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
        }
        self.assertEqual((await self.request(host=PUBLIC, headers=nav)).status, 200)
        self.assertEqual(
            (
                await self.request(
                    "/previews", host=PUBLIC, headers={**nav, "Authorization": BASIC}
                )
            ).status,
            200,
        )
        self.assertEqual(
            (await self.request("/api", host=PUBLIC, headers=nav)).status, 403
        )
        self.assertEqual(
            (
                await self.request(
                    "/api",
                    host=PUBLIC,
                    method="OPTIONS",
                    headers={"Origin": "https://" + FIRST},
                )
            ).status,
            403,
        )
        response = await self.request("/protected", host=PUBLIC)
        self.assertEqual(response.status, 401)
        self.assertIn("OpenCode", response.headers["WWW-Authenticate"])
        for path, headers in [
            ("/protected", {"Authorization": BASIC}),
            ("/protected?auth_token=query-secret", {}),
        ]:
            response = await self.request(path, host=PUBLIC, headers=headers)
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["path"], path)

    async def test_preview_document_navigation_requires_cookie_on_any_path(self):
        nav = {
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
        }
        for site in ("same-site", "cross-site", "none"):
            for auth in (False, True):
                with self.subTest(site=site, auth=auth):
                    response = await self.request(
                        "/trainings",
                        auth=auth,
                        headers={**nav, "Sec-Fetch-Site": site},
                    )
                    self.assertEqual(response.status, 200 if auth else 403)
        nav["Sec-Fetch-Site"] = "same-site"
        for method, changed in (
            ("POST", {}),
            ("GET", {"Sec-Fetch-Mode": "cors"}),
            ("GET", {"Sec-Fetch-Mode": ""}),
            ("GET", {"Sec-Fetch-Dest": "iframe"}),
            ("GET", {"Sec-Fetch-Dest": ""}),
            ("GET", {"Origin": "null"}),
            ("GET", {"Cookie": gateway.COOKIE + "=wrong"}),
        ):
            with self.subTest(method=method, changed=changed):
                before = len(self.requests)
                response = await self.request(
                    "/trainings", method=method, headers={**nav, **changed}
                )
                self.assertEqual(response.status, 403)
                self.assertEqual(len(self.requests), before)

    async def test_preview_origin_policy_cross_port_and_cors(self):
        for headers, expected in [
            ({}, 200),
            ({"Origin": "https://" + FIRST}, 200),
            ({"Origin": "https://" + SECOND}, 200),
            ({"Origin": "https://" + OTHER}, 403),
            ({"Origin": "https://" + PUBLIC}, 403),
            ({"Origin": "null"}, 403),
            ({"Origin": "https://" + SECOND + "/"}, 403),
            ({"Sec-Fetch-Site": "same-site"}, 403),
            ({"Sec-Fetch-Site": "same-origin"}, 200),
        ]:
            with self.subTest(headers=headers):
                response = await self.request("/api", headers=headers)
                self.assertEqual(response.status, expected)
                if headers.get("Origin") == "https://" + SECOND:
                    self.assertEqual(
                        response.headers["Access-Control-Allow-Origin"],
                        "https://" + SECOND,
                    )
                    self.assertEqual(
                        response.headers["Access-Control-Allow-Credentials"], "true"
                    )
        nav = {
            "Origin": "https://" + PUBLIC,
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
        }
        self.assertEqual((await self.request(headers=nav)).status, 200)
        self.assertEqual((await self.request("/api", headers=nav)).status, 200)
        preflight = {
            "Origin": "https://" + SECOND,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type, X-App-Value",
        }
        response = await self.request(
            "/api", auth=False, method="OPTIONS", headers=preflight
        )
        self.assertEqual(response.status, 204)
        self.assertEqual(
            response.headers["Access-Control-Allow-Headers"],
            "content-type, x-app-value",
        )
        self.assertEqual(
            response.headers["Access-Control-Allow-Origin"], "https://" + SECOND
        )
        for changed in (
            {"Origin": "https://" + OTHER},
            {"Access-Control-Request-Method": "CONNECT"},
            {"Access-Control-Request-Headers": "X-Forwarded-Host"},
            {"Access-Control-Request-Headers": "bad header"},
        ):
            response = await self.request(
                "/api", auth=False, method="OPTIONS", headers={**preflight, **changed}
            )
            self.assertEqual(response.status, 403)
        response = await self.request(
            "/api", auth=False, method="POST", headers={"Origin": "https://" + SECOND}
        )
        self.assertEqual(response.status, 403)

    async def test_unknown_hosts_and_public_control_routes_not_exposed(self):
        for host in (
            "unknown.example.com",
            PUBLIC + ".evil.example.com",
            FIRST + ":80",
            "localhost",
            PUBLIC + "@evil.example.com",
        ):
            response = await self.request(host=host, auth=False)
            self.assertEqual(response.status, 404)
        response = await self.request("/exec", host=PUBLIC, method="POST")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["path"], "/exec")

    async def test_http_body_and_hop_headers(self):
        response = await self.request(
            "/echo?value=%2F%26", method="POST", data=b"body value"
        )
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data["body"], "body value")
        self.assertEqual(data["path"], "/echo?value=/%26")
        response = await self.request(
            method="POST", headers={"Content-Length": str(gateway.MAX_BODY + 1)}
        )
        self.assertEqual(response.status, 413)

    async def test_compressed_request_forwarding_and_chunked_body_limit(self):
        payload = gzip.compress(b"a compressed body" * 100)
        response = await self.request(
            method="POST", data=payload, headers={"Content-Encoding": "gzip"}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["body"], "a compressed body" * 100)

        async def chunks():
            yield b"first"
            yield b"too much data"

        with patch.object(gateway, "MAX_BODY", 8):
            response = await self.request(method="POST", data=chunks())
        self.assertEqual(response.status, 413)

    async def test_compressed_response_and_head_preserve_framing(self):
        response = await self.request("/compressed", auto_decompress=False)
        body = await response.read()
        self.assertEqual(response.headers["Content-Encoding"], "gzip")
        self.assertEqual(int(response.headers["Content-Length"]), len(body))
        self.assertEqual(gzip.decompress(body), b"compressed response")
        response = await self.request("/compressed", method="HEAD")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b"")
        self.assertEqual(response.headers["Content-Encoding"], "gzip")

    async def test_upstream_failure_and_unavailable_mapping_are_generic(self):
        entry = self.manager.lookup(FIRST)
        entry["socket_path"] = str(Path(self.temporary.name) / "missing.sock")
        response = await self.request("/?sensitive-query=" + entry["token"])
        self.assertEqual(response.status, 502)
        self.assertEqual(await response.text(), "Upstream unavailable")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        entry["available"] = False
        response = await self.request()
        self.assertEqual(response.status, 503)
        self.assertNotIn(entry["token"], await response.text())

    async def test_redirects_rewrite_own_and_block_control_siblings(self):
        for location in (
            "http://localhost:3000/path?x=1",
            "http://127.0.0.1:3000/path?x=1",
            "http://" + FIRST + ":3000/path?x=1",
        ):
            response = await self.request("/redirect", params={"location": location})
            self.assertEqual(response.status, 302)
            self.assertEqual(
                response.headers["Location"], "https://" + FIRST + "/path?x=1"
            )
        for location in (
            "https://" + PUBLIC + "/",
            "//" + SECOND + "/",
            "/\\" + PUBLIC + "/",
        ):
            response = await self.request("/redirect", params={"location": location})
            self.assertEqual(response.status, 502)
            self.assertNotIn("Location", response.headers)
        response = await self.request(
            "/redirect", params={"location": "https://auth.example.com/login"}
        )
        self.assertEqual(response.status, 302)

    async def test_sse_streams_before_upstream_finishes(self):
        response = await self.request("/sse")
        self.assertEqual(response.headers["Content-Type"], "text/event-stream")
        async with asyncio.timeout(2):
            self.assertEqual(await response.content.readexactly(13), b"data: first\n\n")
        self.assertFalse(self.release_stream.is_set())
        self.release_stream.set()
        self.assertEqual(await response.read(), b"data: second\n\n")

    async def test_preview_websocket_auth_duplex_ping_pong_and_close(self):
        url = self.server.make_url("/ws")
        for headers in (
            self.headers(auth=False),
            self.headers(Origin="https://" + OTHER),
            self.headers(Origin="https://" + PUBLIC),
        ):
            with self.assertRaises(WSServerHandshakeError) as raised:
                await self.client.ws_connect(url, headers=headers)
            self.assertEqual(raised.exception.status, 403)
        async with self.client.ws_connect(
            url,
            headers=self.headers(Origin="https://" + SECOND),
            protocols=["echo"],
            autoping=False,
        ) as ws:
            self.assertEqual(ws.protocol, "echo")
            await ws.send_str("hello")
            self.assertEqual((await ws.receive(timeout=2)).data, "hello")
            await ws.send_bytes(b"binary")
            self.assertEqual((await ws.receive(timeout=2)).data, b"binary")
            await ws.ping(b"client")
            message = await ws.receive(timeout=2)
            self.assertEqual((message.type, message.data), (WSMsgType.PONG, b"client"))
            await ws.send_str("ping")
            message = await ws.receive(timeout=2)
            self.assertEqual(
                (message.type, message.data), (WSMsgType.PING, b"upstream")
            )
            await ws.pong(message.data)
            self.assertEqual((await ws.receive(timeout=2)).data, "pong:upstream")
            await ws.send_str("close")
            message = await ws.receive(timeout=2)
            self.assertEqual(message.type, WSMsgType.CLOSE)
            self.assertEqual(message.data, 4001)
        forwarded = self.requests[-1][1]
        self.assertNotIn(gateway.COOKIE, forwarded.get("Cookie", ""))

    async def test_android_pty_ticket_no_blanket_basic_websocket_auth(self):
        async with self.client.ws_connect(
            self.server.make_url("/pty?ticket=android-ticket"),
            headers={"Host": PUBLIC, "User-Agent": "okhttp"},
        ) as ws:
            await ws.send_str("android")
            self.assertEqual((await ws.receive(timeout=2)).data, "android")
        with self.assertRaises(WSServerHandshakeError) as raised:
            await self.client.ws_connect(
                self.server.make_url("/pty?ticket=wrong"), headers={"Host": PUBLIC}
            )
        self.assertEqual(raised.exception.status, 401)
        self.assertIn("WWW-Authenticate", raised.exception.headers)
        with self.assertRaises(WSServerHandshakeError) as raised:
            await self.client.ws_connect(
                self.server.make_url("/pty?ticket=android-ticket"),
                headers={"Host": PUBLIC, "Origin": "https://" + FIRST},
            )
        self.assertEqual(raised.exception.status, 403)

    async def test_websocket_redirect_is_never_followed(self):
        with self.assertRaises(WSServerHandshakeError) as raised:
            await self.client.ws_connect(
                self.server.make_url("/redirect"),
                params={"location": str(self.public_backend.make_url("/ws"))},
                headers=self.headers(),
            )
        self.assertEqual(raised.exception.status, 502)
        self.assertEqual([path for path, headers in self.requests], ["/redirect"])

    async def test_startup_private_control_credentials_and_shutdown_order(self):
        root = Path(self.temporary.name)
        (root / "server-password").write_text(PASSWORD + "\n")
        started = asyncio.Event()
        signals = {}
        sites = []
        events = []
        owner = self

        class LifecycleManager(FakeManager):
            def __init__(self, config):
                super().__init__(owner.socket)
                owner.assertNotIn(PASSWORD, str(config))

            async def start(self):
                events.append("start")

            async def close(self):
                events.append("close")

            def create_control_app(self):
                app = web.Application()

                async def control(request):
                    return web.Response(text="private control")

                async def cleanup(app):
                    events.append("cleanup")

                app.router.add_get("/control", control)
                app.on_cleanup.append(cleanup)
                return app

        original_tcp = web.TCPSite
        original_unix = web.UnixSite.start

        def tcp(runner, host, port):
            self.assertEqual(host, "127.0.0.1")
            site = original_tcp(runner, host, port)
            sites.append(site)
            return site

        async def unix_start(site):
            await original_unix(site)
            started.set()

        loop = asyncio.get_running_loop()
        config = {**self.config, "runtime_root": str(root), "listen_port": 0}
        with (
            patch.dict(
                sys.modules,
                {"manager": types.SimpleNamespace(Manager=LifecycleManager)},
            ),
            patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(root)}),
            patch.object(
                loop,
                "add_signal_handler",
                side_effect=lambda sig, callback: signals.update({sig: callback}),
            ),
            patch.object(loop, "remove_signal_handler"),
            patch.object(web, "TCPSite", side_effect=tcp),
            patch.object(web.UnixSite, "start", unix_start),
        ):
            task = asyncio.create_task(gateway.serve(config))
            try:
                async with asyncio.timeout(3):
                    await started.wait()
                self.assertEqual((root / "control.sock").stat().st_mode & 0o777, 0o600)
                async with ClientSession(
                    connector=UnixConnector(path=str(root / "control.sock"))
                ) as client:
                    response = await client.get("http://localhost/control")
                    self.assertEqual(await response.text(), "private control")
                port = sites[0]._server.sockets[0].getsockname()[1]
                response = await self.client.get(
                    f"http://127.0.0.1:{port}/control", headers={"Host": PUBLIC}
                )
                self.assertEqual((await response.json())["path"], "/control")
                response = await self.client.get(
                    f"http://127.0.0.1:{port}/previews",
                    headers={"Host": PUBLIC, "Authorization": BASIC},
                )
                self.assertEqual(response.status, 200)
                self.assertNotIn(PASSWORD, str(os.environ))
                signals[gateway.signal.SIGTERM]()
                await asyncio.wait_for(task, 3)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(events, ["start", "close", "cleanup"])

    def test_dump_protection_is_not_applied_on_import_or_non_linux(self):
        with (
            patch.object(gateway.sys, "platform", "darwin"),
            patch.object(gateway.ctypes, "CDLL") as libc,
        ):
            gateway.disable_dumping()
            libc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
