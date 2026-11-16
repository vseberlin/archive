#!/usr/bin/env python3
"""Build a concise TOC for the selected Trafo metadata-source artifacts."""

from __future__ import annotations

import csv
import json
from pathlib import Path


RUN_DIR = Path(
    "/mnt/volume/iss-archive-work/trafo/work/deep-search/"
    "trafo-documentation-usb-20260727-001"
)
STRUCTURE_SOURCE = RUN_DIR / "metadata-source-structure.json"
CSV_OUTPUT = RUN_DIR / "metadata-source-table-of-contents.csv"
MD_OUTPUT = RUN_DIR / "metadata-source-table-of-contents.md"

USB_PREFIX = "/media/vladimir/Elements/restore/archive/WF/Trafo/"
GOOGLE_PREFIX = (
    "/media/vladimir/Elements/restore/archive/WF/GoogleDocsBackup/"
    "drive-download-20220713T123037Z-001.zip!/"
)


CATALOGUE = {
    "1.Upload/WFS_Trafo_csv_xml.zip": (
        "First-upload XML export package: 617 per-record XML files from the "
        "combined upload dataset.",
        "Mixed — contains the first museum-digital upload lineage for Trafo "
        "issues together with WF-Sender flyer records.",
    ),
    "2.Upload/csv_xml.zip": (
        "Second-upload XML export package: 1,043 per-record XML files.",
        "Direct — export-level metadata for the later Trafo newspaper issue batch.",
    ),
    "1.Upload/Archiv/TRO 53,62,63,64 Basisangaben.xlsx": (
        "Archived base-data snapshot for issues from 1953 and "
        "1962–1964; fields include issue, date, volume, number, title, publisher, "
        "pages and price.",
        "Direct — early descriptive metadata for selected Trafo newspaper years.",
    ),
    "1.Upload/Archiv/TRO Basisangaben-Stand 17.11.20.xlsx": (
        "Archived 17 Nov 2020 base-data snapshot with about 502 populated issue "
        "rows.",
        "Direct — working metadata for the first Trafo museum-digital upload.",
    ),
    "1.Upload/Archiv/TRO-Ausgaben_WF-Trafo-Flugblätter_06_Description-fertig.xlsx": (
        "Archived completed-description revision of the combined first-upload "
        "sheet; 618 metadata records plus header.",
        "Mixed — Trafo issue descriptions plus WF-Sender flyer records in one "
        "upload workflow.",
    ),
    "1.Upload/Flugblätter.xlsx": (
        "Early 118-row working list of HF/WF-Sender special-edition flyers with "
        "dates, titles, publishers, dimensions and page counts.",
        "Indirect — WF-Sender material, retained because it was bundled into the "
        "first Trafo upload lineage; not Trafo newspaper holdings.",
    ),
    "1.Upload/Flugblätter_02.xlsx": (
        "Revised 115-record WF-Sender flyer metadata sheet mapped toward "
        "museum-digital fields.",
        "Indirect — WF-Sender flyer data carried through the mixed first-upload "
        "workflow; not Trafo newspaper holdings.",
    ),
    "1.Upload/TRO 53,62,63,64 Basisangaben.xlsx": (
        "Working copy of the 1953 and 1962–1964 base data; its inventory values "
        "use WFS prefixes despite describing Der Transformator issues.",
        "Direct, with naming anomaly — Trafo issue metadata whose working IDs "
        "were temporarily labelled WFS.",
    ),
    "1.Upload/TRO-Ausgaben_WF-Trafo-Flugblätter_02.xlsx": (
        "Early combined sheet of roughly 512 issue records with normalized dates "
        "and basic physical description.",
        "Mostly direct/mixed — Trafo issue data at this stage of the combined "
        "Trafo/WF-Sender upload lineage.",
    ),
    "1.Upload/TRO-Ausgaben_WF-Trafo-Flugblätter_03_zusammen.xlsx": (
        "Merged working dataset with 620 records plus a second sheet matching 620 "
        "source PDF names to inventory identifiers.",
        "Mixed — joins Trafo newspaper issues and WF-Sender flyers and records "
        "which scan files were found.",
    ),
    "1.Upload/TRO-Ausgaben_WF-Trafo-Flugblätter_04.xlsx": (
        "Combined upload revision introducing museum-digital collection, object "
        "type, generated title and description fields.",
        "Mixed — museum-digital preparation for Trafo issues and bundled "
        "WF-Sender flyers.",
    ),
    "1.Upload/TRO-Ausgaben_WF-Trafo-Flugblätter_05.xlsx": (
        "Combined upload revision with generated narrative object descriptions.",
        "Mixed — descriptive metadata for Trafo issues and bundled WF-Sender "
        "flyers.",
    ),
    "1.Upload/TRO-Ausgaben_WF-Trafo-Flugblätter_06_ohne-Description-Formel.xlsx": (
        "Expanded museum-digital import-schema revision with description formulas "
        "removed and rights, places, actors, images, tags and remarks fields.",
        "Mixed — near-import metadata for Trafo issues and bundled WF-Sender "
        "flyers.",
    ),
    "1.Upload/TRO-Ausgaben_WF-Trafo-Flugblätter_07.xlsx": (
        "Later cleanup revision of the expanded first-upload dataset.",
        "Mixed — continued preparation of Trafo issue and WF-Sender flyer records.",
    ),
    "1.Upload/TRO-Ausgaben_WF-Trafo-Flugblätter_08_AF-korr.xlsx": (
        "Correction revision labelled “AF-korr” in the first-upload lineage.",
        "Mixed — corrected Trafo issue and bundled WF-Sender flyer upload data.",
    ),
    "1.Upload/TRO-Ausgaben_WF-Trafo_Flugblaetter_09_final.csv": (
        "Final first-upload CSV, 617 metadata records plus header.",
        "Mixed — final machine-readable import for Trafo issues together with "
        "WF-Sender flyers.",
    ),
    "1.Upload/TRO-Ausgaben_WF-Trafo_Flugblaetter_09_final.xlsx": (
        "Spreadsheet counterpart of the final first-upload dataset, 617 records "
        "plus header.",
        "Mixed — editable final source for the combined Trafo/WF-Sender import.",
    ),
    "2.Upload/Annette_wetransfer-8f8507/TRO Basisangaben-Stand 11.12.20.xlsx": (
        "11 Dec 2020 transfer copy separating candidate rows (“neu”) from about "
        "512 records already on museum-digital.",
        "Direct — base data and reconciliation source for the second Trafo upload.",
    ),
    "2.Upload/TRO Basisangaben_02.xlsx": (
        "Second revision of the two-sheet new-versus-already-on-museum-digital "
        "Trafo base-data workbook.",
        "Direct — second-upload reconciliation and descriptive metadata.",
    ),
    "2.Upload/TRO Basisangaben_03.xlsx": (
        "Revised candidate set of 1,043 records, with separate existing-record "
        "and scan-availability sheets.",
        "Direct — identifies new Trafo issue metadata and available source scans.",
    ),
    "2.Upload/TRO-Basisangaben_04.xlsx": (
        "Consolidated 1,043-record candidate sheet mapped into an expanded "
        "museum-digital field structure.",
        "Direct — working import metadata for the second Trafo issue batch.",
    ),
    "2.Upload/TRO-Basisangaben_05_korrigierter-Herausgeber.xlsx": (
        "Second-upload revision with corrected publisher/editor information.",
        "Direct — corrected descriptive metadata for Trafo issues.",
    ),
    "2.Upload/TRO-Basisangaben_06_Formel-raus.xlsx": (
        "Expanded second-upload import sheet with formulas removed and values "
        "retained across museum-digital fields.",
        "Direct — near-final Trafo issue import metadata.",
    ),
    "2.Upload/TRO-Basisangaben_07_final.csv": (
        "Final second-upload CSV, 1,043 metadata records plus header.",
        "Direct — machine-readable final import for the later Trafo issue batch.",
    ),
    "2.Upload/TRO-Basisangaben_07_final.xlsx": (
        "Editable spreadsheet counterpart of the final 1,043-record second-upload "
        "dataset.",
        "Direct — final source workbook for the later Trafo issue import.",
    ),
    "2.Upload/TRO-Basisangaben_08_finalAG.csv": (
        "AG-labelled final CSV revision, 1,043 metadata records plus header.",
        "Direct — later final-form Trafo import metadata; retained as a distinct "
        "provenance revision.",
    ),
    "2.Upload/TRO-Basisangaben_08_finalAG.xlsx": (
        "Editable AG-labelled final spreadsheet revision of the 1,043-record "
        "second upload.",
        "Direct — later final-form Trafo source workbook retained for provenance.",
    ),
    "Trafo_Bestand.xlsx": (
        "Readable 47-row holdings/scan-planning workbook organized by year, size, "
        "scan status and supplementary issues.",
        "Direct — collection-level Trafo holdings and digitization-planning source.",
    ),
    "Trafo_Bestand_02.xlsx": (
        "Second holdings workbook; content cannot currently be read because the "
        "XLSX ZIP central directory is damaged.",
        "Direct by provenance/filename — likely a later Trafo holdings revision, "
        "but content is unverified and must be condition-noted.",
    ),
    (
        "GoogleDocsBackup/drive-download-20220713T123037Z-001.zip!/"
        "Betriebszeitung TRO/TRO Basisangaben-Stand 11.12.20.xlsx"
    ): (
        "Embedded Google Drive backup copy of the 11 Dec 2020 workbook; it has "
        "“neu” and “schon auf md” sheets and differs structurally/size-wise from "
        "the direct transfer copy.",
        "Direct — independent preserved provenance copy of the second-upload "
        "Trafo base-data and museum-digital reconciliation workbook.",
    ),
}


def source_key(path: str) -> str:
    if path.startswith(USB_PREFIX):
        return path[len(USB_PREFIX) :]
    if path.startswith(GOOGLE_PREFIX):
        return "GoogleDocsBackup/drive-download-20220713T123037Z-001.zip!/" + path[
            len(GOOGLE_PREFIX) :
        ]
    return path


def source_location(path: str) -> str:
    if path.startswith(USB_PREFIX):
        return path[len(USB_PREFIX) :]
    if "!/" in path:
        archive, member = path.split("!/", 1)
        return f"{Path(archive).name}!/{member}"
    return path


def structure_summary(source: dict) -> str:
    structure = source["structure"]
    if structure["format"] == "zip":
        extensions = ", ".join(
            f"{count} {ext}" for ext, count in structure["member_extensions"].items()
        )
        return f"{structure['member_count']} members ({extensions})"
    if structure["format"] == "csv":
        return (
            f"{structure['row_count']} rows; delimiter "
            f"{structure['delimiter']!r}; {structure['encoding']}"
        )
    if structure["condition"] != "readable":
        return structure.get("error", "Unreadable")
    sheets = []
    for sheet in structure.get("sheets", []):
        dimension = f" {sheet['dimension']}" if sheet.get("dimension") else ""
        sheets.append(
            f"{sheet['name']}{dimension} ({sheet['xml_row_count']} XML rows)"
        )
    return "; ".join(sheets)


def escape_md(value: object) -> str:
    return str(value).replace("|", r"\|").replace("\n", " ")


def main() -> None:
    source_data = json.loads(STRUCTURE_SOURCE.read_text(encoding="utf-8"))
    rows = []
    used_keys = set()
    for position, source in enumerate(source_data["sources"], start=1):
        key = source_key(source["path"])
        if key not in CATALOGUE:
            raise KeyError(f"No catalogue description for {source['path']}")
        description, relation = CATALOGUE[key]
        used_keys.add(key)
        rows.append(
            {
                "file_name": source["file_name"],
                "short_description": description,
                "relation_to_trafo": relation,
                "source_location": source_location(source["path"]),
                "format": source["structure"]["format"],
                "condition": source["structure"]["condition"],
                "structure": structure_summary(source),
                "source_path_or_container_member": source["path"],
            }
        )

    unused = set(CATALOGUE) - used_keys
    if unused:
        raise RuntimeError(f"Unused catalogue entries: {sorted(unused)}")
    if len(rows) != 30:
        raise RuntimeError(f"Expected 30 sources, found {len(rows)}")

    fields = [
        "file_name",
        "short_description",
        "relation_to_trafo",
        "source_location",
        "format",
        "condition",
        "structure",
        "source_path_or_container_member",
    ]
    with CSV_OUTPUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    lines = [
        "# Trafo metadata sources — table of contents",
        "",
        "Scope: the 30 preservation-source artifacts selected by the 27 July 2026 "
        "USB documentation review (29 direct files and one spreadsheet embedded "
        "in a Google Drive backup ZIP).",
        "",
        "Descriptions are based on filenames, workbook sheets/fields/row "
        "structure, CSV structure and ZIP member listings. No cryptographic "
        "hashing was performed. “Mixed” means the source combines Trafo newspaper "
        "records with WF-Sender flyer records; it does not assign the WF-Sender "
        "material to the Trafo collection.",
        "",
        "| # | File name | Short description | Relation to Trafo | Source location | "
        "Condition |",
        "|---:|---|---|---|---|---|",
    ]
    for position, row in enumerate(rows, start=1):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(position),
                    escape_md(row["file_name"]),
                    escape_md(row["short_description"]),
                    escape_md(row["relation_to_trafo"]),
                    f"`{escape_md(row['source_location'])}`",
                    escape_md(row["condition"]),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Condition note",
            "",
            "`Trafo_Bestand_02.xlsx` is preserved as a source candidate but its "
            "contents were not interpreted: the XLSX ZIP central directory is "
            "damaged. Its description and Trafo relation are therefore explicitly "
            "provisional.",
            "",
            "## Companion evidence",
            "",
            "- `metadata-source-structure.json` records the structural inspection.",
            "- `review-digest.csv` records selection/classification.",
            "- `embedded-source-members.csv` identifies the exact embedded workbook.",
            "",
        ]
    )
    MD_OUTPUT.write_text("\n".join(lines), encoding="utf-8")
    print(
        json.dumps(
            {
                "source_count": len(rows),
                "csv": str(CSV_OUTPUT),
                "markdown": str(MD_OUTPUT),
            }
        )
    )


if __name__ == "__main__":
    main()
