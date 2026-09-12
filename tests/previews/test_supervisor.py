"""Protocol tests bypass only the native-wrapper attestation, never production main.

Run: python3 -B -m unittest discover -s tests/previews -p test_supervisor.py -v
The real namespace/capability/seccomp launch belongs in the wrapper VM tests.
"""

import base64
import hashlib
import importlib.util
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).resolve().parents[2] / "pkgs/opencode-preview/supervisor.py"
SPEC = importlib.util.spec_from_file_location("preview_supervisor", SOURCE)
supervisor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = supervisor
SPEC.loader.exec_module(supervisor)

LAUNCHER = """
import ctypes, importlib.util, sys
spec = importlib.util.spec_from_file_location('preview_supervisor', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
libc = ctypes.CDLL(None)
assert libc.prctl(38, 1, 0, 0, 0) == 0
assert libc.prctl(4, 0, 0, 0, 0) == 0
assert libc.prctl(3, 0, 0, 0, 0) == 0
module.Supervisor().run()
"""


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        self.frames = queue.Queue()
        self.pending = []
        self.sequence = 0
        # Deliberately inheritable: close_fds must remove this in commands.
        self.extra = os.open(self.temp.name, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.extra)
        self.process = subprocess.Popen(
            [sys.executable, "-B", "-I", "-S", "-c", LAUNCHER, str(SOURCE)],
            cwd=self.workspace,
            env={"PATH": os.defpath, "MASKED_TEST": "allowed"},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(self.extra,),
        )
        self.addCleanup(self.stop)

        def collect():
            try:
                for line in self.process.stdout:
                    self.frames.put(json.loads(line))
            finally:
                self.frames.put(None)

        self.reader = threading.Thread(target=collect, daemon=True)
        self.reader.start()
        ready = self.event("ready")
        self.assertEqual(ready["pid"], self.process.pid)
        heartbeat = self.event("ports")
        self.assertIsInstance(heartbeat["ports"], list)
        self.assertIs(type(heartbeat["processes"]), int)
        self.assertGreaterEqual(heartbeat["processes"], 0)

    def stop(self):
        if not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        self.process.stdout.close()
        self.process.stderr.close()

    def send(self, request):
        self.process.stdin.write(json.dumps(request).encode() + b"\n")
        self.process.stdin.flush()

    def event(self, event, id=None, timeout=5):
        deadline = time.monotonic() + timeout
        while True:
            for i, frame in enumerate(self.pending):
                if frame.get("event") == event and (
                    id is None or frame.get("id") == id
                ):
                    return self.pending.pop(i)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail(f"timeout waiting for {event}/{id}: {self.pending}")
            try:
                frame = self.frames.get(timeout=remaining)
            except queue.Empty:
                self.fail(f"timeout waiting for {event}/{id}: {self.pending}")
            if frame is None:
                self.fail(f"supervisor exited while waiting for {event}/{id}")
            self.pending.append(frame)

    def execute(self, code, cwd=None):
        self.sequence += 1
        id = format(self.sequence, "x")
        self.send(
            {
                "op": "exec",
                "id": id,
                "argv": [sys.executable, "-I", "-S", "-c", code],
                "cwd": str(cwd or self.workspace),
            }
        )
        return id

    def output(self, id, stream="stdout"):
        data = b"".join(
            base64.b64decode(frame["data"])
            for frame in self.pending
            if frame.get("id") == id and frame.get("stream") == stream
        )
        self.pending = [
            frame
            for frame in self.pending
            if not (frame.get("id") == id and frame.get("stream") == stream)
        ]
        return data

    def test_child_output_cannot_inject_protocol(self):
        forged = '{"event":"ready","pid":999}\n{"id":"bad","event":"exit","code":0}\n'
        id = self.execute(
            f"import os; os.write(1, {forged.encode()!r}); os.write(2, b'err')"
        )
        self.assertEqual(self.event("exit", id)["code"], 0)
        self.assertEqual(self.output(id), forged.encode())
        self.assertEqual(self.output(id, "stderr"), b"err")
        self.assertFalse(any(frame["event"] == "ready" for frame in self.pending))

    def test_child_descriptors_and_masked_environment(self):
        code = """
import json, os
fds = {}
for fd in os.listdir('/proc/self/fd'):
    try:
        fds[fd] = os.readlink('/proc/self/fd/' + fd)
    except FileNotFoundError:
        pass
print(json.dumps({'fds': fds, 'env': dict(os.environ), 'stdin': os.read(0, 10).decode()}))
"""
        id = self.execute(code)
        self.assertEqual(self.event("exit", id)["code"], 0)
        result = json.loads(self.output(id))
        self.assertEqual(set(result["fds"]), {"0", "1", "2"})
        self.assertEqual(result["fds"]["0"], "/dev/null")
        self.assertEqual(result["stdin"], "")
        self.assertEqual(result["env"]["MASKED_TEST"], "allowed")
        # Nix's Python wrapper adds this non-secret isolation setting.
        self.assertLessEqual(
            set(result["env"]), {"PATH", "MASKED_TEST", "LC_CTYPE", "PYTHONNOUSERSITE"}
        )
        if "PYTHONNOUSERSITE" in result["env"]:
            self.assertIn(result["env"]["PYTHONNOUSERSITE"], {"1", "true"})

    def test_cwd_escape_and_symlink_rejected(self):
        link = self.workspace / "escape"
        link.symlink_to(self.temp.name, target_is_directory=True)
        for cwd in (self.temp.name, "..", link):
            id = self.execute("raise SystemExit(99)", cwd)
            self.assertIn("cwd", self.event("error", id)["error"])
        child = self.workspace / "child"
        child.mkdir()
        id = self.execute("import os; print(os.getcwd())", "child")
        self.assertEqual(self.event("exit", id)["code"], 0)
        self.assertEqual(self.output(id).decode().strip(), str(child))

    def test_child_cannot_open_supervisor_control_descriptors(self):
        id = self.execute(f"""
import errno, os
for suffix in ('fd/0', 'fd/1', 'fd/2', 'mem'):
    try:
        fd = os.open('/proc/{self.process.pid}/' + suffix, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as error:
        assert error.errno in (errno.EACCES, errno.EPERM), error
    else:
        os.close(fd)
        raise AssertionError('supervisor descriptor accessible')
""")
        self.assertEqual(self.event("exit", id)["code"], 0, self.output(id, "stderr"))

    def test_invalid_schema_and_command_limit(self):
        invalid = [
            [],
            {},
            {"op": "wat", "id": "aa"},
            {"op": "cancel", "id": "not-hex"},
            {"op": [], "id": "aa"},
            {"op": "exec", "id": "aa", "argv": [], "cwd": "."},
            {"op": "exec", "id": "aa", "argv": [1], "cwd": "."},
            {"op": "exec", "id": "aa", "argv": ["a\0b"], "cwd": "."},
            {"op": "exec", "id": "aa", "argv": ["true"], "cwd": ".", "env": {}},
            {
                "op": "exec",
                "id": "aa",
                "argv": ["a" * supervisor.MAX_COMMAND],
                "cwd": ".",
            },
        ]
        invalid += [
            {"op": "connect", "id": "aa", "port": port}
            for port in (True, "80", 0, -1, 65536, 1080, 3128)
        ]
        for request in invalid:
            self.send(request)
            self.assertLessEqual(len(self.event("error")["error"]), 256)
        self.process.stdin.write(b"{bad json}\n")
        self.process.stdin.flush()
        self.assertEqual(self.event("error")["error"], "invalid request")
        id = self.execute("pass")
        self.assertEqual(self.event("exit", id)["code"], 0)

    def test_oversized_frame_terminates_worker(self):
        try:
            self.process.stdin.write(b"x" * (supervisor.MAX_FRAME + 1))
            self.process.stdin.flush()
        except BrokenPipeError:
            pass
        self.assertNotEqual(self.process.wait(timeout=5), 0)

    def test_one_exec_and_cancel_escalates(self):
        id = self.execute(
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"
        )
        self.event("data", id)
        other = self.execute("pass")
        self.assertIn("busy", self.event("error", other)["error"])
        start = time.monotonic()
        self.send({"op": "cancel", "id": id})
        self.assertEqual(self.event("exit", id)["code"], -signal.SIGKILL)
        self.assertGreaterEqual(time.monotonic() - start, 1.9)
        id = self.execute("pass")
        self.assertEqual(self.event("exit", id)["code"], 0)

    def start_background(self, host="127.0.0.1"):
        code = f"""
import os, socket, time
s = socket.socket({"socket.AF_INET6" if ":" in host else "socket.AF_INET"}, socket.SOCK_STREAM)
s.bind(({host!r}, 0))
s.listen()
pid = os.fork()
if pid:
    print(s.getsockname()[1], pid, flush=True)
    os._exit(0)
while True:
    c, _ = s.accept()
    with c:
        while True:
            data = c.recv(16384)
            if not data:
                break
            c.sendall(data)
"""
        start = time.monotonic()
        id = self.execute(code)
        self.assertEqual(self.event("exit", id, timeout=2)["code"], 0)
        self.assertLess(time.monotonic() - start, 2)
        port, pid = map(int, self.output(id).split())
        self.addCleanup(self.kill_background, pid)
        return port, pid

    @staticmethod
    def kill_background(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def test_background_survives_foreground_and_connects(self):
        port, pid = self.start_background()
        os.kill(pid, 0)
        id = self.execute("print('next')")
        self.assertEqual(self.event("exit", id)["code"], 0)
        self.assertEqual(self.output(id), b"next\n")
        deadline = time.monotonic() + 3
        while port not in self.event("ports", timeout=3)["ports"]:
            self.assertLess(time.monotonic(), deadline)
        self.send({"op": "connect", "id": "ca", "port": port})
        self.event("connected", "ca")
        payload = b"GET / HTTP/1.1\r\n\r\n\x00\xff" * 1000
        self.send(
            {"op": "data", "id": "ca", "data": base64.b64encode(payload).decode()}
        )
        self.send({"op": "end", "id": "ca"})
        result = bytearray()
        while len(result) < len(payload):
            frame = self.event("data", "ca")
            self.assertNotIn("stream", frame)
            chunk = base64.b64decode(frame["data"], validate=True)
            self.assertLessEqual(len(chunk), supervisor.CHUNK)
            result.extend(chunk)
        self.assertEqual(result, payload)
        self.event("end", "ca")
        self.event("closed", "ca")
        credits = [
            frame["size"]
            for frame in self.pending
            if frame.get("id") == "ca" and frame["event"] == "written"
        ]
        self.assertTrue(all(0 < size <= supervisor.CHUNK for size in credits))
        self.assertEqual(sum(credits), len(payload))
        os.kill(pid, 0)

    def test_credit_driven_slow_upload(self):
        id = self.execute("""
import hashlib, os, socket, time
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
s.bind(('127.0.0.1', 0)); s.listen()
pid = os.fork()
if pid:
    print(s.getsockname()[1], pid, flush=True)
    os._exit(0)
c, _ = s.accept()
time.sleep(0.2)
digest = hashlib.sha256()
size = 0
while True:
    data = c.recv(16384)
    if not data: break
    digest.update(data)
    size += len(data)
    time.sleep(0.002)
c.sendall(f'{size}:{digest.hexdigest()}'.encode())
c.close()
""")
        self.assertEqual(self.event("exit", id)["code"], 0)
        port, pid = map(int, self.output(id).split())
        self.addCleanup(self.kill_background, pid)
        self.send({"op": "connect", "id": "ca", "port": port})
        self.event("connected", "ca")
        chunk = bytes(range(256)) * 192  # Manager's 48 KiB upload window.
        for _ in range(128):
            self.send(
                {"op": "data", "id": "ca", "data": base64.b64encode(chunk).decode()}
            )
            outstanding = len(chunk)
            while outstanding:
                frame = self.event("written", "ca")
                self.assertEqual(set(frame), {"event", "id", "size"})
                self.assertGreater(frame["size"], 0)
                self.assertLessEqual(frame["size"], min(supervisor.CHUNK, outstanding))
                outstanding -= frame["size"]
        self.send({"op": "end", "id": "ca"})
        self.event("end", "ca")
        self.event("closed", "ca")
        response = b"".join(
            base64.b64decode(frame["data"])
            for frame in self.pending
            if frame.get("id") == "ca" and frame["event"] == "data"
        )
        self.assertEqual(
            response,
            f"{len(chunk) * 128}:{hashlib.sha256(chunk * 128).hexdigest()}".encode(),
        )
        self.assertFalse(
            any(
                frame.get("id") == "ca" and frame["event"] == "error"
                for frame in self.pending
            )
        )

    def test_ipv6_fallback_and_explicit_close(self):
        try:
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
                probe.bind(("::1", 0))
        except OSError:
            self.skipTest("IPv6 loopback unavailable")
        port, _ = self.start_background("::1")
        self.send({"op": "connect", "id": "ca", "port": port})
        self.event("connected", "ca")
        self.send({"op": "data", "id": "ca", "data": "%%%"})
        self.assertEqual(self.event("error", "ca")["error"], "invalid request")
        self.send({"op": "close", "id": "ca"})
        self.event("closed", "ca")

    def test_server_eof_preserves_outbound_half(self):
        id = self.execute("""
import os, socket
s = socket.socket()
s.bind(('127.0.0.1', 0)); s.listen()
if os.fork():
    print(s.getsockname()[1], flush=True)
    os._exit(0)
c, _ = s.accept()
c.shutdown(socket.SHUT_WR)
data = b''
while True:
    chunk = c.recv(100)
    if not chunk: break
    data += chunk
with open('received.tmp', 'wb') as f: f.write(data)
os.replace('received.tmp', 'received')
""")
        self.event("exit", id)
        port = int(self.output(id))
        self.send({"op": "connect", "id": "ca", "port": port})
        self.event("connected", "ca")
        self.event("end", "ca")
        self.send({"op": "data", "id": "ca", "data": "aGk="})
        self.send({"op": "end", "id": "ca"})
        self.event("closed", "ca")
        self.assertEqual(self.event("written", "ca")["size"], 2)
        deadline = time.monotonic() + 3
        while not (self.workspace / "received").exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.assertEqual((self.workspace / "received").read_bytes(), b"hi")

    def test_command_output_chunks_and_eof_shutdown(self):
        id = self.execute("import os; os.write(1, b'x' * 200000)")
        self.assertEqual(self.event("exit", id)["code"], 0)
        for frame in self.pending:
            if frame.get("id") == id and frame["event"] == "data":
                self.assertLessEqual(
                    len(base64.b64decode(frame["data"])), supervisor.CHUNK
                )
        self.assertEqual(self.output(id), b"x" * 200000)
        self.process.stdin.close()
        self.assertEqual(self.process.wait(timeout=3), 0)


class SecurityTests(unittest.TestCase):
    def status(self, **changes):
        fields = {
            "CapEff": "0000000000000000",
            "CapPrm": "0",
            "CapAmb": "0",
            "NoNewPrivs": "1",
            "Seccomp": "2",
            "Pid": str(os.getpid()),
            "NSpid": str(os.getpid()),
        }
        fields.update(changes)
        return "\n".join(key + ":\t" + value for key, value in fields.items())

    def test_security_prerequisites(self):
        cases = [
            {},
            {"CapEff": "1"},
            {"CapPrm": "1"},
            {"CapAmb": "1"},
            {"NoNewPrivs": "0"},
            {"Seccomp": "0"},
            {"Seccomp": "invalid"},
            {"Pid": str(os.getpid() + 1)},
            {"NSpid": ""},
            {"NSpid": str(os.getpid() + 1)},
            {"NSpid": f"{os.getpid()}\t2"},
        ]
        for changes in cases:
            with (
                self.subTest(changes=changes),
                mock.patch.object(supervisor.ctypes, "CDLL") as libc,
                mock.patch.object(
                    supervisor.Path, "read_text", return_value=self.status(**changes)
                ),
            ):
                libc.return_value.prctl.return_value = 0
                if changes:
                    with self.assertRaises(RuntimeError):
                        supervisor.secure_process()
                else:
                    supervisor.secure_process()
                self.assertEqual(
                    libc.return_value.prctl.call_args_list,
                    [mock.call(4, 0, 0, 0, 0), mock.call(3, 0, 0, 0, 0)],
                )

    def test_security_dumpable_and_missing_status_fail_closed(self):
        for results, status in [
            ([-1], self.status()),
            ([0, 1], self.status()),
            ([0, 0], ""),
        ]:
            with (
                mock.patch.object(supervisor.ctypes, "CDLL") as libc,
                mock.patch.object(supervisor.Path, "read_text", return_value=status),
            ):
                libc.return_value.prctl.side_effect = results
                with self.assertRaises(RuntimeError):
                    supervisor.secure_process()

    def test_main_checks_security_before_constructing_worker(self):
        with (
            mock.patch.object(supervisor, "secure_process", side_effect=RuntimeError),
            mock.patch.object(supervisor, "Supervisor") as worker,
            mock.patch.object(supervisor.sys, "stderr"),
        ):
            self.assertEqual(supervisor.main(), 1)
            worker.assert_not_called()

    def test_port_enumeration_all_tcp_no_protocol_sniffing(self):
        table = """  sl local_address rem_address st
0: 0100007F:1F90 00000000:0000 0A
1: 00000000:0438 00000000:0000 0A
2: 00000000:0C38 00000000:0000 0A
3: 0100007F:0050 00000000:0000 01
4: 00000000000000000000000001000000:01BB 00000000:0000 0A
5: malformed 00000000:0000 0A
"""
        with mock.patch("builtins.open", mock.mock_open(read_data=table)):
            self.assertEqual(supervisor.listening_ports(), [443, 8080])


class ProcessTests(unittest.TestCase):
    def entries(self, *names):
        entries = []
        for name in names:
            entry = mock.Mock()
            entry.name = str(name)
            entry.path = "/proc/" + str(name)
            entries.append(entry)
        return entries

    def test_idle_excludes_pid_one_supervisor_and_nonnumeric_entries(self):
        with (
            mock.patch.object(supervisor.os, "scandir") as scan,
            mock.patch.object(supervisor.Path, "read_bytes") as read,
        ):
            scan.return_value.__enter__.return_value = self.entries(
                1, os.getpid(), "self", "net"
            )
            self.assertEqual(supervisor.workload_processes(), 0)
            read.assert_not_called()
        scan.assert_called_once_with("/proc")

    def test_states_and_untrusted_process_names(self):
        states = (b"R", b"S", b"D", b"T", b"t", b"I", b"Z", b"X", b"x")
        with (
            mock.patch.object(supervisor.os, "getpid", return_value=2),
            mock.patch.object(supervisor.os, "scandir") as scan,
            mock.patch.object(supervisor.Path, "read_bytes") as read,
        ):
            scan.return_value.__enter__.return_value = self.entries(1, 2, *range(3, 12))
            read.side_effect = [
                b"3 (misleading ) Z\n (name)) " + state + b" 0" * 16 + b" 1 0\n"
                for state in states
            ]
            self.assertEqual(supervisor.workload_processes(), 6)

    def test_zombie_leader_with_live_or_unknown_threads_counts_busy(self):
        with (
            mock.patch.object(supervisor.os, "getpid", return_value=2),
            mock.patch.object(supervisor.os, "scandir") as scan,
            mock.patch.object(supervisor.Path, "read_bytes") as read,
        ):
            scan.return_value.__enter__.return_value = self.entries(1, 2, 3)
            for state in (b"Z", b"X", b"x"):
                for threads in (b"1", b"2", b"16", b"0", b"-1", b"invalid", b""):
                    with self.subTest(state=state, threads=threads):
                        read.return_value = (
                            b"3 (worker) " + state + b" 0" * 16 + b" " + threads
                        )
                        self.assertEqual(
                            supervisor.workload_processes(), 0 if threads == b"1" else 1
                        )

    def test_failed_or_incomplete_scans_never_report_idle(self):
        with mock.patch.object(supervisor.os, "scandir", side_effect=PermissionError):
            self.assertGreater(supervisor.workload_processes(), 0)
        with mock.patch.object(supervisor.os, "scandir") as scan:
            scan.return_value.__enter__.return_value = []
            self.assertGreater(supervisor.workload_processes(), 0)
            scan.return_value.__enter__.return_value = self.entries(1)
            self.assertGreater(supervisor.workload_processes(), 0)
            scan.return_value.__enter__.return_value = self.entries(
                os.getpid(), os.getpid() + 1
            )
            for failure in (
                PermissionError(),
                FileNotFoundError(),
                b"malformed",
                b"3 (name)",
            ):
                with (
                    self.subTest(failure=failure),
                    mock.patch.object(
                        supervisor.Path, "read_bytes", side_effect=[failure]
                    ),
                ):
                    self.assertGreater(supervisor.workload_processes(), 0)

    def test_redirected_background_process_and_zombie(self):
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        try:
            # The unit runner has host procfs, not the production PID namespace.
            # Restrict enumeration, but read the actual live/zombie stat files.
            with os.scandir("/proc") as entries:
                scoped = [
                    entry
                    for entry in entries
                    if entry.name in {"1", str(os.getpid()), str(process.pid)}
                ]
            self.assertIn(str(process.pid), [entry.name for entry in scoped])
            with mock.patch.object(supervisor.os, "scandir") as scan:
                scan.return_value.__enter__.return_value = scoped
                self.assertEqual(supervisor.workload_processes(), 1)
                process.kill()
                os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
                self.assertEqual(supervisor.workload_processes(), 0)
        finally:
            process.kill()
            process.wait(timeout=5)


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.worker = supervisor.Supervisor()
        self.addCleanup(self.worker.selector.close)
        self.worker.emit = mock.Mock()

    def request(self, **request):
        self.worker.request(json.dumps(request).encode())

    def test_each_ports_heartbeat_includes_process_count(self):
        iterations = iter((True, False))

        def select(timeout):
            self.worker.running = next(iterations)
            return []

        with (
            mock.patch.object(supervisor.os, "set_blocking"),
            mock.patch.object(self.worker, "watch"),
            mock.patch.object(self.worker, "refresh"),
            mock.patch.object(self.worker.selector, "select", side_effect=select),
            mock.patch.object(supervisor.time, "monotonic", side_effect=[0, 0, 1, 1]),
            mock.patch.object(supervisor, "listening_ports", return_value=[]),
            mock.patch.object(supervisor, "workload_processes", side_effect=[0, 1]),
        ):
            self.worker.run()
        self.worker.emit.assert_has_calls(
            [
                mock.call("ready", pid=os.getpid()),
                mock.call("ports", ports=[], processes=0),
                mock.call("ports", ports=[], processes=1),
            ]
        )

    def test_stream_limit_and_duplicate_ids(self):
        with mock.patch.object(self.worker, "dial"):
            for id in range(supervisor.MAX_STREAMS):
                self.request(op="connect", id=format(id, "x"), port=8000)
            self.assertEqual(len(self.worker.streams), supervisor.MAX_STREAMS)
            self.request(op="connect", id="ffff", port=8000)
            self.worker.emit.assert_called_with(
                "error", "ffff", error="stream limit reached"
            )
            self.request(op="connect", id="0", port=8000)
            self.worker.emit.assert_called_with("error", "0", error="id in use")

    def test_stream_buffer_overflow_closes_only_that_stream(self):
        self.worker.streams["a"] = supervisor.Stream("a", 8000, connecting=False)
        self.worker.streams["b"] = supervisor.Stream("b", 8000, connecting=False)
        data = base64.b64encode(b"x" * supervisor.MAX_COMMAND).decode()
        for _ in range(2):
            self.request(op="data", id="a", data=data)
        self.assertEqual(len(self.worker.streams["a"].pending), supervisor.MAX_PENDING)
        self.request(op="data", id="a", data="eA==")
        self.assertEqual(set(self.worker.streams), {"b"})
        self.worker.emit.assert_has_calls(
            [
                mock.call("error", "a", error="stream buffer limit exceeded"),
                mock.call("closed", "a"),
            ]
        )

    def test_upload_credits_follow_successful_partial_writes(self):
        sock = mock.Mock()
        stream = supervisor.Stream("a", 8000, sock=sock, connecting=False)
        self.worker.streams["a"] = stream
        data = b"x" * (48 * 1024)
        self.request(op="data", id="a", data=base64.b64encode(data).decode())
        self.worker.emit.assert_not_called()
        sock.send.side_effect = [
            BlockingIOError,
            0,
            7,
            supervisor.CHUNK,
            supervisor.CHUNK,
            supervisor.CHUNK - 7,
        ]
        for _ in range(2):
            self.worker.service_stream(stream, supervisor.selectors.EVENT_WRITE)
            self.worker.emit.assert_not_called()
            self.assertEqual(bytes(stream.pending), data)
        for count in (7, supervisor.CHUNK, supervisor.CHUNK, supervisor.CHUNK - 7):
            before = len(stream.pending)
            self.worker.service_stream(stream, supervisor.selectors.EVENT_WRITE)
            self.worker.emit.assert_called_with("written", "a", size=count)
            self.assertEqual(len(stream.pending), before - count)
        self.assertFalse(stream.pending)
        self.assertEqual(self.worker.emit.call_count, 4)
        self.assertTrue(
            all(
                len(call.args[0]) <= supervisor.CHUNK
                for call in sock.send.call_args_list
            )
        )

    def test_workload_cannot_forge_upload_credits(self):
        forged = b'{"event":"written","id":"a","size":49152}\n'
        sock = mock.Mock()
        sock.recv.return_value = forged
        stream = supervisor.Stream("a", 8000, sock=sock, connecting=False)
        self.worker.streams["a"] = stream
        self.worker.service_stream(stream, supervisor.selectors.EVENT_READ)
        self.worker.emit.assert_called_once_with(
            "data", "a", data=base64.b64encode(forged).decode()
        )

    def test_output_backpressure_pauses_producers_not_writes_or_discard(self):
        pipe = supervisor.Pipe(mock.Mock(), mock.Mock(finished=False), "stdout")
        discarded = supervisor.Pipe(mock.Mock(), mock.Mock(finished=True), "stdout")
        self.worker.pipes.update((pipe, discarded))
        stream = supervisor.Stream(
            "a", 8000, sock=mock.Mock(), connecting=False, pending=bytearray(b"x")
        )
        self.worker.streams["a"] = stream
        self.worker.output_size = supervisor.OUTPUT_HIGH
        with mock.patch.object(self.worker, "watch") as watch:
            self.worker.refresh()
            watch.assert_any_call(pipe.file, 0, ("pipe", pipe))
            watch.assert_any_call(
                discarded.file, supervisor.selectors.EVENT_READ, ("pipe", discarded)
            )
            watch.assert_any_call(
                stream.sock, supervisor.selectors.EVENT_WRITE, ("stream", stream)
            )
            self.worker.output_size = 0
            self.worker.refresh()
            watch.assert_any_call(
                pipe.file, supervisor.selectors.EVENT_READ, ("pipe", pipe)
            )
            watch.assert_any_call(
                stream.sock,
                supervisor.selectors.EVENT_READ | supervisor.selectors.EVENT_WRITE,
                ("stream", stream),
            )

    def test_control_output_hard_limit(self):
        self.worker.output_size = supervisor.OUTPUT_LIMIT
        with self.assertRaises(RuntimeError):
            supervisor.Supervisor.emit(self.worker, "ready", pid=1)
        self.assertFalse(self.worker.output)


if __name__ == "__main__":
    unittest.main()
