#!/usr/bin/env python3
"""Build a non-mutating visual review bundle for the 42 Trafo 1988 issues."""

from __future__ import annotations

import csv
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SOURCE_ROOT = Path(
    "/media/vladimir/Elements/restore/archive/Trafo_Bibliocopy-gesamt/"
    "Trafo tifs 1970-1990"
)
USB_ROOT = Path("/media/vladimir/Elements")
OUTPUT = Path(
    "/mnt/volume/iss-archive-work/trafo/work/review/"
    "TRO-1988-masthead-review-001"
)
EXPECTED = [f"TRO-1988-{number:03d}" for number in range(1, 43)]
MONTHS = (
    "Januar|Februar|März|Maerz|Marz|April|Mai|Juni|Juli|August|"
    "September|Oktober|November|Dezember"
)
DATE_RE = re.compile(
    rf"(?<!\d)([0-3]?\d)\s*[.,]\s*({MONTHS})\s+1988",
    re.IGNORECASE,
)
JAHRGANG_RE = re.compile(r"(?<!\d)(\d{2})\s*[.,]\s*Jahrgang", re.IGNORECASE)
ISSUE_RE = re.compile(
    r"(?:Ausgabe\s*:\s*)?Nr\s*[.,:]?\s*(\d{1,2})(?!\d)", re.IGNORECASE
)
MONTH_NORMALIZATION = {
    "januar": "Januar",
    "februar": "Februar",
    "märz": "März",
    "maerz": "März",
    "marz": "März",
    "april": "April",
    "mai": "Mai",
    "juni": "Juni",
    "juli": "Juli",
    "august": "August",
    "september": "September",
    "oktober": "Oktober",
    "november": "November",
    "dezember": "Dezember",
}


class ReviewBuildError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def assert_source_read_only() -> None:
    result = subprocess.run(
        ["findmnt", "-no", "OPTIONS", "--target", str(USB_ROOT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    option_rows = [line.strip().split(",") for line in result.stdout.splitlines()]
    if result.returncode or not option_rows or not all(
        "ro" in row for row in option_rows
    ):
        raise ReviewBuildError(f"USB source is not read-only: {result.stdout.strip()}")
    if USB_ROOT == OUTPUT or USB_ROOT in OUTPUT.parents:
        raise ReviewBuildError("review output must remain outside the USB source")


def discover() -> list[dict[str, Any]]:
    discovered: list[dict[str, Any]] = []
    for issue_number, inventory in enumerate(EXPECTED, 1):
        issue_directory = SOURCE_ROOT / inventory
        first_pages = sorted(issue_directory.glob(f"{inventory}_001.tif"))
        all_pages = sorted(issue_directory.glob(f"{inventory}_*.tif"))
        if len(first_pages) != 1:
            raise ReviewBuildError(
                f"{inventory}: expected one first-page TIFF, got {len(first_pages)}"
            )
        if len(all_pages) != 7:
            raise ReviewBuildError(
                f"{inventory}: expected seven TIFF scan files, got {len(all_pages)}"
            )
        stat = first_pages[0].stat()
        discovered.append(
            {
                "inventory": inventory,
                "issue_number": issue_number,
                "source_tiff": first_pages[0],
                "source_size_bytes": stat.st_size,
                "source_mtime_ns": stat.st_mtime_ns,
                "scan_file_count": len(all_pages),
            }
        )
    return discovered


def normalize_ocr(text: str) -> str:
    return " ".join(text.replace("\u00ad", "").split())


def ocr_candidates(text: str) -> dict[str, str]:
    normalized = normalize_ocr(text)
    date = ""
    date_match = DATE_RE.search(normalized)
    if date_match:
        day = str(int(date_match.group(1)))
        month = MONTH_NORMALIZATION[date_match.group(2).casefold()]
        date = f"{day}. {month} 1988"
    jahrgang_match = JAHRGANG_RE.search(normalized)
    issue_match = ISSUE_RE.search(normalized)
    return {
        "candidate_date": date,
        "candidate_jahrgang": jahrgang_match.group(1) if jahrgang_match else "",
        "candidate_issue_number": issue_match.group(1) if issue_match else "",
        "ocr_text": normalized,
    }


def process_issue(row: dict[str, Any], stage: Path) -> dict[str, Any]:
    inventory = row["inventory"]
    masthead_relative = Path("mastheads") / f"{inventory}-masthead.jpg"
    ocr_relative = Path("ocr") / f"{inventory}-masthead.txt"
    masthead_path = stage / masthead_relative
    ocr_path = stage / ocr_relative

    image_result = subprocess.run(
        [
            "magick",
            f"{row['source_tiff']}[0]",
            "-gravity",
            "north",
            "-crop",
            "100%x32%+0+0",
            "+repage",
            "-resize",
            "1800x",
            "-auto-orient",
            "-colorspace",
            "sRGB",
            "-strip",
            "-quality",
            "88",
            str(masthead_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
        check=False,
    )
    if image_result.returncode:
        raise ReviewBuildError(
            f"{inventory}: masthead generation failed: "
            f"{image_result.stderr.decode('utf-8', 'replace').strip()}"
        )

    ocr_result = subprocess.run(
        ["tesseract", str(masthead_path), "stdout", "-l", "deu", "--psm", "6"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
        check=False,
    )
    if ocr_result.returncode:
        raise ReviewBuildError(
            f"{inventory}: OCR failed: "
            f"{ocr_result.stderr.decode('utf-8', 'replace').strip()}"
        )
    ocr_text = ocr_result.stdout.decode("utf-8", "replace")
    ocr_path.write_text(ocr_text, encoding="utf-8")
    result = dict(row)
    result.update(ocr_candidates(ocr_text))
    result["masthead_relative"] = masthead_relative.as_posix()
    result["ocr_relative"] = ocr_relative.as_posix()
    result["masthead_sha256"] = hashlib.sha256(masthead_path.read_bytes()).hexdigest()
    return result


def csv_formula(uri: str, label: str) -> str:
    safe_uri = uri.replace('"', '""')
    safe_label = label.replace('"', '""')
    return f'=HYPERLINK("{safe_uri}";"{safe_label}")'


def write_csv(stage: Path, rows: list[dict[str, Any]]) -> Path:
    path = stage / "TRO-1988-masthead-review.csv"
    fieldnames = [
        "inventory",
        "issue_number",
        "machine_candidate_date",
        "machine_candidate_jahrgang",
        "machine_candidate_issue_number",
        "source_tiff_link",
        "masthead_image_link",
        "decision",
        "verified_date",
        "verified_jahrgang",
        "verified_issue_number",
        "reviewer",
        "reviewed_at",
        "notes",
        "source_tiff_path",
        "masthead_image_path",
        "ocr_text",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            masthead_path = (OUTPUT / row["masthead_relative"]).resolve()
            writer.writerow(
                {
                    "inventory": row["inventory"],
                    "issue_number": row["issue_number"],
                    "machine_candidate_date": row["candidate_date"],
                    "machine_candidate_jahrgang": row["candidate_jahrgang"],
                    "machine_candidate_issue_number": row["candidate_issue_number"],
                    "source_tiff_link": csv_formula(
                        row["source_tiff"].as_uri(), "Open source TIFF"
                    ),
                    "masthead_image_link": csv_formula(
                        masthead_path.as_uri(), "Open masthead"
                    ),
                    "decision": "PENDING",
                    "verified_date": "",
                    "verified_jahrgang": "",
                    "verified_issue_number": "",
                    "reviewer": "",
                    "reviewed_at": "",
                    "notes": "",
                    "source_tiff_path": str(row["source_tiff"]),
                    "masthead_image_path": str(masthead_path),
                    "ocr_text": row["ocr_text"],
                }
            )
    return path


def try_make_xlsx(stage: Path, csv_path: Path) -> dict[str, Any]:
    profile = Path(tempfile.mkdtemp(prefix="trafo-review-lo-profile-"))
    try:
        result = subprocess.run(
            [
                "libreoffice",
                "--headless",
                f"-env:UserInstallation={profile.as_uri()}",
                "--convert-to",
                "xlsx",
                "--outdir",
                str(stage),
                "--infilter=Text - txt - csv (StarCalc):44,34,76,1",
                str(csv_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
            check=False,
        )
        xlsx = csv_path.with_suffix(".xlsx")
        return {
            "created": result.returncode == 0 and xlsx.is_file(),
            "path": xlsx.name if xlsx.is_file() else "",
            "stdout": result.stdout.decode("utf-8", "replace").strip(),
            "stderr": result.stderr.decode("utf-8", "replace").strip(),
        }
    finally:
        shutil.rmtree(profile)


def write_manifest(stage: Path, rows: list[dict[str, Any]], xlsx: dict[str, Any]) -> None:
    manifest = {
        "format": "iss-trafo-masthead-review-v1",
        "run_id": "TRO-1988-masthead-review-001",
        "created_at": now(),
        "source_root": str(SOURCE_ROOT),
        "source_read_only_required": True,
        "scope": "TRO-1988-001 through TRO-1988-042",
        "issue_count": len(rows),
        "status": "review-only-no-catalog-mutation",
        "machine_candidates_are_authoritative": False,
        "xlsx_conversion": xlsx,
        "issues": [
            {
                "inventory": row["inventory"],
                "issue_number": row["issue_number"],
                "source_tiff": str(row["source_tiff"]),
                "source_size_bytes": row["source_size_bytes"],
                "source_mtime_ns": row["source_mtime_ns"],
                "scan_file_count": row["scan_file_count"],
                "candidate_date": row["candidate_date"],
                "candidate_jahrgang": row["candidate_jahrgang"],
                "candidate_issue_number": row["candidate_issue_number"],
                "masthead_relative": row["masthead_relative"],
                "masthead_sha256": row["masthead_sha256"],
                "ocr_relative": row["ocr_relative"],
            }
            for row in rows
        ],
    }
    (stage / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def html_page(rows: list[dict[str, Any]]) -> str:
    cards = []
    for row in rows:
        inventory = row["inventory"]
        cards.append(
            f"""
            <article class="issue" id="{inventory}" data-inventory="{inventory}">
              <header>
                <h2>{inventory}</h2>
                <span class="badge">Issue {row["issue_number"]}</span>
              </header>
              <a href="{html.escape(row["source_tiff"].as_uri())}" title="Open original read-only TIFF">
                <img src="{html.escape(row["masthead_relative"])}"
                     alt="Masthead crop for {inventory}" loading="lazy">
              </a>
              <div class="candidates">
                <span>OCR date: <strong>{html.escape(row["candidate_date"] or "not detected")}</strong></span>
                <span>OCR Jahrgang: <strong>{html.escape(row["candidate_jahrgang"] or "not detected")}</strong></span>
                <span>OCR issue: <strong>{html.escape(row["candidate_issue_number"] or "not detected")}</strong></span>
              </div>
              <div class="fields">
                <label>Decision
                  <select data-field="decision">
                    <option value="PENDING">PENDING</option>
                    <option value="ACCEPT">ACCEPT OCR</option>
                    <option value="CORRECT">CORRECTED</option>
                    <option value="QUESTION">QUESTION</option>
                  </select>
                </label>
                <label>Verified date
                  <input data-field="verified_date" placeholder="e.g. 8. Januar 1988">
                </label>
                <label>Verified Jahrgang
                  <input data-field="verified_jahrgang" placeholder="e.g. 39">
                </label>
                <label>Verified issue no.
                  <input data-field="verified_issue_number" placeholder="e.g. 1">
                </label>
                <label>Reviewer
                  <input data-field="reviewer">
                </label>
                <label class="wide">Notes
                  <textarea data-field="notes" rows="2"></textarea>
                </label>
              </div>
              <footer>
                <a href="{html.escape(row["source_tiff"].as_uri())}">Open source TIFF</a>
                <a href="{html.escape(row["ocr_relative"])}">Open OCR text</a>
              </footer>
            </article>
            """
        )
    dataset = [
        {
            "inventory": row["inventory"],
            "issue_number": row["issue_number"],
            "candidate_date": row["candidate_date"],
            "candidate_jahrgang": row["candidate_jahrgang"],
            "candidate_issue_number": row["candidate_issue_number"],
            "source_tiff": str(row["source_tiff"]),
        }
        for row in rows
    ]
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Trafo 1988 masthead review</title>
  <style>
    :root {{ color-scheme: light; font-family: system-ui, sans-serif; }}
    body {{ margin: 0; background: #eee9df; color: #24211d; }}
    .top {{ position: sticky; top: 0; z-index: 5; padding: 14px 20px;
      background: #24211d; color: white; box-shadow: 0 2px 10px #0005; }}
    .top h1 {{ margin: 0 0 6px; font-size: 1.25rem; }}
    .top p {{ margin: 0 0 10px; color: #ddd5c8; }}
    .controls {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
    button, select, input, textarea {{ font: inherit; }}
    button {{ padding: 8px 12px; border: 0; border-radius: 5px; cursor: pointer; }}
    .progress {{ margin-left: auto; font-weight: 700; }}
    main {{ max-width: 1180px; margin: 18px auto; padding: 0 14px 40px; }}
    .issue {{ background: white; border-radius: 9px; margin: 0 0 18px;
      padding: 15px; box-shadow: 0 2px 8px #594f4025; }}
    .issue header {{ display: flex; align-items: center; gap: 12px; }}
    h2 {{ margin: 0 0 10px; }}
    .badge {{ color: #6e6254; }}
    img {{ display: block; width: 100%; max-height: 620px; object-fit: contain;
      background: #ddd; border: 1px solid #c9c0b4; }}
    .candidates {{ display: flex; flex-wrap: wrap; gap: 8px 22px;
      padding: 10px 0; }}
    .fields {{ display: grid; grid-template-columns: repeat(5, minmax(130px, 1fr));
      gap: 10px; }}
    label {{ display: grid; gap: 4px; font-size: .9rem; font-weight: 650; }}
    input, select, textarea {{ box-sizing: border-box; width: 100%; padding: 7px;
      border: 1px solid #aaa095; border-radius: 4px; background: #fff; }}
    .wide {{ grid-column: 1 / -1; }}
    footer {{ display: flex; gap: 18px; margin-top: 11px; }}
    a {{ color: #7b2e20; }}
    .done {{ border-left: 7px solid #3a8550; }}
    .question {{ border-left: 7px solid #b56b15; }}
    @media (max-width: 800px) {{
      .fields {{ grid-template-columns: 1fr 1fr; }}
      .progress {{ margin-left: 0; width: 100%; }}
    }}
  </style>
</head>
<body>
  <section class="top">
    <h1>Der Trafo — 1988 masthead review</h1>
    <p>42 issues. OCR values are suggestions only. Verify against each masthead.</p>
    <div class="controls">
      <button id="export">Export decisions CSV</button>
      <button id="clear">Clear browser decisions</button>
      <label>Show
        <select id="filter">
          <option value="ALL">ALL</option>
          <option value="PENDING">PENDING</option>
          <option value="REVIEWED">REVIEWED</option>
          <option value="QUESTION">QUESTION</option>
        </select>
      </label>
      <span class="progress" id="progress"></span>
    </div>
  </section>
  <main>{"".join(cards)}</main>
  <script>
    const rows = {json.dumps(dataset, ensure_ascii=False)};
    const key = "tro-1988-masthead-review-v1";
    const fields = ["decision", "verified_date", "verified_jahrgang",
      "verified_issue_number", "reviewer", "notes"];
    let state = JSON.parse(localStorage.getItem(key) || "{{}}");
    const save = () => localStorage.setItem(key, JSON.stringify(state));
    const esc = value => '"' + String(value ?? "").replaceAll('"', '""') + '"';
    function refresh() {{
      let reviewed = 0;
      document.querySelectorAll(".issue").forEach(card => {{
        const inventory = card.dataset.inventory;
        const value = state[inventory] || {{decision: "PENDING"}};
        fields.forEach(field => {{
          const input = card.querySelector(`[data-field="${{field}}"]`);
          input.value = value[field] || (field === "decision" ? "PENDING" : "");
        }});
        const decision = value.decision || "PENDING";
        card.classList.toggle("done", decision === "ACCEPT" || decision === "CORRECT");
        card.classList.toggle("question", decision === "QUESTION");
        if (decision !== "PENDING") reviewed++;
        const filter = document.querySelector("#filter").value;
        card.hidden = filter === "PENDING" ? decision !== "PENDING"
          : filter === "REVIEWED" ? decision === "PENDING"
          : filter === "QUESTION" ? decision !== "QUESTION" : false;
      }});
      document.querySelector("#progress").textContent = `${{reviewed}} / 42 reviewed`;
    }}
    document.querySelectorAll("[data-field]").forEach(input => {{
      input.addEventListener("change", event => {{
        const card = event.target.closest(".issue");
        const inventory = card.dataset.inventory;
        state[inventory] ||= {{decision: "PENDING"}};
        state[inventory][event.target.dataset.field] = event.target.value;
        state[inventory].reviewed_at = new Date().toISOString();
        save();
        refresh();
      }});
    }});
    document.querySelector("#filter").addEventListener("change", refresh);
    document.querySelector("#clear").addEventListener("click", () => {{
      if (confirm("Clear all locally saved review decisions?")) {{
        state = {{}};
        save();
        refresh();
      }}
    }});
    document.querySelector("#export").addEventListener("click", () => {{
      const header = ["inventory", "issue_number", "machine_candidate_date",
        "machine_candidate_jahrgang", "machine_candidate_issue_number",
        "decision", "verified_date", "verified_jahrgang",
        "verified_issue_number", "reviewer", "reviewed_at", "notes",
        "source_tiff_path"];
      const lines = [header.map(esc).join(",")];
      rows.forEach(row => {{
        const value = state[row.inventory] || {{decision: "PENDING"}};
        lines.push([
          row.inventory, row.issue_number, row.candidate_date,
          row.candidate_jahrgang, row.candidate_issue_number,
          value.decision || "PENDING", value.verified_date || "",
          value.verified_jahrgang || "", value.verified_issue_number || "",
          value.reviewer || "", value.reviewed_at || "", value.notes || "",
          row.source_tiff
        ].map(esc).join(","));
      }});
      const blob = new Blob(["\\ufeff" + lines.join("\\r\\n")],
        {{type: "text/csv;charset=utf-8"}});
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = "TRO-1988-masthead-decisions.csv";
      link.click();
      URL.revokeObjectURL(link.href);
    }});
    refresh();
  </script>
</body>
</html>
"""


def write_readme(stage: Path, rows: list[dict[str, Any]], xlsx: dict[str, Any]) -> None:
    detected_dates = sum(bool(row["candidate_date"]) for row in rows)
    detected_jahrgang = sum(bool(row["candidate_jahrgang"]) for row in rows)
    detected_issue = sum(bool(row["candidate_issue_number"]) for row in rows)
    xlsx_line = (
        "- `TRO-1988-masthead-review.xlsx`: LibreOffice spreadsheet\n"
        if xlsx["created"]
        else "- XLSX conversion failed; use the CSV instead (details in `manifest.json`).\n"
    )
    text = f"""# TRO 1988 masthead review

This is a review-only bundle. The Elements USB was read-only during creation.
No archive catalog or source TIFF was changed.

## Recommended review

1. Open `index.html` in Firefox.
2. For each issue, compare the OCR suggestions with the masthead image.
3. Choose `ACCEPT OCR`, `CORRECTED`, or `QUESTION`.
4. Fill the verified fields when correcting or when OCR is blank.
5. Use **Export decisions CSV** before moving to another browser/profile.

The HTML page stores working decisions in browser local storage. The export is
the portable decision record. Clicking a masthead opens the original read-only
TIFF.

## Alternative spreadsheet review

- `TRO-1988-masthead-review.csv`: portable editable review table
{xlsx_line}- `mastheads/`: generated review JPEGs
- `ocr/`: raw machine OCR text
- `manifest.json`: source identity and candidate manifest

Machine extraction coverage:

- dates detected: {detected_dates} / 42
- Jahrgang detected: {detected_jahrgang} / 42
- issue number detected: {detected_issue} / 42

Known review points:

- OCR left date/Jahrgang blank for issues 003 and 042; both mastheads remain
  visible for manual entry.
- The source mastheads visibly say `40. Jahrgang` on issue 016 and
  `39. Jahrgang` on issue 017. This non-monotonic transition is real source
  evidence and should not be normalized automatically.

Machine candidates are derived evidence, not catalog authority. The blank
reviewer fields are the only place for curator-confirmed values.
"""
    (stage / "README.md").write_text(text, encoding="utf-8")


def main() -> int:
    assert_source_read_only()
    if OUTPUT.exists():
        raise ReviewBuildError(f"refusing existing output directory: {OUTPUT}")
    rows = discover()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".TRO-1988-masthead-review-", dir=OUTPUT.parent))
    try:
        (stage / "mastheads").mkdir()
        (stage / "ocr").mkdir()
        processed: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(process_issue, row, stage): row["inventory"]
                for row in rows
            }
            for completed, future in enumerate(as_completed(futures), 1):
                inventory = futures[future]
                processed.append(future.result())
                print(f"{completed}/42 {inventory}", flush=True)
        processed.sort(key=lambda row: row["issue_number"])
        csv_path = write_csv(stage, processed)
        xlsx = try_make_xlsx(stage, csv_path)
        (stage / "index.html").write_text(html_page(processed), encoding="utf-8")
        write_manifest(stage, processed, xlsx)
        write_readme(stage, processed, xlsx)
        os.replace(stage, OUTPUT)
        print(
            json.dumps(
                {
                    "output": str(OUTPUT),
                    "issues": len(processed),
                    "dates_detected": sum(
                        bool(row["candidate_date"]) for row in processed
                    ),
                    "jahrgang_detected": sum(
                        bool(row["candidate_jahrgang"]) for row in processed
                    ),
                    "issue_numbers_detected": sum(
                        bool(row["candidate_issue_number"]) for row in processed
                    ),
                    "xlsx_created": xlsx["created"],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except Exception:
        if stage.exists() and stage.parent == OUTPUT.parent and stage.name.startswith(
            ".TRO-1988-masthead-review-"
        ):
            shutil.rmtree(stage)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
