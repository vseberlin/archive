#!/usr/bin/env python3
"""Continuously enforce the WF visual scan's read/write boundaries."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path


SOURCE_ROOT = Path("/media/vladimir/Elements")
EXPECTED_COMMAND = "visual_indexer.py"
EXPECTED_DATABASE = (
    "/mnt/volume/iss-archive-work/tfa/work/reconciliation/"
    "wf-full-visual-index/wf-full-visual-index.sqlite"
)
ALLOWED_WRITE_ROOTS = (
    Path("/mnt/volume/iss-archive-work"),
    Path("/mnt/volume/iss-archive"),
)
LOG_PATH = Path(
    "/mnt/volume/iss-archive-work/tfa/work/reconciliation/"
    "wf-full-visual-index/scan-guard.jsonl"
)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def log(event: str, **details: object) -> None:
    payload = {"at": now(), "event": event, **details}
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def command(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", errors="replace"
        )
    except OSError:
        return ""


def descendants(root_pid: int) -> set[int]:
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text(encoding="utf-8").split()
            parents[int(entry.name)] = int(fields[3])
        except (OSError, ValueError, IndexError):
            continue
    result = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if parent in result and pid not in result:
                result.add(pid)
                changed = True
    return result


def mount_violation() -> str:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "SOURCE,FSTYPE,OPTIONS"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "source mount could not be verified"
    try:
        filesystems = json.loads(result.stdout).get("filesystems") or []
        mount = filesystems[0]
    except (json.JSONDecodeError, IndexError, TypeError):
        return "source mount response was invalid"
    options = set(str(mount.get("options", "")).split(","))
    if mount.get("source") != "/dev/sdf1" or mount.get("fstype") != "ntfs3":
        return f"unexpected source mount: {mount}"
    if "ro" not in options or "rw" in options:
        return f"source mount is not read-only: {mount}"
    return ""


def allowed_write(path: Path) -> bool:
    for root in ALLOWED_WRITE_ROOTS:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def write_violation(pids: set[int]) -> str:
    for pid in sorted(pids):
        fd_root = Path(f"/proc/{pid}/fd")
        try:
            descriptors = list(fd_root.iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                flags_text = Path(f"/proc/{pid}/fdinfo/{descriptor.name}").read_text()
                flags = next(
                    line.split(":", 1)[1].strip()
                    for line in flags_text.splitlines()
                    if line.startswith("flags:")
                )
                access_mode = int(flags, 8) & os.O_ACCMODE
                target = os.readlink(descriptor)
            except (OSError, StopIteration, ValueError):
                continue
            if access_mode == os.O_RDONLY or not target.startswith("/"):
                continue
            clean_target = target.removesuffix(" (deleted)")
            path = Path(clean_target)
            if str(path).startswith("/dev/"):
                continue
            if not allowed_write(path):
                return f"pid {pid} has writable fd {descriptor.name} outside allowed roots: {path}"
    return ""


def stop_scan(root_pid: int, reason: str) -> None:
    targets = sorted(descendants(root_pid), reverse=True)
    log("violation", reason=reason, targets=targets)
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    initial = command(args.pid)
    if EXPECTED_COMMAND not in initial or EXPECTED_DATABASE not in initial:
        raise SystemExit("refusing to guard an unexpected process")
    log("guard_started", pid=args.pid, interval_seconds=args.interval)
    checks = 0
    while True:
        current = command(args.pid)
        if not current:
            log("scan_exited", pid=args.pid)
            return 0
        if EXPECTED_COMMAND not in current or EXPECTED_DATABASE not in current:
            log("pid_identity_changed", pid=args.pid)
            return 1
        problem = mount_violation()
        if not problem:
            problem = write_violation(descendants(args.pid))
        if problem:
            stop_scan(args.pid, problem)
            return 2
        checks += 1
        if checks % 60 == 0:
            log("guard_ok", pid=args.pid, checks=checks)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
