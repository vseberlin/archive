#!/usr/bin/env python3
"""Fail-safe shutdown watcher for the approved Baeckerhandwerk accession."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


WORK = Path("/mnt/volume/iss-archive-work/collection-827")
PREPARED = WORK / "work/accession"
UNIT = Path("/mnt/volume/iss-archive/units/baeckerhandwerk")
RECOVERY = WORK / "work/pre-metadata-unit-20260828"
RUNNER = WORK / "baeckerhandwerk_accession.py"
LOG = PREPARED / "shutdown-watchdog.log"
STATUS = PREPARED / "shutdown-watchdog-status.json"
DECISIONS = PREPARED / "accession-decisions.csv"
COMPLETION = UNIT / "00_MANIFESTS/accession-completion.json"


class IntegrityAlarm(RuntimeError):
    pass


def utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def log(message: str) -> None:
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{utc()} {message}\n")


def status(state: str, **extra: object) -> None:
    payload = {"format": "iss-baeckerhandwerk-shutdown-watchdog-v1", "updated_utc": utc(), "state": state, **extra}
    temporary = STATUS.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, STATUS)


def source_guard() -> dict[str, str]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", "/media/vladimir/Elements", "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True, capture_output=True, text=True, timeout=10,
    )
    rows = json.loads(result.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise IntegrityAlarm("Elements no longer resolves to exactly one filesystem")
    row = rows[0]
    options = set(str(row.get("options", "")).split(","))
    if row.get("source") != "/dev/sdf1" or row.get("fstype") != "ntfs3" or "ro" not in options:
        raise IntegrityAlarm(f"Elements source guard changed: {row}")
    return {key: str(row.get(key, "")) for key in ("target", "source", "fstype", "options")}


def process_active(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def recovery_sentinels() -> dict[str, str]:
    relatives = (
        "00_MANIFESTS/accession-completion.json",
        "00_MANIFESTS/payload-checksums.csv",
        "CATALOG.csv",
    )
    result: dict[str, str] = {}
    for relative in relatives:
        path = RECOVERY / relative
        if not path.is_file():
            raise IntegrityAlarm(f"recovery sentinel is missing: {relative}")
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def expected_sources() -> dict[Path, int]:
    with DECISIONS.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 76:
        raise IntegrityAlarm("decision manifest no longer contains 76 rows")
    return {Path(row["selected_master_source_path"]): int(row["selected_master_size_bytes"]) for row in rows}


def monitor(pid: int) -> int:
    sources = expected_sources()
    recovery_baseline = recovery_sentinels()
    log(f"armed pid={pid} recovery_sentinels={len(recovery_baseline)}")
    status("monitoring", pid=pid, recovery_sentinels=recovery_baseline, source_mount=source_guard())
    while True:
        try:
            mount = source_guard()
            if recovery_sentinels() != recovery_baseline:
                raise IntegrityAlarm("pre-metadata recovery sentinels changed during accession")
            changed = [str(path) for path, size in sources.items() if not path.is_file() or path.stat().st_size != size]
            if changed:
                raise IntegrityAlarm(f"selected source path/size changed: {changed[:3]}")
            masters = list((UNIT / "masters").glob("*.tif")) if (UNIT / "masters").is_dir() else []
            access = list((UNIT / "export/access").glob("*.jpg")) if (UNIT / "export/access").is_dir() else []
            if len(masters) > 76 or len(access) > 76 or len(masters) - len(access) not in (0, 1):
                raise IntegrityAlarm(f"unexpected in-progress representation counts: masters={len(masters)} access={len(access)}")
            if process_active(pid):
                status("monitoring", pid=pid, masters=len(masters), access_derivatives=len(access), source_mount=mount)
                time.sleep(30)
                continue
            if not COMPLETION.is_file():
                log("ordinary accession exit/incomplete state observed; no shutdown; continuing to monitor")
                status("accession_incomplete_no_shutdown", pid=pid, masters=len(masters), access_derivatives=len(access))
                time.sleep(30)
                continue
            result = subprocess.run(
                [sys.executable, str(RUNNER), "--validate"],
                capture_output=True, text=True, timeout=7200,
            )
            if result.returncode != 0:
                raise IntegrityAlarm(f"independent final validation failed: {(result.stdout + result.stderr)[-4000:]}")
            receipt = json.loads(COMPLETION.read_text(encoding="utf-8"))
            if not receipt.get("accession_verified") or receipt.get("deletion_authorized") is not False:
                raise IntegrityAlarm("completion receipt lacks verified/no-delete state")
            if receipt.get("predecessor_unit") != str(RECOVERY):
                raise IntegrityAlarm("completion receipt does not identify the preserved predecessor")
            status("validated_poweroff_requested", validation_output=result.stdout.strip(), completion=receipt)
            log("independent validation passed; syncing and requesting poweroff")
            subprocess.run(["sync"], check=True, timeout=300)
            subprocess.run(["sudo", "-n", "systemctl", "poweroff"], check=True, timeout=60)
            return 0
        except IntegrityAlarm as error:
            log(f"INTEGRITY_ALARM {error}")
            status("integrity_alarm_no_shutdown", pid=pid, error=str(error))
            if process_active(pid):
                os.kill(pid, signal.SIGTERM)
                log(f"terminated accession pid={pid} due to integrity alarm")
            return 2
        except Exception as error:
            log(f"ordinary watchdog error; retrying: {type(error).__name__}: {error}")
            status("probe_error_retrying", pid=pid, error=f"{type(error).__name__}: {error}")
            time.sleep(30)


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        raise SystemExit("usage: baeckerhandwerk_shutdown_watchdog.py ACCESSION_PID")
    raise SystemExit(monitor(int(sys.argv[1])))
