#!/usr/bin/env python3
"""Record terminal status of the Trafo loss-resolution finalizer."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path


RUN_ROOT = Path(
    "/mnt/volume/iss-archive-work/trafo/work/accession-runs/trafo-full-002"
)
STATE = RUN_ROOT / "loss-resolution-001"
FINAL = Path("/mnt/volume/iss-archive/units/trafo")
HIDDEN = Path("/mnt/volume/iss-archive/units/.trafo.trafo-full-002.tmp")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_once(path: Path, value: dict[str, object]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def main() -> int:
    exit_path = STATE / "runner.exit"
    while not exit_path.exists():
        time.sleep(15)
    try:
        exit_code = int(exit_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as error:
        write_once(
            STATE / "watchdog-failure.json",
            {"result": "error", "at": now(), "error": str(error)},
        )
        return 1
    validation_path = RUN_ROOT / "final-validation.json"
    validation = (
        json.loads(validation_path.read_text(encoding="utf-8"))
        if validation_path.is_file()
        else {}
    )
    if (
        exit_code != 0
        or validation.get("result") != "ok"
        or not FINAL.is_dir()
        or HIDDEN.exists()
    ):
        write_once(
            STATE / "watchdog-failure.json",
            {
                "result": "error",
                "at": now(),
                "runner_exit_code": exit_code,
                "validation_result": validation.get("result"),
                "final_unit_exists": FINAL.is_dir(),
                "hidden_stage_exists": HIDDEN.exists(),
            },
        )
        return 1
    write_once(
        STATE / "watchdog-success.json",
        {
            "result": "ok",
            "at": now(),
            "runner_exit_code": exit_code,
            "final_unit": str(FINAL),
            "objects": validation.get("objects"),
            "representations": validation.get("representations"),
            "source_loss_count": validation.get("source_loss_count"),
            "media_rehashed": validation.get("media_rehashed"),
            "inode_size_identity_preserved": validation.get(
                "inode_size_identity_preserved"
            ),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
