#!/usr/bin/env python3
"""Add a bounded, review-only tube metadata proposal batch to the all-local bench."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


VERSION = "1.0.0"
SOURCE_ROOT = Path("/media/vladimir/Elements")
REVIEW_ROOT = Path("/mnt/volume/iss-archive-work/tfa/work/reconciliation/unit-reconciliation")
CANDIDATES = REVIEW_ROOT / "all-local-metadata-review-candidates.csv"
DECISIONS = REVIEW_ROOT / "all-local-metadata-review-decisions.csv"
RECEIPT = REVIEW_ROOT / "all-local-metadata-review-preparation.json"

PHILIPS_PL83 = (
    "https://frank.pocnet.net/other/Philips/"
    "Philips_ElectronicTubeDivision_ReceivingTubesForTelevision_1951_FP_AdK.pdf"
)
PHILIPS_EL81 = (
    "https://www.worldradiohistory.com/Archive-Catalogs/Suppliers/PQR/"
    "Philips-Electron-tubes-Part-4-june-1972-Receiving-tubes-564-pages.pdf"
)
PHILIPS_EL83 = "https://frank.pocnet.net/sheets/030/e/EL83.pdf"
PCC84_SOURCE = (
    "https://www.worldradiohistory.com/UK/Wireless-World/50s/"
    "Wireless-World-1954-05-S-OCR.pdf"
)
WF_DEVELOPMENT_SOURCE = "https://www.industriesalon.de/wp-content/uploads/2021/03/ER.pdf"
UAA91_SOURCE = "https://datasheet4u.com/datasheets/RFT/UAA91/1036470"
WF_CONTEXT_SOURCE = "https://www.industriesalon.de/das-museum/"

CLASSIFICATION = "Fotostelle WF - Röhren und Halbleiter"
PHOTO_KEYWORDS = "Abzug (Fotografie); Industrie; Sachaufnahme; Schwarzweißfotografie"
IPTC_BASIS = (
    "embedded IPTC/XMP in the WF Freistellen derivative credits Werk für Fernsehelektronik "
    "(WF) and photographer Hans-Joachim Köhler"
)


def proposal(
    title: str,
    description: str,
    keywords: str,
    basis: str,
) -> dict[str, str]:
    return {
        "proposed_title": title,
        "proposed_description": description,
        "proposed_date_text": "",
        "proposed_object_type": "Foto",
        "proposed_material": "Fotoabzug",
        "proposed_dimensions": "",
        "proposed_keywords": f"{PHOTO_KEYWORDS}; {keywords}",
        "proposed_classification": CLASSIFICATION,
        "proposal_basis": basis,
    }


def exact_tube_proposals() -> dict[str, dict[str, str]]:
    common = (
        "Die Röhre trägt die sichtbare Kennzeichnung VEB RFT und den Code 604. "
        "Die Kennzeichnung RFT allein belegt keinen einzelnen Produktionsbetrieb. "
        "Ein Aufnahmedatum ist nicht belegt."
    )
    return {
        "local:TFA-5410009": proposal(
            "Elektronenröhre PL83 mit RFT-Kennzeichnung",
            "Sachaufnahme einer PL83 neben einem Zentimetermaßstab. Die PL83 ist eine "
            f"Video-Endpentode für Fernsehempfänger. {common}",
            "Elektrotechnik; Elektronenröhre; Fernsehen; PL83; RFT; Video-Endpentode; Videoverstärker",
            f"visible PL83 and VEB RFT markings; period Philips television-tube data {PHILIPS_PL83}; "
            f"{IPTC_BASIS}; WF collection context {WF_CONTEXT_SOURCE}; confidence high for type and function",
        ),
        "local:TFA-5410010": proposal(
            "Elektronenröhre EL81 mit RFT-Kennzeichnung",
            "Sachaufnahme einer EL81 neben einem Zentimetermaßstab. Die EL81 ist eine "
            f"Leistungs-Endpentode für die Zeilenablenkung kleiner Fernsehempfänger. {common}",
            "EL81; Elektrotechnik; Elektronenröhre; Fernsehen; RFT; Zeilenablenkung; Zeilenendpentode",
            f"visible EL81 and VEB RFT markings; Philips electron-tube handbook {PHILIPS_EL81}; "
            f"{IPTC_BASIS}; confidence high for type and function",
        ),
        "local:TFA-5410011": proposal(
            "Elektronenröhre EL83 mit RFT-Kennzeichnung",
            "Sachaufnahme einer EL83 neben einem Zentimetermaßstab. Die EL83 ist eine "
            f"Video-Endpentode mit 6,3-V-Heizung. {common}",
            "EL83; Elektrotechnik; Elektronenröhre; Fernsehen; RFT; Video-Endpentode; Videoverstärker",
            f"visible EL83 and VEB RFT markings; period Philips data sheet {PHILIPS_EL83}; "
            f"{IPTC_BASIS}; confidence high for type and function",
        ),
        "local:TFA-5410012": proposal(
            "Elektronenröhre PL81 mit RFT-Kennzeichnung",
            "Sachaufnahme einer PL81 neben einem Zentimetermaßstab. Die PL81 ist eine speziell "
            f"für die Zeilenendstufe von Fernsehempfängern entwickelte Pentode. {common}",
            "Elektrotechnik; Elektronenröhre; Fernsehen; PL81; RFT; Zeilenablenkung; Zeilenendpentode",
            f"visible PL81 and VEB RFT markings; period Philips television-tube data {PHILIPS_PL83}; "
            f"{IPTC_BASIS}; confidence high for type and function",
        ),
        "local:TFA-5410189": proposal(
            "Elektronenröhre PCC84 mit RFT-Kennzeichnung",
            "Sachaufnahme einer PCC84 neben einem Zentimetermaßstab. Die PCC84 ist eine steile "
            "Doppeltriode für rauscharme VHF-Kaskodenverstärker in Fernsehempfängern. "
            "Ein Aufnahmedatum ist nicht belegt.",
            "Doppeltriode; Elektrotechnik; Elektronenröhre; Fernsehen; Hochfrequenztechnik; PCC84; RFT; VHF",
            f"visible PCC84 marking; contemporary Mullard technical notice {PCC84_SOURCE}; "
            f"{IPTC_BASIS}; confidence high for type and function",
        ),
        "local:TFA-5410293": proposal(
            "RFT-Reflexklystron 707B",
            "Sachaufnahme eines mit VEB RFT und 707B gekennzeichneten Reflexklystrons neben einem "
            "Zentimetermaßstab. Der Typ 707B ist eine Mikrowellen-Oszillatorröhre für den "
            "SHF-Bereich; der WF-Entwicklungsbestand nennt ein Projekt „Klystron 707 B“. "
            "Das Foto selbst trägt kein belastbares Aufnahmedatum.",
            "707B; Elektrotechnik; Elektronenröhre; Hochfrequenztechnik; Klystron; Mikrowellentechnik; Reflexklystron; RFT; WF",
            f"visible 707B and VEB RFT markings; WF development index {WF_DEVELOPMENT_SOURCE}; "
            f"{IPTC_BASIS}; confidence high for type, WF development relation supported",
        ),
        "local:TFA-5410499": proposal(
            "WF-Doppeldiode UAA91",
            "Sachaufnahme einer mit dem WF-Zeichen und UAA91 gekennzeichneten Elektronenröhre "
            "neben einem Zentimetermaßstab. Die UAA91 ist eine Doppeldiode, unter anderem für "
            "Demodulator- und Detektorschaltungen. Ein Aufnahmedatum ist nicht belegt.",
            "Detektor; Doppeldiode; Elektrotechnik; Elektronenröhre; Rundfunktechnik; UAA91; WF",
            f"visible WF and UAA91 markings; RFT UAA91 data sheet {UAA91_SOURCE}; "
            f"{IPTC_BASIS}; confidence high for type and maker mark",
        ),
    }


def generic_tube_proposals() -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    source_basis = (
        f"visual review of the selected Master and adjacent tube-photo series; {IPTC_BASIS}; "
        "no reliable type or photograph date visible; confidence high for object class, low for exact type"
    )

    result["local:TFA-5410050"] = proposal(
        "Experimentelle Elektronenröhre mit Maßstab",
        "Sachaufnahme einer handbeschrifteten experimentellen Elektronenröhre mit sichtbarem "
        "Elektrodensystem und Zentimetermaßstab. Typ und Funktion sind nicht sicher bestimmt.",
        "Elektrotechnik; Elektronenröhre; Röhrenentwicklung; Versuchsaufbau",
        source_basis,
    )
    result["local:TFA-5410051"] = proposal(
        "Offener Versuchsaufbau einer Elektronenröhre",
        "Sachaufnahme eines offenen Glaskolbens mit Elektrodensystem. Der handschriftliche "
        "Kartenvermerk nennt Kennlinien; die genaue Röhrenfunktion bleibt ungeklärt.",
        "Elektrotechnik; Elektronenröhre; Kennlinie; Röhrenentwicklung; Versuchsaufbau",
        source_basis,
    )

    component_titles = {
        5410106: "Röhrensystem auf Metallsockel",
        5410107: "Röhrenbauteil mit Metallzylinder, Ansicht 1",
        5410108: "Röhrenbauteil mit Metallzylinder, Ansicht 2",
    }
    for inventory, title in component_titles.items():
        result[f"local:TFA-{inventory}"] = proposal(
            title,
            "Sachaufnahme eines Bauteils bzw. Elektrodensystems einer Elektronenröhre. "
            "Die genaue Röhrentype und Funktion sind auf dem Foto nicht sicher bestimmbar.",
            "Bauteil; Elektrodensystem; Elektrotechnik; Elektronenröhre; Röhrenaufbau",
            source_basis,
        )

    picture_titles = {
        5410109: "Bildröhre mit konischem Kolben, Vorderansicht",
        5410110: "Bildröhre mit konischem Kolben, Rückansicht",
        5410111: "Bildröhre mit konischem Kolben, Seitenansicht",
        5410112: "Bildröhre mit konischem Kolben, liegend",
        5410113: "Langhalsige Bildröhre mit Maßstab, Ansicht 1",
        5410114: "Langhalsige Bildröhre mit Maßstab, Ansicht 2",
        5410115: "Langhalsige Bildröhre mit Maßstab, Ansicht 3",
        5410116: "Bildröhre mit konischem Kolben, liegend",
        5410117: "Bildröhre mit konischem Kolben, Ansicht 1",
        5410118: "Bildröhre mit konischem Kolben, Ansicht 2",
        5410119: "Rechteckiger Schirm einer Bildröhre",
        5410120: "Bildröhre mit konischem Kolben",
        5410121: "Langhalsige Bildröhre mit Maßstab, Ansicht 1",
        5410122: "Langhalsige Bildröhre mit Maßstab, Ansicht 2",
        5410123: "Langhalsige Bildröhre mit Maßstab, Ansicht 3",
        5410124: "Bildröhre mit konischem Kolben, Ansicht 1",
        5410125: "Bildröhre mit konischem Kolben, Ansicht 2",
        5410126: "Bildröhre mit konischem Kolben, liegend",
        5410127: "Bildröhre mit konischem Kolben, Ansicht 3",
        5410128: "Bildröhre mit konischem Kolben, Seitenansicht",
        5410129: "Langhalsige Bildröhre mit Maßstab, Ansicht 1",
        5410130: "Langhalsige Bildröhre mit Maßstab, Ansicht 2",
        5410131: "Langhalsige Bildröhre mit Maßstab, Ansicht 3",
    }
    for inventory, title in picture_titles.items():
        result[f"local:TFA-{inventory}"] = proposal(
            title,
            "Sachaufnahme einer Bildröhre bzw. eines Bildröhrenkolbens. Bauform und Ansicht "
            "sind sichtbar; eine genaue Typenbezeichnung ist nicht sicher lesbar.",
            "Bildröhre; Elektrotechnik; Elektronenröhre; Fernsehen; Fernsehtechnik",
            source_basis,
        )

    for inventory in range(5410132, 5410147):
        result[f"local:TFA-{inventory}"] = proposal(
            "Elektronenröhre mit Maßstab",
            "Sachaufnahme einer Elektronenröhre mit sichtbarem innerem Elektrodensystem und "
            "Zentimetermaßstab. Die Beschriftung reicht nicht für eine sichere Typenbestimmung.",
            "Elektrodensystem; Elektrotechnik; Elektronenröhre; Röhrenaufbau",
            source_basis,
        )
    for inventory in range(5410147, 5410153):
        result[f"local:TFA-{inventory}"] = proposal(
            "Große Sender- oder Leistungsröhre mit Maßstab",
            "Sachaufnahme einer großen Elektronenröhre mit Metall-Glas-Aufbau und "
            "Zentimetermaßstab. Die genaue Type und elektrische Funktion sind nicht sicher bestimmt.",
            "Elektrotechnik; Elektronenröhre; Hochfrequenztechnik; Leistungsröhre; Senderöhre",
            source_basis,
        )

    component_views = {
        5410183: (
            "Glaskolben und Anodenzylinder einer Großröhre",
            "Sachaufnahme des Glaskolbens mit großem innerem Metallzylinder einer Großröhre.",
        ),
        5410184: (
            "Gitter- und Elektrodensystem einer Großröhre",
            "Sachaufnahme des freiliegenden Gitter- und Elektrodensystems einer Großröhre.",
        ),
        5410185: (
            "Sockel- und Elektrodensystem einer Großröhre",
            "Sachaufnahme des Sockels mit Anschlussdurchführungen und Gitteraufbau einer Großröhre.",
        ),
    }
    for inventory, (title, description) in component_views.items():
        result[f"local:TFA-{inventory}"] = proposal(
            title,
            f"{description} Die genaue Röhrentype ist nicht belegt.",
            "Bauteil; Elektrodensystem; Elektrotechnik; Elektronenröhre; Röhrenaufbau",
            source_basis,
        )

    result["local:TFA-5410390"] = proposal(
        "Große Sender- oder Leistungsröhre mit Maßstab",
        "Sachaufnahme einer etwa 30 cm hohen Elektronenröhre mit großem Metall-Glas-Aufbau "
        "neben einem Zentimetermaßstab. Die genaue Type und Funktion sind nicht sicher bestimmt.",
        "Elektrotechnik; Elektronenröhre; Hochfrequenztechnik; Leistungsröhre; Senderöhre",
        source_basis,
    )
    return result


ENRICHMENTS = generic_tube_proposals() | exact_tube_proposals()


class EnrichmentError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or ()), list(reader)


def fingerprint(row: dict[str, Any], fields: list[str]) -> str:
    protected = {
        key: str(row.get(key, ""))
        for key in fields
        if key not in {"candidate_fingerprint", "deletion_authorized"}
    }
    payload = json.dumps(protected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def validate_mount() -> None:
    completed = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
    )
    filesystems = json.loads(completed.stdout).get("filesystems", [])
    if len(filesystems) != 1:
        raise EnrichmentError("protected source mount is ambiguous")
    mount = filesystems[0]
    options = set(str(mount.get("options", "")).split(","))
    if mount.get("fstype") != "ntfs3" or "ro" not in options:
        raise EnrichmentError("protected source must be mounted ntfs3 read-only")


def validate(rows: list[dict[str, str]], fields: list[str], receipt: dict[str, Any]) -> None:
    if (
        receipt.get("format") != "iss-tfa-all-local-metadata-review-v1"
        or receipt.get("source_read_only") is not True
        or receipt.get("source_modified") is not False
        or receipt.get("automatic_metadata_application") is not False
        or receipt.get("selected_master_decisions_changed") is not False
        or receipt.get("deletion_authorized") is not False
        or len(rows) != 2483
        or len({row.get("record_key", "") for row in rows}) != len(rows)
        or any(row.get("deletion_authorized") != "false" for row in rows)
        or any(row.get("candidate_fingerprint") != fingerprint(row, fields) for row in rows)
    ):
        raise EnrichmentError("all-local metadata projection is unsafe or drifted")


def atomic_csv(fields: list[str], rows: list[dict[str, str]]) -> None:
    temporary = CANDIDATES.with_name(f".{CANDIDATES.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, CANDIDATES)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(payload: dict[str, Any]) -> None:
    temporary = RECEIPT.with_name(f".{RECEIPT.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, RECEIPT)
    finally:
        temporary.unlink(missing_ok=True)


def apply() -> dict[str, Any]:
    validate_mount()
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    if receipt.get("candidate_sha256") != sha256(CANDIDATES):
        raise EnrichmentError("candidate CSV does not match its guarded receipt")
    fields, rows = read_csv(CANDIDATES)
    validate(rows, fields, receipt)
    _, decisions = read_csv(DECISIONS)
    target_keys = set(ENRICHMENTS)
    if target_keys & {row.get("record_key", "") for row in decisions}:
        raise EnrichmentError("refusing to replace a human decision with a proposal")
    by_key = {row["record_key"]: row for row in rows}
    if target_keys - set(by_key):
        raise EnrichmentError(f"target records missing: {sorted(target_keys - set(by_key))}")
    for record_key, values in ENRICHMENTS.items():
        row = by_key[record_key]
        if row.get("prior_review_state"):
            raise EnrichmentError(f"target already has prior review state: {record_key}")
        row.update(values)
        row["candidate_fingerprint"] = fingerprint(row, fields)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate_backup = REVIEW_ROOT / f"all-local-metadata-review-candidates.pre-tube-web-{stamp}.csv"
    receipt_backup = REVIEW_ROOT / f"all-local-metadata-review-preparation.pre-tube-web-{stamp}.json"
    shutil.copy2(CANDIDATES, candidate_backup)
    shutil.copy2(RECEIPT, receipt_backup)
    atomic_csv(fields, rows)
    receipt["candidate_sha256"] = sha256(CANDIDATES)
    receipt["tube_metadata_enrichment"] = {
        "format": "iss-tfa-all-local-tube-metadata-enrichment-v1",
        "version": VERSION,
        "enriched_at": utc_now(),
        "record_keys": sorted(target_keys),
        "proposal_count": len(target_keys),
        "exact_type_count": len(exact_tube_proposals()),
        "photograph_dates_supplied": 0,
        "human_metadata_decisions_written": 0,
        "automatic_metadata_application": False,
        "selected_master_decisions_changed": False,
        "source_modified": False,
        "deletion_authorized": False,
    }
    atomic_json(receipt)
    return {
        "result": "ok",
        "proposals_enriched": len(target_keys),
        "exact_types": len(exact_tube_proposals()),
        "photograph_dates_supplied": 0,
        "human_decisions_written": 0,
        "source_modified": False,
        "selected_master_decisions_changed": False,
        "deletion_authorized": False,
        "candidate_backup": str(candidate_backup),
        "receipt_backup": str(receipt_backup),
    }


def check() -> dict[str, Any]:
    validate_mount()
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    if receipt.get("candidate_sha256") != sha256(CANDIDATES):
        raise EnrichmentError("candidate CSV does not match its guarded receipt")
    fields, rows = read_csv(CANDIDATES)
    validate(rows, fields, receipt)
    by_key = {row["record_key"]: row for row in rows}
    for record_key, values in ENRICHMENTS.items():
        if any(by_key.get(record_key, {}).get(key) != value for key, value in values.items()):
            raise EnrichmentError(f"tube proposal drifted: {record_key}")
    enrichment = receipt.get("tube_metadata_enrichment", {})
    if (
        enrichment.get("proposal_count") != len(ENRICHMENTS)
        or enrichment.get("exact_type_count") != len(exact_tube_proposals())
        or enrichment.get("photograph_dates_supplied") != 0
        or enrichment.get("human_metadata_decisions_written") != 0
        or enrichment.get("automatic_metadata_application") is not False
        or enrichment.get("selected_master_decisions_changed") is not False
        or enrichment.get("source_modified") is not False
        or enrichment.get("deletion_authorized") is not False
    ):
        raise EnrichmentError("tube enrichment receipt is unsafe or drifted")
    return {
        "result": "ok",
        "proposals_enriched": len(ENRICHMENTS),
        "exact_types": len(exact_tube_proposals()),
        "photograph_dates_supplied": 0,
        "human_decisions_written": 0,
        "source_read_only": True,
        "deletion_authorized": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.apply == args.check:
        raise SystemExit("choose exactly one of --apply or --check")
    result = apply() if args.apply else check()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
