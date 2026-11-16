#!/usr/bin/env python3
"""Import museum-digital comparison runs into the V2 reconciliation campaign."""

from __future__ import annotations

import argparse
import json
from contextlib import closing
from pathlib import Path

import catalog_core
import reconciliation


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    command = subparsers.add_parser("init")
    command.add_argument("--root", required=True)

    command = subparsers.add_parser("import-run")
    command.add_argument("--root", required=True)
    command.add_argument("--run", required=True)
    command.add_argument("--campaign", default="")
    command.add_argument("--title", default="")
    command.add_argument(
        "--record",
        action="append",
        default=[],
        help="import only this inventory, local ID/label, or MD ID; repeatable",
    )

    command = subparsers.add_parser("status")
    command.add_argument("--root", required=True)

    command = subparsers.add_parser("attach-preview")
    command.add_argument("--root", required=True)
    command.add_argument("--evidence-id", required=True)
    command.add_argument("--source", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = Path(args.root).expanduser()
        if args.command == "init":
            result = {
                "result": "ok",
                "campaign_db": str(reconciliation.initialize_campaign_store(root)),
            }
        elif args.command == "import-run":
            result = reconciliation.import_museum_digital_run(
                root,
                Path(args.run).expanduser(),
                campaign_id=args.campaign,
                title=args.title,
                record_keys=tuple(args.record),
            )
        elif args.command == "status":
            with closing(
                reconciliation.connect_campaign(root, read_only=True)
            ) as campaign, closing(
                catalog_core.connect_catalog(
                    root / "work" / "catalog.sqlite",
                    read_only=True,
                )
            ) as catalog:
                result = {
                    "result": "ok",
                    "summary": reconciliation.campaign_summary(campaign),
                    "health": reconciliation.campaign_health(campaign, catalog),
                }
        else:
            result = reconciliation.attach_visual_preview(
                root,
                evidence_id=args.evidence_id,
                source_path=Path(args.source),
            )
    except (catalog_core.CoreCatalogError, reconciliation.ReconciliationError) as error:
        print(json.dumps({"result": "error", "error": str(error)}, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
