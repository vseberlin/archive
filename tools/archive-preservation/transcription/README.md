# Retained local transcription scripts

These existing manual batch scripts moved here on 16 November 2026. No batch
was resumed. The private workspace is `/home/vladimir/archive-local/transcripts`;
it contains URL lists, audio, outputs, completion markers and logs. Machine text
remains unverified research material, not an adopted transcript or exact quotation.

| Script | Existing engine and inputs |
| --- | --- |
| `batch_from_urls.sh` | whisper.cpp `base`; URL list, `input/`, `output/`, root `done.txt` |
| `transcribe_batch.sh` | whisper.cpp `base`; existing MP3 files in `input/` |
| `transcribe_batch.py` | Python Whisper `small`; `audio/`, `output/`, `done/done.txt` |

The engines and completion files differ. Do not treat them as interchangeable or
run them together. Review existing logs and outputs before explicitly starting
another batch; relocation does not reconcile their historical model provenance.

All scripts accept `ISS_TRANSCRIPTS_DIR` as an environment override. The shell
scripts also accept `WHISPER_CPP_HOME`, defaulting to
`$HOME/.local/opt/whisper.cpp`. Their executable/model paths are independent of
the caller's working directory. `yt-dlp` remains an external shell dependency;
the Python script requires a Python Whisper environment. The workstation's default
`python3` had no `whisper` module at relocation; this is a pre-existing runtime
limitation, and no dependency was installed during cleanup.

whisper.cpp is an independent upstream checkout under `.local/opt`, with existing
models. It is separate from the website's optional container transcription
experiment. Source relocation does not install Python packages or download models.

Full original scripts, data and build are recoverable from
`/mnt/volume/sites-backup/home-cleanup-20261116/workspaces-before-relocation.tar`.
