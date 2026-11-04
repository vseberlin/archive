#!/usr/bin/env python3
"""Face index for person proposals and same-person clusters.

SUPPORT_INDEX tool, sibling of visual_knn.py. Detects and embeds faces in the
institution museum-digital capture and in new images, then

  * builds a gallery of *seed* faces: reference images with exactly one usable
    face and exactly one person linked through a "depicted" event,
  * proposes names for faces in new images, but only for people on an explicit
    allowlist (public figures / Personen der Zeitgeschichte),
  * clusters all faces (mutual-kNN graph) so a reviewer can name one cluster.

Face embeddings are biometric data. Everything stays under --work-root, is
internal review evidence, and must not be published. Nothing here writes to a
unit, CATALOG.csv or museum-digital.

Commands:
    index-reference  detect + embed faces in MD full images (resumable)
    index-new        detect + embed faces in new images (CSV image_id,path)
    evaluate         leave-one-image-out on seed faces, threshold sweep
    propose          allowlisted name proposals for faces in new images
    cluster          same-person clusters over all indexed faces

Backends:
    sface    OpenCV YuNet + SFace (Apache-2.0, small, CPU)   [default]
    arcface  InsightFace buffalo_l / buffalo_s (better; weights are
             non-commercial research licence - check before use)
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

import visual_knn as vk  # shared capture parsing, fetch, atomic writes

VERSION = "0.1.0"
DEFAULT_THRESHOLD = {"sface": 0.40, "arcface": 0.40}
DEPICTED_DEFAULT = r"abgebildet|dargestellt|portr[äa]tiert|depicted|portrayed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    side TEXT NOT NULL, item_id TEXT NOT NULL, backend TEXT NOT NULL,
    source_key TEXT NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
    faces INTEGER NOT NULL DEFAULT 0, meta_json TEXT NOT NULL DEFAULT '{}',
    indexed_at TEXT NOT NULL,
    PRIMARY KEY (side, item_id, backend)
);
CREATE TABLE IF NOT EXISTS faces (
    face_id TEXT NOT NULL, side TEXT NOT NULL, item_id TEXT NOT NULL,
    backend TEXT NOT NULL, x INTEGER, y INTEGER, w INTEGER, h INTEGER,
    face_px INTEGER, det_score REAL, vector BLOB NOT NULL, crop TEXT NOT NULL,
    PRIMARY KEY (face_id, backend)
);
"""


# ------------------------------------------------------------------ backends

class FaceBackend:
    def __init__(self, args) -> None:
        self.name = args.backend
        self.max_side = args.max_side
        if self.name == "sface":
            import cv2

            self.cv2 = cv2
            models = Path(args.models)
            self.detector = cv2.FaceDetectorYN.create(
                str(models / "face_detection_yunet_2023mar.onnx"), "", (320, 320), args.det_score, 0.3, 500
            )
            self.recognizer = cv2.FaceRecognizerSF.create(str(models / "face_recognition_sface_2021dec.onnx"), "")
        elif self.name == "arcface":
            from insightface.app import FaceAnalysis

            self.app = FaceAnalysis(name=args.arcface_pack, allowed_modules=["detection", "recognition"],
                                    providers=["CPUExecutionProvider"])
            self.app.prepare(ctx_id=-1, det_size=(640, 640), det_thresh=args.det_score)
        else:
            raise vk.VisualKnnError(f"unknown backend {self.name}")

    def faces(self, image: Image.Image) -> list[dict[str, Any]]:
        """Return faces in original-image pixel coordinates."""
        scale = min(1.0, self.max_side / max(image.size))
        small = image if scale == 1.0 else image.resize(
            (round(image.width * scale), round(image.height * scale)), Image.Resampling.BICUBIC
        )
        bgr = np.ascontiguousarray(np.asarray(small.convert("RGB"))[:, :, ::-1])
        found = []
        if self.name == "sface":
            self.detector.setInputSize((bgr.shape[1], bgr.shape[0]))
            _, rows = self.detector.detect(bgr)
            for row in [] if rows is None else rows:
                aligned = self.recognizer.alignCrop(bgr, row)
                vector = self.recognizer.feature(aligned).astype(np.float32).ravel()
                found.append({"box": row[:4] / scale, "score": float(row[-1]), "vector": vector})
        else:
            for face in self.app.get(bgr):
                x1, y1, x2, y2 = face.bbox
                found.append({"box": np.array([x1, y1, x2 - x1, y2 - y1]) / scale,
                              "score": float(face.det_score), "vector": face.normed_embedding.astype(np.float32)})
        for face in found:
            face["vector"] /= max(float(np.linalg.norm(face["vector"])), 1e-12)
            face["box"] = [int(round(v)) for v in face["box"]]
        return found


# ---------------------------------------------------------------- reference

def md_full_url(item: dict[str, Any]) -> str:
    images = item.get("object_images") if isinstance(item.get("object_images"), list) else []
    images = [r for r in images if isinstance(r, dict) and str(r.get("type") or "image").casefold() == "image"]
    if not images:
        return ""
    image = sorted(images, key=lambda r: (str(r.get("is_main")) != "j", int(r.get("order") or 999)))[0]
    folder, filename = vk.clean(image.get("folder")).strip("/"), vk.clean(image.get("filename_loc"))
    return f"{vk.MD_DATA}/{folder}/{filename}" if folder and filename else ""


def depicted_people(item: dict[str, Any], pattern: re.Pattern) -> list[str]:
    """Only persons linked by a depicted-type event; creators/photographers excluded."""
    names = []
    for event in item.get("object_events") or []:
        if not isinstance(event, dict) or not pattern.search(vk.clean(event.get("event_type_name"))):
            continue
        people = event.get("people")
        if isinstance(people, dict):
            names.append(people.get("people_display_name") or people.get("displayname"))
    return vk.unique(names)


def load_reference(run: Path, expected: int | None, pattern: re.Pattern) -> list[dict[str, Any]]:
    records = vk.load_capture(run, expected)  # validates coverage and counts
    raw: dict[str, dict[str, Any]] = {}
    for path in sorted((run / "raw").glob("*.json")):
        values = json.loads(path.read_text(encoding="utf-8")).get("results", {}).get("objects", {})
        for item in values.values() if isinstance(values, dict) else values:
            if isinstance(item, dict) and vk.clean(item.get("object_id")):
                raw[vk.clean(item.get("object_id"))] = item
    out = []
    for md_id, record in records.items():
        url = md_full_url(raw[md_id])
        if url:
            out.append({"id": md_id, "source_key": url, "meta": {
                "inventory": record["inventory"], "md_url": record["md_url"], "title": record["title"],
                "depicted": depicted_people(raw[md_id], pattern)}})
    return out


# -------------------------------------------------------------------- index

def connect(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(vk.inside(root, root / "faces.sqlite"))
    connection.executescript(SCHEMA)
    return connection


def index_images(args, side: str, jobs: list[dict[str, Any]], loader) -> int:
    connection = connect(args.work_root)
    backend = FaceBackend(args)
    done = {r[0]: r[1] for r in connection.execute(
        "SELECT item_id, source_key FROM images WHERE side=? AND backend=? AND status='ok'", (side, backend.name))}
    jobs = [j for j in jobs if done.get(j["id"]) != j["source_key"]][: args.limit or None]
    crops = vk.inside(args.work_root, args.work_root / "crops" / backend.name)
    crops.mkdir(parents=True, exist_ok=True)
    print(f"{side}: {len(jobs)} images to index with {backend.name}", file=sys.stderr)
    counts = {"images": 0, "faces": 0, "errors": 0}
    for number, job in enumerate(jobs, 1):
        connection.execute("DELETE FROM faces WHERE side=? AND item_id=? AND backend=?", (side, job["id"], backend.name))
        try:
            image = loader(job)
            found = backend.faces(image)
        except Exception as error:  # noqa: BLE001 - recorded per image
            connection.execute("INSERT OR REPLACE INTO images VALUES (?,?,?,?,?,?,?,?,?)",
                               (side, job["id"], backend.name, job["source_key"], "error",
                                vk.clean(f"{type(error).__name__}: {error}")[:500], 0,
                                json.dumps(job["meta"], ensure_ascii=False), vk.utc_now()))
            counts["errors"] += 1
            continue
        for n, face in enumerate(found):
            x, y, w, h = face["box"]
            face_id = f"{side}:{job['id']}:{n}"
            pad = int(0.35 * max(w, h))
            crop = image.crop((max(0, x - pad), max(0, y - pad), min(image.width, x + w + pad), min(image.height, y + h + pad)))
            crop.thumbnail((192, 192))
            crop_path = crops / (re.sub(r"[^A-Za-z0-9_.-]+", "_", face_id) + ".jpg")
            crop.convert("RGB").save(crop_path, quality=85)
            connection.execute("INSERT OR REPLACE INTO faces VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                               (face_id, side, job["id"], backend.name, x, y, w, h, min(w, h), face["score"],
                                face["vector"].astype(np.float16).tobytes(), str(crop_path)))
        connection.execute("INSERT OR REPLACE INTO images VALUES (?,?,?,?,?,?,?,?,?)",
                           (side, job["id"], backend.name, job["source_key"], "ok", "", len(found),
                            json.dumps(job["meta"], ensure_ascii=False), vk.utc_now()))
        counts["images"] += 1
        counts["faces"] += len(found)
        if number % 20 == 0 or number == len(jobs):
            connection.commit()
            print(f"\r{side}: {number}/{len(jobs)} images, {counts['faces']} faces, {counts['errors']} errors",
                  end="", file=sys.stderr, flush=True)
    connection.commit()
    print(file=sys.stderr)
    print(json.dumps({"backend": backend.name, **counts}))
    return 0


def load_full(data: bytes) -> Image.Image:
    from io import BytesIO
    from PIL import ImageOps

    with Image.open(BytesIO(data)) as opened:
        return ImageOps.exif_transpose(opened).convert("RGB")


def cmd_index_reference(args) -> int:
    jobs = load_reference(args.capture, args.expected_objects, re.compile(args.depicted_events, re.I))
    # Full images are large: no download cache; crops are kept instead.
    return index_images(args, "ref", jobs, lambda job: load_full(fetch_nocache(job["source_key"])))


def fetch_nocache(url: str) -> bytes:
    from urllib.request import Request, urlopen

    with urlopen(Request(url, headers={"User-Agent": vk.USER_AGENT}), timeout=90) as response:
        data = response.read(vk.MAX_DOWNLOAD_BYTES + 1)
    if len(data) > vk.MAX_DOWNLOAD_BYTES:
        raise vk.VisualKnnError("download exceeds bound")
    return data


def cmd_index_new(args) -> int:
    jobs = []
    with args.manifest.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            item_id, path = vk.clean(row.get(args.id_column)), Path(vk.clean(row.get(args.path_column)))
            if item_id and path.is_file():
                stat = path.stat()
                jobs.append({"id": item_id, "source_key": f"{path}|{stat.st_size}|{int(stat.st_mtime)}",
                             "meta": {"path": str(path)}})
            else:
                print(f"skip: {item_id or '?'} {path}", file=sys.stderr)
    return index_images(args, "new", jobs, lambda job: load_full(Path(job["meta"]["path"]).read_bytes()))


# ------------------------------------------------------------------ gallery

def load_faces(connection, backend: str, min_px: int) -> tuple[list[dict[str, Any]], np.ndarray]:
    meta = {(r[0], r[1]): json.loads(r[2]) for r in connection.execute(
        "SELECT side, item_id, meta_json FROM images WHERE backend=? AND status='ok'", (backend,))}
    counts = {(r[0], r[1]): r[2] for r in connection.execute(
        "SELECT side, item_id, faces FROM images WHERE backend=? AND status='ok'", (backend,))}
    rows, vectors = [], []
    for face_id, side, item_id, x, y, w, h, px, score, blob, crop in connection.execute(
        "SELECT face_id, side, item_id, x, y, w, h, face_px, det_score, vector, crop FROM faces "
        "WHERE backend=? AND face_px>=? ORDER BY face_id", (backend, min_px)):
        rows.append({"face_id": face_id, "side": side, "item_id": item_id, "box": [x, y, w, h], "face_px": px,
                     "det_score": round(score, 3), "crop": crop, "image_faces": counts.get((side, item_id), 0),
                     "meta": meta.get((side, item_id), {})})
        vectors.append(np.frombuffer(blob, dtype=np.float16))
    if not rows:
        return [], np.zeros((0, 1), np.float32)
    matrix = np.stack(vectors).astype(np.float32)
    matrix /= np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12)
    return rows, matrix


def seeds(rows: list[dict[str, Any]], min_px: int) -> list[int]:
    """Indices of reference faces that are the only face >= min_px in an image
    with exactly one depicted person."""
    usable_per_image = defaultdict(list)
    for i, row in enumerate(rows):
        if row["side"] == "ref":
            usable_per_image[row["item_id"]].append(i)
    return [ix[0] for ix in usable_per_image.values()
            if len(ix) == 1 and len(rows[ix[0]]["meta"].get("depicted", [])) == 1]


def match(vector: np.ndarray, seed_ix: list[int], rows, matrix, exclude_item: str | None = None) -> list[tuple[str, float, str]]:
    """Best similarity per person, descending: (person, sim, seed face_id)."""
    sims = matrix[seed_ix] @ vector
    best: dict[str, tuple[float, str]] = {}
    for sim, i in zip(sims, seed_ix):
        if exclude_item and rows[i]["item_id"] == exclude_item:
            continue
        person = rows[i]["meta"]["depicted"][0]
        if person not in best or sim > best[person][0]:
            best[person] = (float(sim), rows[i]["face_id"])
    return sorted(((p, s, f) for p, (s, f) in best.items()), key=lambda t: -t[1])


def load_allowlist(path: Path | None) -> set[str]:
    if not path:
        return set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {vk.clean(r.get("name")).casefold() for r in csv.DictReader(handle) if vk.clean(r.get("name"))}


# ----------------------------------------------------------------- commands

def cmd_evaluate(args) -> int:
    connection = connect(args.work_root)
    rows, matrix = load_faces(connection, args.backend, args.min_face_px)
    seed_ix = seeds(rows, args.min_face_px)
    per_person = defaultdict(int)
    for i in seed_ix:
        per_person[rows[i]["meta"]["depicted"][0]] += 1
    testable = [i for i in seed_ix if per_person[rows[i]["meta"]["depicted"][0]] >= 2]
    if len(testable) < 10:
        raise vk.VisualKnnError(f"only {len(testable)} seed faces of people with >=2 seeds")
    results = []
    for i in testable:
        ranked = match(matrix[i], seed_ix, rows, matrix, exclude_item=rows[i]["item_id"])
        if not ranked:
            continue
        margin = ranked[0][1] - (ranked[1][1] if len(ranked) > 1 else -1.0)
        results.append((ranked[0][1], margin, ranked[0][0] == rows[i]["meta"]["depicted"][0]))
    sweep = []
    for threshold in np.arange(0.20, 0.76, 0.05):
        chosen = [ok for sim, margin, ok in results if sim >= threshold and margin >= args.margin]
        sweep.append({"threshold": round(float(threshold), 2), "proposed": len(chosen),
                      "coverage": round(len(chosen) / len(results), 3),
                      "precision": round(sum(chosen) / len(chosen), 3) if chosen else None})
    report = {"backend": args.backend, "faces": len(rows), "seed_faces": len(seed_ix),
              "seed_people": len(per_person), "tested": len(results), "margin": args.margin,
              "min_face_px": args.min_face_px, "sweep": sweep, "created_at": vk.utc_now()}
    path = args.work_root / f"face-eval-{args.backend}.json"
    vk.atomic_write(args.work_root, path, lambda h: (json.dump(report, h, indent=2, ensure_ascii=False), h.write("\n")))
    print(f"faces {len(rows)}  seeds {len(seed_ix)} ({len(per_person)} people)  tested {len(results)}  margin {args.margin}")
    print(f"  {'threshold':>9} {'proposed':>8} {'coverage':>8} {'precision':>9}")
    for s in sweep:
        print(f"  {s['threshold']:>9} {s['proposed']:>8} {s['coverage']:>8} {s['precision'] if s['precision'] is not None else '-':>9}")
    print(f"receipt: {path}", file=sys.stderr)
    return 0


def cmd_propose(args) -> int:
    connection = connect(args.work_root)
    rows, matrix = load_faces(connection, args.backend, args.min_face_px)
    allow = load_allowlist(args.allowlist)
    # Match against ALL seeds so a closer non-allowlisted person blocks a wrong
    # allowlisted name; only the final name is filtered by the allowlist.
    seed_ix = seeds(rows, args.min_face_px)
    by_face = {r["face_id"]: r for r in rows}
    threshold = args.threshold if args.threshold is not None else DEFAULT_THRESHOLD[args.backend]
    if not allow:
        print("no --allowlist: names are withheld; faces are listed for clustering/review only", file=sys.stderr)
    run = args.run or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    jsonl_path, csv_path = (args.work_root / f"face-proposals-{run}.{ext}" for ext in ("jsonl", "csv"))
    for path in (jsonl_path, csv_path):
        if path.exists():
            raise vk.VisualKnnError(f"refusing to overwrite {path}")
    records = []
    for i, row in enumerate(rows):
        if row["side"] != "new":
            continue
        ranked = match(matrix[i], seed_ix, rows, matrix) if seed_ix else []
        best = ranked[0] if ranked else None
        margin = best[1] - ranked[1][1] if len(ranked) > 1 else None
        accepted = bool(best and best[1] >= threshold and (margin is None or margin >= args.margin)
                        and best[0].casefold() in allow)
        evidence_row = by_face[best[2]] if accepted else None
        records.append({
            "face_id": row["face_id"], "image_id": row["item_id"], "path": row["meta"].get("path", ""),
            "box_xywh": row["box"], "face_px": row["face_px"], "crop": row["crop"],
            "proposed_person": best[0] if accepted else "",
            "similarity": round(best[1], 4) if best else None, "margin": round(margin, 4) if margin is not None else None,
            "evidence": {"seed_face": best[2], "md_url": evidence_row["meta"].get("md_url", ""),
                         "inventory": evidence_row["meta"].get("inventory", "")} if accepted else None,
            "relation": "depicted", "status": "proposed" if accepted else ("withheld" if best and best[1] >= threshold else "unmatched"),
            "backend": args.backend, "threshold": threshold, "created_at": vk.utc_now(),
        })
    vk.atomic_write(args.work_root, jsonl_path, lambda h: [h.write(json.dumps(r, ensure_ascii=False) + "\n") for r in records])
    fields = ["image_id", "face_id", "proposed_person", "similarity", "margin", "status", "box_xywh", "crop", "evidence_md_url", "path"]

    def write_csv(handle):
        writer = csv.DictWriter(handle, fieldnames=fields, quoting=csv.QUOTE_ALL, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            writer.writerow({**r, "box_xywh": " ".join(map(str, r["box_xywh"])),
                             "evidence_md_url": (r["evidence"] or {}).get("md_url", "")})

    vk.atomic_write(args.work_root, csv_path, write_csv)
    named = sum(1 for r in records if r["status"] == "proposed")
    print(json.dumps({"faces": len(records), "named": named, "seed_faces": len(seed_ix), "allowlisted": len(allow),
                      "threshold": threshold, "csv": str(csv_path)}))
    return 0


def cmd_cluster(args) -> int:
    """Mutual-kNN graph: link two faces when each is in the other's top-k and
    similarity >= --link. Connected components with >= --min-size faces.
    Chunked, so memory stays bounded for tens of thousands of faces."""
    connection = connect(args.work_root)
    rows, matrix = load_faces(connection, args.backend, args.min_face_px)
    if len(rows) < 2:
        raise vk.VisualKnnError("not enough faces")
    k = min(args.k, len(rows) - 1)
    neighbours: list[set[int]] = []
    for start in range(0, len(rows), 1024):
        sims = matrix[start:start + 1024] @ matrix.T
        for offset, line in enumerate(sims):
            line[start + offset] = -2.0
            top = np.argpartition(-line, k)[:k]
            neighbours.append({int(j) for j in top if line[j] >= args.link})
    parent = list(range(len(rows)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a, near in enumerate(neighbours):
        for b in near:
            if a in neighbours[b] and rows[a]["item_id"] != rows[b]["item_id"]:
                parent[find(a)] = find(b)
    groups = defaultdict(list)
    for i in range(len(rows)):
        groups[find(i)].append(i)
    allow = load_allowlist(args.allowlist)
    seed_set = set(seeds(rows, args.min_face_px))
    clusters = sorted((g for g in groups.values() if len(g) >= args.min_size), key=lambda g: -len(g))
    run = args.run or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = args.work_root / f"face-clusters-{run}.csv"
    if path.exists():
        raise vk.VisualKnnError(f"refusing to overwrite {path}")
    out = []
    for number, members in enumerate(clusters, 1):
        names = [rows[i]["meta"]["depicted"][0] for i in members if i in seed_set]
        names = [n for n in names if n.casefold() in allow]
        hint = max(set(names), key=names.count) if names else ""
        for i in members:
            r = rows[i]
            out.append({"cluster": f"c{number:05d}", "size": len(members), "name_hint": hint,
                        "name_hint_votes": names.count(hint) if hint else 0, "face_id": r["face_id"],
                        "side": r["side"], "image_id": r["item_id"], "face_px": r["face_px"],
                        "inventory": r["meta"].get("inventory", ""), "crop": r["crop"]})

    def write_csv(handle):
        writer = csv.DictWriter(handle, fieldnames=list(out[0].keys()) if out else ["cluster"], quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(out)

    vk.atomic_write(args.work_root, path, write_csv)
    print(json.dumps({"faces": len(rows), "clusters": len(clusters), "clustered_faces": len(out), "csv": str(path)}))
    return 0


# ---------------------------------------------------------------------- cli

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--backend", choices=["sface", "arcface"], default="sface")
    parser.add_argument("--min-face-px", type=int, default=40, help="smaller faces are indexed but not matched")
    commands = parser.add_subparsers(dest="command", required=True)

    def detect_options(p):
        p.add_argument("--models", default=str(Path(__file__).with_name("models")), help="dir with YuNet/SFace .onnx")
        p.add_argument("--arcface-pack", default="buffalo_l", choices=["buffalo_l", "buffalo_s"])
        p.add_argument("--max-side", type=int, default=1600)
        p.add_argument("--det-score", type=float, default=0.75)
        p.add_argument("--limit", type=int, default=0)

    p = commands.add_parser("index-reference")
    p.add_argument("--capture", type=Path, required=True)
    p.add_argument("--expected-objects", type=int, default=11170)
    p.add_argument("--depicted-events", default=DEPICTED_DEFAULT, help="regex on event_type_name")
    detect_options(p)
    p.set_defaults(func=cmd_index_reference)

    p = commands.add_parser("index-new")
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--id-column", default="image_id")
    p.add_argument("--path-column", default="path")
    detect_options(p)
    p.set_defaults(func=cmd_index_new)

    p = commands.add_parser("evaluate")
    p.add_argument("--margin", type=float, default=0.05)
    p.set_defaults(func=cmd_evaluate)

    p = commands.add_parser("propose")
    p.add_argument("--allowlist", type=Path, help="CSV with column 'name': people that may be named")
    p.add_argument("--threshold", type=float)
    p.add_argument("--margin", type=float, default=0.05)
    p.add_argument("--run", default="")
    p.set_defaults(func=cmd_propose)

    p = commands.add_parser("cluster")
    p.add_argument("--allowlist", type=Path)
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--link", type=float, default=0.45)
    p.add_argument("--min-size", type=int, default=3)
    p.add_argument("--run", default="")
    p.set_defaults(func=cmd_cluster)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except vk.VisualKnnError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
