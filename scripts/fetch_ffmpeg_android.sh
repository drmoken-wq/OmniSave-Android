#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="app/src/main/jniLibs/arm64-v8a"
mkdir -p "$OUT_DIR"

python3 - "$OUT_DIR" <<'PYEOF'
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

out_dir = Path(sys.argv[1])
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def http_get_bytes(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_download(url: str, dest: Path, timeout: int = 120) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            with open(dest, "wb") as fp:
                shutil.copyfileobj(resp, fp, length=131072)
        return dest.exists() and dest.stat().st_size > 100_000
    except Exception as exc:
        print(f"  Download failed ({url}): {exc}", flush=True)
        return False


def is_arm64_elf(path: Path, min_size: int = 100_000) -> bool:
    try:
        if not path.exists() or path.stat().st_size < min_size:
            return False
        with open(path, "rb") as fp:
            hdr = fp.read(20)
        if len(hdr) < 20 or hdr[:4] != b"\x7fELF":
            return False
        return struct.unpack("<H", hdr[18:20])[0] == 183  # EM_AARCH64
    except Exception:
        return False


def check_android_bionic_elf(path: Path) -> bool:
    """Confirm ELF is ARM64 and does NOT require desktop Linux /lib/ld-linux-aarch64.so.1."""
    if not is_arm64_elf(path, min_size=50_000):
        return False
    try:
        p = subprocess.run(["readelf", "-l", str(path)], capture_output=True, text=True, timeout=10)
        txt = p.stdout or ""
        if "ld-linux" in txt:
            print(f"  Rejected {path.name}: requires desktop Linux ld-linux interpreter!", flush=True)
            return False
        if "/system/bin/linker64" in txt:
            print(f"  Verified Android Bionic ELF (/system/bin/linker64): {path.name}", flush=True)
            return True
        # Statically linked ARM64 ELF without ld-linux
        return True
    except Exception:
        return True


def install_from_aar(aar_path: Path, target_dir: Path, tmp_dir: Path) -> bool:
    if not zipfile.is_zipfile(aar_path):
        print("  Downloaded file is not a valid AAR/ZIP.", flush=True)
        return False

    extracted_so = []
    with zipfile.ZipFile(aar_path, "r") as zf:
        arm64_entries = [n for n in zf.namelist() if n.startswith("jni/arm64-v8a/") and n.endswith(".so")]
        print(f"  AAR arm64-v8a entries ({len(arm64_entries)}): {arm64_entries}", flush=True)
        if not arm64_entries:
            return False

        stage = tmp_dir / "aar_stage"
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)

        for entry in arm64_entries:
            fname = Path(entry).name
            dest_f = stage / fname
            with zf.open(entry) as sf, open(dest_f, "wb") as df:
                shutil.copyfileobj(sf, df)
            extracted_so.append(dest_f)

        # Case 1: libffmpeg.zip.so is a nested zip archive (youtubedl-android format)
        zip_so = stage / "libffmpeg.zip.so"
        if zip_so.exists() and zipfile.is_zipfile(zip_so):
            print("  Inspecting nested libffmpeg.zip.so...", flush=True)
            unzip_dir = tmp_dir / "nested_ff"
            unzip_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_so, "r") as nzf:
                nzf.extractall(unzip_dir)
                print(f"  Nested zip members: {nzf.namelist()[:30]}", flush=True)

            # Extract ffmpeg & ffprobe binaries if not already top-level .so files
            for bname, so_target in (("ffmpeg", "libffmpeg.so"), ("ffprobe", "libffprobe.so")):
                if not (stage / so_target).exists() or not is_arm64_elf(stage / so_target, 50_000):
                    for cand in unzip_dir.rglob(bname):
                        if cand.is_file() and is_arm64_elf(cand, 50_000):
                            shutil.copy2(cand, stage / so_target)
                            break

            # Copy any unversioned lib*.so shared libs from nested zip straight into stage
            for so_cand in unzip_dir.rglob("lib*.so"):
                if so_cand.is_file() and not so_cand.is_symlink() and is_arm64_elf(so_cand, 4096):
                    dst = stage / so_cand.name
                    if not dst.exists():
                        shutil.copy2(so_cand, dst)

        ff_elf = stage / "libffmpeg.so"
        if not check_android_bionic_elf(ff_elf):
            print("  AAR did not yield a valid Android Bionic libffmpeg.so!", flush=True)
            return False

        # Clean target_dir and copy all staged .so files (including libffmpeg.zip.so if present for any versioned libs)
        for old_f in list(target_dir.glob("*.so")):
            old_f.unlink()
        for f in sorted(stage.glob("*.so")):
            dst = target_dir / f.name
            shutil.copy2(f, dst)
            os.chmod(dst, 0o755)
            print(f"  Installed {dst.name} ({dst.stat().st_size / 1048576:.2f} MB)", flush=True)

    return (target_dir / "libffmpeg.so").exists()


def try_maven_ffmpeg(base_url: str, label: str, tmp_dir: Path) -> bool:
    meta_url = f"{base_url.rstrip('/')}/maven-metadata.xml"
    print(f"Checking {label}: {meta_url}", flush=True)
    try:
        raw_xml = http_get_bytes(meta_url, timeout=25)
        root = ET.fromstring(raw_xml)
        ver = (root.findtext("./versioning/release") or root.findtext("./versioning/latest") or "").strip()
        versions = [v.text.strip() for v in root.findall("./versioning/versions/version") if v.text and v.text.strip()]
        cand_vers = []
        if ver:
            cand_vers.append(ver)
        for v in reversed(versions):
            if v not in cand_vers and "alpha" not in v.lower() and "beta" not in v.lower():
                cand_vers.append(v)
        for v in cand_vers[:3]:
            aar_url = f"{base_url.rstrip('/')}/{v}/ffmpeg-{v}.aar"
            print(f"  Downloading {label} v{v}: {aar_url}", flush=True)
            aar_p = tmp_dir / f"ffmpeg_{v}.aar"
            if http_download(aar_url, aar_p):
                if install_from_aar(aar_p, out_dir, tmp_dir):
                    return True
    except Exception as exc:
        print(f"  {label} failed: {exc}", flush=True)
    return False


with tempfile.TemporaryDirectory() as td:
    tmp_p = Path(td)
    ok = False

    # 1. Maven Central: junkfood02/youtubedl-android:ffmpeg (Seal / YTDLnis Android NDK Bionic FFmpeg!)
    if not ok:
        ok = try_maven_ffmpeg(
            "https://repo1.maven.org/maven2/io/github/junkfood02/youtubedl-android/ffmpeg",
            "Maven Central (junkfood02/youtubedl-android)",
            tmp_p,
        )

    # 2. JitPack: yausername/youtubedl-android:ffmpeg (Android NDK Bionic FFmpeg!)
    if not ok:
        ok = try_maven_ffmpeg(
            "https://jitpack.io/com/github/yausername/youtubedl-android/ffmpeg",
            "JitPack (yausername/youtubedl-android)",
            tmp_p,
        )

    if not ok:
        print("ERROR: Could not download genuine Android NDK Bionic FFmpeg AAR!", file=sys.stderr)
        sys.exit(1)

print("Successfully bundled genuine Android NDK Bionic FFmpeg!", flush=True)
PYEOF

echo "=== Bundled jniLibs/arm64-v8a ==="
ls -lh "$OUT_DIR"
file "$OUT_DIR/libffmpeg.so"
readelf -l "$OUT_DIR/libffmpeg.so" | grep -i interpreter || true
readelf -d "$OUT_DIR/libffmpeg.so" | grep NEEDED || true

if [ -f "scripts/build_qjs_android.sh" ]; then
    bash scripts/build_qjs_android.sh
fi
