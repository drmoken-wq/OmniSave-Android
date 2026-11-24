#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="app/src/main/jniLibs/arm64-v8a"
mkdir -p "$OUT_DIR"

echo "=== Building Android ARM64 Bionic QuickJS-NG (libqjs.so) ==="

# 1. Locate Android NDK on GitHub ubuntu-latest runner
NDK_DIR="${ANDROID_NDK_LATEST_HOME:-${ANDROID_NDK_HOME:-${ANDROID_NDK_ROOT:-}}}"
if [ -z "$NDK_DIR" ] && [ -n "${ANDROID_HOME:-}" ] && [ -d "$ANDROID_HOME/ndk" ]; then
    NDK_DIR="$(ls -d "$ANDROID_HOME/ndk/"* 2>/dev/null | sort -V | tail -n 1 || true)"
fi
if [ -z "$NDK_DIR" ] && [ -d "/usr/local/lib/android/sdk/ndk" ]; then
    NDK_DIR="$(ls -d /usr/local/lib/android/sdk/ndk/* 2>/dev/null | sort -V | tail -n 1 || true)"
fi
if [ -z "$NDK_DIR" ] || [ ! -d "$NDK_DIR" ]; then
    echo "ERROR: Android NDK not found on runner! Cannot build Android QuickJS." >&2
    exit 1
fi

TOOLCHAIN_CMAKE="$NDK_DIR/build/cmake/android.toolchain.cmake"
TOOLCHAIN_BIN="$NDK_DIR/toolchains/llvm/prebuilt/linux-x86_64/bin"
ANDROID_CLANG=""
for api in 24 28 29 30 31; do
    if [ -x "$TOOLCHAIN_BIN/aarch64-linux-android${api}-clang" ]; then
        ANDROID_CLANG="$TOOLCHAIN_BIN/aarch64-linux-android${api}-clang"
        break
    fi
done

if [ ! -f "$TOOLCHAIN_CMAKE" ] || [ -z "$ANDROID_CLANG" ]; then
    echo "ERROR: Android NDK CMake toolchain or aarch64 clang missing in $NDK_DIR" >&2
    exit 1
fi
echo "NDK: $NDK_DIR"
echo "Compiler: $ANDROID_CLANG"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# 2. Download quickjs-ng/quickjs (latest release tarball via API, fallback to master/main)
TAR_PATH="$TMP_DIR/qjsng.tar.gz"
python3 - "$TAR_PATH" <<'PYDL'
import json
import os
import shutil
import sys
import urllib.request

dest = sys.argv[1]
gh_token = os.environ.get("GITHUB_TOKEN") or ""
UA = "OmniSave-CI/1.0"

urls = []
try:
    hdrs = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if gh_token:
        hdrs["Authorization"] = f"Bearer {gh_token}"
    req = urllib.request.Request("https://api.github.com/repos/quickjs-ng/quickjs/releases/latest", headers=hdrs)
    with urllib.request.urlopen(req, timeout=20) as r:
        rel = json.loads(r.read().decode("utf-8", errors="ignore"))
        tb = rel.get("tarball_url")
        if tb:
            urls.append(tb)
except Exception as e:
    print(f"Release API note: {e}", flush=True)

urls.extend([
    "https://github.com/quickjs-ng/quickjs/archive/refs/heads/master.tar.gz",
    "https://github.com/quickjs-ng/quickjs/archive/refs/heads/main.tar.gz",
])

for u in urls:
    try:
        print(f"Downloading quickjs-ng from: {u}", flush=True)
        req = urllib.request.Request(u, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=45) as r, open(dest, "wb") as fp:
            shutil.copyfileobj(r, fp)
        if os.path.getsize(dest) > 50000:
            sys.exit(0)
    except Exception as e:
        print(f"  Skipped {u}: {e}", flush=True)

print("ERROR: Failed to download quickjs-ng source!", file=sys.stderr)
sys.exit(1)
PYDL

tar -xzf "$TAR_PATH" -C "$TMP_DIR"
SRC_DIR="$(find "$TMP_DIR" -maxdepth 1 -mindepth 1 -type d | head -n 1)"
echo "Source dir: $SRC_DIR"
QJS_VER="$(tr -d '\r\n ' < "$SRC_DIR/VERSION" 2>/dev/null || echo '0.9.0')"
echo "QuickJS-NG version: $QJS_VER"

# 3. Build host qjsc (for generating repl.c / standalone.c bytecode)
echo "Building host qjsc..."
cmake -S "$SRC_DIR" -B "$TMP_DIR/build-host" \
    -DCMAKE_BUILD_TYPE=Release \
    -DQJS_BUILD_EXAMPLES=OFF \
    -DBUILD_SHARED_LIBS=OFF
cmake --build "$TMP_DIR/build-host" --target qjsc -j"$(nproc)"

HOST_QJSC="$(find "$TMP_DIR/build-host" -maxdepth 3 -type f -name qjsc -executable | head -n 1 || true)"
if [ -z "$HOST_QJSC" ]; then
    echo "ERROR: host qjsc binary not produced!" >&2
    exit 1
fi
echo "Host qjsc: $HOST_QJSC"

# 4. Cross-compile libqjs.a (and qjs_exe if CMake supports it) using Official Android NDK CMake Toolchain!
echo "Cross-compiling QuickJS-NG static library for Android arm64-v8a..."
cmake -S "$SRC_DIR" -B "$TMP_DIR/build-android" \
    -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN_CMAKE" \
    -DANDROID_ABI=arm64-v8a \
    -DANDROID_PLATFORM=android-24 \
    -DCMAKE_BUILD_TYPE=Release \
    -DQJS_BUILD_EXAMPLES=OFF \
    -DBUILD_SHARED_LIBS=OFF \
    -DQJSC_EXECUTABLE="$HOST_QJSC"

cmake --build "$TMP_DIR/build-android" --target qjs -j"$(nproc)"

# Try official qjs_exe target first
if cmake --build "$TMP_DIR/build-android" --target qjs_exe -j"$(nproc)" 2>/dev/null; then
    BUILT_EXE="$(find "$TMP_DIR/build-android" -maxdepth 3 -type f -name qjs -executable | head -n 1 || true)"
    if [ -n "$BUILT_EXE" ] && [ -s "$BUILT_EXE" ]; then
        cp "$BUILT_EXE" "$OUT_DIR/libqjs.so"
        chmod 755 "$OUT_DIR/libqjs.so"
        echo "Built libqjs.so directly via CMake qjs_exe target!"
    fi
fi

# Fallback link: link qjs.c + host-generated repl.c against the officially NDK-compiled libqjs.a!
if [ ! -s "$OUT_DIR/libqjs.so" ]; then
    echo "Linking qjs executable against Android NDK libqjs.a..."
    LIBQJS_A="$(find "$TMP_DIR/build-android" -maxdepth 3 -type f -name 'libqjs.a' | head -n 1 || true)"
    if [ -z "$LIBQJS_A" ]; then
        echo "ERROR: Android libqjs.a not found in build-android!" >&2
        find "$TMP_DIR/build-android" -maxdepth 3
        exit 1
    fi

    GEN_DIR="$TMP_DIR/gen"
    mkdir -p "$GEN_DIR"
    GEN_SRCS=()

    if [ -f "$SRC_DIR/repl.js" ]; then
        "$HOST_QJSC" -s -c -o "$GEN_DIR/repl.c" -m "$SRC_DIR/repl.js"
        GEN_SRCS+=("$GEN_DIR/repl.c")
    fi
    if [ -f "$SRC_DIR/standalone.js" ]; then
        "$HOST_QJSC" -s -c -o "$GEN_DIR/standalone.c" -m "$SRC_DIR/standalone.js"
        GEN_SRCS+=("$GEN_DIR/standalone.c")
    fi

    "$ANDROID_CLANG" -O2 -std=gnu11 -fPIE -pie \
        -D_GNU_SOURCE \
        -DCONFIG_VERSION="\"$QJS_VER\"" \
        -I"$SRC_DIR" -I"$TMP_DIR/build-android" -I"$GEN_DIR" \
        "$SRC_DIR/qjs.c" "${GEN_SRCS[@]}" "$LIBQJS_A" \
        -lm -ldl -o "$OUT_DIR/libqjs.so"
    chmod 755 "$OUT_DIR/libqjs.so"
fi

# 5. Strict Verification — NEVER silently ship an APK without working ARM64 Bionic QuickJS!
if [ ! -s "$OUT_DIR/libqjs.so" ]; then
    echo "ERROR: libqjs.so was not created!" >&2
    exit 1
fi

echo "=== Verifying Android ARM64 Bionic QuickJS (libqjs.so) ==="
ls -lh "$OUT_DIR/libqjs.so"
file "$OUT_DIR/libqjs.so"
INTERP_LINE="$(readelf -l "$OUT_DIR/libqjs.so" | grep -i interpreter || true)"
echo "$INTERP_LINE"
readelf -d "$OUT_DIR/libqjs.so" | grep NEEDED || true

if echo "$INTERP_LINE" | grep -qi "ld-linux"; then
    echo "ERROR: libqjs.so linked against desktop ld-linux instead of Android Bionic!" >&2
    rm -f "$OUT_DIR/libqjs.so"
    exit 1
fi
if ! file "$OUT_DIR/libqjs.so" | grep -qiE "aarch64|arm aarch64"; then
    echo "ERROR: libqjs.so is NOT ARM64!" >&2
    rm -f "$OUT_DIR/libqjs.so"
    exit 1
fi

echo "Verified Android Bionic ARM64 QuickJS-NG (libqjs.so) successfully!"
