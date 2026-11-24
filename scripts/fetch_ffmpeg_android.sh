#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="app/src/main/jniLibs/arm64-v8a"
mkdir -p "$OUT_DIR"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

echo "=== Fetching statically-linked ARM64 FFmpeg for Android ==="

# Primary source: John Van Sickle's statically linked arm64 release (includes ffmpeg + ffprobe)
if curl -fsSL --retry 3 --connect-timeout 20 -o "$TMP_DIR/ffmpeg.tar.xz"     "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz"; then
    tar -xf "$TMP_DIR/ffmpeg.tar.xz" -C "$TMP_DIR"
    FF_BIN="$(find "$TMP_DIR" -maxdepth 2 -type f -name ffmpeg | head -n 1 || true)"
    FP_BIN="$(find "$TMP_DIR" -maxdepth 2 -type f -name ffprobe | head -n 1 || true)"
    if [ -n "$FF_BIN" ] && [ -f "$FF_BIN" ]; then
        cp "$FF_BIN" "$OUT_DIR/libffmpeg.so"
        chmod 755 "$OUT_DIR/libffmpeg.so"
    fi
    if [ -n "$FP_BIN" ] && [ -f "$FP_BIN" ]; then
        cp "$FP_BIN" "$OUT_DIR/libffprobe.so"
        chmod 755 "$OUT_DIR/libffprobe.so"
    fi
fi

# Fallback mirror: eugeneware/ffmpeg-static GitHub Releases
if [ ! -s "$OUT_DIR/libffmpeg.so" ]; then
    echo "Primary mirror unavailable, trying GitHub fallback..."
    curl -fsSL --retry 3 --connect-timeout 20 -o "$OUT_DIR/libffmpeg.so"         "https://github.com/eugeneware/ffmpeg-static/releases/latest/download/ffmpeg-linux-arm64"
    chmod 755 "$OUT_DIR/libffmpeg.so"
fi

echo "=== Verifying bundled FFmpeg binary ==="
ls -lh "$OUT_DIR"
file "$OUT_DIR/libffmpeg.so"
