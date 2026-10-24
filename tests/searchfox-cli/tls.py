"""Exercise certificate trust using the installed CLI and a private test CA."""

import http.server
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading


def openssl(*args):
    subprocess.run(
        ["openssl", *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        assert self.path == "/mozilla-central/source/fixture.cpp", self.path
        body = b'<code class="source-line">trusted fixture</code>'
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    for name in ("proxy", "unrelated"):
        openssl(
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            f"/CN={name}",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-keyout",
            str(root / f"{name}.key"),
            "-out",
            str(root / f"{name}.pem"),
        )
    openssl(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-subj",
        "/CN=127.0.0.1",
        "-keyout",
        str(root / "server.key"),
        "-out",
        str(root / "server.csr"),
    )
    (root / "extensions").write_text(
        "subjectAltName=IP:127.0.0.1\n"
        "basicConstraints=critical,CA:FALSE\n"
        "extendedKeyUsage=serverAuth\n"
    )
    openssl(
        "x509",
        "-req",
        "-days",
        "1",
        "-in",
        str(root / "server.csr"),
        "-CA",
        str(root / "proxy.pem"),
        "-CAkey",
        str(root / "proxy.key"),
        "-CAcreateserial",
        "-extfile",
        str(root / "extensions"),
        "-out",
        str(root / "server.pem"),
    )
    # The relevant CA need not be the first certificate in the runtime bundle.
    (root / "bundle.pem").write_bytes(
        (root / "unrelated.pem").read_bytes() + (root / "proxy.pem").read_bytes()
    )
    (root / "invalid.pem").write_text("not a certificate\n")

    for port in (3000, 3001, 3002, 5173):
        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    else:
        raise RuntimeError("No permitted test port is available")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(root / "server.pem", root / "server.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def run(bundle=None, host="127.0.0.1"):
        env = dict(os.environ)
        for key in list(env):
            if key.lower().endswith("_proxy") or key == "SSL_CERT_FILE":
                del env[key]
        env["SEARCHFOX_BASE_URL"] = f"https://{host}:{port}"
        env["XDG_CACHE_HOME"] = str(root / "cache")
        if bundle is not None:
            env["SSL_CERT_FILE"] = str(root / bundle)
        return subprocess.run(
            ["searchfox-cli", "--get-file", "fixture.cpp", "--no-cache"],
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
        )

    try:
        for bundle in (None, "unrelated.pem"):
            result = run(bundle)
            assert result.returncode != 0, result
            assert "UnknownIssuer" in result.stderr, result.stderr
        result = run("bundle.pem")
        assert result.returncode == 0, result.stderr
        assert "trusted fixture" in result.stdout, result.stdout
        result = run("bundle.pem", host="localhost")
        assert result.returncode != 0, result
        assert "certificate not valid for name" in result.stderr, result.stderr
        for bundle in ("missing.pem", "invalid.pem"):
            result = run(bundle)
            assert result.returncode != 0, result
            assert "SSL_CERT_FILE" in result.stderr, result.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

print("searchfox-cli TLS trust checks passed")
