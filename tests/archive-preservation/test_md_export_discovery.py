from __future__ import annotations

import importlib.util
from argparse import Namespace
from io import BytesIO
from pathlib import Path
import unittest
import urllib.error
from unittest import mock


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tools/archive-preservation/md_export_discovery.py"
)
SPEC = importlib.util.spec_from_file_location("md_export_discovery", MODULE_PATH)
assert SPEC and SPEC.loader
discovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(discovery)


class Response:
    status = 200

    def __init__(self, payload: bytes) -> None:
        self.handle = BytesIO(payload)

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.handle.read()


def arguments(**overrides: object) -> Namespace:
    values = {
        "limit": 100,
        "delay": 0.6,
        "timeout": 30.0,
        "retries": 3,
        "retry_delay": 2.0,
    }
    values.update(overrides)
    return Namespace(**values)


class MuseumDigitalDiscoveryTests(unittest.TestCase):
    def test_polite_capture_contract(self) -> None:
        discovery.validate_polite_args(arguments())
        with self.assertRaisesRegex(discovery.DiscoveryError, "page limit"):
            discovery.validate_polite_args(arguments(limit=101))
        with self.assertRaisesRegex(discovery.DiscoveryError, "page delay"):
            discovery.validate_polite_args(arguments(delay=0.2))

    def test_retryable_response_uses_retry_after(self) -> None:
        error = urllib.error.HTTPError(
            "https://example.invalid",
            429,
            "rate limited",
            {"Retry-After": "7"},
            None,
        )
        with (
            mock.patch.object(
                discovery.urllib.request,
                "urlopen",
                side_effect=[error, Response(b"{}")],
            ),
            mock.patch.object(discovery.time, "sleep") as sleep,
        ):
            status, payload, attempts = discovery.fetch_page(
                "https://example.invalid",
                timeout=30.0,
                retries=3,
                retry_delay=2.0,
            )
        self.assertEqual(200, status)
        self.assertEqual(b"{}", payload)
        self.assertEqual(2, attempts)
        sleep.assert_called_once_with(7.0)

    def test_non_retryable_response_fails_immediately(self) -> None:
        error = urllib.error.HTTPError(
            "https://example.invalid",
            404,
            "not found",
            {},
            None,
        )
        with (
            mock.patch.object(
                discovery.urllib.request,
                "urlopen",
                side_effect=error,
            ),
            mock.patch.object(discovery.time, "sleep") as sleep,
            self.assertRaises(urllib.error.HTTPError),
        ):
            discovery.fetch_page(
                "https://example.invalid",
                timeout=30.0,
                retries=3,
                retry_delay=2.0,
            )
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
