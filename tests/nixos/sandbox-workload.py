"""VM-only fixtures and assertions; uses the real wrapper, proxies and TLS."""

import base64
import errno
import http.server
import json
import os
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit


def denied(operation, allowed=(errno.EACCES, errno.EPERM, errno.ENOENT)):
    try:
        operation()
    except OSError as error:
        assert error.errno in allowed, error
    else:
        raise AssertionError("Forbidden operation succeeded")


def curl(url, *args, success=True):
    result = subprocess.run(
        ["curl", "--silent", "--show-error", "--fail", "--max-time", "8", *args, url],
        capture_output=True,
        text=True,
        check=False,
    )
    if success:
        assert result.returncode == 0, (url, args, result.stderr)
        return result.stdout
    # Policy rejection, not a DNS failure, dead fixture or timeout.
    assert result.returncode in (22, 52, 56, 97), (url, args, result)


mode = sys.argv[1]
fixture = Path("/run/sandbox-fixture")
if mode == "serve":
    token = (fixture / "private/token").read_text().strip()

    class HTTP(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                body = b"fixture"
            elif self.server.server_port == 443:
                expected = "Bearer " + token
                if self.headers["Host"] == "github.com":
                    expected = (
                        "Basic "
                        + base64.b64encode(
                            ("x-access-token:" + token).encode()
                        ).decode()
                    )
                assert self.headers["Authorization"] == expected
                with (fixture / "events").open("a") as log:
                    log.write(self.headers["Host"] + " authenticated\n")
                body = b"authenticated"
            else:
                # A forbidden request reaching the fixture fails the final log check.
                with (fixture / "events").open("a") as log:
                    log.write("private request reached fixture\n")
                body = b"private"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

    class Unix(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.sendall(b"host-unix")

    unix = socketserver.UnixStreamServer(str(fixture / "host.sock"), Unix)
    os.chmod(fixture / "host.sock", 0o777)
    servers = [unix]
    for port in (8080, 8081, 443):
        server = http.server.ThreadingHTTPServer(("0.0.0.0", port), HTTP)
        if port == 443:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(fixture / "leaf.crt", fixture / "private/leaf.key")
            server.socket = context.wrap_socket(server.socket, server_side=True)
        servers.append(server)
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Event().wait()
elif mode == "outside":
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(fixture / "host.sock"))
        assert client.recv(32) == b"host-unix"
elif mode == "disclose":
    workspace, temporary = map(Path, sys.argv[2:])
    brokers = list(temporary.glob(".srt-broker.*"))
    assert len(brokers) == 1, brokers
    files = [p for p in brokers[0].rglob("*") if p.is_file()]
    keys = [p for p in files if p.name == "ca.key"]
    configs = [p for p in files if "settings" in p.name]
    assert keys and len(configs) == 1, files
    config = json.loads(configs[0].read_text())
    helper = Path(config["seccomp"]["applyPath"])
    assert helper.is_relative_to("/nix/store") and os.access(helper, os.X_OK)
    with helper.open("rb") as binary:
        assert binary.read(4) == b"\x7fELF", helper
    config["seccomp"]["applyPath"] = "/no-such-apply-seccomp"
    (fixture / "missing.json").write_text(json.dumps(config))
    certs = [p for p in files if p.name == "ca.crt"]
    assert certs, files
    manifest = {
        "broker": str(brokers[0]),
        "hidden": list(map(str, keys + configs)),
        "certs": list(map(str, certs)),
    }
    (workspace / "manifest.new").write_text(json.dumps(manifest))
    (workspace / "manifest.new").rename(workspace / "manifest.json")
elif mode == "verify":
    workspace, temporary = map(Path, sys.argv[2:])
    assert not list(temporary.glob(".srt-broker.*"))
    secret = (fixture / "private/token").read_text().strip()
    child_env = (workspace / "child-env.json").read_text()
    assert secret not in child_env
    assert (
        base64.b64encode(("x-access-token:" + secret).encode()).decode()
        not in child_env
    )
    assert json.loads(child_env)["GH_TOKEN"]
elif mode == "inside":
    assert os.environ["TMPDIR"] == "/tmp/claude"
    Path("child-env.json").write_text(json.dumps(dict(os.environ)))
    # AF_UNIX creation is forbidden even though the pathname itself is visible.
    assert (fixture / "host.sock").is_socket()
    for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        denied(
            lambda kind=kind: socket.socket(socket.AF_UNIX, kind),
            (errno.EPERM, errno.EACCES),
        )
    with socket.socket(socket.AF_INET) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with socket.create_connection(listener.getsockname(), timeout=5) as client:
            peer, _ = listener.accept()
            with peer:
                client.sendall(b"loopback")
                assert peer.recv(8) == b"loopback"

    Path("ready").touch()
    for _ in range(600):
        if Path("manifest.json").exists():
            break
        time.sleep(0.1)
    manifest = json.loads(Path("manifest.json").read_text())
    broker = Path(manifest["broker"])
    for name in manifest["hidden"]:
        path = Path(name)
        # Try the original path and both former session-/tmp alias layouts.
        for candidate in (
            path,
            Path("/var/tmp") / path.relative_to(broker),
            Path("/tmp") / path.relative_to(broker),
            Path("/tmp") / broker.name / path.relative_to(broker),
        ):
            denied(candidate.read_bytes)
    bundle = Path(os.environ["SSL_CERT_FILE"])
    assert bundle.is_relative_to("/var/tmp"), bundle
    assert b"BEGIN CERTIFICATE" in bundle.read_bytes()
    assert b"PRIVATE KEY" not in bundle.read_bytes()
    denied(lambda: bundle.open("ab"), (errno.EROFS, errno.EACCES, errno.EPERM))
    denied(bundle.unlink, (errno.EROFS, errno.EACCES, errno.EPERM, errno.EBUSY))
    for cert in manifest["certs"]:
        public_path = Path("/var/tmp") / Path(cert).relative_to(broker)
        assert b"BEGIN CERTIFICATE" in public_path.read_bytes()
    Path("/tmp/persist").write_text("session-one")

    assert (
        curl(
            "https://api.github.com/auth",
            "-H",
            "Authorization: Bearer " + os.environ["GH_TOKEN"],
        )
        == "authenticated"
    )
    assert (
        curl("https://github.com/auth", "-H", os.environ["GIT_CONFIG_VALUE_0"])
        == "authenticated"
    )
    proxy = os.environ["HTTP_PROXY"]
    socks = "socks5h://" + urlsplit(os.environ["ALL_PROXY"]).netloc
    for options in (
        ("--proxy", proxy),
        ("--proxy", proxy, "--proxytunnel"),
        ("--proxy", socks),
    ):
        # Explicitly override NO_PROXY, including the generated private ranges.
        assert (
            curl("http://github.com:8080/health", "--noproxy", "", *options)
            == "fixture"
        )
        for host in ("127.0.0.1", "10.55.0.1", "169.254.169.254", "private.github.com"):
            curl(
                f"http://{host}:8081/forbidden",
                "--noproxy",
                "",
                *options,
                success=False,
            )
    curl(
        "https://private.github.com/forbidden",
        "--noproxy",
        "",
        "--proxy",
        proxy,
        success=False,
    )
    # Bypassing the proxy cannot reach the host's otherwise live listener.
    direct = subprocess.run(
        [
            "curl",
            "--noproxy",
            "*",
            "--max-time",
            "2",
            "http://10.55.0.1:8081/forbidden",
        ],
        check=False,
    )
    assert direct.returncode in (7, 28), direct.returncode
else:
    raise AssertionError(mode)
