"""Trusted host-side cgroup entry point; never executes workload arguments."""

import argparse
import os
import re
import shlex
import sys
from pathlib import Path


def delegated_root():
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::"):
            relative = line[3:].lstrip("/")
            current = Path("/sys/fs/cgroup") / relative
            if ".." in current.parts or current.name != "manager":
                break
            return current.parent
    raise RuntimeError("manager must run in a delegated cgroup's manager subgroup")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cgroup", required=True)
    parser.add_argument("--sandbox-exec", required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--supervisor", required=True)
    args = parser.parse_args()
    group = Path(args.cgroup)
    root = delegated_root()
    if (
        group.parent != root
        or not re.fullmatch(r"runtime-[a-f0-9]{24}", group.name)
        or group.is_symlink()
        or group.resolve(strict=True) != group
    ):
        parser.error("invalid runtime cgroup")
    for value in (args.sandbox_exec, args.python, args.supervisor):
        if not os.path.isabs(value):
            parser.error("launch executables must be absolute trusted paths")
    (group / "cgroup.procs").write_text(str(os.getpid()))
    command = "exec " + shlex.join([args.python, "-I", "-S", args.supervisor])
    os.execv(args.sandbox_exec, [args.sandbox_exec, "-c", command])


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError) as error:
        print(f"preview launch failed: {error}", file=sys.stderr)
        sys.exit(70)
