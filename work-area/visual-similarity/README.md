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

## Hardware

ViT-B-32 on an old 4-core CPU: roughly 3–10 images/s for embedding, so the
11,170-record reference is an overnight job, run once. kNN itself is a
11,170 × 512 matrix product, effectively instant. `--model pixel` is a
dependency-free smoke-test backend, useless for real proposals.
