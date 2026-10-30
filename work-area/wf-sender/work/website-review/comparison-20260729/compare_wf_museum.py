#!/usr/bin/env python3
"""Compare the 2022 WF-Museum backup with the current ~/wp import."""

from __future__ import annotations

import collections
import csv
import difflib
import hashlib
import html
import json
import os
import re
import subprocess
import time
import unicodedata
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
from scipy.fft import dctn


HERE = Path(__file__).resolve().parent
BACKUP_UPLOADS = Path(
    "/mnt/volume/iss-archive-work/wf-sender/work/website-review/"
    "latest-2022-03-06/site/wp-content/uploads"
)
LOCAL_UPLOADS = Path("/home/vladimir/wp/wp/wp-content/uploads")


def query(
    container: str, user: str, password: str, database: str, sql: str
) -> list[dict[str, object]]:
    result = subprocess.run(
        [
            "docker",
            "exec",
            container,
            "mariadb",
            "--raw",
            "-N",
            f"-u{user}",
            f"-p{password}",
            database,
            "-e",
            sql,
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def decode_hex(value: object) -> str:
    return bytes.fromhex(str(value or "")).decode("utf-8", "replace")


def title_key(value: str) -> str:
    value = unicodedata.normalize("NFKD", html.unescape(value)).casefold()
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def text_value(value: str) -> str:
    value = re.sub(r"<!--.*?-->", " ", value, flags=re.S)
    value = re.sub(
        r"<script.*?</script>|<style.*?</style>", " ", value, flags=re.S | re.I
    )
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def post_comparison() -> tuple[list[dict[str, object]], dict[str, object]]:
    backup = query(
        "wf_sender_review_db",
        "wf_review",
        "local-review-only",
        "wf_review",
        """
        SELECT JSON_OBJECT(
          'id', ID,
          'date', DATE_FORMAT(post_date, '%Y-%m-%d'),
          'slug', post_name,
          'title', post_title,
          'content_hex', HEX(post_content)
        )
        FROM o8fuu_posts
        WHERE post_type='post' AND post_status='publish'
        ORDER BY post_date, ID
        """,
    )
    local = query(
        "wp_db",
        "wpuser",
        "wp_pass",
        "wordpress",
        """
        SELECT JSON_OBJECT(
          'id', p.ID,
          'date', DATE_FORMAT(p.post_date, '%Y-%m-%d'),
          'slug', p.post_name,
          'title', p.post_title,
          'status', p.post_status,
          'source_url', (
            SELECT meta_value FROM wp_postmeta
            WHERE post_id=p.ID AND meta_key='iss_source_url' LIMIT 1
          ),
          'content_hex', HEX(p.post_content)
        )
        FROM wp_posts p
        WHERE p.post_type='archivbeitrag'
          AND EXISTS(
            SELECT 1 FROM wp_postmeta m
            WHERE m.post_id=p.ID
              AND m.meta_key='iss_source_site'
              AND m.meta_value='wf-museum'
          )
        ORDER BY p.post_date, p.ID
        """,
    )

    for row in backup + local:
        row["content"] = decode_hex(row.pop("content_hex"))
        row["text"] = text_value(str(row["content"]))
        row["title_key"] = title_key(str(row["title"]))

    local_by_title: dict[str, list[dict[str, object]]] = collections.defaultdict(list)
    local_by_source_slug: dict[str, list[dict[str, object]]] = collections.defaultdict(
        list
    )
    for row in local:
        local_by_title[str(row["title_key"])].append(row)
        source_url = str(row.get("source_url") or "").rstrip("/")
        if source_url:
            local_by_source_slug[source_url.rsplit("/", 1)[-1]].append(row)

    rows: list[dict[str, object]] = []
    matched_local_ids: set[int] = set()
    for source in backup:
        candidates = local_by_title.get(str(source["title_key"]), [])
        basis = "normalized_title"
        if not candidates:
            candidates = local_by_source_slug.get(str(source["slug"]), [])
            basis = "source_url_slug"
        target = candidates[0] if candidates else None
        if not target:
            rows.append(
                {
                    "backup_id": source["id"],
                    "backup_date": source["date"],
                    "backup_title": source["title"],
                    "backup_slug": source["slug"],
                    "local_id": "",
                    "local_status": "",
                    "local_title": "",
                    "match_basis": "",
                    "text_ratio": "",
                    "classification": "missing_from_local_import",
                    "backup_text_chars": len(str(source["text"])),
                    "local_text_chars": "",
                }
            )
            continue
        matched_local_ids.add(int(target["id"]))
        ratio = (
            difflib.SequenceMatcher(
                None,
                str(source["text"]),
                str(target["text"]),
                autojunk=False,
            ).ratio()
            if source["text"] or target["text"]
            else 1.0
        )
        if len(str(target["text"])) <= 20 and len(str(source["text"])) > 20:
            classification = "empty_or_nearly_empty_local_body"
        elif ratio < 0.95:
            classification = "material_text_difference"
        else:
            classification = "text_preserved"
        rows.append(
            {
                "backup_id": source["id"],
                "backup_date": source["date"],
                "backup_title": source["title"],
                "backup_slug": source["slug"],
                "local_id": target["id"],
                "local_status": target["status"],
                "local_title": target["title"],
                "match_basis": basis,
                "text_ratio": f"{ratio:.6f}",
                "classification": classification,
                "backup_text_chars": len(str(source["text"])),
                "local_text_chars": len(str(target["text"])),
            }
        )

    later = [row for row in local if int(row["id"]) not in matched_local_ids]
    summary = {
        "backup_published_posts": len(backup),
        "local_wf_museum_archivbeitrag": len(local),
        "matched_posts": sum(bool(row["local_id"]) for row in rows),
        "text_preserved": sum(
            row["classification"] == "text_preserved" for row in rows
        ),
        "material_text_difference": sum(
            row["classification"] == "material_text_difference" for row in rows
        ),
        "empty_or_nearly_empty_local_body": sum(
            row["classification"] == "empty_or_nearly_empty_local_body"
            for row in rows
        ),
        "missing_from_local_import": sum(
            row["classification"] == "missing_from_local_import" for row in rows
        ),
        "later_local_records": len(later),
        "later_local_published": sum(row["status"] == "publish" for row in later),
        "later_local_private": sum(row["status"] == "private" for row in later),
        "later_local_titles": [
            {
                "id": row["id"],
                "date": row["date"],
                "status": row["status"],
                "title": row["title"],
            }
            for row in later
        ],
    }
    return rows, summary


def attachment_rows(
    container: str,
    user: str,
    password: str,
    database: str,
    posts: str,
    postmeta: str,
) -> list[dict[str, object]]:
    return query(
        container,
        user,
        password,
        database,
        f"""
        SELECT JSON_OBJECT(
          'id', p.ID,
          'title', p.post_title,
          'relative_path', m.meta_value
        )
        FROM {posts} p
        JOIN {postmeta} m
          ON m.post_id=p.ID AND m.meta_key='_wp_attached_file'
        WHERE p.post_type='attachment'
        """,
    )


def image_phash(path: Path) -> tuple[int, tuple[int, int]]:
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("L")
        dimensions = image.size
        pixels = np.asarray(
            image.resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float32
        )
        transformed = dctn(pixels, type=2, norm="ortho")[:8, :8]
        median = np.median(transformed.ravel()[1:])
        bits = (transformed > median).ravel()
        value = 0
        for bit in bits:
            value = (value << 1) | int(bit)
        return value, dimensions


class BKTree:
    def __init__(self) -> None:
        self.root: list[object] | None = None

    def add(self, value: int) -> None:
        if self.root is None:
            self.root = [value, {}]
            return
        node = self.root
        while True:
            distance = (value ^ int(node[0])).bit_count()
            children = node[1]
            assert isinstance(children, dict)
            if distance in children:
                node = children[distance]
            else:
                children[distance] = [value, {}]
                return

    def find(self, value: int, radius: int) -> list[tuple[int, int]]:
        if self.root is None:
            return []
        found: list[tuple[int, int]] = []
        stack = [self.root]
        while stack:
            node = stack.pop()
            distance = (value ^ int(node[0])).bit_count()
            if distance <= radius:
                found.append((distance, int(node[0])))
            children = node[1]
            assert isinstance(children, dict)
            for child_distance, child in children.items():
                if distance - radius <= child_distance <= distance + radius:
                    stack.append(child)
        return found


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def media_comparison() -> tuple[list[dict[str, object]], dict[str, object]]:
    backup_rows = attachment_rows(
        "wf_sender_review_db",
        "wf_review",
        "local-review-only",
        "wf_review",
        "o8fuu_posts",
        "o8fuu_postmeta",
    )
    local_rows = attachment_rows(
        "wp_db",
        "wpuser",
        "wp_pass",
        "wordpress",
        "wp_posts",
        "wp_postmeta",
    )
    image_extensions = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
    backup_images: list[tuple[dict[str, object], Path]] = []
    local_images: list[tuple[dict[str, object], Path]] = []
    for row in backup_rows:
        path = BACKUP_UPLOADS / str(row["relative_path"])
        if path.suffix.casefold() in image_extensions:
            backup_images.append((row, path))
    for row in local_rows:
        path = LOCAL_UPLOADS / str(row["relative_path"])
        if path.suffix.casefold() in image_extensions:
            local_images.append((row, path))

    values: dict[str, list[tuple[int, tuple[int, int], dict[str, object], Path]]] = {}
    for label, images in (("local", local_images), ("backup", backup_images)):
        start = time.time()
        calculated = []
        for index, (row, path) in enumerate(images, 1):
            try:
                perceptual_hash, dimensions = image_phash(path)
                calculated.append((perceptual_hash, dimensions, row, path))
            except Exception:
                continue
            if index % 1000 == 0:
                print(
                    f"{label}: {index}/{len(images)} images hashed",
                    flush=True,
                )
        print(
            f"{label}: {len(calculated)} hashes in {time.time() - start:.1f}s",
            flush=True,
        )
        values[label] = calculated

    local_by_hash: dict[int, list[tuple[tuple[int, int], dict[str, object], Path]]] = (
        collections.defaultdict(list)
    )
    tree = BKTree()
    for perceptual_hash, dimensions, row, path in values["local"]:
        if perceptual_hash not in local_by_hash:
            tree.add(perceptual_hash)
        local_by_hash[perceptual_hash].append((dimensions, row, path))

    results: list[dict[str, object]] = []
    distances: collections.Counter[int] = collections.Counter()
    for perceptual_hash, (width, height), row, path in values["backup"]:
        aspect = width / height if height else 0
        selected = None
        for distance, local_hash in sorted(tree.find(perceptual_hash, 4)):
            for local_dimensions, local_row, local_path in local_by_hash[local_hash]:
                local_width, local_height = local_dimensions
                local_aspect = local_width / local_height if local_height else 0
                if aspect and abs(aspect - local_aspect) / aspect <= 0.04:
                    selected = (
                        distance,
                        local_dimensions,
                        local_row,
                        local_path,
                    )
                    break
            if selected:
                break
        if selected:
            distance, local_dimensions, local_row, local_path = selected
            distances[distance] += 1
            classification = "perceptual_match"
            local_relative_path = local_row["relative_path"]
            local_id = local_row["id"]
        else:
            classification = "not_perceptually_matched"
            local_relative_path = ""
            local_id = ""
            distance = ""
        results.append(
            {
                "backup_attachment_id": row["id"],
                "backup_title": row["title"],
                "backup_relative_path": row["relative_path"],
                "backup_width": width,
                "backup_height": height,
                "classification": classification,
                "phash_distance": distance,
                "local_attachment_id": local_id,
                "local_relative_path": local_relative_path,
            }
        )

    matched = sum(row["classification"] == "perceptual_match" for row in results)
    summary = {
        "backup_attachment_rows": len(backup_rows),
        "local_attachment_rows": len(local_rows),
        "backup_image_rows": len(values["backup"]),
        "local_image_rows": len(values["local"]),
        "perceptual_matches_hamming_le_4": matched,
        "perceptual_unmatched": len(results) - matched,
        "perceptual_coverage_percent": round(100 * matched / len(results), 1),
        "phash_distance_counts": dict(sorted(distances.items())),
    }
    return results, summary


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    posts, post_summary = post_comparison()
    media, media_summary = media_comparison()
    write_csv(HERE / "post-comparison.csv", posts)
    write_csv(HERE / "media-perceptual-comparison.csv", media)
    summary = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "backup": "wf-museum.de Duplicator snapshot 2022-03-06",
        "local": "/home/vladimir/wp active WordPress database",
        "public_current": "offline notice only",
        "posts": post_summary,
        "media": media_summary,
    }
    (HERE / "comparison-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
