#!/usr/bin/env python3
"""Record completion of the exception-tolerant Trafo traversal."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path


STATE = Path(
    "/mnt/volume/iss-archive-work/trafo/work/accession-runs/"
    "trafo-full-002/exception-pass-001"
)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_once(path: Path, value: dict[str, object]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def main() -> int:
    exit_path = STATE / "runner.exit"
    while not exit_path.exists():
        time.sleep(30)
    try:
        exit_code = int(exit_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as error:
        write_once(
            STATE / "watchdog-failure.json",
            {"result": "error", "at": now(), "error": str(error)},
        )
        return 1
    complete_path = STATE / "batch-complete.json"
    if exit_code != 0 or not complete_path.is_file():
        write_once(
            STATE / "watchdog-failure.json",
            {"result": "error", "at": now(), "runner_exit_code": exit_code},
        )
        return 1
    complete = json.loads(complete_path.read_text(encoding="utf-8"))
    if not complete.get("batch_traversal_complete"):
        write_once(
            STATE / "watchdog-failure.json",
            {
                "result": "error",
                "at": now(),
                "runner_exit_code": exit_code,
                "error": "batch traversal is not complete",
            },
        )
        return 1
    write_once(
        STATE / "watchdog-success.json",
        {
            "result": "ok",
            "at": now(),
            "runner_exit_code": exit_code,
            "batch_result": complete.get("result"),
            "manifest_rows": complete.get("manifest_rows"),
            "exception_rows": complete.get("exception_rows"),
            "publication_blocked_until_exceptions_resolved": complete.get(
                "publication_blocked_until_exceptions_resolved"
            ),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
