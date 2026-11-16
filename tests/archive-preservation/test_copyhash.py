from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools" / "archive-preservation"
SOURCE = TOOLS / "copyhash.c"
sys.path.insert(0, str(TOOLS))

import copyhash_runner  # noqa: E402


@unittest.skipUnless(shutil.which("gcc"), "GCC is required")
class CopyHashTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build = tempfile.TemporaryDirectory()
        cls.helper = Path(cls.build.name) / "archive-copyhash"
        subprocess.run(
            [
                "gcc",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Wpedantic",
                "-Werror",
                "-o",
                str(cls.helper),
                str(SOURCE),
                "-lcrypto",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.build.cleanup()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.bin"
        self.source.write_bytes((b"archive-copyhash-test\n" * 8192) + b"end")
        self.source.chmod(0o640)
        os.utime(
            self.source,
            ns=(1_700_000_000_000_000_000, 1_700_000_001_000_000_000),
        )
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_helper(
        self,
        destination: Path,
        expected_digest: str = "",
    ) -> subprocess.CompletedProcess[str]:
        command = [str(self.helper), str(self.source), str(destination)]
        if expected_digest:
            command.append(expected_digest)
        return subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def assert_no_temporary(self, destination: Path) -> None:
        self.assertEqual(
            [],
            list(destination.parent.glob(f"{destination.name}.copyhash.*.tmp")),
        )

    def test_copy_hashes_once_preserves_basic_metadata_and_publishes(self) -> None:
        destination = self.root / "destination.bin"
        result = self.run_helper(
            destination,
            "sha256:" + self.digest.upper(),
        )
        self.assertEqual(0, result.returncode, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual("ok", payload["result"])
        self.assertEqual("sha256:" + self.digest, payload["sha256"])
        self.assertEqual(self.source.stat().st_size, payload["size_bytes"])
        self.assertEqual(self.source.read_bytes(), destination.read_bytes())
        self.assertEqual(
            stat.S_IMODE(self.source.stat().st_mode),
            stat.S_IMODE(destination.stat().st_mode),
        )
        self.assertEqual(
            self.source.stat().st_mtime_ns,
            destination.stat().st_mtime_ns,
        )
        self.assert_no_temporary(destination)

    def test_wrong_expected_digest_leaves_no_output(self) -> None:
        destination = self.root / "wrong.bin"
        result = self.run_helper(destination, "sha256:" + ("0" * 64))
        self.assertEqual(1, result.returncode)
        self.assertEqual("error", json.loads(result.stdout)["result"])
        self.assertFalse(destination.exists())
        self.assert_no_temporary(destination)

    def test_malformed_expected_digest_is_rejected_before_copy(self) -> None:
        destination = self.root / "malformed.bin"
        result = self.run_helper(destination, "not-a-digest")
        self.assertEqual(1, result.returncode)
        self.assertIn("exactly 64", json.loads(result.stdout)["error"])
        self.assertFalse(destination.exists())
        self.assert_no_temporary(destination)

    def test_existing_destination_is_never_replaced(self) -> None:
        destination = self.root / "existing.bin"
        destination.write_bytes(b"keep this")
        result = self.run_helper(destination)
        self.assertEqual(1, result.returncode)
        self.assertEqual(b"keep this", destination.read_bytes())
        self.assert_no_temporary(destination)

    def test_symlink_source_is_rejected(self) -> None:
        symlink = self.root / "source-link.bin"
        symlink.symlink_to(self.source)
        destination = self.root / "linked.bin"
        result = subprocess.run(
            [str(self.helper), str(symlink), str(destination)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.assertEqual(1, result.returncode)
        self.assertFalse(destination.exists())
        self.assert_no_temporary(destination)

    def test_shared_runner_checks_result_and_destination_size(self) -> None:
        destination = self.root / "runner.bin"
        digest = copyhash_runner.copy_file(
            self.helper,
            self.source,
            destination,
            "sha256:" + self.digest,
        )
        self.assertEqual("sha256:" + self.digest, digest)
        self.assertEqual(self.source.stat().st_size, destination.stat().st_size)

    def test_shared_runner_cleans_exact_temporary_after_timeout(self) -> None:
        destination = self.root / "timeout.bin"
        temporary = Path(f"{destination}.copyhash.12345.tmp")
        temporary.write_bytes(b"partial")

        class TimedOutProcess:
            pid = 12345
            returncode = None

            def __init__(self) -> None:
                self.stopped = False

            def communicate(self, timeout: int | None = None):
                if not self.stopped:
                    raise subprocess.TimeoutExpired(["copyhash"], timeout)
                self.returncode = -15
                return "", None

            def terminate(self) -> None:
                self.stopped = True

            def kill(self) -> None:
                self.stopped = True

        with mock.patch.object(
            copyhash_runner.subprocess,
            "Popen",
            return_value=TimedOutProcess(),
        ):
            with self.assertRaisesRegex(copyhash_runner.CopyHashError, "timed out"):
                copyhash_runner.copy_file(
                    self.helper,
                    self.source,
                    destination,
                    timeout=1,
                )
        self.assertFalse(temporary.exists())
        self.assertFalse(destination.exists())

    def test_non_executable_helper_is_unavailable(self) -> None:
        helper = self.root / "not-executable"
        helper.write_bytes(self.helper.read_bytes())
        helper.chmod(0o644)
        self.assertFalse(copyhash_runner.helper_available(helper))


if __name__ == "__main__":
    unittest.main()
