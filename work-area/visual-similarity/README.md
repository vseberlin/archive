# visual-similarity — kNN metadata proposals

Bucket: `SUPPORT_INDEX` (rebuildable, never archival authority).
Proposed work path: `/mnt/volume/iss-archive-work/visual-similarity`.

Embeds the display preview of every record in the completed institution
museum-digital capture (`iss-md-export-discovery-v1`, 11,170 objects), embeds a
list of new images, and proposes `object_type`, `tags`, `places`,
`collections`, `date` (and optionally `people`) from the most similar
catalogued records. Every proposal carries its supporting MD object IDs.

Proposals are review evidence. Nothing here writes to a unit, CATALOG.csv,
museum-digital or WordPress. Adoption of a value is a human decision recorded
in the unit's own workflow.

## Approval needed (AGENTS.md gates)

- new work directory `iss-archive-work/visual-similarity/`
- new dependencies: `open_clip_torch`, `torch` (CPU wheel)
- new rebuildable SQLite `index.sqlite` (embedding cache only)

## Install (CPU only)

```sh
python3 -m venv ~/venvs/visual-similarity
~/venvs/visual-similarity/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
~/venvs/visual-similarity/bin/pip install open_clip_torch pillow numpy
```

First run downloads the model weights (ViT-B-32, ~350 MB) from Hugging Face.

## Run

```sh
PY=~/venvs/visual-similarity/bin/python
W=/mnt/volume/iss-archive-work/visual-similarity
CAP=/mnt/volume/iss-archive-work/tfa/work/reconciliation/museum-digital/runs/20260814T201913Z-tfa-institution-29-clean-local-only-recheck

# 1. reference (resumable; re-run after interruption; skips unchanged URLs)
$PY visual_knn.py --work-root $W index-reference --capture $CAP

# 2. validate before trusting anything
$PY visual_knn.py --work-root $W evaluate --sample 400

# 3. new images: CSV with columns image_id,path (names configurable)
$PY visual_knn.py --work-root $W index-new --manifest new-images.csv
$PY visual_knn.py --work-root $W propose
```

`propose` writes `proposals-<run>.jsonl` (full neighbour evidence) and
`proposals-<run>.csv` (one row per proposed value) and refuses to overwrite an
existing run.

## Reading the evaluation

`evaluate` runs leave-one-out twice:

- `all_neighbours`: other photos of the same series may be neighbours. Upper
  bound; realistic for new material from a Konvolut that is already catalogued.
- `series_hidden`: neighbours with the same inventory prefix and a number within
  `--series-window` (default 20) are hidden. Realistic for material from an
  unknown context.

Per field it reports precision overall and per `confidence_key`
(high/medium/low), plus date MAE and the top-1 similarity quartiles. Use these
to set `--min-similarity`, `--min-support` and which fields to propose at all.
Drop any field whose `series_hidden` precision is not useful.

## Voting

Neighbour weight is `exp((sim - top_sim) / temperature)`. A value is proposed
when its weighted support ≥ `--min-support` and at least two neighbours carry
it. `confidence_key`: high = support ≥ 0.75 and ≥ 3 voters; medium = ≥ 0.5 and
≥ 2. Date: weighted median of neighbour mid-years, reported as the weighted
inter-quartile year range. A top-1 similarity ≥ `--duplicate-similarity`
(0.95) is reported as `possible_md_duplicate`: the photo may already be on
museum-digital.

`people` is off by default: a person in similar photos is weak evidence that
the same person is in this one.

## face_index.py — person proposals and same-person clusters

Face embeddings are biometric data (GDPR Art. 9). The index, crops and outputs
are internal review evidence: never publish them, never offer public face
search.

Additional approval needed: `opencv-python-headless` (sface backend) or
`insightface` + `onnxruntime` (arcface backend), and `faces.sqlite` plus
`crops/` under the same work root.

Model files (not committed; place in `--models`, default `./models`):

```sh
mkdir -p models && cd models
curl -LO https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
curl -LO https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx
```

Backends: `sface` (OpenCV YuNet + SFace, Apache-2.0, default) and `arcface`
(InsightFace `buffalo_l`/`buffalo_s`; more accurate, but its pretrained
weights are licensed for non-commercial research only — check before use).

```sh
$PY face_index.py --work-root $W index-reference --capture $CAP
$PY face_index.py --work-root $W evaluate
$PY face_index.py --work-root $W index-new --manifest new-images.csv
$PY face_index.py --work-root $W propose --allowlist public-figures.csv --threshold 0.45
$PY face_index.py --work-root $W cluster --allowlist public-figures.csv
```

How it decides:

- **Seeds**: a reference image with exactly one face ≥ `--min-face-px` (40)
  and exactly one person linked by a *depicted* event (`--depicted-events`,
  regex on `event_type_name`; photographers/creators are excluded).
- **Propose**: each new face is matched against all seeds (best similarity per
  person). A name is proposed only if similarity ≥ threshold, the lead over the
  second person ≥ `--margin`, and the person is on `--allowlist` (CSV column
  `name`). A confident match to someone not on the allowlist is reported as
  `withheld`, without a name.
- **Cluster**: mutual-kNN graph (top `--k`, similarity ≥ `--link`), connected
  components ≥ `--min-size`. Faces from the same image are never linked. A
  cluster gets a `name_hint` only from allowlisted seeds inside it.
- **Evaluate**: leave-one-image-out over seeds of people with ≥ 2 seeds; prints
  precision/coverage per threshold. Pick the threshold from this sweep on your
  own material, not from defaults.

Allowlist: public figures and Personen der Zeitgeschichte only. Everyone else
stays unnamed in clusters until a reviewer decides.

## Hardware

ViT-B-32 on an old 4-core CPU: roughly 3–10 images/s for embedding, so the
11,170-record reference is an overnight job, run once. kNN itself is a
11,170 × 512 matrix product, effectively instant. `--model pixel` is a
dependency-free smoke-test backend, useless for real proposals.

## vision_pilot.py — structured descriptions via `claude -p` (Pro subscription)

Pilot for the Claude vision route without an API key: each image is downscaled
to `--max-side` (1568) under the work root, one headless Claude Code call reads
it (Read tool only, empty working directory) and returns JSON validated against
the schema in the script: visible text, German description, tags, place
(candidate id or abstain, with quoted evidence), date range with evidence.

Calls draw from the subscription usage limits. The run appends to
`vision-<name>.jsonl`, skips finished images, and exits with code 3 on a usage
limit; restart it later. Images leave the workstation: keep rights- or
GDPR-sensitive units out of the manifest.

```sh
claude login   # once, with the Pro account
$PY vision_pilot.py describe --work-root $W --manifest pilot-400.csv --name eval400 \
    --vocabulary tfa-schlagworte.txt
$PY vision_pilot.py score --results $W/vision-eval400.jsonl --truth pilot-400-truth.csv
```

Manifest: `image_id,path` plus optional context `inventory,unit,konvolut,place_candidates`
(`id|label; id|label`). For evaluation, never put the answers being scored into
the context. Truth CSV: `image_id,tags(;-separated),place_id,year`. Compare
`score` output against the same matched baselines as the kNN evaluation.
