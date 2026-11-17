"""Baselines, label rarity, strata and holdout sensitivity for a completed reference index.

Reads only the retained reference projection (index.sqlite) and, optionally, the
catalogue capture. No image is decoded, no model is loaded and nothing is adopted.
All figures remain catalogue agreement, not independent identification accuracy.

    diagnose        baselines + rare labels + strata + holdout sweep -> diagnostics-<run>.json
    preview-gaps    why captured records have no selected preview URL
    review-export   top-k neighbours for curator relevance judgements (CSV)
    review-score    score a completed review CSV
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
import re
import sys
from typing import Any

import numpy as np

from .contracts import require
from .reference import load_matrix
from .reference_catalogue import (FIELDS_MULTI, aggregate, atomic_write, clean, neighbours_for,
                                  series_key, utc_now)

CATEGORICAL = ("object_type", "tags", "places", "collections")
JUDGEMENTS = ("same_photograph", "same_place_or_object", "same_type", "unrelated", "unclear")
DEFAULT_STRATA = ("Technisches Fotoarchiv", "Kurt Schwarz")


def connect_ro(work_root: Path):
    """Open the existing projection read-only; never create or migrate it."""
    import sqlite3
    path = work_root / "index.sqlite"
    require(path.is_file() and not path.is_symlink(), f"no reference index at {path}")
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


def values(meta: dict[str, Any], field: str) -> list[str]:
    raw = meta.get(field)
    items = raw if isinstance(raw, list) else ([raw] if raw else [])
    seen, out = set(), []
    for item in items:
        key = clean(item).casefold()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def midpoint(meta: dict[str, Any]) -> float | None:
    return (meta["year_start"] + meta["year_end"]) / 2 if meta.get("year_start") else None


def session_key(meta: dict[str, Any]) -> tuple | None:
    """Proxy for a photographic session: identical date text within identical collections."""
    date = clean(meta.get("date_text"))
    return (tuple(sorted(values(meta, "collections"))), date) if date else None


class Holdouts:
    """Vectorised equivalent of reference_catalogue.heldout_mask, plus wider windows and sessions."""

    def __init__(self, matrix: np.ndarray, meta: list[dict[str, Any]]):
        self.matrix = matrix
        keys = [series_key(m.get("inventory", "")) for m in meta]
        prefixes = sorted({k[0] for k in keys if k})
        self.prefix_index = {p: i for i, p in enumerate(prefixes)}
        self.prefix = np.asarray([self.prefix_index[k[0]] if k else -1 for k in keys])
        self.number = np.asarray([k[1] if k else 0 for k in keys])
        digests = [m.get("preview_sha256") or f"none:{i}" for i, m in enumerate(meta)]
        self.digest = np.asarray(digests, dtype=object)
        sessions = [session_key(m) for m in meta]
        lookup = {s: i for i, s in enumerate(sorted({s for s in sessions if s}, key=repr))}
        self.session = np.asarray([lookup[s] if s else -1 for s in sessions])

    def mask(self, index: int, *, window: int | None, near: float | None, session: bool) -> np.ndarray:
        excluded = self.digest == self.digest[index]
        excluded[index] = True
        if window is not None and self.prefix[index] >= 0:
            excluded |= (self.prefix == self.prefix[index]) & (np.abs(self.number - self.number[index]) <= window)
        if near is not None:
            excluded |= (self.matrix @ self.matrix[index]) >= near
        if session and self.session[index] >= 0:
            excluded |= self.session == self.session[index]
        return excluded


class Pool:
    """Label counts of all references; a query's eligible pool subtracts its exclusions."""

    def __init__(self, meta: list[dict[str, Any]], fields: tuple[str, ...]):
        self.meta = meta
        self.labels = {f: [values(m, f) for m in meta] for f in fields}
        self.counts = {f: Counter(v for row in self.labels[f] for v in row) for f in fields}
        self.mids = np.asarray([midpoint(m) if midpoint(m) is not None else np.nan for m in meta])

    def top(self, field: str, excluded: np.ndarray, n: int) -> list[str]:
        if n <= 0:
            return []
        counts = self.counts[field].copy()
        for i in np.flatnonzero(excluded):
            counts.subtract(self.labels[field][i])
        return [label for label, count in counts.most_common() if count > 0][:n]

    def date_median(self, keep: np.ndarray) -> float | None:
        mids = self.mids[keep & ~np.isnan(self.mids)]
        return float(np.median(mids)) if len(mids) else None


def stratum(meta: dict[str, Any], patterns: tuple[str, ...]) -> str:
    joined = " | ".join(values(meta, "collections"))
    for pattern in patterns:
        if pattern.casefold() in joined:
            return pattern
    return "other"


def ratio(correct: int, proposed: int) -> float | None:
    return round(correct / proposed, 4) if proposed else None


def new_tally() -> dict[str, Any]:
    return {"proposed": 0, "correct": 0, "queries": 0}


def finish(tally: dict[str, Any]) -> dict[str, Any]:
    tally["precision"] = ratio(tally["correct"], tally["proposed"])
    return tally


def evaluate_mode(matrix, ids, meta, sample, holdouts, pool, args, *, window, near, session, rare, patterns,
                  detailed: bool) -> dict[str, Any]:
    fields = tuple(args.fields)
    categorical = [f for f in fields if f in CATEGORICAL]
    visual = {f: new_tally() for f in categorical}
    visual_high = {f: new_tally() for f in categorical}
    baseline = {f: new_tally() for f in categorical}
    baseline_high = {f: new_tally() for f in categorical}
    rare_visual = {f: new_tally() for f in categorical}
    strata: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: {f: new_tally() for f in categorical})
    strata_queries: Counter = Counter()
    date_rows: list[dict[str, float]] = []
    excluded_counts = []
    for index in sample:
        excluded = holdouts.mask(int(index), window=window, near=near, session=session)
        excluded_counts.append(int(excluded.sum()) - 1)
        found = neighbours_for(matrix[index], ids, matrix, meta, args.k, excluded)
        proposals = aggregate(found, fields, args)
        group = stratum(meta[index], patterns)
        strata_queries[group] += 1
        for field in categorical:
            truth = set(values(meta[index], field))
            mine = [p for p in proposals if p["field_key"] == field]
            high = [p for p in mine if p["confidence_key"] == "high"]
            for tally, chosen in ((visual[field], mine), (visual_high[field], high)):
                hits = sum(p["value"].casefold() in truth for p in chosen)
                tally["proposed"] += len(chosen)
                tally["correct"] += hits
                tally["queries"] += int(bool(chosen))
            if detailed:
                for tally, n in ((baseline[field], len(mine)), (baseline_high[field], len(high))):
                    guess = pool.top(field, excluded, n if field in FIELDS_MULTI else min(n, 1))
                    tally["proposed"] += len(guess)
                    tally["correct"] += sum(label in truth for label in guess)
                    tally["queries"] += int(bool(guess))
                uncommon = [p for p in mine if p["value"].casefold() not in rare[field]]
                rare_visual[field]["proposed"] += len(uncommon)
                rare_visual[field]["correct"] += sum(p["value"].casefold() in truth for p in uncommon)
                rare_visual[field]["queries"] += int(bool(uncommon))
                s = strata[group][field]
                s["proposed"] += len(mine)
                s["correct"] += sum(p["value"].casefold() in truth for p in mine)
                s["queries"] += int(bool(mine))
        date = next((p for p in proposals if p["field_key"] == "date"), None)
        truth_mid = midpoint(meta[index])
        if "date" in fields and date and truth_mid is not None:
            row = {"visual": abs(date["year_estimate"] - truth_mid), "high": date["confidence_key"] == "high"}
            if detailed:
                keep = ~excluded
                pool_median = pool.date_median(keep)
                own = set(values(meta[index], "collections"))
                in_own = np.asarray([bool(own & set(c)) for c in pool.labels["collections"]]) if own else np.zeros(len(meta), bool)
                own_median = pool.date_median(keep & in_own)
                predicted = next((p["value"].casefold() for p in proposals if p["field_key"] == "collections"), None)
                in_predicted = np.asarray([predicted in c for c in pool.labels["collections"]]) if predicted else np.zeros(len(meta), bool)
                predicted_median = pool.date_median(keep & in_predicted)
                for name, value in (("pool_median", pool_median), ("true_collection_median", own_median),
                                    ("predicted_collection_median", predicted_median)):
                    row[name] = abs(value - truth_mid) if value is not None else None
            date_rows.append(row)
    result: dict[str, Any] = {
        "holdout": {"window": window, "near_duplicate_similarity": near, "session": session},
        "excluded_other_records": {"mean": round(float(np.mean(excluded_counts)), 2),
                                   "median": float(np.median(excluded_counts)), "max": int(np.max(excluded_counts))},
        "visual": {f: finish(t) for f, t in visual.items()},
        "visual_high": {f: finish(t) for f, t in visual_high.items()},
    }
    if detailed:
        result.update({
            "prevalence_baseline_matched_count": {f: finish(t) for f, t in baseline.items()},
            "prevalence_baseline_matched_high_count": {f: finish(t) for f, t in baseline_high.items()},
            "visual_excluding_common_labels": {f: finish(t) for f, t in rare_visual.items()},
            "strata": {g: {"queries": strata_queries[g], **{f: finish(t) for f, t in s.items()}}
                       for g, s in sorted(strata.items())},
        })
    result["date"] = date_summary(date_rows, detailed)
    return result


def date_summary(rows: list[dict[str, Any]], detailed: bool) -> dict[str, Any]:
    if not rows:
        return {"scored": 0}
    out: dict[str, Any] = {"scored": len(rows)}
    names = ["visual"] + (["pool_median", "true_collection_median", "predicted_collection_median"] if detailed else [])
    for subset, chosen in (("all", rows), ("high", [r for r in rows if r["high"]])):
        part: dict[str, Any] = {"scored": len(chosen)}
        for name in names:
            errors = [r[name] for r in chosen if r.get(name) is not None]
            part[f"{name}_mae"] = round(float(np.mean(errors)), 2) if errors else None
            part[f"{name}_scored"] = len(errors)
        paired = [r for r in chosen if all(r.get(name) is not None for name in names)]
        part["paired_scored"] = len(paired)
        for name in names:
            part[f"{name}_paired_mae"] = round(float(np.mean([r[name] for r in paired])), 2) if paired else None
        out[subset] = part
    return out


def common_labels(pool: Pool, fields, n: int) -> dict[str, list[str]]:
    return {f: [label for label, _ in pool.counts[f].most_common(n)] for f in fields if f in CATEGORICAL}


def cmd_diagnose(args) -> int:
    connection = connect_ro(args.work_root)
    model = select_model(connection, args.model)
    ids, matrix, meta = load_matrix(connection, "ref", model)
    connection.close()
    require(len(ids) >= 50, f"only {len(ids)} reference embeddings for {model}")
    rng = np.random.default_rng(args.seed)
    sample = rng.choice(len(ids), size=min(args.sample, len(ids)), replace=False)
    fields = tuple(args.fields)
    pool = Pool(meta, tuple(f for f in fields if f in CATEGORICAL) + (("collections",) if "collections" not in fields else ()))
    holdouts = Holdouts(matrix, meta)
    common = common_labels(pool, fields, args.common_labels)
    rare = {f: set(v) for f, v in common.items()}
    patterns = tuple(args.strata)
    run = args.run or utc_now().replace(":", "").replace("-", "")
    path = args.work_root / f"diagnostics-{run}.json"
    require(not path.exists(), f"refusing to overwrite {path}")
    report: dict[str, Any] = {
        "model": model, "reference": len(ids), "sample": int(len(sample)), "seed": args.seed,
        "sample_ids": [ids[i] for i in sample],
        "settings": {k: getattr(args, k) for k in ("k", "min_similarity", "min_support", "temperature",
                                                   "date_tolerance", "fields", "windows", "common_labels", "strata")},
        "common_labels_excluded": common,
        "corpus_label_prevalence": {f: {label: round(pool.counts[f][label] / len(ids), 4) for label in labels}
                                    for f, labels in common.items()},
        "created_at": utc_now(), "metadata_adopted": False, "images_decoded": 0, "models_loaded": 0,
        "limitations": [
            "Catalogue agreement only; no curator relevance judgements are included.",
            "Prevalence baselines propose the eligible pool's most frequent labels, matched to the visual method's per-query proposal count.",
            "Session holdout uses identical date text within identical collections as a proxy; it is not a documented session.",
            "true_collection_median uses the query's own catalogue collection and is an upper-bound context baseline, not a deployable predictor.",
        ],
    }
    reference_mode = dict(window=args.windows[0], near=args.near_duplicate_similarity, session=False)
    report["series_hidden"] = evaluate_mode(matrix, ids, meta, sample, holdouts, pool, args, **reference_mode,
                                            rare=rare, patterns=patterns, detailed=True)
    report["holdout_sweep"] = []
    for window in args.windows:
        for session in (False, True):
            report["holdout_sweep"].append(evaluate_mode(
                matrix, ids, meta, sample, holdouts, pool, args, window=window,
                near=args.near_duplicate_similarity, session=session, rare=rare, patterns=patterns, detailed=False))
    atomic_write(args.work_root, path, lambda h: (json.dump(report, h, ensure_ascii=False, indent=2, sort_keys=True), h.write("\n")))
    print_report(report)
    print(f"receipt: {path}", file=sys.stderr)
    return 0


def select_model(connection, model: str | None) -> str:
    models = [row[0] for row in connection.execute("SELECT DISTINCT model FROM embeddings WHERE side='ref' AND status='ok'")]
    if model:
        require(model in models, f"model space {model} not in index")
        return model
    require(len(models) == 1, f"select --model; index holds {len(models)} reference spaces")
    return models[0]


def fmt(t: dict[str, Any] | None) -> str:
    if not t or not t.get("proposed"):
        return "        -"
    return f"{t['precision']*100:5.1f}%/{t['proposed']:<4}"


def print_report(report: dict[str, Any]) -> None:
    main = report["series_hidden"]
    print(f"model {report['model'][:12]}  references {report['reference']}  sample {report['sample']}  "
          f"holdout window ±{main['holdout']['window']}, near ≥{main['holdout']['near_duplicate_similarity']}")
    print(f"\n{'field':<12} {'visual':>11} {'baseline':>11} {'vis high':>11} {'base high':>11} {'rare only':>11}")
    for field in main["visual"]:
        print(f"{field:<12} {fmt(main['visual'][field]):>11} {fmt(main['prevalence_baseline_matched_count'][field]):>11} "
              f"{fmt(main['visual_high'][field]):>11} {fmt(main['prevalence_baseline_matched_high_count'][field]):>11} "
              f"{fmt(main['visual_excluding_common_labels'][field]):>11}")
    date = main["date"]
    if date.get("scored"):
        a = date["all"]
        print(f"\ndate MAE, paired on {a['paired_scored']} identical queries (years): visual {a['visual_paired_mae']}  "
              f"pool median {a['pool_median_paired_mae']}  true-collection median {a['true_collection_median_paired_mae']}  "
              f"predicted-collection median {a['predicted_collection_median_paired_mae']}")
        print(f"  unpaired scored: visual {a['visual_scored']}, pool {a['pool_median_scored']}, "
              f"true-collection {a['true_collection_median_scored']}, predicted-collection {a['predicted_collection_median_scored']}")
    print("\nstrata (visual precision/proposals):")
    for group, s in main["strata"].items():
        cells = "  ".join(f"{f} {fmt(s[f]).strip()}" for f in main["visual"])
        print(f"  {group:<24} q={s['queries']:<4} {cells}")
    print("\nholdout sweep (visual precision/proposals; date MAE):")
    for row in report["holdout_sweep"]:
        h = row["holdout"]
        cells = "  ".join(f"{f} {fmt(t).strip()}" for f, t in row["visual"].items())
        mae = (row["date"].get("all") or {}).get("visual_mae")
        print(f"  ±{h['window']:<5} session={'y' if h['session'] else 'n'} excl.med={row['excluded_other_records']['median']:<6} {cells}  date {mae}")


# ------------------------------------------------------------- preview gaps

def cmd_preview_gaps(args) -> int:
    reasons: Counter = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    for path in sorted((args.capture / "raw").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        objects = payload.get("results", {}).get("objects", {})
        for item in objects.values() if isinstance(objects, dict) else objects if isinstance(objects, list) else []:
            if not isinstance(item, dict) or not clean(item.get("object_id")):
                continue
            reason = preview_gap(item)
            reasons[reason] += 1
            if reason != "ok" and len(examples[reason]) < args.examples:
                examples[reason].append(clean(item.get("object_id")))
    report = {"capture": str(args.capture), "reasons": dict(reasons), "examples": dict(examples),
              "created_at": utc_now(), "note": "Counts parsed object occurrences; duplicates across raw files count again."}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def preview_gap(item: dict[str, Any]) -> str:
    images = item.get("object_images")
    if not isinstance(images, list) or not images:
        return "no_object_images"
    typed = [r for r in images if isinstance(r, dict)]
    usable = [r for r in typed if str(r.get("type") or "image").casefold() == "image"]
    if not usable:
        kinds = ",".join(sorted({str(r.get("type")).casefold() for r in typed})[:3])
        previews = [r for r in typed if re.search(r"\.(jpe?g|png|webp|gif|tiff?)$", clean(r.get("preview")), re.I)]
        return (f"non_image_type_with_image_preview:{kinds}" if previews else f"only_non_image_types:{kinds}")
    first = sorted(usable, key=lambda r: (str(r.get("is_main")) != "j", int(r.get("order") or 999)))[0]
    if not clean(first.get("folder")).strip("/"):
        return "selected_image_without_folder"
    if not clean(first.get("preview") or first.get("filename_loc")):
        others = any(clean(r.get("preview") or r.get("filename_loc")) and clean(r.get("folder")) for r in usable)
        return "selected_image_without_filename" + ("_other_image_usable" if others else "")
    return "ok"


# ----------------------------------------------------------- curator review

def cmd_review_export(args) -> int:
    connection = connect_ro(args.work_root)
    model = select_model(connection, args.model)
    ids, matrix, meta = load_matrix(connection, "ref", model)
    connection.close()
    holdouts = Holdouts(matrix, meta)
    rng = np.random.default_rng(args.seed)
    sample = rng.choice(len(ids), size=min(args.queries, len(ids)), replace=False)
    path = args.work_root / f"review-{args.run}.csv"
    require(not path.exists(), f"refusing to overwrite {path}")
    rows = []
    for index in sample:
        excluded = holdouts.mask(int(index), window=args.window, near=args.near_duplicate_similarity, session=False)
        for rank, n in enumerate(neighbours_for(matrix[index], ids, matrix, meta, args.top, excluded), 1):
            rows.append({"query_id": ids[index], "query_inventory": meta[index].get("inventory", ""),
                         "query_title": meta[index].get("title", ""), "query_url": meta[index].get("md_url", ""),
                         "query_preview": meta[index].get("preview_path", ""), "rank": rank,
                         "neighbour_id": n["md_object_id"], "neighbour_inventory": n["inventory"],
                         "neighbour_title": n["meta"].get("title", ""), "neighbour_url": n["md_url"],
                         "neighbour_preview": n["meta"].get("preview_path", ""), "similarity": n["similarity"],
                         "judgement": "", "note": ""})

    def write(handle):
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)

    require(rows, "no neighbours to export")
    atomic_write(args.work_root, path, write)
    print(json.dumps({"queries": int(len(sample)), "rows": len(rows), "csv": str(path),
                      "judgements": list(JUDGEMENTS), "model": model}))
    return 0


def cmd_review_score(args) -> int:
    with args.review.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for r in rows:
        r["judgement"] = clean(r.get("judgement"))
    judged = [r for r in rows if r["judgement"]]
    unknown = {r["judgement"] for r in judged} - set(JUDGEMENTS)
    require(not unknown, f"unknown judgements: {sorted(unknown)}; use {JUDGEMENTS}")
    by_query: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        by_query[r["query_id"]].append(r)
    relevant = {"strict": {"same_photograph", "same_place_or_object"},
                "lenient": {"same_photograph", "same_place_or_object", "same_type"}}
    out: dict[str, Any] = {"rows": len(rows), "judged": len(judged), "queries": len(by_query),
                           "judgements": dict(Counter(r["judgement"] for r in judged)), "created_at": utc_now(),
                           "scored_over": "queries whose exported top-k rows are all judged"}
    for k in (1, 3, 5):
        complete = {q: [r for r in rs if int(r["rank"]) <= k] for q, rs in by_query.items()}
        complete = {q: rs for q, rs in complete.items() if rs and all(r["judgement"] for r in rs)}
        incomplete = len(by_query) - len(complete)
        out[f"complete_queries_at_{k}"] = len(complete)
        out[f"incomplete_queries_at_{k}"] = incomplete
        require(args.allow_partial or not incomplete,
                f"{incomplete} queries have unjudged rows within top-{k}; judge them or pass --allow-partial")
        prefix = "partial_" if incomplete else ""
        for name, accepted in relevant.items():
            hits = [any(r["judgement"] in accepted for r in rs) for rs in complete.values()]
            precision = [r["judgement"] in accepted for rs in complete.values() for r in rs]
            out[f"{prefix}{name}_hit_at_{k}"] = ratio(sum(hits), len(hits))
            out[f"{prefix}{name}_precision_at_{k}"] = ratio(sum(precision), len(precision))
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------- cli

def main(argv: list[str] | None = None) -> int:
    from .common import work_root
    parser = argparse.ArgumentParser(prog="archive-research reference-diagnostics", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    def index_options(p):
        p.add_argument("--work-root", type=Path, required=True)
        p.add_argument("--model", help="vector-space id; default: the only one in the index")
        p.add_argument("--seed", type=int, default=47)
        p.add_argument("--near-duplicate-similarity", type=float, default=0.95)

    p = commands.add_parser("diagnose")
    index_options(p)
    p.add_argument("--sample", type=int, default=400)
    p.add_argument("--k", type=int, default=12)
    p.add_argument("--min-similarity", type=float, default=0.5)
    p.add_argument("--min-support", type=float, default=0.34)
    p.add_argument("--temperature", type=float, default=0.05)
    p.add_argument("--date-tolerance", type=float, default=5.0)
    p.add_argument("--fields", nargs="+", default=list(CATEGORICAL) + ["date"], choices=list(CATEGORICAL) + ["date"])
    p.add_argument("--windows", type=int, nargs="+", default=[20, 100, 500])
    p.add_argument("--common-labels", type=int, default=5, help="most frequent labels per field excluded in 'rare only'")
    p.add_argument("--strata", nargs="+", default=list(DEFAULT_STRATA), help="collection substrings, first match wins")
    p.add_argument("--run", default="")
    p.set_defaults(func=cmd_diagnose)

    p = commands.add_parser("preview-gaps")
    p.add_argument("--capture", type=Path, required=True)
    p.add_argument("--examples", type=int, default=5)
    p.set_defaults(func=cmd_preview_gaps)

    p = commands.add_parser("review-export")
    index_options(p)
    p.add_argument("--queries", type=int, default=50)
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--window", type=int, default=20)
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_review_export)

    p = commands.add_parser("review-score")
    p.add_argument("--review", type=Path, required=True)
    p.add_argument("--allow-partial", action="store_true",
                   help="score only fully judged queries; metric names are prefixed partial_")
    p.set_defaults(func=cmd_review_score)

    args = parser.parse_args(argv)
    try:
        if hasattr(args, "work_root"):
            args.work_root = work_root(args.work_root)
        if args.command == "diagnose":
            require(1 <= args.sample <= 2000 and 1 <= args.k <= 100 and args.temperature > 0, "Invalid diagnostic bounds")
            require(args.windows and all(w >= 0 for w in args.windows), "Windows must be non-negative")
            require(re.fullmatch(r"[A-Za-z0-9_.-]*", args.run) is not None, "Run label: letters, digits, _.- only")
        if args.command == "review-export":
            require(1 <= args.queries <= 500 and 1 <= args.top <= 20, "Invalid review bounds")
            require(re.fullmatch(r"[A-Za-z0-9_.-]+", args.run) is not None, "Run label: letters, digits, _.- only")
        return args.func(args)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
