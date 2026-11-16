from __future__ import annotations

import importlib.util
import csv
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tools/archive-preservation/kurt_schwarz_identity_provenance.py"
)
SPEC = importlib.util.spec_from_file_location(
    "kurt_schwarz_identity_provenance", MODULE_PATH
)
assert SPEC and SPEC.loader
identity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(identity)


class KurtSchwarzIdentityProvenanceTests(unittest.TestCase):
    def test_alias_maps_to_one_canonical_record(self) -> None:
        mapping = identity.canonical_record_map(
            {"md:1", "md:2", "local:3"},
            [
                {
                    "alias_record_key": "md:2",
                    "canonical_record_key": "md:1",
                    "action": "same_object_alias",
                }
            ],
        )
        self.assertEqual("md:1", mapping["md:1"])
        self.assertEqual("md:1", mapping["md:2"])
        self.assertEqual("local:3", mapping["local:3"])

    def test_alias_chain_is_rejected(self) -> None:
        with self.assertRaisesRegex(identity.IdentityProvenanceError, "chains"):
            identity.canonical_record_map(
                {"md:1", "md:2", "md:3"},
                [
                    {
                        "alias_record_key": "md:3",
                        "canonical_record_key": "md:2",
                        "action": "same_object_alias",
                    },
                    {
                        "alias_record_key": "md:2",
                        "canonical_record_key": "md:1",
                        "action": "same_object_alias",
                    },
                ],
            )

    def test_allocated_ids_use_frozen_check_contract(self) -> None:
        assigned = identity.allocate_entity_ids(
            {"md:1", "md:2", "local:3"},
            year=2026,
            reserved={"ISS-2026-K7M3-J"},
        )
        self.assertEqual(3, len(set(assigned.values())))
        self.assertNotIn("ISS-2026-K7M3-J", assigned.values())
        self.assertTrue(
            all(identity.catalog_core.validate_entity_id(value) for value in assigned.values())
        )

    def test_collection_roles_keep_555_separate(self) -> None:
        self.assertEqual(
            ("pressebildarchiv_wf_unit_scope", ""),
            identity.collection_role("555"),
        )
        self.assertEqual(
            ("kurt_schwarz_subdivision", "783"),
            identity.collection_role("846"),
        )

    def test_reusable_entity_map_must_match_keys_and_validate(self) -> None:
        assigned = identity.allocate_entity_ids(
            {"md:1", "md:2"}, year=2026, reserved=set()
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "entities.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=("canonical_record_key", "entity_id"),
                )
                writer.writeheader()
                for key, value in assigned.items():
                    writer.writerow(
                        {"canonical_record_key": key, "entity_id": value}
                    )
            self.assertEqual(
                assigned,
                identity.read_reusable_entity_ids(path, {"md:1", "md:2"}),
            )


if __name__ == "__main__":
    unittest.main()
