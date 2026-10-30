#!/usr/bin/env python3
"""Compatibility entry point for the established archive workbench."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


TOOL_ROOT = Path(__file__).resolve().parents[1]


def _implementation() -> ModuleType:
    """Load the existing interface without changing its proven design."""

    path = TOOL_ROOT / "workbench.py"
    specification = importlib.util.spec_from_file_location(
        "iss_archive_established_workbench",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load established workbench: {path}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    """Run the established workbench through the interface boundary."""

    return int(_implementation().main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
