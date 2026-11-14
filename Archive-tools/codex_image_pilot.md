# Ten-image Codex pilot

`codex_image_pilot.py` runs a bounded sequential batch using the existing
ChatGPT-authenticated Codex CLI. It reads existing research previews only and
never applies catalogue metadata. The fixed work area is
`/mnt/volume/iss-archive-work/visual-similarity`; files use `openai-batch10-`.

The owner confirmed Berlin and Kurt Schwarz for all ten selected photographs.
The plan records ten distinct candidates: two earlier pilot controls and eight
varied scenes. This is purposeful sampling, not a representative evaluation.
Previous model responses and neighbour dates are not supplied.

Each image starts a fresh ephemeral `gpt-6-astra` session with low reasoning.
Compact archive instructions replace coding instructions, automatic skills
instructions are excluded, and project-document loading and shell tools are
disabled. Web search is disabled. The exact prompt, schema, instructions, image
hashes, commands, raw events and results are retained locally. Results containing
unexpected tool activity, invalid schema or missing completion/usage fail checks.

Run with the installed Python that provides `jsonschema`:

```sh
python3 -B /home/vladimir/archive/Archive-tools/codex_image_pilot.py check
python3 -B /home/vladimir/archive/Archive-tools/codex_image_pilot.py start
python3 -B /home/vladimir/archive/Archive-tools/codex_image_pilot.py status
```

`start` detaches the worker and writes `openai-batch10-launch.json`; that receipt
means launched, not completed. `progress.json` records the PID, current image,
success/failure counts and cumulative reported usage. `completion.json` is written
on normal completion or a handled failure/interruption. After abrupt process or
machine termination, a stale `running` state is possible; verify the recorded PID
and command before reporting activity or sending a signal.

After every image, `NN-receipt.json` contains validation, usage and its proposal;
`harvest.json` aggregates those receipts and `summary.csv` provides a readable
title/date/place and token overview. The raw result and JSONL events are also
retained. Missing usage remains unknown, not zero. Reasoning tokens are already
included in output tokens; total = input + output. These are full Codex-call
tokens, not image-only usage or an API bill.

No automatic retries or resumes: one attempt per image, 240-second timeout,
stop after two consecutive failures. A second launch is rejected to avoid
duplicate charges. To stop, verify the worker PID against its command, then send
SIGTERM to that worker; it terminates its active child and saves an interruption
receipt. Failed attempts are retained for inspection; any new batch requires
explicit preparation instead of deleting or overwriting the existing run.

Tests: `python3 -B -m unittest test_codex_image_pilot.py` from `Archive-tools`.
They cover token arithmetic, retaining usage on failure, rejecting unexpected
tool activity/missing usage, and fresh low-reasoning command construction.

## Remaining 101 candidates

The user authorized the same clues and procedure for the rest of the 111-image
set. The `openai-remaining101` plan excludes the ten successful batch10 IDs and
checks exact 111-candidate coverage with no overlap. Its prompt, schema and
instructions are byte-identical to batch10. Existing batch10 files are preserved.

```sh
python3 -B /home/vladimir/archive/Archive-tools/codex_image_pilot.py check --batch openai-remaining101
python3 -B /home/vladimir/archive/Archive-tools/codex_image_pilot.py start --batch openai-remaining101
python3 -B /home/vladimir/archive/Archive-tools/codex_image_pilot.py status --batch openai-remaining101
```

The same progress, completion, result and receipt suffixes use the prefix
`openai-remaining101-`. After each attempt, `openai-set111-harvest.json` and
`openai-set111-summary.csv` combine the original ten receipts with newly finished
attempts. Batch and candidate IDs distinguish rows. A combined harvest is partial
until all remaining attempts finish; schema-valid success is not factual review.
The same two-consecutive-failure stop and no-retry policy applies.
