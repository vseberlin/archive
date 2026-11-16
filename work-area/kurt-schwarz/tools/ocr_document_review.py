#!/usr/bin/env python3
"""Content/OCR review of the finalized Kurt Schwarz document packages."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess
import zipfile
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree


UNIT = Path("/mnt/volume/iss-archive/units/kurt-schwarz")
RUN = Path(
    "/mnt/volume/iss-archive-work/kurt-schwarz/work/reconciliation/documentation/"
    "20260814T-kurt-schwarz-documentation-final"
)
PACKAGES = {
    "personal": UNIT / "documents/restricted/personal.zip",
    "contracts": UNIT / "documents/restricted/contracts.zip",
    "finance": UNIT / "documents/restricted/finance.zip",
    "historical": UNIT / "documents/historical/drafts-and-superseded.zip",
}
DIRECT_PATTERNS = (
    re.compile(r"\bkurt\s*[-_]?[ ]*schwarz\b", re.I),
    re.compile(r"\bnegativarchiv.{0,80}\bkurt\b", re.I | re.S),
    re.compile(r"\bost[- ]berliner lebenswelten\b", re.I),
    re.compile(r"\bKS-[5-9]-(?:BZ|NB|WF|ZP)[-_]\d+", re.I),
)
CROSS_PATTERNS = (
    re.compile(r"\bWF[- ]?Sender\b", re.I),
    re.compile(r"\bWerk für Fernsehelektronik\b", re.I),
    re.compile(r"\bWFS[-_ ]?\d{4}", re.I),
)
PROJECT_PATTERNS = (
    re.compile(r"\bdigiS\b", re.I),
    re.compile(r"\bDigitalisierungsprojekt\b", re.I),
    re.compile(r"\bIndustriesalon(?: Schöneweide)?\b", re.I),
    re.compile(r"\bmuseum[- ]digital\b", re.I),
)
FIELDS = (
    "package", "member", "original_filename", "original_source_path", "source_sha256",
    "extension", "extraction_method", "page_count", "ocr_pages", "text_characters",
    "direct_kurt_hits", "cross_wf_hits", "project_hits", "inventory_hits",
    "content_signal", "suggested_disposition", "condition", "text_excerpt",
)


def clean(text: str) -> str:
    return " ".join(text.replace("\x00", " ").split())


def decode(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def xml_archive_text(path: Path) -> str:
    values: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not name.endswith(".xml"):
                continue
            if not (
                name.startswith(("word/", "ppt/slides/", "xl/", "META-INF/"))
                or name == "content.xml"
            ):
                continue
            try:
                root = ElementTree.fromstring(archive.read(name))
            except ElementTree.ParseError:
                continue
            values.extend(node.text or "" for node in root.iter() if node.text)
    return clean(" ".join(values))


def pdf_pages(path: Path) -> int:
    result = subprocess.run(
        ["pdfinfo", str(path)], capture_output=True, text=True, timeout=30, check=False
    )
    match = re.search(r"^Pages:\s+(\d+)", result.stdout, re.M)
    return int(match.group(1)) if match else 0


def embedded_pdf_text(path: Path) -> str:
    result = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"],
        capture_output=True, timeout=120, check=False,
    )
    return clean(result.stdout.decode("utf-8", errors="replace"))


def ocr_pdf(path: Path, pages: int, all_pages: bool) -> tuple[str, int, list[str]]:
    page_limit = pages if all_pages else min(pages, 3)
    text: list[str] = []
    errors: list[str] = []
    completed = 0
    prefix = RUN / f"ocr-page-{os.getpid()}"
    image = prefix.with_suffix(".png")
    for page in range(1, page_limit + 1):
        try:
            render = subprocess.run(
                [
                    "pdftoppm", "-f", str(page), "-l", str(page), "-singlefile",
                    "-r", "150", "-png", str(path), str(prefix),
                ],
                capture_output=True, timeout=120, check=False,
            )
            if render.returncode != 0 or not image.is_file():
                errors.append(f"render_page_{page}:{render.returncode}")
                continue
            result = subprocess.run(
                ["tesseract", str(image), "stdout", "-l", "deu+eng"],
                capture_output=True, timeout=120, check=False,
            )
            if result.returncode != 0:
                errors.append(f"ocr_page_{page}:{result.returncode}")
            text.append(result.stdout.decode("utf-8", errors="replace"))
            completed += 1
        except subprocess.TimeoutExpired:
            errors.append(f"timeout_page_{page}")
        finally:
            image.unlink(missing_ok=True)
    return clean(" ".join(text)), completed, errors


def extract(path: Path, extension: str, force_ocr: bool) -> tuple[str, str, int, int, str]:
    extension = extension.casefold()
    if extension == ".pdf":
        pages = pdf_pages(path)
        embedded = embedded_pdf_text(path)
        need_ocr = force_ocr or len(embedded) < 200
        if need_ocr:
            ocr, done, errors = ocr_pdf(path, pages, all_pages=force_ocr)
            combined = clean(embedded + " " + ocr)
            method = "embedded+ocr" if embedded else "ocr"
            return combined, method, pages, done, ";".join(errors)
        return embedded, "embedded_pdf_text", pages, 0, ""
    if extension in {".docx", ".xlsx", ".pptx", ".odt", ".ods"}:
        try:
            return xml_archive_text(path), "office_xml", 0, 0, ""
        except zipfile.BadZipFile as error:
            return "", "office_xml_error", 0, 0, str(error)
    if extension in {".doc", ".xls"}:
        command = ["catdoc", str(path)] if extension == ".doc" else ["xls2csv", str(path)]
        try:
            result = subprocess.run(command, capture_output=True, timeout=90, check=False)
            return clean(decode(result.stdout)), command[0], 0, 0, "" if result.returncode == 0 else f"returncode:{result.returncode}"
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            return "", command[0] + "_error", 0, 0, str(error)
    if extension in {".csv", ".txt", ".html", ".url", ".vcf", ""}:
        return clean(decode(path.read_bytes())), "plain_text", 0, 0, ""
    if extension == ".zip":
        try:
            with zipfile.ZipFile(path) as archive:
                return clean(" | ".join(archive.namelist())), "zip_member_names", 0, 0, ""
        except zipfile.BadZipFile as error:
            return "", "zip_error", 0, 0, str(error)
    try:
        result = subprocess.run(
            ["strings", "-a", "-n", "5", str(path)],
            capture_output=True, timeout=60, check=False,
        )
        return clean(decode(result.stdout)), "strings", 0, 0, ""
    except subprocess.TimeoutExpired as error:
        return "", "strings_timeout", 0, 0, str(error)


def signals(text: str, filename: str) -> tuple[int, int, int, int, str, str]:
    combined = clean(filename + " " + text)
    direct = sum(len(pattern.findall(combined)) for pattern in DIRECT_PATTERNS)
    cross = sum(len(pattern.findall(combined)) for pattern in CROSS_PATTERNS)
    project = sum(len(pattern.findall(combined)) for pattern in PROJECT_PATTERNS)
    inventories = len(set(re.findall(r"\bKS-[5-9]-(?:BZ|NB|WF|ZP)[-_]\d+", combined, re.I)))
    if direct or inventories:
        signal = "direct_kurt"
        disposition = "retain_in_kurt_unit"
    elif cross:
        signal = "wf_cross_unit_only"
        disposition = "cross_unit_reference"
    elif project:
        signal = "industriesalon_or_digis_without_kurt"
        disposition = "manual_project_relation_review"
    else:
        signal = "no_kurt_project_evidence"
        disposition = "historical_reference_noise"
    return direct, cross, project, inventories, signal, disposition


def main() -> int:
    rows: list[dict[str, str]] = []
    by_digest: dict[str, dict[str, str]] = {}
    for package, package_path in PACKAGES.items():
        with zipfile.ZipFile(package_path) as archive:
            manifest = list(
                csv.DictReader(io.StringIO(archive.read("MANIFEST.csv").decode("utf-8-sig")))
            )
            for number, item in enumerate(manifest, 1):
                digest = item["source_sha256"] or f"{package}:{item['member']}"
                prior = by_digest.get(digest)
                extension = Path(item["original_filename"]).suffix.casefold()
                if prior is None:
                    payload = archive.read(item["member"])
                    suffix = extension if extension else ".bin"
                    temporary = RUN / f"ocr-current-{os.getpid()}{suffix}"
                    temporary.write_bytes(payload)
                    try:
                        text, method, page_count, ocr_pages, condition = extract(
                            temporary, extension, force_ocr=(package == "personal" and extension == ".pdf")
                        )
                    finally:
                        temporary.unlink(missing_ok=True)
                    direct, cross, project, inventories, signal, disposition = signals(
                        text, item["original_filename"]
                    )
                    evidence = {
                        "extension": extension,
                        "extraction_method": method,
                        "page_count": str(page_count),
                        "ocr_pages": str(ocr_pages),
                        "text_characters": str(len(text)),
                        "direct_kurt_hits": str(direct),
                        "cross_wf_hits": str(cross),
                        "project_hits": str(project),
                        "inventory_hits": str(inventories),
                        "content_signal": signal,
                        "suggested_disposition": disposition,
                        "condition": condition,
                        "text_excerpt": text[:4000],
                    }
                    by_digest[digest] = evidence
                else:
                    evidence = dict(prior)
                    evidence["extraction_method"] += "+reused_exact_payload_evidence"
                rows.append({
                    "package": package,
                    "member": item["member"],
                    "original_filename": item["original_filename"],
                    "original_source_path": item["original_source_path"],
                    "source_sha256": item["source_sha256"],
                    **evidence,
                })
                if package == "personal":
                    print(json.dumps({
                        "progress": f"{number}/{len(manifest)}", "file": item["original_filename"],
                        "signal": evidence["content_signal"], "ocr_pages": evidence["ocr_pages"],
                    }, ensure_ascii=False), flush=True)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELDS, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    output = RUN / "ocr-document-review.csv"
    output.write_text(stream.getvalue(), encoding="utf-8")
    summary = {
        "result": "ok",
        "rows": len(rows),
        "unique_payloads_examined": len(by_digest),
        "package_counts": dict(__import__("collections").Counter(row["package"] for row in rows)),
        "content_signal_counts": dict(__import__("collections").Counter(row["content_signal"] for row in rows)),
        "ocr_pages": sum(int(row["ocr_pages"]) for row in rows if "reused_exact" not in row["extraction_method"]),
        "errors": [
            {"source_path": row["original_source_path"], "condition": row["condition"]}
            for row in rows if row["condition"]
        ],
        "source_modified": False,
        "deletion_authorized": False,
    }
    (RUN / "ocr-document-review-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
