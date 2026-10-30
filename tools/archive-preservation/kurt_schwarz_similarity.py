#!/usr/bin/env python3
"""Build read-only, object-centric similarity evidence for Kurt Schwarz.

The scan compares unresolved census images with each other and with the one
curator-selected master of every published Kurt object.  Existing derivatives
and Redundant representations are deliberately not visual-search references.
The output is discovery evidence only: it never changes archive bytes, census
decisions, identifiers, or deletion authority.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import ctypes.util
import hashlib
import json
import os
import subprocess
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image, ImageOps

from kurt_schwarz_reconciliation import KurtSchwarzReview, sha256_file
from kurt_schwarz_visual_verify import hamming, image_hashes


FORMAT = "iss-kurt-schwarz-census-similarity-v2"
POLICY = {
    "exact_duplicate_method": "size+xxh3-2k-prehash+stored-sha256+xxh3-full",
    "xxh3_prehash_bytes": 2048,
    "phash_maximum": 8,
    "dhash_maximum": 10,
    "mean_hash_maximum": 16,
    "gradient_hash_maximum": 20,
    "block_hash_maximum": 20,
    "hash_size": 16,
    "czkawka_hashes": ["gradient", "mean", "blockhash"],
    "comparison_size": 512,
    "high_confidence_ssim": 0.99,
    "high_confidence_ncc": 0.995,
    "likely_ssim": 0.975,
    "likely_ncc": 0.99,
    "automatic_decisions": False,
}
PAIR_FIELDS = (
    "pair_id",
    "group_id",
    "candidate_a_id",
    "candidate_a_filename",
    "candidate_b_id",
    "candidate_b_filename",
    "phash_distance",
    "dhash_distance",
    "mean_hash_distance",
    "gradient_hash_distance",
    "block_hash_distance",
    "ssim",
    "ncc",
    "trimmed_ssim",
    "trimmed_ncc",
    "exact_preview",
    "decoded_pixel_state",
    "confidence",
)


class SimilarityError(RuntimeError):
    pass


class XXH3:
    """Small ctypes wrapper around Debian's libxxhash streaming API."""

    def __init__(self) -> None:
        library = ctypes.util.find_library("xxhash") or "libxxhash.so.0"
        try:
            self.library = ctypes.CDLL(library)
        except OSError as error:
            raise SimilarityError("libxxhash with XXH3 support is required") from error
        self.library.XXH3_64bits.argtypes = (ctypes.c_void_p, ctypes.c_size_t)
        self.library.XXH3_64bits.restype = ctypes.c_uint64
        self.library.XXH3_createState.restype = ctypes.c_void_p
        self.library.XXH3_freeState.argtypes = (ctypes.c_void_p,)
        self.library.XXH3_freeState.restype = ctypes.c_int
        self.library.XXH3_64bits_reset.argtypes = (ctypes.c_void_p,)
        self.library.XXH3_64bits_reset.restype = ctypes.c_int
        self.library.XXH3_64bits_update.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
        )
        self.library.XXH3_64bits_update.restype = ctypes.c_int
        self.library.XXH3_64bits_digest.argtypes = (ctypes.c_void_p,)
        self.library.XXH3_64bits_digest.restype = ctypes.c_uint64

    def bytes(self, value: bytes) -> str:
        if not value:
            return f"{self.library.XXH3_64bits(None, 0):016x}"
        buffer = ctypes.create_string_buffer(value)
        return f"{self.library.XXH3_64bits(buffer, len(value)):016x}"

    def file(self, path: Path) -> str:
        state = self.library.XXH3_createState()
        if not state or self.library.XXH3_64bits_reset(state) != 0:
            raise SimilarityError("cannot initialize XXH3 state")
        try:
            with path.open("rb") as handle:
                while block := handle.read(16 * 1024 * 1024):
                    buffer = ctypes.create_string_buffer(block)
                    if self.library.XXH3_64bits_update(
                        state, buffer, len(block)
                    ) != 0:
                        raise SimilarityError(f"XXH3 update failed: {path}")
            return f"{self.library.XXH3_64bits_digest(state):016x}"
        finally:
            self.library.XXH3_freeState(state)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def bit_hash(bits: Iterable[bool]) -> int:
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def extended_hashes(path: Path) -> tuple[int, int, int]:
    """Return Czkawka-style 16x16 mean, gradient, and block hashes."""

    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened).convert("L")
        mean_pixels = np.asarray(
            image.resize((16, 16), Image.Resampling.LANCZOS),
            dtype=np.float64,
        )
        mean_hash = bit_hash((mean_pixels >= np.median(mean_pixels)).flat)
        gradient_pixels = np.asarray(
            image.resize((17, 16), Image.Resampling.LANCZOS),
            dtype=np.int16,
        )
        gradient_hash = bit_hash(
            (gradient_pixels[:, 1:] > gradient_pixels[:, :-1]).flat
        )
        block_bits = []
        for top in range(0, 16, 4):
            for left in range(0, 16, 4):
                block = mean_pixels[top : top + 4, left : left + 4]
                block_bits.extend((block >= block.mean()).flat)
        block_hash = bit_hash(block_bits)
    return mean_hash, gradient_hash, block_hash


def selected_master_references(review: KurtSchwarzReview) -> list[dict[str, Any]]:
    """Return exactly one selected published master per Kurt object."""

    records = {
        item["record_key"]: item
        for item in review.records()
        if not item.get("alias_decision")
    }
    with review.connect(read_only=True) as connection:
        rows = connection.execute(
            """WITH latest AS (
                   SELECT * FROM media_decisions
                   WHERE decision_id IN (
                     SELECT MAX(decision_id) FROM media_decisions
                     GROUP BY candidate_id
                   )
                 )
                 SELECT media.*
                 FROM media_candidates AS media
                 JOIN latest ON latest.candidate_id=media.candidate_id
                 WHERE latest.disposition='selected_master'
                 ORDER BY media.record_key, media.candidate_id"""
        ).fetchall()
    result = []
    seen_records = set()
    for raw in rows:
        item = dict(raw)
        record_key = str(item["record_key"])
        if record_key not in records:
            continue
        if record_key in seen_records:
            raise SimilarityError(
                f"published object has multiple selected masters: {record_key}"
            )
        seen_records.add(record_key)
        record = records[record_key]
        result.append(
            {
                **item,
                "record_key": record_key,
                "inventory": record["display_inventory"],
                "title": record["display_title"],
                "path": review.candidate_path(item["candidate_id"]),
            }
        )
    if len(result) != len(records):
        missing = sorted(set(records) - seen_records)
        raise SimilarityError(
            "every published object must have one selected master; missing: "
            + ", ".join(missing[:20])
        )
    return result


def exact_hash_evidence(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply Czkawka's size/prehash/full-hash funnel without rehashing uniques."""

    xxh3 = XXH3()
    by_size: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_size[int(item["size_bytes"])].append(item)
    prehashed = 0
    full_hashed = 0
    exact_groups = []
    for size, size_group in by_size.items():
        if len(size_group) < 2:
            continue
        by_prehash: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in size_group:
            with Path(item["path"]).open("rb") as handle:
                beginning = handle.read(POLICY["xxh3_prehash_bytes"])
            item["xxh3_prehash"] = xxh3.bytes(beginning)
            prehashed += 1
            by_prehash[item["xxh3_prehash"]].append(item)
        for prehash, prehash_group in by_prehash.items():
            if len(prehash_group) < 2:
                continue
            by_sha: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for item in prehash_group:
                by_sha[str(item["content_sha256"])].append(item)
            for digest, digest_group in by_sha.items():
                if len(digest_group) < 2:
                    continue
                # SHA-256 is already the authoritative digest for unchanged
                # archive bytes.  Compute one independent full XXH3 value per
                # exact SHA group, not once for every repeated manifest link.
                full_value = xxh3.file(Path(digest_group[0]["path"]))
                full_hashed += 1
                for item in digest_group:
                    item["xxh3_full"] = full_value
                exact_groups.append(
                    {
                        "size_bytes": size,
                        "xxh3_prehash": prehash,
                        "xxh3_full": full_value,
                        "content_sha256": digest,
                        "members": [
                            {
                                "kind": item["kind"],
                                "candidate_id": item["candidate_id"],
                                "record_key": item.get("record_key", ""),
                                "filename": item["filename"],
                            }
                            for item in digest_group
                        ],
                    }
                )
    return {
        "size_collision_files_prehashed": prehashed,
        "exact_group_files_full_hashed": full_hashed,
        "exact_groups": exact_groups,
    }


def active_similarity_pair_evidence(
    review: KurtSchwarzReview,
) -> list[dict[str, Any]]:
    """Carry confirmed pair evidence into a replacement run for audit/UI use."""

    with review.connect(read_only=True) as connection:
        latest = review._latest_object_census_similarity_decisions(connection)
    required = {
        pair_id
        for pair_id, decision in latest.items()
        if decision["outcome"] == "same_photo"
    }
    found: dict[str, dict[str, Any]] = {}
    runs = review.review_root / "object-census-similarity/runs"
    for path in sorted(runs.glob("*/similarity-pairs.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for raw in payload.get("pairs", []):
            pair = str(raw.get("pair_id", ""))
            if pair in required and pair not in found:
                found[pair] = dict(raw)
        if set(found) == required:
            break
    missing = required - set(found)
    if missing:
        raise SimilarityError(
            "active similarity decisions lost their evidence pairs: "
            + ", ".join(sorted(missing))
        )
    return list(found.values())


def pair_id(left: str, right: str) -> str:
    first, second = sorted((left, right))
    digest = hashlib.sha256(
        f"iss-kurt-schwarz-census-similarity-v1\0{first}\0{second}".encode()
    ).hexdigest()
    return "KSPAIR-" + digest[:24]


def compare_metric(
    left: Path,
    right: Path,
    metric: str,
    *,
    trim: bool = False,
    timeout: int = 60,
) -> float:
    def image_arguments(path: Path) -> list[str]:
        arguments = ["(", str(path), "-auto-orient", "-alpha", "remove"]
        if trim:
            arguments.extend(["-fuzz", "2%", "-trim", "+repage"])
        arguments.extend(
            ["-resize", "512x512!", "-colorspace", "sRGB", ")"]
        )
        return arguments

    process = subprocess.run(
        [
            "magick",
            "compare",
            "-metric",
            metric,
            *image_arguments(left),
            *image_arguments(right),
            "null:",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    raw = (process.stderr or process.stdout).strip().split()
    if process.returncode not in {0, 1} or not raw:
        raise SimilarityError(
            f"{metric} comparison failed for {left.name} / {right.name}: "
            f"{(process.stderr or process.stdout).strip()[:1000]}"
        )
    try:
        return float(raw[0])
    except ValueError as error:
        raise SimilarityError(
            f"cannot parse {metric} comparison for {left.name} / {right.name}"
        ) from error


def decoded_pixel_state(left: Path, right: Path, *, timeout: int = 300) -> str:
    process = subprocess.run(
        [
            "magick",
            "compare",
            "-metric",
            "AE",
            f"{left}[0]",
            f"{right}[0]",
            "null:",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    raw = (process.stderr or process.stdout).strip().splitlines()
    if process.returncode not in {0, 1} or not raw:
        return "comparison_failed"
    metric = raw[-1].strip()
    return "identical" if metric in {"0", "0 (0)"} else "different"


def confidence(pair: dict[str, Any]) -> str:
    if (
        pair.get("exact_content")
        or pair["decoded_pixel_state"] == "identical"
        or pair["exact_preview"]
    ):
        return "exact_visual"
    if pair["ssim"] >= POLICY["high_confidence_ssim"] and pair["ncc"] >= POLICY[
        "high_confidence_ncc"
    ]:
        return "high_confidence"
    best_ssim = max(pair["ssim"], pair["trimmed_ssim"])
    best_ncc = max(pair["ncc"], pair["trimmed_ncc"])
    if best_ssim >= POLICY["likely_ssim"] and best_ncc >= POLICY["likely_ncc"]:
        return "likely_review"
    return "possible_review"


def connected_group_ids(pairs: list[dict[str, Any]]) -> dict[str, str]:
    parent: dict[str, str] = {}

    def find(value: str) -> str:
        parent.setdefault(value, value)
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for item in pairs:
        union(item["candidate_a_id"], item["candidate_b_id"])
    members: dict[str, list[str]] = defaultdict(list)
    for candidate in parent:
        members[find(candidate)].append(candidate)
    result = {}
    for values in members.values():
        digest = hashlib.sha256("\0".join(sorted(values)).encode()).hexdigest()
        group = "KSGROUP-" + digest[:20]
        result.update({candidate: group for candidate in values})
    return result


def build(
    *,
    unit_root: Path,
    review_root: Path,
    output: Path,
    workers: int = 2,
    verify_decoded_pixels: bool = True,
) -> dict[str, Any]:
    if workers not in {1, 2, 3, 4}:
        raise SimilarityError("workers must be between one and four")
    if output.exists():
        raise SimilarityError(f"refusing to overwrite similarity run: {output}")
    review = KurtSchwarzReview(unit_root, review_root)
    candidates = [
        item
        for item in review.object_census_candidates()
        if item["review_state"] == "pending"
        and item["queue_class"] in {
            "new_local_review",
            "surrogate_master_review",
        }
    ]
    references = selected_master_references(review)
    candidate_fingerprint = hashlib.sha256(
        stable_json(
            [
                {
                    "candidate_id": item["candidate_id"],
                    "content_sha256": item["content_sha256"],
                    "final_path": item["final_path"],
                }
                for item in candidates
            ]
        ).encode()
    ).hexdigest()
    reference_fingerprint = hashlib.sha256(
        stable_json(
            [
                {
                    "candidate_id": item["candidate_id"],
                    "record_key": item["record_key"],
                    "content_sha256": item["content_sha256"],
                    "final_path": item["final_path"],
                }
                for item in references
            ]
        ).encode()
    ).hexdigest()
    run_fingerprint = "sha256:" + hashlib.sha256(
        stable_json(
            {
                "format": FORMAT,
                "candidate_fingerprint": candidate_fingerprint,
                "reference_fingerprint": reference_fingerprint,
                "policy": POLICY,
            }
        ).encode()
    ).hexdigest()

    exact_items = [
        {
            **item,
            "kind": "census_candidate",
            "path": review.object_census_path(item["candidate_id"]),
        }
        for item in candidates
    ] + [
        {**item, "kind": "selected_master"}
        for item in references
    ]
    exact_evidence = exact_hash_evidence(exact_items)

    hashed: list[dict[str, Any]] = []
    hashed_references: list[dict[str, Any]] = []
    preview_errors: list[dict[str, str]] = []

    def hash_item(item: dict[str, Any], kind: str) -> dict[str, Any]:
        preview = (
            review.object_census_preview(item["candidate_id"])
            if kind == "census_candidate"
            else review.preview(item["candidate_id"])
        )
        phash, dhash = image_hashes(preview)
        mean_hash, gradient_hash, block_hash = extended_hashes(preview)
        with Image.open(preview) as opened:
            preview_dimensions = opened.size
        return {
            "item": item,
            "kind": kind,
            "preview": preview,
            "path": (
                review.object_census_path(item["candidate_id"])
                if kind == "census_candidate"
                else Path(item["path"])
            ),
            "preview_sha256": sha256_file(preview),
            "preview_dimensions": preview_dimensions,
            "phash": phash,
            "dhash": dhash,
            "mean_hash": mean_hash,
            "gradient_hash": gradient_hash,
            "block_hash": block_hash,
        }

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(hash_item, item, kind): (item, kind)
            for kind, values in (
                ("census_candidate", candidates),
                ("selected_master", references),
            )
            for item in values
        }
        for future in as_completed(futures):
            item, kind = futures[future]
            try:
                result = future.result()
                (
                    hashed
                    if kind == "census_candidate"
                    else hashed_references
                ).append(result)
            except Exception as error:  # keep unreadable candidates manual
                preview_errors.append(
                    {
                        "kind": kind,
                        "candidate_id": item["candidate_id"],
                        "filename": item["filename"],
                        "error": f"{type(error).__name__}: {str(error)[:1000]}",
                    }
                )
    hashed.sort(key=lambda item: item["item"]["candidate_id"])
    hashed_references.sort(key=lambda item: item["item"]["candidate_id"])

    def distances(left: dict[str, Any], right: dict[str, Any]) -> dict[str, int]:
        return {
            "phash_distance": hamming(left["phash"], right["phash"]),
            "dhash_distance": hamming(left["dhash"], right["dhash"]),
            "mean_hash_distance": hamming(
                left["mean_hash"], right["mean_hash"]
            ),
            "gradient_hash_distance": hamming(
                left["gradient_hash"], right["gradient_hash"]
            ),
            "block_hash_distance": hamming(
                left["block_hash"], right["block_hash"]
            ),
        }

    def bounded(
        left: dict[str, Any],
        right: dict[str, Any],
        values: dict[str, int],
    ) -> bool:
        if left["item"]["content_sha256"] == right["item"]["content_sha256"]:
            return True
        legacy = (
            values["phash_distance"] <= POLICY["phash_maximum"]
            and values["dhash_distance"] <= POLICY["dhash_maximum"]
        )
        czkawka = (
            values["mean_hash_distance"] <= POLICY["mean_hash_maximum"]
            and values["gradient_hash_distance"]
            <= POLICY["gradient_hash_maximum"]
            and values["block_hash_distance"] <= POLICY["block_hash_maximum"]
        )
        return legacy or czkawka

    possible: list[tuple[dict[str, Any], dict[str, Any], dict[str, int]]] = []
    for index, left in enumerate(hashed):
        for right in hashed[index + 1 :]:
            values = distances(left, right)
            if bounded(left, right, values):
                possible.append((left, right, values))

    reference_possible = []
    for candidate in hashed:
        for reference in hashed_references:
            values = distances(candidate, reference)
            if bounded(candidate, reference, values):
                reference_possible.append((candidate, reference, values))

    comparison_errors: list[dict[str, str]] = []

    def compare_pair(
        task: tuple[dict[str, Any], dict[str, Any], dict[str, int]]
    ) -> dict[str, Any]:
        left, right, distances = task
        left_candidate, right_candidate = left["item"], right["item"]
        row: dict[str, Any] = {
            "pair_id": pair_id(
                left_candidate["candidate_id"], right_candidate["candidate_id"]
            ),
            "group_id": "",
            "candidate_a_id": left_candidate["candidate_id"],
            "candidate_a_filename": left_candidate["filename"],
            "candidate_a_final_path": left_candidate["final_path"],
            "candidate_a_size_bytes": int(left_candidate["size_bytes"]),
            "candidate_b_id": right_candidate["candidate_id"],
            "candidate_b_filename": right_candidate["filename"],
            "candidate_b_final_path": right_candidate["final_path"],
            "candidate_b_size_bytes": int(right_candidate["size_bytes"]),
            **distances,
            "ssim": compare_metric(left["preview"], right["preview"], "SSIM"),
            "ncc": compare_metric(left["preview"], right["preview"], "NCC"),
            "trimmed_ssim": compare_metric(
                left["preview"], right["preview"], "SSIM", trim=True
            ),
            "trimmed_ncc": compare_metric(
                left["preview"], right["preview"], "NCC", trim=True
            ),
            "exact_preview": left["preview_sha256"] == right["preview_sha256"],
            "exact_content": (
                left_candidate["content_sha256"]
                == right_candidate["content_sha256"]
            ),
            "decoded_pixel_state": "not_checked",
            "confidence": "",
        }
        same_dimensions = left["preview_dimensions"] == right["preview_dimensions"]
        if (
            verify_decoded_pixels
            and same_dimensions
            and row["ssim"] >= POLICY["high_confidence_ssim"]
            and row["ncc"] >= POLICY["high_confidence_ncc"]
        ):
            row["decoded_pixel_state"] = decoded_pixel_state(
                left["path"], right["path"],
            )
        row["confidence"] = confidence(row)
        return row

    pairs: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(compare_pair, task): task for task in possible}
        for future in as_completed(futures):
            left, right, _ = futures[future]
            try:
                pairs.append(future.result())
            except Exception as error:
                comparison_errors.append(
                    {
                        "candidate_a_id": left["item"]["candidate_id"],
                        "candidate_b_id": right["item"]["candidate_id"],
                        "error": f"{type(error).__name__}: {str(error)[:1000]}",
                    }
                )

    reference_matches = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(compare_pair, task): task
            for task in reference_possible
        }
        for future in as_completed(futures):
            candidate, reference, _ = futures[future]
            try:
                row = future.result()
                candidate_item = candidate["item"]
                reference_item = reference["item"]
                row.update(
                    {
                        "match_id": "KSMATCH-" + hashlib.sha256(
                            (
                                f"{FORMAT}\0{candidate_item['candidate_id']}\0"
                                f"{reference_item['candidate_id']}"
                            ).encode()
                        ).hexdigest()[:24],
                        "candidate_id": candidate_item["candidate_id"],
                        "candidate_filename": candidate_item["filename"],
                        "reference_candidate_id": reference_item["candidate_id"],
                        "reference_record_key": reference_item["record_key"],
                        "reference_inventory": reference_item["inventory"],
                        "reference_title": reference_item["title"],
                        "reference_filename": reference_item["filename"],
                    }
                )
                reference_matches.append(row)
            except Exception as error:
                comparison_errors.append(
                    {
                        "candidate_id": candidate["item"]["candidate_id"],
                        "reference_candidate_id": reference["item"]["candidate_id"],
                        "error": f"{type(error).__name__}: {str(error)[:1000]}",
                    }
                )
    existing_pair_ids = {item["pair_id"] for item in pairs}
    carried_pairs = [
        item
        for item in active_similarity_pair_evidence(review)
        if item["pair_id"] not in existing_pair_ids
    ]
    pairs.extend(carried_pairs)
    groups = connected_group_ids(pairs)
    for item in pairs:
        item["group_id"] = groups[item["candidate_a_id"]]
    confidence_order = {
        "exact_visual": 0,
        "high_confidence": 1,
        "likely_review": 2,
        "possible_review": 3,
    }
    pairs.sort(
        key=lambda item: (
            confidence_order[item["confidence"]],
            -max(item["ssim"], item["trimmed_ssim"]),
            item["pair_id"],
        )
    )
    reference_matches.sort(
        key=lambda item: (
            confidence_order[item["confidence"]],
            -max(item["ssim"], item["trimmed_ssim"]),
            item["match_id"],
        )
    )
    output.mkdir(parents=True)
    generated_at = utc_now()
    payload = {
        "format": FORMAT,
        "generated_at": generated_at,
        "run_fingerprint": run_fingerprint,
        "candidate_fingerprint": "sha256:" + candidate_fingerprint,
        "reference_fingerprint": "sha256:" + reference_fingerprint,
        "unit_root": str(unit_root),
        "review_root": str(review_root),
        "policy": POLICY,
        "candidate_count": len(candidates),
        "hashed_candidate_count": len(hashed),
        "selected_master_count": len(references),
        "hashed_selected_master_count": len(hashed_references),
        "pair_count": len(pairs),
        "carried_active_pair_count": len(carried_pairs),
        "reference_match_count": len(reference_matches),
        "group_count": len(set(groups.values())),
        "preview_errors": preview_errors,
        "comparison_errors": comparison_errors,
        "confidence_counts": dict(Counter(item["confidence"] for item in pairs)),
        "reference_confidence_counts": dict(
            Counter(item["confidence"] for item in reference_matches)
        ),
        "exact_hash_evidence": exact_evidence,
        "deletion_authorized": False,
        "pairs": pairs,
        "reference_matches": reference_matches,
    }
    temporary = output / f".similarity-pairs.json.tmp-{os.getpid()}"
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, output / "similarity-pairs.json")
    with (output / "similarity-pairs.csv").open(
        "x", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=PAIR_FIELDS)
        writer.writeheader()
        writer.writerows(
            {field: item.get(field, "") for field in PAIR_FIELDS} for item in pairs
        )
    summary = {
        key: value
        for key, value in payload.items()
        if key not in {"pairs", "reference_matches"}
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build guarded Kurt object-census similarity evidence"
    )
    parser.add_argument("--unit-root", type=Path, required=True)
    parser.add_argument("--review-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--skip-decoded-pixels", action="store_true")
    arguments = parser.parse_args()
    result = build(
        unit_root=arguments.unit_root,
        review_root=arguments.review_root,
        output=arguments.output,
        workers=arguments.workers,
        verify_decoded_pixels=not arguments.skip_decoded_pixels,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
