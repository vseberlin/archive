#!/bin/bash

TRANSCRIPTS="${ISS_TRANSCRIPTS_DIR:-$HOME/archive-local/transcripts}"
WHISPER_HOME="${WHISPER_CPP_HOME:-$HOME/.local/opt/whisper.cpp}"

URLS="$TRANSCRIPTS/videos.txt"
INPUT="$TRANSCRIPTS/input"
OUTPUT="$TRANSCRIPTS/output"
LOGS="$TRANSCRIPTS/logs"
DONE="$TRANSCRIPTS/done.txt"
MODEL="$WHISPER_HOME/models/ggml-base.bin"

mkdir -p "$INPUT" "$OUTPUT" "$LOGS"
touch "$DONE"

cd "$WHISPER_HOME" || exit 1

while IFS= read -r url; do
    [ -z "$url" ] && continue
    case "$url" in \#*) continue ;; esac
    grep -Fxq "$url" "$DONE" && continue

    echo ""
    echo "======================================="
    echo "URL: $url"
    echo "======================================="

    video_id="$(yt-dlp --print id "$url" | tail -n 1)"
    title="$(yt-dlp --print title "$url" | tail -n 1)"

    safe_title="$(echo "$title" \
        | tr '/' '-' \
        | tr ':' '-' \
        | tr -d '"' \
        | tr -d "'" \
        | sed 's/[[:space:]]\+/ /g' \
        | sed 's/^ *//;s/ *$//')"

    base="${safe_title} [${video_id}]"
    audio="$INPUT/${base}.mp3"
    out="$OUTPUT/${base}"

    if [ ! -f "$audio" ]; then
        yt-dlp \
            --extract-audio \
            --audio-format mp3 \
            --output "$INPUT/${base}.%(ext)s" \
            "$url"
    fi

    "$WHISPER_HOME/build/bin/whisper-cli" \
        -m "$MODEL" \
        -t 4 \
        -l de \
        -f "$audio" \
        -otxt \
        -ovtt \
        -of "$out"

    echo "$url" >> "$DONE"
    echo "Done: $base"

done < "$URLS"
