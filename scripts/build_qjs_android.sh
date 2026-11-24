#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="app/src/main/jniLibs/arm64-v8a"
mkdir -p "$OUT_DIR"

echo "=== Building Android ARM64 Bionic QuickJS (libqjs.so) for YouTube HD JS Solver ==="

# 1. Locate Android NDK preinstalled on GitHub ubuntu-latest runner
NDK_DIR="${ANDROID_NDK_LATEST_HOME:-${ANDROID_NDK_HOME:-${ANDROID_NDK_ROOT:-}}}"
if [ -z "$NDK_DIR" ] && [ -n "${ANDROID_HOME:-}" ] && [ -d "$ANDROID_HOME/ndk" ]; then
    NDK_DIR="$(ls -d "$ANDROID_HOME/ndk/"* 2>/dev/null | sort -V | tail -n 1 || true)"
fi
if [ -z "$NDK_DIR" ] && [ -d "/usr/local/lib/android/sdk/ndk" ]; then
    NDK_DIR="$(ls -d /usr/local/lib/android/sdk/ndk/* 2>/dev/null | sort -V | tail -n 1 || true)"
fi
if [ -z "$NDK_DIR" ] || [ ! -d "$NDK_DIR" ]; then
    echo "WARNING: Android NDK not found; skipping QuickJS build."
    exit 0
fi

TOOLCHAIN="$NDK_DIR/toolchains/llvm/prebuilt/linux-x86_64/bin"
ANDROID_CLANG=""
for api in 28 29 30 31 24; do
    if [ -x "$TOOLCHAIN/aarch64-linux-android${api}-clang" ]; then
        ANDROID_CLANG="$TOOLCHAIN/aarch64-linux-android${api}-clang"
        break
    fi
done
if [ -z "$ANDROID_CLANG" ]; then
    echo "WARNING: No aarch64-linux-android*-clang found in $TOOLCHAIN; skipping QuickJS."
    exit 0
fi
echo "Using Android NDK compiler: $ANDROID_CLANG"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# Strategy A: Compile quickjs-ng/quickjs (modern, officially supported by yt-dlp!)
build_quickjs_ng() {
    echo "Downloading quickjs-ng/quickjs source..."
    curl -fsSL --retry 3 --connect-timeout 25 -o "$TMP_DIR/qjsng.tar.gz" \
        "https://github.com/quickjs-ng/quickjs/archive/refs/heads/master.tar.gz"
    tar -xzf "$TMP_DIR/qjsng.tar.gz" -C "$TMP_DIR"
    SRC_DIR="$(find "$TMP_DIR" -maxdepth 1 -type d -name 'quickjs-*' | head -n 1)"
    [ -n "$SRC_DIR" ] && [ -f "$SRC_DIR/CMakeLists.txt" ]

    echo "Running host CMake build to generate repl.c / standalone.c & compile_commands.json..."
    cmake -S "$SRC_DIR" -B "$TMP_DIR/build-host" \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
        -DQJS_BUILD_EXAMPLES=OFF \
        -DBUILD_SHARED_LIBS=OFF
    cmake --build "$TMP_DIR/build-host" --target qjs -j"$(nproc)"

    python3 - "$TMP_DIR/build-host/compile_commands.json" "$ANDROID_CLANG" "$OUT_DIR/libqjs.so" <<'PYQJS'
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

cc_json = Path(sys.argv[1])
android_clang = sys.argv[2]
out_so = Path(sys.argv[3])

entries = json.loads(cc_json.read_text(encoding="utf-8"))
sources = []
seen_src = set()
includes = []
seen_inc = set()
defines = []
seen_def = set()

for ent in entries:
    src_f = Path(ent.get("file") or "")
    out_f = str(ent.get("output") or "")
    if not src_f.exists() or src_f.suffix != ".c":
        continue
    low_name = src_f.name.lower()
    low_out = out_f.lower()
    # Skip host tools, tests, and examples
    if low_name in ("qjsc.c", "run-test262.c", "unicode_gen.c") or "test" in low_out or "example" in low_out or "qjsc.dir" in low_out:
        continue
    # Only allow main() from qjs.c (prevents duplicate main linker errors!)
    txt = src_f.read_text(encoding="utf-8", errors="ignore")
    if "int main(" in txt and low_name != "qjs.c":
        continue

    if str(src_f) not in seen_src:
        seen_src.add(str(src_f))
        sources.append(str(src_f))

    cmd_str = ent.get("command") or ""
    toks = shlex.split(cmd_str) if cmd_str else (ent.get("arguments") or [])
    i = 0
    while i < len(toks):
        t = toks[i]
        if t == "-I" and i + 1 < len(toks):
            inc = toks[i + 1]
            if inc not in seen_inc:
                seen_inc.add(inc)
                includes.extend(["-I", inc])
            i += 2
            continue
        if t.startswith("-I") and len(t) > 2:
            inc = t[2:]
            if inc not in seen_inc:
                seen_inc.add(inc)
                includes.append(t)
        elif t.startswith("-isystem") and i + 1 < len(toks):
            inc = toks[i + 1]
            if inc not in seen_inc:
                seen_inc.add(inc)
                includes.extend(["-I", inc])
            i += 2
            continue
        elif t.startswith("-D") and len(t) > 2:
            if t not in seen_def:
                seen_def.add(t)
                defines.append(t)
        i += 1

print(f"Cross-compiling {len(sources)} QuickJS-NG source files for Android ARM64:", [Path(s).name for s in sources], flush=True)
assert any(Path(s).name == "qjs.c" for s in sources), "qjs.c missing from compile_commands.json"

cmd = [
    android_clang,
    "-O2",
    "-std=gnu11",
    "-fPIE",
    "-pie",
    "-Wno-everything",
] + defines + includes + sources + ["-lm", "-ldl", "-o", str(out_so)]

res = subprocess.run(cmd, capture_output=True, text=True)
if res.returncode != 0:
    print("QuickJS-NG compile stderr:\n", res.stderr, file=sys.stderr)
    sys.exit(res.returncode)
os.chmod(out_so, 0o755)
PYQJS
}

# Strategy B: Fallback to bellard/quickjs
build_bellard_qjs() {
    echo "Fallback: Downloading bellard/quickjs source..."
    curl -fsSL --retry 3 --connect-timeout 25 -o "$TMP_DIR/qjsbell.tar.gz" \
        "https://github.com/bellard/quickjs/archive/refs/heads/master.tar.gz"
    mkdir -p "$TMP_DIR/bellard"
    tar -xzf "$TMP_DIR/qjsbell.tar.gz" -C "$TMP_DIR/bellard"
    B_DIR="$(find "$TMP_DIR/bellard" -maxdepth 1 -type d -name 'quickjs-*' | head -n 1)"
    make -C "$B_DIR" qjsc repl.c -j"$(nproc)"
    VER="$(cat "$B_DIR/VERSION" | tr -d '\r\n')"
    C_SRCS=()
    for f in qjs.c repl.c quickjs.c dtoa.c libregexp.c libunicode.c cutils.c quickjs-libc.c; do
        if [ -f "$B_DIR/$f" ]; then
            C_SRCS+=("$B_DIR/$f")
        fi
    done
    "$ANDROID_CLANG" -O2 -std=gnu11 -fPIE -pie -D_GNU_SOURCE -DCONFIG_VERSION="\"$VER\"" \
        -I"$B_DIR" "${C_SRCS[@]}" -lm -ldl -o "$OUT_DIR/libqjs.so"
    chmod 755 "$OUT_DIR/libqjs.so"
}

if ! build_quickjs_ng; then
    echo "QuickJS-NG build failed; trying bellard/quickjs fallback..."
    rm -f "$OUT_DIR/libqjs.so"
    build_bellard_qjs || true
fi

if [ -s "$OUT_DIR/libqjs.so" ]; then
    echo "=== Verifying Android ARM64 QuickJS (libqjs.so) ==="
    ls -lh "$OUT_DIR/libqjs.so"
    file "$OUT_DIR/libqjs.so"
    readelf -l "$OUT_DIR/libqjs.so" | grep -i interpreter || true
    if readelf -l "$OUT_DIR/libqjs.so" | grep -qi "ld-linux"; then
        echo "ERROR: libqjs.so was linked against desktop ld-linux; removing!"
        rm -f "$OUT_DIR/libqjs.so"
    fi
else
    echo "WARNING: libqjs.so was not built; YouTube will fall back to multi-client mode."
fi
