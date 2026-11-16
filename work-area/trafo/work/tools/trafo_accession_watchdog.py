#!/usr/bin/env python3
"""Wait for the Trafo runner, independently validate success, and record status."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path


RUN_ROOT = Path(
    "/mnt/volume/iss-archive-work/trafo/work/accession-runs/trafo-full-002"
)
EXIT_FILE = RUN_ROOT / "runner.exit"
RUNNER = Path(
    "/mnt/volume/iss-archive-work/trafo/work/tools/trafo_accession_full.py"
)
SUCCESS = RUN_ROOT / "watchdog-success.json"
FAILURE = RUN_ROOT / "watchdog-failure.json"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_once(path: Path, value: dict[str, object]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")


def main() -> int:
    while not EXIT_FILE.exists():
        time.sleep(30)
    try:
        exit_code = int(EXIT_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as error:
        write_once(FAILURE, {"result": "error", "at": now(), "error": str(error)})
        return 1
    if exit_code != 0:
        write_once(
            FAILURE,
            {"result": "error", "at": now(), "runner_exit_code": exit_code},
        )
        return 1
    result = subprocess.run(
        ["python3", "-B", str(RUNNER), "--validate-only"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
    )
    if result.returncode:
        write_once(
            FAILURE,
            {
                "result": "error",
                "at": now(),
                "runner_exit_code": exit_code,
                "validation_output": result.stdout,
            },
        )
        return 1
    write_once(
        SUCCESS,
        {
            "result": "ok",
            "at": now(),
            "runner_exit_code": exit_code,
            "independent_validation": json.loads(result.stdout),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
