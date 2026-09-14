"""Stdio MCP and real headless shell under the production AF_UNIX filter.

Runs inside the real SRT bwrap/seccomp sandbox. An offline proxy fixture exercises
authenticated CONNECT, NSS CA trust, rejection of invalid TLS, and exact bypass.
The session manager and public network are not exercised here.
"""

import base64
import errno
import http.server
import json
import os
import select
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

PAGE = b"<title>MCP preview</title><h1>Headless shell rendered</h1>"
AUTH = "Basic " + base64.b64encode(b"srt:fixture-secret").decode()


class BrowserTests(unittest.TestCase):
    def test_mcp_browser(self):
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            with self.assertRaises(OSError) as denied:
                socket.socket(socket.AF_UNIX, kind)
            self.assertIn(denied.exception.errno, (errno.EPERM, errno.EACCES))
        # Chromium's anonymous IPC is distinct from named Unix sockets.
        first, second = socket.socketpair()
        first.close()
        second.close()
        # The launcher uses this minimal namespace operation: it must preserve
        # SRT's read-only .git mounts and inherited no-new-privileges/seccomp.
        subprocess.run(
            [
                os.environ["OPENCODE_UNSHARE"],
                "--user",
                "--map-current-user",
                "--pid",
                "--fork",
                "--kill-child=SIGKILL",
                "--mount-proc",
                "--",
                sys.executable,
                "-c",
                """
import errno, os, socket
from pathlib import Path
assert os.getpid() == 1
assert 'NoNewPrivs:\\t1' in Path('/proc/self/status').read_text()
for operation in (lambda: socket.socket(socket.AF_UNIX),
                  lambda: Path('.git/config').open('w')):
    try:
        operation()
    except OSError as error:
        assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
    else:
        raise AssertionError('Inherited sandbox restriction was lost')
Path('namespace-write-probe').touch()
""",
            ],
            check=True,
        )

        with tempfile.TemporaryDirectory(
            prefix="mcp-browser-test-", dir="/tmp"
        ) as temporary:
            root = Path(temporary)
            private_home = root / "browser-home"
            private_home.mkdir(mode=0o700)
            ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            ca_name = x509.Name(
                [x509.NameAttribute(NameOID.COMMON_NAME, "MCP test CA")]
            )
            now = datetime.now(timezone.utc)
            ca = (
                x509.CertificateBuilder()
                .subject_name(ca_name)
                .issuer_name(ca_name)
                .public_key(ca_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1))
                .not_valid_after(now + timedelta(days=1))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
                .sign(ca_key, hashes.SHA256())
            )
            bundle = root / "trust-bundle.crt"
            bundle.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
            contexts = {}
            for name, trusted in (
                ("public.mcp.invalid", True),
                ("untrusted.mcp.invalid", False),
            ):
                key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
                cert = (
                    x509.CertificateBuilder()
                    .subject_name(subject)
                    .issuer_name(ca_name if trusted else subject)
                    .public_key(key.public_key())
                    .serial_number(x509.random_serial_number())
                    .not_valid_before(now - timedelta(days=1))
                    .not_valid_after(now + timedelta(days=1))
                    .add_extension(
                        x509.SubjectAlternativeName([x509.DNSName(name)]), False
                    )
                    .sign(ca_key if trusted else key, hashes.SHA256())
                )
                cert_path, key_path = root / f"{name}.crt", root / f"{name}.key"
                cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
                key_path.write_bytes(
                    key.private_bytes(
                        serialization.Encoding.PEM,
                        serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption(),
                    )
                )
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(cert_path, key_path)
                contexts[name] = context

            requests = []

            class Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass

                def do_GET(self):
                    if self.server.is_proxy:
                        requests.append(self.path)
                        self.send_error(403)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(PAGE)))
                    self.end_headers()
                    self.wfile.write(PAGE)

                def do_CONNECT(self):
                    if self.headers.get("Proxy-Authorization") != AUTH:
                        self.send_response(407)
                        self.send_header(
                            "Proxy-Authenticate", 'Basic realm="SRT fixture"'
                        )
                        self.end_headers()
                        return
                    requests.append(self.path)
                    self.send_response(200)
                    self.end_headers()
                    host = self.path.rsplit(":", 1)[0]
                    context = contexts.get(host, contexts["public.mcp.invalid"])
                    try:
                        with context.wrap_socket(
                            self.connection, server_side=True
                        ) as tls:
                            tls.settimeout(10)
                            incoming = b""
                            while b"\r\n\r\n" not in incoming:
                                chunk = tls.recv(4096)
                                if not chunk:
                                    return
                                incoming += chunk
                            tls.sendall(
                                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                                b"Connection: close\r\nContent-Length: "
                                + str(len(PAGE)).encode()
                                + b"\r\n\r\n"
                                + PAGE
                            )
                    except (ssl.SSLError, ConnectionError):
                        pass  # Expected for the invalid certificate controls.

            class IPv6Server(http.server.ThreadingHTTPServer):
                address_family = socket.AF_INET6

            servers = []
            for server_class, address, is_proxy in (
                (http.server.ThreadingHTTPServer, "127.0.0.1", False),
                (http.server.ThreadingHTTPServer, "127.0.0.1", True),
                (IPv6Server, "::1", False),
            ):
                server = server_class((address, 0), Handler)
                server.is_proxy = is_proxy
                servers.append(server)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                self.addCleanup(server.server_close)
                self.addCleanup(server.shutdown)
            local, proxy, local6 = servers
            env = dict(
                os.environ,
                SANDBOX_RUNTIME="1",
                OPENCODE_PLAYWRIGHT_HOME=str(private_home),
                HTTP_PROXY=f"http://srt:fixture-secret@127.0.0.1:{proxy.server_port}",
                SSL_CERT_FILE=str(bundle),
                NODE_EXTRA_CA_CERTS=str(bundle),
                NO_PROXY="*",
                GH_TOKEN="must-not-reach-browser",
                NODE_OPTIONS="--invalid-option-must-be-removed",
                PLAYWRIGHT_MCP_IGNORE_HTTPS_ERRORS="true",
                PLAYWRIGHT_MCP_PORT="9999",
                PLAYWRIGHT_MCP_CDP_ENDPOINT="http://127.0.0.1:1",
                PLAYWRIGHT_MCP_EXECUTABLE_PATH="/not/a/browser",
            )
            command = os.environ["OPENCODE_PLAYWRIGHT_MCP"]
            rejected = subprocess.run(
                [command, "--ignore-https-errors"], env=env, check=False
            )
            self.assertEqual(rejected.returncode, 64)
            rejected = subprocess.run(
                [command], env=dict(env, SANDBOX_RUNTIME=""), check=False
            )
            self.assertEqual(rejected.returncode, 77)
            alias = root / "home-alias"
            alias.symlink_to(private_home)
            for invalid in ("", "/tmp", str(alias), str(alias / ".." / "browser-home")):
                rejected = subprocess.run(
                    [command],
                    env=dict(env, OPENCODE_PLAYWRIGHT_HOME=invalid),
                    check=False,
                )
                self.assertEqual(rejected.returncode, 77)
            with ExitStack() as stack:
                mcp = stack.enter_context(
                    subprocess.Popen(
                        [command],
                        env=env,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                        start_new_session=True,
                    )
                )
                stack.callback(lambda: mcp.kill() if mcp.poll() is None else None)
                sequence = 0

                def request(method, params):
                    nonlocal sequence
                    sequence += 1
                    mcp.stdin.write(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": sequence,
                                "method": method,
                                "params": params,
                            }
                        )
                        + "\n"
                    )
                    mcp.stdin.flush()
                    deadline = time.monotonic() + 90
                    while time.monotonic() < deadline:
                        ready, _, _ = select.select([mcp.stdout], [], [], 1)
                        if not ready:
                            self.assertIsNone(mcp.poll(), "MCP exited before response")
                            continue
                        line = mcp.stdout.readline()
                        self.assertTrue(line, "MCP stdout closed")
                        response = json.loads(line)
                        if response.get("id") == sequence:
                            self.assertNotIn("error", response, response)
                            return response["result"]
                    self.fail(f"MCP request timed out: {method}")

                def tool(name, arguments, error=False):
                    result = request(
                        "tools/call", {"name": name, "arguments": arguments}
                    )
                    self.assertEqual(bool(result.get("isError")), error, result)
                    return result

                initialized = request(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "mcp-browser-test", "version": "1"},
                    },
                )
                self.assertIn("serverInfo", initialized)
                mcp.stdin.write(
                    '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
                )
                mcp.stdin.flush()
                tools = request("tools/list", {})
                self.assertIn(
                    "browser_take_screenshot", {t["name"] for t in tools["tools"]}
                )
                for host, port in (
                    ("127.0.0.1", local.server_port),
                    ("localhost", local.server_port),
                    ("[::1]", local6.server_port),
                ):
                    result = tool(
                        "browser_navigate",
                        {"url": f"http://{host}:{port}/"},
                    )
                    self.assertIn("MCP preview", json.dumps(result))
                    rendered = tool(
                        "browser_evaluate",
                        {"function": "() => document.querySelector('h1').textContent"},
                    )
                    self.assertIn("Headless shell rendered", json.dumps(rendered))
                self.assertEqual(requests, [], "Session loopback went through proxy")
                screenshot = tool("browser_take_screenshot", {"type": "png"})
                images = [
                    item for item in screenshot["content"] if item["type"] == "image"
                ]
                self.assertTrue(images, screenshot)
                self.assertTrue(
                    base64.b64decode(images[0]["data"]).startswith(b"\x89PNG\r\n\x1a\n")
                )

                for url in (
                    "http://10.0.0.1/",
                    "http://169.254.169.254/",
                    "http://127.0.0.2/",
                    "http://other.localhost/",
                ):
                    tool("browser_navigate", {"url": url})
                    self.assertIn(url, requests, "Non-allowlisted IP bypassed proxy")
                result = tool(
                    "browser_navigate", {"url": "https://public.mcp.invalid/"}
                )
                self.assertIn("MCP preview", json.dumps(result))
                self.assertIn("public.mcp.invalid:443", requests)
                for host in ("untrusted.mcp.invalid", "wrong-name.mcp.invalid"):
                    result = tool(
                        "browser_navigate", {"url": f"https://{host}/"}, error=True
                    )
                    self.assertIn("ERR_CERT_", json.dumps(result))

                child_env = dict(
                    entry.split("=", 1)
                    for entry in Path(f"/proc/{mcp.pid}/environ")
                    .read_text()
                    .split("\0")
                    if "=" in entry
                )
                self.assertEqual(Path(child_env["HOME"]), private_home)
                self.assertEqual(private_home.stat().st_mode & 0o777, 0o700)
                self.assertNotIn("GH_TOKEN", child_env)
                self.assertNotIn("NODE_OPTIONS", child_env)
                self.assertNotIn("PLAYWRIGHT_MCP_IGNORE_HTTPS_ERRORS", child_env)
                self.assertEqual(child_env["NO_PROXY"], "127.0.0.1,localhost,[::1]")
                tool("browser_close", {})
                mcp.stdin.close()
                self.assertEqual(mcp.wait(timeout=30), 0)
                self.assertTrue(private_home.exists(), "Backend-owned HOME was removed")

                # Reproduce backend cancellation while Chromium is alive in its
                # own detached process group. PID fds avoid PID-reuse races.
                cancelled_home = root / "cancelled-home"
                cancelled_home.mkdir(mode=0o700)
                mcp = stack.enter_context(
                    subprocess.Popen(
                        [command],
                        env=dict(env, OPENCODE_PLAYWRIGHT_HOME=str(cancelled_home)),
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                        start_new_session=True,
                    )
                )
                stack.callback(lambda: mcp.kill() if mcp.poll() is None else None)
                request(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "mcp-cancellation-test", "version": "1"},
                    },
                )
                mcp.stdin.write(
                    '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
                )
                mcp.stdin.flush()
                tool(
                    "browser_navigate",
                    {"url": f"http://127.0.0.1:{local.server_port}/"},
                )
                descendants = []
                detached_browser = False
                for entry in Path("/proc").iterdir():
                    if not entry.name.isdecimal():
                        continue
                    try:
                        environ = (entry / "environ").read_bytes().split(b"\0")
                        if f"HOME={cancelled_home}".encode() not in environ:
                            continue
                        pid = int(entry.name)
                        descriptor = os.pidfd_open(pid)
                        stack.callback(os.close, descriptor)
                        descendants.append(descriptor)
                        if b"chrome-headless-shell" in (entry / "cmdline").read_bytes():
                            detached_browser |= os.getpgid(pid) != mcp.pid
                    except (FileNotFoundError, PermissionError, ProcessLookupError):
                        continue
                self.assertTrue(detached_browser, "Did not observe detached Chromium")
                self.assertGreater(len(descendants), 3)
                os.killpg(mcp.pid, signal.SIGKILL)
                self.assertEqual(mcp.wait(timeout=30), -signal.SIGKILL)
                deadline = time.monotonic() + 30
                while descendants and time.monotonic() < deadline:
                    dead, _, _ = select.select(descendants, [], [], 1)
                    descendants = [fd for fd in descendants if fd not in dead]
                self.assertEqual(
                    descendants, [], "Detached browser survived cancellation"
                )
                self.assertTrue(
                    cancelled_home.exists(), "Backend-owned HOME was removed"
                )
                print(
                    "MCP handshake, local preview, PNG, proxy authentication, NSS trust, "
                    "TLS rejection, exact bypass and SIGKILL descendant cleanup passed "
                    "inside SRT with socket(AF_UNIX) denied."
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
