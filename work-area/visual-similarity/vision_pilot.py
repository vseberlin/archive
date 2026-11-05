#!/usr/bin/env python3
"""Pilot: structured image descriptions via headless Claude Code (`claude -p`).

For a Pro subscription without an API key. Each image is downscaled into the
work root, then one `claude -p` call reads it (Read tool only) and returns JSON
validated against SCHEMA. Calls draw from the subscription's usage limits, so
the run is resumable: done image_ids are skipped, and the run stops cleanly on
a usage-limit or repeated error and can simply be restarted later.

SUPPORT_INDEX evidence only. Images leave the workstation: exclude units with
rights or GDPR concerns from the manifest.

    describe  manifest CSV (image_id,path[,context columns]) -> results JSONL
    score     compare results with a truth CSV (image_id,tags,place_id,year)

Context columns (all optional) are passed to the model as known context:
inventory, unit, konvolut, place_candidates ("id|label; id|label").
Never put catalogue answers you want to *evaluate* into the context.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

VERSION = "0.1.0"
CONTEXT_COLUMNS = ("inventory", "unit", "konvolut", "place_candidates")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["visible_text", "description", "tags", "place", "date", "confidence_note"],
    "properties": {
        "visible_text": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "required": ["text", "kind"],
            "properties": {"text": {"type": "string"},
                           "kind": {"type": "string", "enum": ["sign", "lettering", "caption", "handwriting", "stamp", "other"]}}}},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "place": {"type": "object", "additionalProperties": False,
            "required": ["candidate_id", "free_text", "evidence"],
            "properties": {"candidate_id": {"type": "string"}, "free_text": {"type": "string"},
                           "evidence": {"type": "string"}}},
        "date": {"type": "object", "additionalProperties": False,
            "required": ["year_from", "year_to", "evidence"],
            "properties": {"year_from": {"type": ["integer", "null"]}, "year_to": {"type": ["integer", "null"]},
                           "evidence": {"type": "string"}}},
        "confidence_note": {"type": "string"},
    },
}

PROMPT = """You catalogue historical photographs for Industriesalon Schöneweide, an industrial \
heritage museum in Berlin (AEG, KWO, WF, TRO, Schöneweide industrial district).

Read the image file {image} and return JSON for the given schema. Rules:
- visible_text: transcribe exactly what is legible; no guessing of illegible text.
- description: 1–3 factual sentences in German, what is visible, no interpretation.
- tags: German subject terms (Schlagworte), concrete and visible only, max 8.
{tag_hint}- place: if place candidates are given, candidate_id is one of their ids or "" when none \
fits; free_text names the place only if visible evidence (sign, lettering, landmark) supports it, \
else "". evidence quotes that visible evidence.
- date: year_from/year_to only from visible evidence (fashion, vehicles, technology, \
lettering, photo process); null when not supportable. evidence says why.
- Do not identify people by name.
- confidence_note: one sentence on what limits certainty.

Known context (may be empty): {context}"""


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def prepare_image(source: Path, cache: Path, max_side: int) -> Path:
    stat = source.stat()
    key = hashlib.sha256(f"{source}|{stat.st_size}|{int(stat.st_mtime)}|{max_side}".encode()).hexdigest()[:24]
    target = cache / f"{key}.jpg"
    if not target.is_file():
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
            image.save(temporary, "JPEG", quality=90)
            os.replace(temporary, target)
    return target


def validate(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Small validator for the subset used in SCHEMA (no new dependency)."""
    errors: list[str] = []
    kinds = schema.get("type")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    checks = {"object": dict, "array": list, "string": str, "integer": int, "null": type(None)}
    if not any(isinstance(value, checks[k]) and not (k == "integer" and isinstance(value, bool)) for k in kinds):
        return [f"{path}: expected {kinds}"]
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: missing")
        for key, item in value.items():
            if key not in schema.get("properties", {}):
                errors.append(f"{path}.{key}: unexpected")
            else:
                errors += validate(item, schema["properties"][key], f"{path}.{key}")
    if isinstance(value, list):
        for i, item in enumerate(value):
            errors += validate(item, schema["items"], f"{path}[{i}]")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: not in enum")
    return errors


def call_claude(args, image: Path, context: dict[str, str], run_dir: Path) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
    tag_hint = f"  Prefer terms from this vocabulary when they fit: {args.vocabulary_text}\n" if args.vocabulary_text else ""
    prompt = PROMPT.format(image=image, context=json.dumps(context, ensure_ascii=False), tag_hint=tag_hint)
    command = [
        args.claude_bin, "-p", prompt,
        "--model", args.model,
        "--output-format", "json",
        "--json-schema", json.dumps(SCHEMA),
        "--tools", "Read",
        "--allowedTools", "Read",
        "--add-dir", str(image.parent),
        "--no-session-persistence",
    ]
    try:
        completed = subprocess.run(command, cwd=run_dir, capture_output=True, text=True, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        return None, "timeout", {}
    raw = completed.stdout.strip()
    try:
        envelope = json.loads(raw.splitlines()[-1]) if raw else {}
    except json.JSONDecodeError:
        return None, clean(f"unparseable CLI output: {raw[:300]} {completed.stderr[:300]}"), {}
    usage = {k: envelope.get(k) for k in ("total_cost_usd", "duration_ms", "num_turns", "usage") if k in envelope}
    if completed.returncode != 0 or envelope.get("is_error"):
        return None, clean(f"{envelope.get('subtype', '')} {envelope.get('result', '')} {completed.stderr[:300]}")[:600], usage
    payload = envelope.get("structured_output")
    if payload is None:
        text = str(envelope.get("result") or "").strip().removeprefix("```json").removesuffix("```").strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None, f"result is not JSON: {text[:300]}", usage
    problems = validate(payload, SCHEMA)
    return (None, "schema: " + "; ".join(problems[:5]), usage) if problems else (payload, "", usage)


def is_limit_error(error: str) -> bool:
    folded = error.casefold()
    return any(term in folded for term in ("usage limit", "rate limit", "limit reached", "too many requests", "overloaded", "429"))


def cmd_describe(args) -> int:
    work = args.work_root
    cache, run_dir = work / "vision-cache", work / "vision-run"
    cache.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)  # empty cwd: no project CLAUDE.md is loaded
    results = work / f"vision-{args.name}.jsonl"
    done: set[str] = set()
    if results.is_file():
        for line in results.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("status") == "ok" or (row.get("status") == "error" and not args.retry_errors):
                done.add(row["image_id"])
    args.vocabulary_text = ""
    if args.vocabulary:
        terms = [clean(t) for t in args.vocabulary.read_text(encoding="utf-8").splitlines() if clean(t)]
        args.vocabulary_text = "; ".join(terms[:300])
    with args.manifest.open(encoding="utf-8-sig", newline="") as handle:
        rows = [r for r in csv.DictReader(handle) if clean(r.get("image_id")) and clean(r.get("image_id")) not in done]
    if args.limit:
        rows = rows[: args.limit]
    print(f"{len(done)} already done, {len(rows)} to describe with {args.model}", file=sys.stderr)
    consecutive_errors = 0
    with results.open("a", encoding="utf-8") as out:
        for number, row in enumerate(rows, 1):
            image_id, source = clean(row["image_id"]), Path(clean(row.get("path")))
            context = {k: clean(row.get(k)) for k in CONTEXT_COLUMNS if clean(row.get(k))}
            started = time.time()
            try:
                image = prepare_image(source, cache, args.max_side)
                payload, error, usage = call_claude(args, image, context, run_dir)
            except Exception as exception:  # noqa: BLE001 - recorded per image
                payload, error, usage = None, clean(f"{type(exception).__name__}: {exception}"), {}
            if error and is_limit_error(error):
                print(f"\nstopping at {image_id}: {error}\nrestart later; finished rows are kept.", file=sys.stderr)
                return 3
            record = {"image_id": image_id, "path": str(source), "model": args.model, "context": context,
                      "status": "ok" if payload else "error", "error": error, "result": payload,
                      "usage": usage, "seconds": round(time.time() - started, 1), "created_at": utc_now(),
                      "tool": f"vision_pilot {VERSION}"}
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()
            consecutive_errors = consecutive_errors + 1 if error else 0
            print(f"[{number}/{len(rows)}] {image_id} {'ok' if payload else 'ERROR ' + error[:120]}", file=sys.stderr)
            if consecutive_errors >= args.max_consecutive_errors:
                print("too many consecutive errors; stopping", file=sys.stderr)
                return 4
            time.sleep(args.sleep)
    return 0


def split(value: str) -> list[str]:
    return [clean(v).casefold() for v in str(value or "").split(";") if clean(v)]


def cmd_score(args) -> int:
    """Catalogue agreement for tags, place candidate and year. Missing truth is unscorable."""
    truth = {}
    with args.truth.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            truth[clean(row.get("image_id"))] = row
    tags_emitted = tags_hit = places = places_hit = places_abstain = 0
    year_errors: list[float] = []
    years_abstain = scored = 0
    for line in args.results.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        t = truth.get(record["image_id"])
        if not t or record.get("status") != "ok":
            continue
        scored += 1
        result = record["result"]
        truth_tags = set(split(t.get("tags", "")))
        if truth_tags:
            emitted = {clean(x).casefold() for x in result["tags"]}
            tags_emitted += len(emitted)
            tags_hit += len(emitted & truth_tags)
        if clean(t.get("place_id")):
            if result["place"]["candidate_id"]:
                places += 1
                places_hit += int(clean(result["place"]["candidate_id"]).casefold() == clean(t["place_id"]).casefold())
            else:
                places_abstain += 1
        if clean(t.get("year")).isdigit():
            date = result["date"]
            if date["year_from"] is None:
                years_abstain += 1
            else:
                middle = (date["year_from"] + (date["year_to"] or date["year_from"])) / 2
                year_errors.append(abs(middle - int(t["year"])))
    report = {
        "scored_images": scored,
        "tag_agreement": round(tags_hit / tags_emitted, 3) if tags_emitted else None,
        "tags_emitted": tags_emitted,
        "place_agreement": round(places_hit / places, 3) if places else None,
        "place_proposed": places, "place_abstained": places_abstain,
        "year_mae": round(sum(year_errors) / len(year_errors), 2) if year_errors else None,
        "year_proposed": len(year_errors), "year_abstained": years_abstain,
        "note": "compare against the same matched baselines as the kNN report before drawing conclusions",
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("describe")
    p.add_argument("--work-root", type=Path, required=True)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--name", default="pilot", help="results file vision-<name>.jsonl (appended, resumable)")
    p.add_argument("--model", default="sonnet", help="claude CLI model alias or id")
    p.add_argument("--max-side", type=int, default=1568, help="long edge sent to the model")
    p.add_argument("--vocabulary", type=Path, help="text file, one preferred Schlagwort per line")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--sleep", type=float, default=2.0)
    p.add_argument("--timeout", type=int, default=240)
    p.add_argument("--max-consecutive-errors", type=int, default=5)
    p.add_argument("--retry-errors", action="store_true", help="re-run images whose last result was an error")
    p.add_argument("--claude-bin", default="claude")
    p.set_defaults(func=cmd_describe)

    p = commands.add_parser("score")
    p.add_argument("--results", type=Path, required=True)
    p.add_argument("--truth", type=Path, required=True)
    p.set_defaults(func=cmd_score)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
