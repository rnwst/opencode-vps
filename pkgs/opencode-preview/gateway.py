"""Authenticated preview directory and streaming, loopback-only HTTP gateway.

The manager is imported only by main(). Tests can inject a manager implementing
list_previews(), lookup(host), and async stop(runtime_id).
"""

import asyncio
import base64
import binascii
import ctypes
import hmac
import html
import json
import os
import re
import secrets
import signal
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from aiohttp import (
    ClientSession,
    ClientTimeout,
    TCPConnector,
    TraceConfig,
    UnixConnector,
    WSMsgType,
    WSServerHandshakeError,
    web,
)
from multidict import CIMultiDict

COOKIE = "__Host-opencode-preview"
MAX_BODY = 64 * 1024 * 1024
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-auth",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
TOKEN_HEADER = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}


def setting(config, name, default=None):
    return (
        config.get(name, default)
        if isinstance(config, dict)
        else getattr(config, name, default)
    )


def equal(left, right):
    return (
        isinstance(left, str)
        and isinstance(right, str)
        and hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    )


def host_of(request):
    # The tunnel serves HTTPS on 443. Do not accept ambiguous authorities or
    # trust any client-supplied forwarding header when selecting an upstream.
    values = request.headers.getall("Host", [])
    if len(values) != 1:
        return None
    host = values[0].lower().removesuffix(":443")
    return host if HOST.fullmatch(host) else None


def filtered_headers(headers, *, preview=False, response=False):
    blocked = HOP_HEADERS | {
        item.strip().lower()
        for value in headers.getall("Connection", [])
        for item in value.split(",")
    }
    result = CIMultiDict()
    for name, value in headers.items():
        key = name.lower()
        if key in blocked:
            continue
        if not response and (
            key in {"host", "forwarded", "x-real-ip"}
            or key.startswith(("x-forwarded-", "cf-", "cloudflare-"))
        ):
            continue
        if preview and not response and key in {"authorization", "cookie"}:
            continue
        if (
            preview
            and response
            and key
            in {
                "set-cookie",
                "clear-site-data",
                "www-authenticate",
                "service-worker-allowed",
                "content-security-policy",
                "content-security-policy-report-only",
                "cross-origin-opener-policy",
                "referrer-policy",
                "cache-control",
                "expires",
                "access-control-allow-origin",
                "access-control-allow-credentials",
                "access-control-allow-headers",
                "access-control-allow-methods",
                "access-control-expose-headers",
                "access-control-max-age",
                "refresh",
                # Apps must not persist reporting or transport policy for login URLs.
                "nel",
                "report-to",
                "reporting-endpoints",
                "alt-svc",
            }
        ):
            continue
        result.add(name, value)
    if preview and not response:
        # Preserve app cookies verbatim, but never send the gateway capability.
        cookies = [
            part.strip()
            for value in headers.getall("Cookie", [])
            for part in value.split(";")
            if part.strip() and part.split("=", 1)[0].strip() != COOKIE
        ]
        if cookies:
            result["Cookie"] = "; ".join(cookies)
    return result


def app_cookies(headers):
    for value in headers.getall("Set-Cookie", []):
        parts = value.split(";")
        name = parts[0].split("=", 1)[0].strip()
        if not TOKEN_HEADER.fullmatch(name) or name == COOKIE or "=" not in parts[0]:
            continue
        # Treat every Set-Cookie separately; Expires attributes contain commas.
        yield ";".join(
            part
            for i, part in enumerate(parts)
            if i == 0 or part.split("=", 1)[0].strip().lower() != "domain"
        )


def preview_headers(headers, origin=None):
    headers["Cache-Control"] = "no-store"
    headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    headers["Cross-Origin-Opener-Policy"] = "same-origin"
    headers["Referrer-Policy"] = "no-referrer"
    headers["X-Frame-Options"] = "DENY"
    if origin:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Access-Control-Allow-Credentials"] = "true"
        headers.add("Vary", "Origin")


class Gateway:
    def __init__(self, config, manager, password):
        self.config = config
        self.manager = manager
        self.password = password
        self.public_host = setting(config, "public_host").lower()
        self.csrf = secrets.token_urlsafe(32)

    def origin_policy(self, request, host, entry=None):
        origins = request.headers.getall("Origin", [])
        if len(origins) > 1 or origins == ["null"]:
            return False, None
        origin = origins[0] if origins else None
        if origin == "https://" + host:
            return True, None
        if origin and entry:
            parsed = urlsplit(origin)
            if (
                parsed.scheme == "https"
                and parsed.netloc
                and origin == "https://" + parsed.netloc
            ):
                other = self.manager.lookup(parsed.netloc)
                if (
                    other
                    and other["runtime_id"] == entry["runtime_id"]
                    and other.get("available")
                ):
                    return True, origin
        # App redirects retain the initiating navigation's cross-origin metadata.
        # Preview documents can use any path, but still require their host cookie.
        navigation = (
            request.method == "GET"
            and (entry is not None or request.path in {"/", "/previews"})
            and request.headers.get("Sec-Fetch-Mode") == "navigate"
            and request.headers.get("Sec-Fetch-Dest") == "document"
        )
        if navigation:
            return True, None
        if origin:
            return False, None
        fetch_headers = any(
            key.lower().startswith("sec-fetch-") for key in request.headers
        )
        if not fetch_headers:
            return True, None  # Native clients still need the upstream's auth.
        return request.headers.get("Sec-Fetch-Site") == "same-origin", None

    def basic_auth(self, request):
        header = request.headers.get("Authorization", "")
        try:
            scheme, encoded = header.split(" ", 1)
            if scheme.lower() != "basic":
                return False
            user, password = (
                base64.b64decode(encoded, validate=True).decode("utf-8").split(":", 1)
            )
            return equal(user, "opencode") & equal(password, self.password)
        except (ValueError, UnicodeError, binascii.Error):
            return False

    async def handle(self, request):
        host = host_of(request)
        if not host:
            raise web.HTTPNotFound(text="Not found")
        entry = None if host == self.public_host else self.manager.lookup(host)
        if host != self.public_host and not entry:
            raise web.HTTPNotFound(text="Not found")
        if request.content_length is not None and request.content_length > MAX_BODY:
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_BODY, actual_size=request.content_length
            )
        if entry:
            if any(
                value.strip().lower() == "script"
                for value in request.headers.getall("Service-Worker", [])
            ):
                raise web.HTTPForbidden(text="Forbidden")
            if not entry.get("available"):
                raise web.HTTPServiceUnavailable(text="Preview unavailable")
            if request.path == "/__preview_login":
                return self.login(request, entry)
        allowed, cors_origin = self.origin_policy(request, host, entry)
        if not allowed:
            raise web.HTTPForbidden(text="Forbidden")
        if host == self.public_host:
            if request.path == "/previews" or request.path.startswith("/previews/"):
                return await self.directory(request)
            return await self.proxy(request, host)
        if (
            request.method == "OPTIONS"
            and "Access-Control-Request-Method" in request.headers
        ):
            return self.preflight(request, cors_origin)
        if not equal(request.cookies.get(COOKIE), entry["token"]):
            raise web.HTTPForbidden(text="Forbidden")
        return await self.proxy(request, host, entry, cors_origin)

    def login(self, request, entry):
        if request.method != "GET" or not equal(
            request.query.get("token"), entry["token"]
        ):
            raise web.HTTPForbidden(text="Forbidden")
        response = web.Response(status=303, headers={"Location": "/"})
        response.set_cookie(
            COOKIE, entry["token"], secure=True, httponly=True, samesite="Lax", path="/"
        )
        preview_headers(response.headers)
        return response

    def preflight(self, request, origin):
        method = request.headers.get("Access-Control-Request-Method", "")
        names = [
            name.strip().lower()
            for name in request.headers.get("Access-Control-Request-Headers", "").split(
                ","
            )
            if name.strip()
        ]
        safe = {
            "accept",
            "accept-language",
            "content-language",
            "content-type",
            "range",
            "if-match",
            "if-none-match",
        }
        if (
            not origin
            or method not in METHODS
            or any(
                not TOKEN_HEADER.fullmatch(name)
                or not (name in safe or name.startswith("x-"))
                or name.startswith(("x-forwarded-", "x-real-", "x-cloudflare-"))
                for name in names
            )
        ):
            raise web.HTTPForbidden(text="Forbidden")
        response = web.Response(status=204)
        preview_headers(response.headers, origin)
        response.headers["Access-Control-Allow-Methods"] = method
        if names:
            response.headers["Access-Control-Allow-Headers"] = ", ".join(names)
        response.headers.add(
            "Vary", "Access-Control-Request-Method, Access-Control-Request-Headers"
        )
        return response

    async def directory(self, request):
        if not self.basic_auth(request):
            raise web.HTTPUnauthorized(
                text="Authentication required",
                headers={
                    "WWW-Authenticate": 'Basic realm="OpenCode previews", charset="UTF-8"'
                },
            )
        if request.path.startswith("/previews/stop/") and request.method == "POST":
            if request.headers.get("Origin") != "https://" + self.public_host:
                raise web.HTTPForbidden(text="Forbidden")
            if request.content_length is None or request.content_length > 4096:
                raise web.HTTPForbidden(text="Forbidden")
            form = await request.post()
            if not equal(form.get("csrf"), self.csrf):
                raise web.HTTPForbidden(text="Forbidden")
            runtime_id = request.path[len("/previews/stop/") :]
            if not any(
                item["runtime_id"] == runtime_id
                for item in self.manager.list_previews()
            ):
                raise web.HTTPNotFound(text="Not found")
            await self.manager.stop(runtime_id)
            return web.Response(status=303, headers={"Location": "/previews"})
        if request.path != "/previews":
            raise web.HTTPNotFound(text="Not found")
        if request.method != "GET":
            raise web.HTTPMethodNotAllowed(request.method, ["GET"])
        rows = []
        esc = lambda value: html.escape(str(value), quote=True)
        for entry in self.manager.list_previews():
            hostname = entry["hostname"]
            available = entry.get("available", False)
            link = "Unavailable"
            if available and HOST.fullmatch(hostname):
                url = (
                    "https://"
                    + hostname
                    + "/__preview_login?token="
                    + quote(entry["token"], safe="")
                )
                link = f'<a target="_blank" rel="noopener noreferrer" href="{esc(url)}">Open preview</a>'
            rows.append(
                "<article><div><h2>"
                + esc(entry["workspace"])
                + '</h2><p class="path">'
                + esc(entry["directory"])
                + "</p><p>Session <code>"
                + esc(entry["session_id"])
                + "</code> &middot; Port <strong>"
                + esc(entry["port"])
                + "</strong> &middot; "
                + ("Available" if available else "Unavailable")
                + '</p></div><div class="actions">'
                + link
                + '<form method="post" action="/previews/stop/'
                + quote(entry["runtime_id"], safe="")
                + '"><input type="hidden" name="csrf" value="'
                + esc(self.csrf)
                + '"><button type="submit">Stop / reset session</button></form></div></article>'
            )
        body = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>OpenCode previews</title>
<style>body{font:16px system-ui,sans-serif;color:#18212b;background:#f4f5f2;margin:0;padding:clamp(1rem,4vw,3rem)}
main{max-width:64rem;margin:auto}header{border-bottom:3px solid #18212b;margin-bottom:1.5rem}h1{font-size:2rem}
article{display:flex;justify-content:space-between;gap:1.5rem;padding:1.25rem 0;border-bottom:1px solid #bbc2c6}
h2{font-size:1.1rem;margin:0}p{line-height:1.5}.path,code{overflow-wrap:anywhere}.actions{flex-shrink:0}
a{color:#075a72}button{margin-top:1rem;padding:.6rem;border:1px solid #697b83;background:white;cursor:pointer}
@media(max-width:600px){article{display:block}.actions{margin-top:1rem}}</style>
<main><header><h1>OpenCode previews</h1><p>Persistent development sessions. <a href="/previews">Refresh</a></p></header>"""
        body += (
            "".join(rows)
            if rows
            else "<p>No previews yet. Run an ordinary server command in your workspace, then refresh this page. Listening ports are exposed automatically.</p>"
        )
        response = web.Response(text=body + "</main></html>", content_type="text/html")
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    def response_headers(self, upstream, host, entry, origin):
        headers = filtered_headers(upstream.headers, preview=bool(entry), response=True)
        if entry:
            for cookie in app_cookies(upstream.headers):
                headers.add("Set-Cookie", cookie)
            if "Location" in headers:
                location = headers["Location"]
                if "\\" in location or any(
                    ord(char) < 32 or ord(char) == 127 for char in location
                ):
                    raise web.HTTPBadGateway(text="Upstream redirect rejected")
                parsed = urlsplit(location)
                target = parsed.hostname
                if target in {"localhost", "127.0.0.1", "::1", host}:
                    headers["Location"] = urlunsplit(
                        ("https", host, parsed.path, parsed.query, parsed.fragment)
                    )
                elif target and (
                    target == self.public_host or self.manager.lookup(target)
                ):
                    del headers["Location"]
                    # A preview must not trigger authenticated control-plane navigation.
                    raise web.HTTPBadGateway(text="Upstream redirect rejected")
            preview_headers(headers, origin)
        return headers

    async def proxy(self, request, host, entry=None, origin=None):
        headers = filtered_headers(request.headers, preview=bool(entry))
        headers["Host"] = host
        headers["X-Forwarded-Host"] = host
        headers["X-Forwarded-Proto"] = "https"
        connector = (
            UnixConnector(path=str(entry["socket_path"])) if entry else TCPConnector()
        )
        base = (
            "http://localhost"
            if entry
            else "http://127.0.0.1:" + str(setting(self.config, "opencode_port", 4080))
        )
        # raw_path preserves OpenCode auth_token and PTY ticket query semantics.
        from yarl import URL

        url = URL(base + request.raw_path, encoded=True)
        timeout = ClientTimeout(
            total=None,
            sock_connect=10,
            sock_read=setting(self.config, "stream_timeout", None),
        )

        async def no_redirect(session, context, params):
            # ws_connect otherwise follows redirects, including to arbitrary
            # loopback services. HTTP redirects are returned, never followed.
            raise web.HTTPBadGateway(text="Upstream redirect rejected")

        trace = TraceConfig()
        trace.on_request_redirect.append(no_redirect)
        async with ClientSession(
            connector=connector,
            timeout=timeout,
            auto_decompress=False,
            trust_env=False,
            trace_configs=[trace],
        ) as session:
            if request.headers.get("Upgrade", "").lower() == "websocket":
                return await self.websocket(
                    request, session, url, headers, host, entry, origin
                )

            body_size = 0

            async def body():
                nonlocal body_size
                async for chunk in request.content.iter_chunked(65536):
                    body_size += len(chunk)
                    if body_size > MAX_BODY:
                        raise web.HTTPRequestEntityTooLarge(
                            max_size=MAX_BODY, actual_size=body_size
                        )
                    yield chunk

            try:
                # OpenCode shell/prompt APIs can wait for work before sending headers.
                async with asyncio.timeout(
                    setting(self.config, "handshake_timeout", 30)
                    if entry is not None
                    else None
                ):
                    upstream = await session.request(
                        request.method,
                        url,
                        headers=headers,
                        data=body() if request.can_read_body else None,
                        allow_redirects=False,
                        skip_auto_headers={
                            "Accept-Encoding",
                            "Content-Type",
                            "User-Agent",
                        },
                    )
            except Exception:
                if body_size > MAX_BODY:
                    raise web.HTTPRequestEntityTooLarge(
                        max_size=MAX_BODY, actual_size=body_size
                    ) from None
                raise
            async with upstream:
                response = web.StreamResponse(
                    status=upstream.status,
                    headers=self.response_headers(upstream, host, entry, origin),
                )
                await response.prepare(request)
                try:
                    async for chunk in upstream.content.iter_chunked(65536):
                        await response.write(chunk)
                    await response.write_eof()
                except (Exception, asyncio.CancelledError):
                    # Once headers have been sent, terminate the stream, not a
                    # second HTTP response. Never log URLs or exception strings.
                    if request.transport:
                        request.transport.close()
                    raise
                return response

    async def websocket(self, request, session, url, headers, host, entry, origin):
        protocols = [
            p.strip()
            for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",")
            if p.strip()
        ]
        for key in list(headers):
            if key.lower().startswith("sec-websocket-"):
                del headers[key]
        try:
            async with asyncio.timeout(setting(self.config, "handshake_timeout", 30)):
                upstream = await session.ws_connect(
                    url,
                    headers=headers,
                    protocols=protocols,
                    autoping=False,
                    autoclose=False,
                    max_msg_size=MAX_BODY,
                )
        except WSServerHandshakeError as exc:
            response_headers = self.response_headers(exc, host, entry, origin)
            for key in ("Content-Length", "Content-Encoding", "Content-Type"):
                response_headers.popall(key, None)
            return web.Response(
                status=exc.status if 400 <= exc.status < 600 else 502,
                text="WebSocket unavailable",
                headers=response_headers,
            )
        async with upstream:
            downstream = web.WebSocketResponse(
                protocols=[upstream.protocol] if upstream.protocol else [],
                autoping=False,
                autoclose=False,
                max_msg_size=MAX_BODY,
            )
            # ws_connect validates the upstream handshake; its HTTP response is
            # retained by aiohttp for headers such as application Set-Cookie.
            response_headers = self.response_headers(
                upstream._response, host, entry, origin
            )
            for key, value in response_headers.items():
                if (
                    not key.lower().startswith("sec-websocket-")
                    and key.lower() != "content-length"
                ):
                    downstream.headers.add(key, value)
            await downstream.prepare(request)

            async def relay(source, destination):
                while True:
                    message = await source.receive()
                    if message.type == WSMsgType.TEXT:
                        await destination.send_str(message.data)
                    elif message.type == WSMsgType.BINARY:
                        await destination.send_bytes(message.data)
                    elif message.type == WSMsgType.PING:
                        await destination.ping(message.data)
                    elif message.type == WSMsgType.PONG:
                        await destination.pong(message.data)
                    elif message.type == WSMsgType.CLOSE:
                        code = (
                            message.data
                            if isinstance(message.data, int) and message.data != 1006
                            else 1001
                        )
                        await destination.close(
                            code=code, message=(message.extra or "").encode("utf-8")
                        )
                        return
                    else:
                        await destination.close(
                            code=1011 if message.type == WSMsgType.ERROR else 1001
                        )
                        return

            tasks = [
                asyncio.create_task(relay(downstream, upstream)),
                asyncio.create_task(relay(upstream, downstream)),
            ]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await downstream.close()
            return downstream


def create_app(config, manager, password):
    gateway = Gateway(config, manager, password)

    @web.middleware
    async def errors(request, handler):
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = web.Response(
                status=exc.status, headers=exc.headers, body=exc.body
            )
        except Exception:  # noqa: BLE001 - exceptions can contain bearer URLs
            response = web.Response(status=502, text="Upstream unavailable")
        host = host_of(request)
        if host and host != gateway.public_host:
            preview_headers(response.headers)
        elif request.path == "/previews" or request.path.startswith("/previews/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        return response

    # Preserve encoded request bytes and Content-Length together. Decompressing
    # here while forwarding Content-Encoding would corrupt the upstream framing.
    app = web.Application(
        client_max_size=MAX_BODY,
        middlewares=[errors],
        handler_args={"auto_decompress": False, "handler_cancellation": True},
    )
    app.router.add_route("*", "/{path:.*}", gateway.handle)
    return app


def disable_dumping():
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
            raise RuntimeError("Cannot disable process dumps")


async def serve(config):
    from manager import Manager

    credential = Path(os.environ["CREDENTIALS_DIRECTORY"]) / "server-password"
    password = credential.read_text().rstrip("\r\n")
    if not password:
        raise RuntimeError("Missing server credential")
    manager = Manager(config)
    runners = []
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    installed = []
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stopped.set)
            installed.append(sig)
        await manager.start()
        # No access or request exception logs: both may contain bearer URLs.
        public = web.AppRunner(
            create_app(config, manager, password), access_log=None, logger=QuietLogger()
        )
        runners.append(public)
        await public.setup()
        await web.TCPSite(
            public, "127.0.0.1", int(setting(config, "listen_port"))
        ).start()
        control = web.AppRunner(
            manager.create_control_app(), access_log=None, logger=QuietLogger()
        )
        runners.append(control)
        await control.setup()
        socket = Path(setting(config, "runtime_root")) / "control.sock"
        socket.unlink(missing_ok=True)
        # Set restrictive permissions at bind time, not only after a chmod race.
        mask = os.umask(0o177)
        try:
            await web.UnixSite(control, str(socket)).start()
        finally:
            os.umask(mask)
        socket.chmod(0o600)
        await stopped.wait()
    finally:
        try:
            await manager.close()
        finally:
            for runner in reversed(runners):
                await runner.cleanup()
            for sig in installed:
                loop.remove_signal_handler(sig)


class QuietLogger:
    """aiohttp's parser/transport errors must not render untrusted request data."""

    def debug(self, *args, **kwargs):
        pass

    info = warning = error = exception = debug


def main():
    disable_dumping()
    try:
        with open(sys.argv[1], encoding="utf-8") as source:
            config = json.load(source)
        asyncio.run(serve(config))
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 - never print secret-bearing exceptions
        # Exception messages can contain upstream request URLs or credentials.
        print("Preview gateway stopped", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
