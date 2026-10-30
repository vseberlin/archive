from __future__ import annotations

import ast
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = REPO_ROOT / "tools" / "archive-preservation"
sys.path.insert(0, str(TOOL_ROOT))

import archive as ARCHIVE  # noqa: E402
from interfaces import workbench as WORKBENCH_INTERFACE  # noqa: E402


EXPECTED_HELP_DIGESTS = {
    "add-gap": "ba7f1a7c0a73b4ebafb3514c9dfdf920059935931f8ea9a2f0ba5e908fe5d4df",
    "add-identifier": "b7b260d7fa8dbfd5b2427fc8e17a913ad5f48bda4fa9250d49e92e9286043028",
    "add-testimony": "c8dca0f4214836e87af97761a336b1fe53e4262a298d71244017db07eae68aec",
    "core-create-entity": "60b80cebbd47f51ff1e4e53423ffaafe95e7a5cbb9e2c231e84bd056e5fc7fbc",
    "core-create-unit": "7795548be0746298eb73e9c0dac739e5b26b166c2286db0105b3b70943cae9a1",
    "core-document-assess": "19301ece22bb1e9657c8a3bba5eff36c324fcd62246a4b2e2df0c8196dcfde38",
    "core-export-unit-catalog": "50507956ff732bd51db286cfb78e9f0cea82c23aec80f80a2ef4370af6951e3e",
    "core-init": "8d1d4b27c80099ae6df902bf518c903f84e6075ea46e723a367778ee61932716",
    "core-migrate": "d3a09cc9da1347d3d17043b988aeaf027d309e67800a5454dc3ac40a2d32bfe5",
    "core-publish": "4c00790ffe898d5021b99f6e45339dbe458fac3874afc4e63c3c75af53f2d3f1",
    "core-pdf-accession": "b26cfbc71e24c7eae9c3255d446d266ad4bc38ef4d2f501d846fc7e08d5f8e3c",
    "core-pdf-reconcile": "fdafa2b08304393ee8a61c1a137ca1049209ad51cb3dc79cab8ce83d5a14d118",
    "core-rebuild": "b105449b13f484c0d778703602b086741be28d229ba2f81dc9febb4e18fad0d3",
    "core-status": "8e48b81a08f0e02981289cbf1b56d8a32947e01f46f6ca5266ef18fac7301425",
    "create-event": "5dfcf4edc6cd4f37b54a2a718d3ea4365f98a767f9433aeacf0b18f241107ccf",
    "create-object": "07c1366140401affeaa19012d2dbd30276eb99c358d095f8c73ced21bedfe273",
    "create-unit": "8e4a1fe21338a6854f72f36093b3631098a55131a6ea0e1861a0893128ff8a24",
    "decide-metadata": "98843cfcf35fb0f6c669e32ef71416386a2c81464c9a957fe5e12bdb2575cc48",
    "init": "917f67fff12184057214552f16c5f6a5e947bfd1d568a52b0b111591c8f1c46d",
    "intake": "6c109891e64f99cd28e02781db05c242d71af2a0dc37ec71864f7456fc32fb55",
    "link": "c70d7e2d1bc2fe8a2c114eab045ebe862d4019ccb95892b64c520a9a161784c2",
    "rebuild": "5b1b8c64d08a563ed3b375f7e0ae41b7847933e7bbf56237c41116e44b2426b0",
    "reconcile-museum-digital": "9363dfc011b0fea81bf313e2abdd3d7649a0945748721fb63fa0b9ac23cbf33b",
    "refresh-controls": "5da0be6ea98d7efe961ff0bc0a4c7a79eda7a5abf02442338055240a019443c2",
    "review-external-identity": "becb5b94e13f9826d4206a1b9dea52a2e9f157f06a90669c63ca274c14a7f361",
    "scan-source-registry": "b5bc3417d867b3f151dea6fe2fbf14b1f70782d1fba6698d36d8dd11f2cee990",
    "seal": "e5b76d196d7608210a132ed66971db1eb4b32f758dcb3669997b3082de05bb4c",
    "split-working-databases": "02d891d92c07a8666832e2c5093b799fbd8d8b459bb6ff6efbbd75033c8e4c1a",
    "status": "7b0670063083a2865f7695f98f30ea4b9d9dfd525ba93683e817184dfce6b754",
    "validate": "d9317dadaefb6722731a905a82a3323ed04a250ca42fea937bcfb235bfe504ec",
}

EXPECTED_HANDLER_MODULES = {
    **{
        command: "workflows.ingest"
        for command in (
            "add-gap",
            "add-identifier",
            "add-testimony",
            "core-document-assess",
            "core-pdf-accession",
            "create-event",
            "create-object",
            "create-unit",
            "init",
            "intake",
            "link",
        )
    },
    **{
        command: "workflows.reconcile"
        for command in (
            "decide-metadata",
            "core-pdf-reconcile",
            "reconcile-museum-digital",
            "review-external-identity",
        )
    },
    **{
        command: "workflows.publish"
        for command in (
            "core-create-entity",
            "core-create-unit",
            "core-export-unit-catalog",
            "core-init",
            "core-migrate",
            "core-publish",
            "core-rebuild",
            "core-status",
            "rebuild",
            "refresh-controls",
            "scan-source-registry",
            "seal",
            "split-working-databases",
            "status",
            "validate",
        )
    },
}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


class ArchiveArchitectureTest(unittest.TestCase):
    def test_archive_py_is_a_small_compatibility_entry_point(self) -> None:
        path = TOOL_ROOT / "archive.py"
        self.assertLessEqual(len(path.read_text(encoding="utf-8").splitlines()), 60)
        self.assertIs(ARCHIVE.ArchiveError, sys.modules["archive_core"].ArchiveError)
        self.assertTrue(callable(ARCHIVE.command_intake))
        self.assertTrue(callable(ARCHIVE.status_report))

    def test_dependency_direction_is_interfaces_workflows_core(self) -> None:
        for path in (TOOL_ROOT / "archive_core").glob("*.py"):
            imports = imported_modules(path)
            self.assertFalse(
                any(
                    name == "workflows"
                    or name.startswith("workflows.")
                    or name == "interfaces"
                    or name.startswith("interfaces.")
                    for name in imports
                ),
                path,
            )
        for path in (TOOL_ROOT / "workflows").glob("*.py"):
            imports = imported_modules(path)
            self.assertFalse(
                any(
                    name == "interfaces" or name.startswith("interfaces.")
                    for name in imports
                ),
                path,
            )

    def test_all_cli_commands_keep_exact_help_and_workflow_ownership(
        self,
    ) -> None:
        with mock.patch.object(sys, "argv", ["-"]):
            parser = ARCHIVE.build_parser()
        commands = {}
        for action in parser._actions:
            choices = getattr(action, "choices", None)
            if choices:
                commands.update(choices)
        self.assertEqual(set(EXPECTED_HELP_DIGESTS), set(commands))
        top_level = subprocess.run(
            [sys.executable, str(TOOL_ROOT / "archive.py"), "--help"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(
            "30b06f0b99d8269d79ac8533e5a62a92d5291975135f023b3a032d880305cdf3",
            digest(top_level),
        )
        for name, command in commands.items():
            self.assertEqual(
                EXPECTED_HELP_DIGESTS[name],
                digest(command.format_help()),
                name,
            )
            self.assertEqual(
                EXPECTED_HANDLER_MODULES[name],
                command.get_default("handler").__module__,
                name,
            )

    def test_workbench_interface_reuses_the_established_workbench(
        self,
    ) -> None:
        implementation = WORKBENCH_INTERFACE._implementation()
        self.assertTrue(callable(implementation.main))
        self.assertEqual("0.7.1", implementation.WORKBENCH_VERSION)

    def test_cli_runs_core_lifecycle_through_workflow_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"

            def run(*arguments: str) -> dict:
                output = io.StringIO()
                with redirect_stdout(output):
                    result = ARCHIVE.main(list(arguments))
                self.assertEqual(0, result, output.getvalue())
                return json.loads(output.getvalue())

            self.assertEqual(
                "ok",
                run(
                    "core-init",
                    "--root",
                    str(root),
                    "--actor",
                    "Architecture test",
                )["result"],
            )
            run(
                "core-create-unit",
                "--root",
                str(root),
                "--actor",
                "Architecture test",
                "--unit",
                "TEST-ARCHITECTURE",
                "--title",
                "Architecture test",
                "--kind",
                "fixture",
            )
            entity = run(
                "core-create-entity",
                "--root",
                str(root),
                "--actor",
                "Architecture test",
                "--kind",
                "archive_object",
                "--name",
                "Architecture test object",
                "--unit",
                "TEST-ARCHITECTURE",
                "--assignment-year",
                "2026",
                "--archival-label",
                "ARCHITECTURE-TEST",
                "--descriptive-title",
                "Architecture test object",
            )
            self.assertRegex(entity["entity_id"], r"^ISS-2026-[A-Z0-9]{4}-[A-Z0-9]$")
            status = run("core-status", "--root", str(root))
            self.assertEqual("unpublished", status["filesystem_freshness"])

    def test_protected_roots_remain_core_filesystem_gates(self) -> None:
        self.assertEqual(
            (
                Path("/mnt/volume/archive"),
                Path("/mnt/volume/archive-intake"),
            ),
            ARCHIVE.PROTECTED_SOURCE_ROOTS,
        )


if __name__ == "__main__":
    unittest.main()
