"""Private preview shell client. The first argument is the installed config file."""

import asyncio
import base64
import binascii
import json
import os
import re
import signal
import sys
from pathlib import Path

import aiohttp

MAX_FRAME = 256 * 1024
SESSION = re.compile(r"ses_[A-Za-z0-9]+\Z")


async def request(config, endpoint, body):
    connector = aiohttp.UnixConnector(
        path=str(Path(config["runtime_root"]) / "control.sock")
    )
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=10)
    async with (
        aiohttp.ClientSession(
            connector=connector, timeout=timeout, read_bufsize=MAX_FRAME
        ) as session,
        session.post("http://localhost/" + endpoint, json=body) as response,
    ):
        if response.status != 200:
            detail = (await response.content.read(4096)).decode("utf-8", "replace")
            print(f"preview: {detail}", file=sys.stderr)
            return 77 if response.status in {400, 403, 409} else 70
        if endpoint == "stop":
            return 0
        async for line in response.content:
            if len(line) > MAX_FRAME or not line.endswith(b"\n"):
                raise ValueError("invalid manager frame")
            frame = json.loads(line)
            if not isinstance(frame, dict):
                raise TypeError("invalid manager frame")
            if frame.get("event") == "data":
                stream = frame.get("stream")
                if stream not in {"stdout", "stderr"}:
                    raise ValueError("invalid manager stream")
                data = base64.b64decode(frame["data"], validate=True)
                output = sys.stdout.buffer if stream == "stdout" else sys.stderr.buffer
                output.write(data)
                output.flush()
            elif frame.get("event") == "exit":
                code = frame.get("code")
                if type(code) is not int or not -64 <= code <= 255:
                    raise ValueError("invalid manager exit code")
                return 128 - code if code < 0 else code
            else:
                raise ValueError("runtime execution failed")
        raise ValueError("manager disconnected before exit")


async def run(config, endpoint, body):
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    caught = []

    def interrupt(signum):
        if not caught:
            caught.append(signum)
            task.cancel()

    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, interrupt, signum)
    try:
        return await request(config, endpoint, body)
    except asyncio.CancelledError:
        return 128 + caught[0] if caught else 130
    finally:
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)


def main():
    if len(sys.argv) < 2:
        print(
            "usage: client.py CONFIG [-c COMMAND | -- ARGV... | stop --session ID --directory PATH]",
            file=sys.stderr,
        )
        return 64
    with open(sys.argv[1], encoding="utf-8") as source:
        config = json.load(source)
    args = sys.argv[2:]
    if args and args[0] == "stop":
        import argparse

        parser = argparse.ArgumentParser(prog="preview stop")
        parser.add_argument("--session", required=True)
        parser.add_argument("--directory", required=True)
        options = parser.parse_args(args[1:])
        if not SESSION.fullmatch(options.session) or len(options.session) > 128:
            return 64
        return asyncio.run(
            run(
                config,
                "stop",
                {"session_id": options.session, "directory": options.directory},
            )
        )
    session = os.environ.get("OPENCODE_SESSION_ID")
    if session is None:
        # Fallback is fixed by installed configuration, never a caller-selected
        # executable or runtime environment selector.
        fallback = config["sandbox_exec"]
        if not isinstance(fallback, str) or not os.path.isabs(fallback):
            raise ValueError("invalid configured fallback")
        os.execv(fallback, [fallback, *args])
    if not SESSION.fullmatch(session) or len(session) > 128:
        print("preview: invalid OpenCode session ID", file=sys.stderr)
        return 77
    if len(args) == 2 and args[0] == "-c":
        argv = [config["shell"], "-c", args[1]]
    elif len(args) >= 2 and args[0] == "--":
        argv = args[1:]
    else:
        print("usage: preview -c COMMAND | preview -- ARGV...", file=sys.stderr)
        return 64
    return asyncio.run(
        run(
            config,
            "exec",
            {"directory": os.getcwd(), "session_id": session, "argv": argv},
        )
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        binascii.Error,
        aiohttp.ClientError,
    ) as error:
        print(f"preview: {error}", file=sys.stderr)
        sys.exit(70)
