#!/usr/bin/env python3
"""Add a bounded, review-only technical metadata proposal batch to the all-local bench."""

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

ARCHIVE_SOURCE = (
    "/mnt/volume/archive/WF/Thesaurus/_Archiv-Ordner-Thesaurus/"
    "Alle-md-Texte_Schlagwörter_31-01-2020_02.xlsx"
)
ELEKTRA_SOURCE = "https://www.radiomuseum.org/dsp_hersteller_detail.cfm?company_id=14610"
ELEKTRA_COMPANY = "https://www.elektra-schalkau.de/"
EAW_SOURCE = "https://www.deutsche-digitale-bibliothek.de/item/DICLWOH7F6UUAYHINEGDRZYAC5UKOXEY"
STEP_SWITCH_SOURCE = "https://patents.google.com/patent/EP0048811B1/de"

COMMON_CONTEXT = (
    "Besteller: Langer, von 1952 bis 1958 für Bauelemente in der Entwicklung verantwortlich. "
    "Streichholzschachtel als Größenvergleich. Foto der Bildstelle des WF, Februar 1955. "
    "Nach dem überlieferten Findbucheintrag wurde das Negativ 1968 im Zusammenhang mit der "
    "Auslagerung der Messelektronik an den VEB Meßelektronik Berlin abgegeben; im WF-Archiv "
    "blieb der Fotoabzug."
)

ENRICHMENTS: dict[str, dict[str, str]] = {
    "local:TFA-538520": {
        "proposed_title": "Chassis eines Fernsehempfängers, Unteransicht",
        "proposed_description": (
            "Teilansicht des Chassis eines Fernsehgeräts, auf dem die Bildröhre montiert wird, "
            "ohne Gehäuse von unten gesehen. Sichtbar sind unter anderem Lautsprecher, "
            "Transformator, Widerstände, Kondensatoren, Anschlussleisten und zwei Bedienachsen. "
            "Besteller: Görnandt, von 1948 bis 1958 als Verantwortlicher für die Gerätekonstruktion "
            "in der Entwicklung nachweisbar. Foto der Bildstelle des WF, 9. Oktober 1953."
        ),
        "proposed_date_text": "9. Oktober 1953",
        "proposed_object_type": "Foto",
        "proposed_material": "SW-Negativ, Planfilm",
        "proposed_dimensions": "18x24 cm",
        "proposed_keywords": (
            "Elektrotechnik; Fernsehgerät; Gerätebau; Industrie; Negativ (Fotografie); Planfilm; "
            "Sachaufnahme; Schwarzweißfotografie; Teilansicht"
        ),
        "proposed_classification": (
            "Fotostelle WF - Geräte, Einschübe, Bauteile; "
            "Fotostelle WF - Telekommunikation, Sende- und Fernsehtechnik"
        ),
        "proposal_basis": (
            "/mnt/volume/archive/WF/Thesaurus/14_neue-scans/"
            "Kopie von 1953_und_1957-fuer_md.-korr_05_FINALE.AG.xlsx, row 114; "
            "archive caption and visual confirmation; confidence high; WF photographic and "
            "development context established, component manufacturers not attributed"
        ),
    },
    "local:TFA-5510602": {
        "proposed_title": "Transformator für 220/440 V",
        "proposed_description": (
            "Sachaufnahme eines Transformators mit zylindrischer Wicklung, Blechkern und "
            "Anschlussklemmen. Das sichtbare Typenschild nennt 220 V, 50 Hz, 440 V, 0,006 A "
            "und 2,4 kV~ Prüfspannung; die Typ- bzw. Zeichnungsangaben sind nur teilweise "
            f"lesbar. {COMMON_CONTEXT} Ein Hersteller ist nicht belegt."
        ),
        "proposed_date_text": "Februar 1955",
        "proposed_object_type": "Foto",
        "proposed_material": "Fotoabzug",
        "proposed_dimensions": "12x15,7 cm",
        "proposed_keywords": (
            "Abzug (Fotografie); Bauelement; Elektrotechnik; Industrie; Sachaufnahme; "
            "Schwarzweißfotografie; Transformator"
        ),
        "proposed_classification": "Fotostelle WF - Geräte, Einschübe, Bauteile",
        "proposal_basis": (
            f"archive source {ARCHIVE_SOURCE}, row 829; visual reading of electrical ratings; "
            "confidence high for component class, low for unreadable type code; manufacturer "
            "unknown; no WF or EAW maker attribution"
        ),
    },
    "local:TFA-5510603": {
        "proposed_title": "Zweifach-Luftdrehkondensator der Elektra OHG, Schalkau/Th.",
        "proposed_description": (
            "Sachaufnahme eines Zweifach-Luftdrehkondensators in offenem Metallgehäuse. "
            "Zwei ineinandergreifende Rotor- und Statorplattenpakete sitzen auf einer gemeinsamen "
            "Welle. Das Herstelleretikett nennt Elektra und Schalkau/Th.; die Zuordnung zur Elektra "
            f"OHG Oelsner & Hess wird durch die Herstellerüberlieferung gestützt. {COMMON_CONTEXT}"
        ),
        "proposed_date_text": "Februar 1955",
        "proposed_object_type": "Foto",
        "proposed_material": "Fotoabzug",
        "proposed_dimensions": "15,9x10,8 cm",
        "proposed_keywords": (
            "Abzug (Fotografie); Drehkondensator; Elektra OHG Oelsner & Hess; Elektrotechnik; "
            "Industrie; Kondensator; Sachaufnahme; Schwarzweißfotografie"
        ),
        "proposed_classification": "Fotostelle WF - Geräte, Einschübe, Bauteile",
        "proposal_basis": (
            f"archive source {ARCHIVE_SOURCE}, row 830; photographed maker label Elektra, "
            f"Schalkau/Th.; web manufacturer history {ELEKTRA_SOURCE}; current company location "
            f"corroboration {ELEKTRA_COMPANY}; confidence high; not EAW"
        ),
    },
    "local:TFA-5510604": {
        "proposed_title": "Stabförmiges Bauelement, vermutlich Regler oder Fühler",
        "proposed_description": (
            "Sachaufnahme eines nicht beschrifteten elektromechanischen Bauelements mit "
            "zylindrischem Metallgehäuse, stirnseitigem Anschluss bzw. Lager und langem flachem "
            f"Metallstab. Die genaue Funktion und der Hersteller bleiben ungeklärt. {COMMON_CONTEXT}"
        ),
        "proposed_date_text": "Februar 1955",
        "proposed_object_type": "Foto",
        "proposed_material": "Fotoabzug",
        "proposed_dimensions": "15,8x11,7 cm",
        "proposed_keywords": (
            "Abzug (Fotografie); Bauelement; Elektrotechnik; Industrie; Regler; Sachaufnahme; "
            "Schwarzweißfotografie"
        ),
        "proposed_classification": "Fotostelle WF - Geräte, Einschübe, Bauteile",
        "proposal_basis": (
            f"archive source {ARCHIVE_SOURCE}, row 831; visual description only; confidence low; "
            "deep-web comparison did not establish an exact part or maker; no WF or EAW maker attribution"
        ),
    },
    "local:TFA-5510605": {
        "proposed_title": "Mehrpolige Dreh- und Stufenschalter mit keramischen Kontaktträgern",
        "proposed_description": (
            "Sachaufnahme zweier offener mehrpoliger Dreh- bzw. Stufenschalter. Sichtbar sind "
            "kreisförmig angeordnete Lötanschlüsse, keramische Kontaktträger, bewegliche "
            f"Kontaktarme und bei einem Exemplar zwei gekoppelte Schaltebenen. {COMMON_CONTEXT} "
            "Ein Hersteller ist nicht belegt."
        ),
        "proposed_date_text": "Februar 1955",
        "proposed_object_type": "Foto",
        "proposed_material": "Fotoabzug",
        "proposed_dimensions": "15,9x11 cm",
        "proposed_keywords": (
            "Abzug (Fotografie); Bauelement; Drehschalter; Elektrotechnik; Industrie; "
            "Sachaufnahme; Schwarzweißfotografie; Stufenschalter"
        ),
        "proposed_classification": "Fotostelle WF - Geräte, Einschübe, Bauteile",
        "proposal_basis": (
            f"archive source {ARCHIVE_SOURCE}, row 832; visual construction agrees with generic "
            f"multi-level step-switch terminology in {STEP_SWITCH_SOURCE}; EAW context {EAW_SOURCE} "
            "shows that EAW made switchgear but does not identify this part; confidence medium; maker unknown"
        ),
    },
    "local:TFA-5510606": {
        "proposed_title": "Mehrstufiger Drehschalter, Vorder- und Rückansicht",
        "proposed_description": (
            "Sachaufnahme eines gekapselten mehrstufigen Drehschalters in zwei Ansichten. "
            "Erkennbar sind der Bedienknopf, das Rast- bzw. Schaltwerk und zahlreiche kreisförmig "
            f"angeordnete Anschlüsse auf der Rückseite. {COMMON_CONTEXT} Ein Hersteller ist nicht belegt."
        ),
        "proposed_date_text": "Februar 1955",
        "proposed_object_type": "Foto",
        "proposed_material": "Fotoabzug",
        "proposed_dimensions": "15,9x11,8 cm",
        "proposed_keywords": (
            "Abzug (Fotografie); Bauelement; Drehschalter; Elektrotechnik; Industrie; "
            "Sachaufnahme; Schwarzweißfotografie; Stufenschalter"
        ),
        "proposed_classification": "Fotostelle WF - Geräte, Einschübe, Bauteile",
        "proposal_basis": (
            f"archive source {ARCHIVE_SOURCE}, row 833; visual construction agrees with generic "
            f"step-switch terminology in {STEP_SWITCH_SOURCE}; EAW context {EAW_SOURCE} is only "
            "plausibility evidence, not identification; confidence medium; maker unknown"
        ),
    },
    "local:TFA-5510607": {
        "proposed_title": "Mehrdeck-Drehschalter verschiedener Baugrößen",
        "proposed_description": (
            "Sachaufnahme von vier mehrpoligen Drehschaltern verschiedener Baugrößen. Drei "
            "Exemplare besitzen mehrere hintereinander gekoppelte Schaltebenen auf einer langen "
            "Welle; ein kleineres Exemplar ist von der Anschlussseite gezeigt. Kreisförmig "
            f"angeordnete Kontakte und Kontaktbrücken sind sichtbar. {COMMON_CONTEXT} Ein Hersteller ist nicht belegt."
        ),
        "proposed_date_text": "Februar 1955",
        "proposed_object_type": "Foto",
        "proposed_material": "Fotoabzug",
        "proposed_dimensions": "16x11 cm",
        "proposed_keywords": (
            "Abzug (Fotografie); Bauelement; Drehschalter; Elektrotechnik; Industrie; "
            "Mehrdeck-Drehschalter; Sachaufnahme; Schwarzweißfotografie; Stufenschalter"
        ),
        "proposed_classification": "Fotostelle WF - Geräte, Einschübe, Bauteile",
        "proposal_basis": (
            f"archive source {ARCHIVE_SOURCE}, row 834; visual construction agrees with generic "
            f"multi-level step-switch terminology in {STEP_SWITCH_SOURCE}; EAW context {EAW_SOURCE} "
            "does not establish a maker; confidence high for component class, maker unknown"
        ),
    },
    "local:TFA-5713358": {
        "proposed_title": "WF-Reporter-Einrichtung für eine Fernsehreportage",
        "proposed_description": (
            "Sachaufnahme einer aus vier Geräten bestehenden Anlage für eine "
            "Fernsehreportagekamera, im Findbuch als „Reporter-Einrichtung“ bezeichnet. "
            "Die Geräte tragen WF-Marken; gezeigt werden Bedien- und Kontrolleinschübe sowie "
            "ein Bildmonitor. Ein Bleistiftvermerk im Findbuch nennt die Firma Kolbe als "
            "Fotografin bzw. Auftragnehmerin auf Veranlassung der Werbeabteilung und trägt das "
            "Kürzel des Bildstellenleiters Hans-Joachim Köhler. Foto, November 1956."
        ),
        "proposed_date_text": "November 1956",
        "proposed_object_type": "Foto",
        "proposed_material": "SW-Negativ, Planfilm",
        "proposed_dimensions": "9x12 cm",
        "proposed_keywords": (
            "Aufnahmegerät; Elektrotechnik; Fernsehen; Fernsehreportage; Industrie; Kamera; "
            "Monitor; Negativ (Fotografie); Planfilm; Sachaufnahme; Schwarzweißfotografie"
        ),
        "proposed_classification": (
            "Fotostelle WF - Geräte, Einschübe, Bauteile; "
            "Fotostelle WF - Telekommunikation, Sende- und Fernsehtechnik"
        ),
        "proposal_basis": (
            "/mnt/volume/archive/WF/Thesaurus/14_neue-scans/"
            "Kopie von 1953_und_1957-fuer_md.-korr_05_FINALE.AG.xlsx, row 356; "
            "archive caption, visible WF marks and visual confirmation; confidence high; EAW not indicated"
        ),
    },
}


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
    candidate_backup = REVIEW_ROOT / f"all-local-metadata-review-candidates.pre-technical-web-{stamp}.csv"
    receipt_backup = REVIEW_ROOT / f"all-local-metadata-review-preparation.pre-technical-web-{stamp}.json"
    shutil.copy2(CANDIDATES, candidate_backup)
    shutil.copy2(RECEIPT, receipt_backup)
    atomic_csv(fields, rows)
    receipt["candidate_sha256"] = sha256(CANDIDATES)
    receipt["technical_metadata_enrichment"] = {
        "format": "iss-tfa-all-local-technical-metadata-enrichment-v1",
        "version": VERSION,
        "enriched_at": utc_now(),
        "record_keys": sorted(target_keys),
        "proposal_count": len(target_keys),
        "human_metadata_decisions_written": 0,
        "automatic_metadata_application": False,
        "manufacturer_attribution": {
            "local:TFA-5510603": "Elektra OHG Oelsner & Hess, high confidence",
            "other_targets": "unknown",
        },
        "source_modified": False,
        "deletion_authorized": False,
    }
    atomic_json(receipt)
    return {
        "result": "ok",
        "proposals_enriched": len(target_keys),
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
            raise EnrichmentError(f"technical proposal drifted: {record_key}")
    enrichment = receipt.get("technical_metadata_enrichment", {})
    if (
        enrichment.get("proposal_count") != len(ENRICHMENTS)
        or enrichment.get("human_metadata_decisions_written") != 0
        or enrichment.get("automatic_metadata_application") is not False
        or enrichment.get("source_modified") is not False
        or enrichment.get("deletion_authorized") is not False
    ):
        raise EnrichmentError("technical enrichment receipt is unsafe or drifted")
    return {
        "result": "ok",
        "proposals_enriched": len(ENRICHMENTS),
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
