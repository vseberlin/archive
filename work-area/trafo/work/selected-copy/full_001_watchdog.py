#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


PID = 224036
OUTPUT = Path("/mnt/volume/iss-archive-work/trafo/work/selected-copy/full-001")
LOG = Path("/mnt/volume/iss-archive-work/trafo/work/selected-copy/full-001-watchdog.log")
EXPECTED_RECORDS = 1548
EXPECTED_FILES = 11402


def log(message: str) -> None:
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
        handle.flush()
        os.fsync(handle.fileno())


def fail(message: str) -> int:
    log(f"FAIL: {message}")
    return 1


def cmdline(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", "replace")


def pid_is_target() -> bool:
    command = cmdline(PID)
    return bool(command and "trafo_copy_pilot.py" in command and "full-001" in command)


def count_csv_rows(path: Path) -> int:
    with path.open(encoding="utf-8", newline="") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def validate() -> None:
    summary_path = OUTPUT / "summary.json"
    selected_path = OUTPUT / "manifests" / "selected-copy.csv"
    checksums_path = OUTPUT / "manifests" / "checksums.csv"
    if not summary_path.is_file():
        raise RuntimeError("missing summary.json")
    if not selected_path.is_file():
        raise RuntimeError("missing selected-copy.csv")
    if not checksums_path.is_file():
        raise RuntimeError("missing checksums.csv")

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("records") != EXPECTED_RECORDS:
        raise RuntimeError(f"records mismatch: {summary.get('records')} != {EXPECTED_RECORDS}")
    if summary.get("copied_files") != EXPECTED_FILES:
        raise RuntimeError(
            f"copied_files mismatch: {summary.get('copied_files')} != {EXPECTED_FILES}"
        )
    if count_csv_rows(selected_path) != EXPECTED_FILES:
        raise RuntimeError("selected-copy.csv row count mismatch")
    if count_csv_rows(checksums_path) != EXPECTED_FILES:
        raise RuntimeError("checksums.csv row count mismatch")

    result = subprocess.run(
        ["find", str(OUTPUT), "-type", "l", "-print", "-quit"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.stdout.strip():
        raise RuntimeError("symlink found under output")

    result = subprocess.run(
        ["find", str(OUTPUT), "-type", "f", "-links", "+1", "-print", "-quit"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.stdout.strip():
        raise RuntimeError("hard-linked file found under output")

    if Path("/mnt/volume/iss-archive/units/trafo").exists():
        raise RuntimeError("final Trafo unit exists unexpectedly")

    bad_pdfinfo: list[str] = []
    with selected_path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["role"] in {"local_pdf", "local_pdf_fallback"} and row.get("pdfinfo_status") != "0":
                bad_pdfinfo.append(row["inventory"])
    if bad_pdfinfo:
        raise RuntimeError("pdfinfo failures: " + ", ".join(bad_pdfinfo[:20]))


def main() -> int:
    log(f"python watchdog start pid={PID} output={OUTPUT}")
    if not pid_is_target():
        return fail(f"pid {PID} is not the expected full-001 copy process")

    heartbeat = 0
    while pid_is_target():
        time.sleep(60)
        heartbeat += 1
        if heartbeat % 10 == 0:
            log(f"heartbeat pid={PID} still running")

    log("copy process exited; waiting for output to settle")
    time.sleep(10)
    try:
        validate()
    except Exception as error:
        return fail(str(error))

    log("clean finish validated; requesting shutdown")
    subprocess.run(["sync"], check=False)
    subprocess.run(
        ["sudo", "/sbin/shutdown", "-h", "now", "Trafo full selected copy clean finish"],
        check=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
