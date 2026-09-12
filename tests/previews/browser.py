"""Real Chromium against create_app, with a local, simulated HTTPS tunnel edge.

Run with OPENCODE_PREVIEW_SOURCE and OPENCODE_PREVIEW_CHROMIUM set (see browser.nix).
Only the allowlisted conceptual HTTPS origins are transported to loopback HTTP;
Playwright skips routing redirect hops and relaxes CORS on fulfilled responses.
Redirects and fetch therefore use a loopback TLS edge with a throwaway certificate.
No public DNS/TLS, cloudflared, real OpenCode server or runtime discovery is exercised.
Service-worker denial and reporting-header filtering also have gateway unit tests.
Absence of browser reports is NOT evidence that reporting policy is safe.
"""

import asyncio
import base64
import importlib.util
import os
import ssl
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright, expect
from yarl import URL

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
FIRST = "preview-alpha-3000.example.com"
SECOND = "preview-alpha-4000.example.com"
OTHER = "preview-beta-3000.example.com"
HOSTS = {PUBLIC, FIRST, SECOND, OTHER}
PASSWORD = "browser-fixture-only-password"
BASIC = "Basic " + base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
POLICY_HEADERS = {
    "NEL": '{"report_to":"collector","max_age":86400,"success_fraction":1}',
    "Report-To": '{"group":"collector","max_age":86400,"endpoints":[{"url":"https://collector.example.com/reports"}]}',
    "Reporting-Endpoints": 'collector="https://collector.example.com/reports"',
    "Alt-Svc": 'h3="collector.example.com:443"; ma=86400',
}
APP = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Socket preview fixture</title><body><h1 id="app">Loading</h1>
<script>
document.querySelector('#app').textContent = 'Socket app rendered';
window.probe = async (url, method = 'GET') => {
  try {
    const response = await fetch(url, {
      method, credentials: 'include',
      ...(method === 'POST' ? {body: 'browser mutation'} : {})
    });
    return {status: response.status, body: await response.text()};
  } catch (error) {
    return {error: error.name};
  }
};
</script></body></html>"""


class Manager:
    def __init__(self, root):
        self.entries = [
            {
                "hostname": host,
                "runtime_id": runtime,
                "session_id": "ses_" + runtime,
                "workspace": "workspace " + runtime,
                "directory": "/work/.tasks/" + "long-directory-" * 12,
                "slug": runtime,
                "port": port,
                "socket_path": str(root / f"{runtime}-{port}.sock"),
                "token": f"fake-capability-{runtime}-{port}",
                "available": True,
            }
            for host, runtime, port in (
                (FIRST, "alpha", 3000),
                (SECOND, "alpha", 4000),
                (OTHER, "beta", 3000),
            )
        ]

    def lookup(self, host):
        return next((e for e in self.entries if e["hostname"] == host), None)

    def list_previews(self):
        return self.entries

    async def stop(self, runtime_id):
        raise AssertionError("Browser must not stop a runtime")


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="preview-browser-")
        self.addCleanup(temporary.cleanup)
        self.manager = Manager(Path(temporary.name))
        self.upstream_requests = []
        self.edge_requests = []
        self.external_requests = []
        self.derived_metadata = set()
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.upstream)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        for entry in self.manager.entries:
            await web.UnixSite(runner, entry["socket_path"]).start()
        public = TestServer(app)
        await public.start_server()
        self.addAsyncCleanup(public.close)
        self.server = TestServer(
            gateway.create_app(
                {
                    "public_host": PUBLIC,
                    "preview_domain": "example.com",
                    "opencode_port": public.port,
                },
                self.manager,
                PASSWORD,
            )
        )
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.transport = ClientSession(
            cookie_jar=DummyCookieJar(),
            auto_decompress=False,
            trust_env=False,
            timeout=ClientTimeout(total=10),
        )
        self.addAsyncCleanup(self.transport.close)
        # route.fulfill(303) is real browser navigation, but Playwright does not
        # route the following hop. Resolve controlled hosts ONLY to this local
        # TLS listener, keeping the URL's HTTPS authority and browser cookies.
        root = Path(temporary.name)
        certificate = await asyncio.create_subprocess_exec(
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(root / "key.pem"),
            "-out",
            str(root / "cert.pem"),
            "-days",
            "1",
            "-subj",
            "/CN=browser-fixture.invalid",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, errors = await certificate.communicate()
        self.assertEqual(certificate.returncode, 0, errors.decode())
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(root / "cert.pem", root / "key.pem")
        edge_app = web.Application()
        edge_app.router.add_route("*", "/{path:.*}", self.network_edge)
        self.tls_edge = TestServer(edge_app, scheme="https")
        await self.tls_edge.start_server(ssl=tls)
        self.addAsyncCleanup(self.tls_edge.close)
        playwright = await async_playwright().start()
        self.addAsyncCleanup(playwright.stop)
        self.browser = await playwright.chromium.launch(
            executable_path=os.environ["OPENCODE_PREVIEW_CHROMIUM"],
            headless=True,
            chromium_sandbox=False,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-quic",
                "--no-proxy-server",
                "--ignore-certificate-errors",  # Throwaway loopback TLS only.
                "--host-resolver-rules="
                + ", ".join(
                    [
                        f"MAP {host} 127.0.0.1:{self.tls_edge.port}"
                        for host in sorted(HOSTS)
                    ]
                    + ["MAP * ~NOTFOUND"]
                ),
            ],
        )
        self.addAsyncCleanup(self.browser.close)

    async def upstream(self, request):
        self.upstream_requests.append(
            (request.host, request.path, request.method, dict(request.headers))
        )
        if request.host == PUBLIC and request.headers.get("Authorization") != BASIC:
            return web.Response(status=401)
        if request.host == PUBLIC and request.path == "/":
            return web.Response(text=APP, content_type="text/html")
        if request.path == "/":
            response = web.Response(
                text=APP, content_type="text/html", headers=POLICY_HEADERS
            )
            # Exercise separate Set-Cookie fields, including a comma in Expires.
            response.headers.add(
                "Set-Cookie", "app=one; Domain=example.com; Path=/; Secure"
            )
            response.headers.add(
                "Set-Cookie",
                "other=two; Domain=.example.com; Path=/; Secure; HttpOnly; "
                "Expires=Wed, 21 Oct 2037 07:28:00 GMT",
            )
            return response
        return web.Response(text=f"socket-response:{request.host}:{request.method}")

    async def edge(self, route):
        request = route.request
        url = urlsplit(request.url)
        if url.scheme != "https" or url.netloc not in HOSTS:
            self.external_requests.append(request.url)
            await route.abort("blockedbyclient")
            return
        headers = await request.all_headers()
        self.assertEqual(headers.get("host", url.netloc), url.netloc)
        headers["host"] = url.netloc
        if not request.is_navigation_request():
            # fulfill() adds CORS allowances in Playwright. Use actual local
            # HTTPS for fetch so Chromium, not the fixture, enforces CORS.
            await route.continue_()
            return
        # Interception can precede Chromium adding Fetch Metadata. Fill only
        # missing metadata for this fixture's top-level documents;
        # never synthesize Origin or cookies, or weaken an existing header.
        # A target=_blank navigation may not have a frame yet. Unknown source
        # is conservatively cross-site; the gateway explicitly permits safe nav.
        same_origin = headers.get("origin") == f"https://{url.netloc}"
        metadata = {
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
            "sec-fetch-site": "same-origin" if same_origin else "cross-site",
        }
        for name, value in metadata.items():
            if name not in headers:
                headers[name] = value
                self.derived_metadata.add(name)
        path = url.path or "/"
        if url.query:
            path += "?" + url.query
        status, response_headers, body = await self.forward(
            url.netloc, path, request.method, headers, request.post_data_buffer
        )
        fulfilled_headers = {}
        for name in response_headers:
            # Playwright represents repeated response fields with LF, not
            # commas: combining Set-Cookie with commas loses cookies.
            fulfilled_headers[name.lower()] = "\n".join(response_headers.getall(name))
        await route.fulfill(status=status, headers=fulfilled_headers, body=body)

    async def network_edge(self, request):
        if request.host not in HOSTS:
            self.external_requests.append(str(request.rel_url))
            raise web.HTTPForbidden()
        headers = {name.lower(): value for name, value in request.headers.items()}
        status, headers, body = await self.forward(
            request.host,
            request.raw_path,
            request.method,
            headers,
            await request.read(),
        )
        return web.Response(status=status, headers=headers, body=body)

    async def forward(self, host, path, method, headers, body):
        # Simulate cached OpenCode Basic ONLY on the public authority, even for
        # hostile requests. A 403 must not merely result from absent credentials.
        if host == PUBLIC:
            headers["authorization"] = BASIC
        else:
            self.assertNotIn("authorization", headers)
        async with self.transport.request(
            method,
            URL(str(self.server.make_url(path)), encoded=True),
            headers=headers,
            data=body,
            allow_redirects=False,
        ) as response:
            body = await response.read()
            response_headers = response.headers.copy()
            for name in ("transfer-encoding", "connection", "content-length"):
                response_headers.popall(name, None)
            self.edge_requests.append(
                {
                    "host": host,
                    "path": urlsplit(path).path,
                    "method": method,
                    "headers": headers,
                    "status": response.status,
                    "response_headers": response_headers,
                    "cookies": response.headers.getall("Set-Cookie", []),
                }
            )
            return response.status, response_headers, body

    async def context(self, mobile=False):
        context = await self.browser.new_context(
            viewport={
                "width": 390 if mobile else 1440,
                "height": 844 if mobile else 900,
            },
            is_mobile=mobile,
            has_touch=mobile,
            service_workers="block",
        )
        self.addAsyncCleanup(context.close)
        context.set_default_timeout(10000)
        await context.route("**/*", self.edge)
        return context

    async def no_overflow(self, page):
        self.assertTrue(
            await page.evaluate(
                "Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)"
                " <= window.innerWidth"
            ),
            "Page overflows the viewport horizontally",
        )

    async def open_preview(self, context, directory, host):
        entry = self.manager.lookup(host)
        link = directory.get_by_role("link", name="Open preview").and_(
            directory.locator(f'a[href^="https://{host}/"]')
        )
        await expect(link).to_be_visible()
        self.assertEqual(await link.get_attribute("target"), "_blank")
        async with context.expect_page() as opened:
            await link.click()
        page = await opened.value
        await page.wait_for_url(f"https://{host}/")
        await expect(page.locator("#app")).to_have_text("Socket app rendered")
        await expect(page.locator("#app")).to_be_visible()
        self.assertTrue(await page.evaluate("window.isSecureContext"))
        async with asyncio.timeout(10):
            while True:
                cookies = await context.cookies(f"https://{host}/")
                auth = [c for c in cookies if c["name"] == gateway.COOKIE]
                if len(auth) == 1 and auth[0]["value"] == entry["token"]:
                    break
                await asyncio.sleep(0.02)
        cookie = auth[0]
        self.assertEqual(cookie["domain"], host)  # No leading dot: host-only.
        self.assertEqual(cookie["path"], "/")
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httpOnly"])
        self.assertEqual(cookie["sameSite"], "Lax")
        self.assertEqual(cookie["expires"], -1)
        self.assertNotIn(gateway.COOKIE, await page.evaluate("document.cookie"))
        self.assertNotIn(entry["token"], page.url)
        self.assertEqual(
            {c["name"]: c["value"] for c in cookies},
            {
                gateway.COOKIE: entry["token"],
                "app": "one",
                "other": "two",
            },
        )
        self.assertTrue(all(c["domain"] == host for c in cookies))
        login = next(
            r
            for r in reversed(self.edge_requests)
            if r["host"] == host and r["path"] == "/__preview_login"
        )
        self.assertEqual(login["status"], 303)
        self.assertEqual(login["response_headers"]["location"], "/")
        self.assertEqual(len(login["cookies"]), 1)
        self.assertNotIn("domain=", login["cookies"][0].lower())
        app = next(
            r
            for r in reversed(self.edge_requests)
            if r["host"] == host and r["path"] == "/"
        )
        self.assertEqual(app["status"], 200)
        for response in (login, app):
            for name in POLICY_HEADERS:
                self.assertNotIn(name.lower(), response["response_headers"])
        upstream = next(r for r in reversed(self.upstream_requests) if r[0] == host)
        self.assertNotIn(entry["token"], str(upstream))
        self.assertNotIn("Authorization", upstream[3])
        await self.no_overflow(page)
        return page

    async def directory_login(self, mobile):
        context = await self.context(mobile)
        page = await context.new_page()
        response = await page.goto(f"https://{FIRST}/")
        self.assertEqual(response.status, 403)
        self.assertEqual(self.upstream_requests, [])
        self.assertEqual(await context.cookies(), [])
        response = await page.goto(f"https://{PUBLIC}/previews")
        self.assertEqual(response.status, 200)
        await expect(
            page.get_by_role("heading", name="OpenCode previews")
        ).to_be_visible()
        await self.no_overflow(page)
        await self.open_preview(context, page, FIRST)
        self.assertEqual(await context.cookies(f"https://{SECOND}/"), [])
        self.assertEqual(await context.cookies(f"https://{PUBLIC}/"), [])
        self.assertEqual(self.external_requests, [])

    async def test_desktop_directory_login(self):
        await self.directory_login(mobile=False)

    async def test_mobile_directory_login(self):
        await self.directory_login(mobile=True)

    async def test_browser_origin_isolation_and_same_runtime_cors(self):
        context = await self.context()
        public = await context.new_page()
        await public.goto(f"https://{PUBLIC}/")
        # Positive control: the fake public backend accepts authenticated mutation.
        result = await public.evaluate("probe('/mutate', 'POST')")
        self.assertEqual(result["status"], 200)
        directory = await context.new_page()
        await directory.goto(f"https://{PUBLIC}/previews")
        pages = {}
        for host in (FIRST, SECOND, OTHER):
            pages[host] = await self.open_preview(context, directory, host)
        for host, method in ((OTHER, "GET"), (OTHER, "POST"), (PUBLIC, "POST")):
            with self.subTest(host=host, method=method):
                before = len(self.upstream_requests)
                start = len(self.edge_requests)
                result = await pages[FIRST].evaluate(
                    "([url, method]) => probe(url, method)",
                    [f"https://{host}/mutate", method],
                )
                self.assertEqual(result, {"error": "TypeError"})
                self.assertEqual(len(self.upstream_requests), before)
                requests = [
                    r for r in self.edge_requests[start:] if r["method"] == method
                ]
                self.assertEqual(len(requests), 1)
                denied = requests[0]
                self.assertEqual(denied["status"], 403)
                self.assertEqual(denied["headers"]["origin"], f"https://{FIRST}")
                self.assertEqual(denied["headers"]["sec-fetch-mode"], "cors")
                self.assertNotIn(
                    "access-control-allow-origin", denied["response_headers"]
                )
                if host == PUBLIC:
                    self.assertEqual(denied["headers"]["authorization"], BASIC)
                else:
                    self.assertIn(
                        self.manager.lookup(host)["token"], denied["headers"]["cookie"]
                    )
        # Simple credentialed requests exercise browser CORS with already issued
        # host-only cookies. Preflight policy is covered separately by unit tests.
        for source, target in ((FIRST, SECOND), (SECOND, FIRST)):
            for method in ("GET", "POST"):
                result = await pages[source].evaluate(
                    "([url, method]) => probe(url, method)",
                    [f"https://{target}/api", method],
                )
                self.assertEqual(
                    result,
                    {
                        "status": 200,
                        "body": f"socket-response:{target}:{method}",
                    },
                )
                allowed = self.edge_requests[-1]
                self.assertEqual(allowed["headers"]["origin"], f"https://{source}")
                self.assertEqual(allowed["headers"]["sec-fetch-mode"], "cors")
                self.assertIn(
                    self.manager.lookup(target)["token"], allowed["headers"]["cookie"]
                )
                self.assertEqual(
                    allowed["response_headers"]["access-control-allow-origin"],
                    f"https://{source}",
                )
                self.assertEqual(
                    allowed["response_headers"]["access-control-allow-credentials"],
                    "true",
                )
        self.assertEqual(self.external_requests, [])
        print(
            "Simulated edge Fetch Metadata fallback:",
            sorted(self.derived_metadata),
            flush=True,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
