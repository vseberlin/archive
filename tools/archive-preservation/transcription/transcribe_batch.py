from pathlib import Path
import os
import subprocess
import sys

import whisper


BASE = Path(os.environ.get("ISS_TRANSCRIPTS_DIR", str(Path.home() / "archive-local/transcripts"))).expanduser()
URLS = BASE / "videos.txt"
AUDIO = BASE / "audio"
OUT = BASE / "output"
DONE = BASE / "done" / "done.txt"

AUDIO.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)
DONE.parent.mkdir(parents=True, exist_ok=True)


def run(cmd):
    subprocess.run(cmd, check=True)


def timestamp(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02}:{m:02}:{s:06.3f}".replace(".", ",")


if not URLS.exists():
    print(f"Missing URL list: {URLS}")
    sys.exit(1)


done = set()
if DONE.exists():
    done = set(DONE.read_text(encoding="utf-8").splitlines())


print("Loading Whisper model...", flush=True)
model = whisper.load_model("small")
print("Model loaded.", flush=True)


for url in URLS.read_text(encoding="utf-8").splitlines():
    url = url.strip()

    if not url or url.startswith("#") or url in done:
        continue

    print(f"\n=== Processing {url} ===", flush=True)

    try:
        video_id = subprocess.check_output(
            ["yt-dlp", "--print", "id", url],
            text=True
        ).strip()

        audio_file = AUDIO / f"{video_id}.mp3"
        txt_file = OUT / f"{video_id}.txt"
        vtt_file = OUT / f"{video_id}.vtt"

        if not audio_file.exists():
            run([
                "yt-dlp",
                "-x",
                "--audio-format", "mp3",
                "-o", str(AUDIO / "%(id)s.%(ext)s"),
                url,
            ])

        result = model.transcribe(
            str(audio_file),
            language="de",
            fp16=False,
            verbose=False,
        )

        segments = result.get("segments", [])

        with txt_file.open("w", encoding="utf-8") as txt, vtt_file.open("w", encoding="utf-8") as vtt:
            vtt.write("WEBVTT\n\n")

            for i, segment in enumerate(segments, start=1):
                text = segment.get("text", "").strip()
                if not text:
                    continue

                txt.write(text + "\n")

                vtt.write(f"{i}\n")
                vtt.write(f"{timestamp(segment['start'])} --> {timestamp(segment['end'])}\n")
                vtt.write(text + "\n\n")

        with DONE.open("a", encoding="utf-8") as f:
            f.write(url + "\n")

        print(f"Done: {video_id}", flush=True)

    except Exception as exc:
        print(f"ERROR while processing {url}: {exc}", flush=True)
        continue