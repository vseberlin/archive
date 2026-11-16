#!/bin/bash

TRANSCRIPTS="${ISS_TRANSCRIPTS_DIR:-$HOME/archive-local/transcripts}"
WHISPER_HOME="${WHISPER_CPP_HOME:-$HOME/.local/opt/whisper.cpp}"

INPUT="$TRANSCRIPTS/input"
OUTPUT="$TRANSCRIPTS/output"
MODEL="$WHISPER_HOME/models/ggml-base.bin"

mkdir -p "$OUTPUT"

for file in "$INPUT"/*.mp3; do
    [ -e "$file" ] || continue

    base="$(basename "$file" .mp3)"

    echo ""
    echo "=== Processing: $base ==="

    "$WHISPER_HOME/build/bin/whisper-cli" \
        -m "$MODEL" \
        -t 4 \
        -l de \
        -f "$file" \
        -otxt \
        -ovtt \
        -of "$OUTPUT/$base"

done
