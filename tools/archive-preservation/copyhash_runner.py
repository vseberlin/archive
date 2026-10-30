"""Shared guarded subprocess wrapper for the compiled copy/hash helper."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path


DEFAULT_TIMEOUT_SECONDS = 900
TERMINATE_GRACE_SECONDS = 5
SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class CopyHashError(RuntimeError):
    pass


def helper_available(helper: Path) -> bool:
    return helper.is_file() and os.access(helper, os.X_OK)


def _temporary_path(destination: Path, process_id: int) -> Path:
    return Path(f"{destination}.copyhash.{process_id}.tmp")


def _stop_process(process: subprocess.Popen[str]) -> str:
    process.terminate()
    try:
        stdout, _ = process.communicate(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, _ = process.communicate()
    return stdout


def _remove_temporary(path: Path) -> str:
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        return f"; temporary cleanup failed: {error}"
    return ""


def copy_file(
    helper: Path,
    source: Path,
    destination: Path,
    expected_digest: str = "",
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    if not helper_available(helper):
        raise CopyHashError(f"copy helper is unavailable or not executable: {helper}")
    command = [str(helper), str(source), str(destination)]
    if expected_digest:
        command.append(expected_digest)
    try:
        process = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as error:
        raise CopyHashError(f"cannot start copy helper: {error}") from error
    temporary = _temporary_path(destination, process.pid)
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        stdout = _stop_process(process)
        cleanup_note = _remove_temporary(temporary)
        raise CopyHashError(
            f"copy helper timed out after {timeout}s: {stdout.strip()}{cleanup_note}"
        ) from error

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        cleanup_note = _remove_temporary(temporary)
        raise CopyHashError(
            f"copy helper returned invalid output: {stdout.strip()}{cleanup_note}"
        ) from error
    if process.returncode != 0 or payload.get("result") != "ok":
        cleanup_note = _remove_temporary(temporary)
        raise CopyHashError(
            str(payload.get("error") or "copy helper failed") + cleanup_note
        )

    digest = payload.get("sha256")
    size_bytes = payload.get("size_bytes")
    if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
        raise CopyHashError("copy helper returned an invalid SHA-256 digest")
    if not isinstance(size_bytes, int) or size_bytes < 0:
        raise CopyHashError("copy helper returned an invalid byte count")
    if not destination.is_file():
        raise CopyHashError("copy helper reported success without a destination file")
    destination_size = destination.stat().st_size
    if destination_size != size_bytes:
        raise CopyHashError(
            "copy helper byte count differs from the destination size: "
            f"{size_bytes} != {destination_size}"
        )
    return digest


def hash_file(
    helper: Path,
    source: Path,
    expected_digest: str = "",
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    if not helper_available(helper):
        raise CopyHashError(f"hash helper is unavailable or not executable: {helper}")
    command = [str(helper), "--hash-only", str(source)]
    if expected_digest:
        command.append(expected_digest)
    try:
        result = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CopyHashError(f"cannot hash source: {error}") from error
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise CopyHashError(
            f"hash helper returned invalid output: {result.stdout.strip()}"
        ) from error
    if (
        result.returncode != 0
        or payload.get("result") != "ok"
        or payload.get("mode") != "hash-only"
    ):
        raise CopyHashError(
            str(payload.get("error") or "hash helper failed")
        )
    digest = payload.get("sha256")
    size_bytes = payload.get("size_bytes")
    if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
        raise CopyHashError("hash helper returned an invalid SHA-256 digest")
    if not isinstance(size_bytes, int) or size_bytes != source.stat().st_size:
        raise CopyHashError("hash helper byte count differs from source size")
    return digest
