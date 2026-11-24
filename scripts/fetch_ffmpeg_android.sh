#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="app/src/main/jniLibs/arm64-v8a"
mkdir -p "$OUT_DIR"

python3 - "$OUT_DIR" <<'PYEOF'
import gzip
import json
import os
import shutil
import struct
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

out_dir = Path(sys.argv[1])
gh_token = os.environ.get("GITHUB_TOKEN") or ""
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def make_req(url: str, is_gh_api: bool = False):
    hdrs = {"User-Agent": UA}
    if is_gh_api:
        hdrs["Accept"] = "application/vnd.github+json"
        if gh_token:
            hdrs["Authorization"] = f"Bearer {gh_token}"
    return urllib.request.Request(url, headers=hdrs)


def is_valid_arm64_elf(path: Path) -> bool:
    try:
        if not path.exists() or path.stat().st_size < 1_500_000:
            return False
        with open(path, "rb") as fp:
            hdr = fp.read(20)
        if len(hdr) < 20 or hdr[:4] != b"\x7fELF":
            return False
        return struct.unpack("<H", hdr[18:20])[0] == 183  # EM_AARCH64
    except Exception:
        return False


def download_file(url: str, dest: Path) -> bool:
    try:
        with urllib.request.urlopen(make_req(url), timeout=60) as resp:
            with open(dest, "wb") as fp:
                shutil.copyfileobj(resp, fp, length=131072)
        return dest.exists() and dest.stat().st_size > 0
    except Exception as exc:
        print(f"  Skipped ({url}): {exc}", flush=True)
        return False


def install_candidate(raw_p: Path, bin_name: str, final_dest: Path, tmp_dir: Path, label: str) -> bool:
    low = raw_p.name.lower()
    out_tmp = tmp_dir / f"cand_{bin_name}"
    try:
        if low.endswith(".tar.xz") or low.endswith(".tar.gz"):
            with tarfile.open(raw_p, "r:*") as tf:
                for member in tf.getmembers():
                    if member.isfile() and Path(member.name).name == bin_name:
                        with tf.extractfile(member) as sf, open(out_tmp, "wb") as df:
                            shutil.copyfileobj(sf, df)
                        break
        elif low.endswith(".gz"):
            with gzip.open(raw_p, "rb") as sf, open(out_tmp, "wb") as df:
                shutil.copyfileobj(sf, df)
        else:
            shutil.copy2(raw_p, out_tmp)

        if is_valid_arm64_elf(out_tmp):
            shutil.move(str(out_tmp), str(final_dest))
            os.chmod(final_dest, 0o755)
            print(f"  OK: {final_dest.name} ({final_dest.stat().st_size // 1048576} MB) from {label}", flush=True)
            return True
        print(f"  Rejected {label}: not a valid ARM64 ELF binary", flush=True)
    except Exception as exc:
        print(f"  Extract failed ({label}): {exc}", flush=True)
    return False


def try_direct_urls(urls: list, bin_name: str, final_dest: Path, tmp_dir: Path) -> bool:
    for idx, url in enumerate(urls):
        print(f"Trying direct mirror for {bin_name}: {url}", flush=True)
        fname = url.split("?")[0].rstrip("/").split("/")[-1] or f"dl_{idx}"
        raw_p = tmp_dir / f"direct_{idx}_{fname}"
        if download_file(url, raw_p):
            if install_candidate(raw_p, bin_name, final_dest, tmp_dir, url):
                return True
    return False


def try_github_api(repo: str, bin_name: str, final_dest: Path, tmp_dir: Path) -> bool:
    api_url = f"https://api.github.com/repos/{repo}/releases/latest"
    print(f"Checking GitHub API {repo} for {bin_name}...", flush=True)
    try:
        with urllib.request.urlopen(make_req(api_url, is_gh_api=True), timeout=25) as resp:
            rel = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception as exc:
        print(f"  API error ({repo}): {exc}", flush=True)
        return False

    bad_exts = (".readme", ".sha256", ".sha512", ".sig", ".asc", ".txt", ".json", ".md5")
    cands = []
    for a in (rel.get("assets") or []):
        nm = (a.get("name") or "").strip()
        dl = (a.get("browser_download_url") or "").strip()
        low = nm.lower()
        if not nm or not dl or any(low.endswith(b) for b in bad_exts):
            continue
        if "linux" not in low or not ("arm64" in low or "aarch64" in low):
            continue
        if bin_name == "ffmpeg" and "ffprobe" in low:
            continue
        if bin_name == "ffprobe" and "ffprobe" not in low:
            continue
        prio = 3 if (low.endswith(".tar.xz") or low.endswith(".tar.gz")) else (2 if low.endswith(".gz") else 1)
        cands.append((prio, nm, dl))

    cands.sort(key=lambda x: x[0])
    for _, nm, dl in cands:
        raw_p = tmp_dir / f"api_{nm}"
        if download_file(dl, raw_p):
            if install_candidate(raw_p, bin_name, final_dest, tmp_dir, f"{repo}/{nm}"):
                return True
    return False


ffmpeg_out = out_dir / "libffmpeg.so"
ffprobe_out = out_dir / "libffprobe.so"

with tempfile.TemporaryDirectory() as td:
    tmp_p = Path(td)

    if not is_valid_arm64_elf(ffmpeg_out):
        try_direct_urls([
            "https://github.com/eugeneware/ffmpeg-static/releases/latest/download/ffmpeg-linux-arm64",
            "https://github.com/eugeneware/ffmpeg-static/releases/latest/download/ffmpeg-linux-arm64.gz",
            "https://github.com/eugeneware/ffmpeg-static/releases/latest/download/linux-arm64",
            "https://github.com/shaka-project/static-ffmpeg-binaries/releases/latest/download/ffmpeg-linux-arm64",
            "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz",
        ], "ffmpeg", ffmpeg_out, tmp_p)

    if not is_valid_arm64_elf(ffprobe_out):
        try_direct_urls([
            "https://github.com/shaka-project/static-ffmpeg-binaries/releases/latest/download/ffprobe-linux-arm64",
            "https://github.com/eugeneware/ffmpeg-static/releases/latest/download/ffprobe-linux-arm64",
            "https://github.com/eugeneware/ffmpeg-static/releases/latest/download/ffprobe-linux-arm64.gz",
        ], "ffprobe", ffprobe_out, tmp_p)

    for repo in ("eugeneware/ffmpeg-static", "shaka-project/static-ffmpeg-binaries"):
        if not is_valid_arm64_elf(ffmpeg_out):
            try_github_api(repo, "ffmpeg", ffmpeg_out, tmp_p)
        if not is_valid_arm64_elf(ffprobe_out):
            try_github_api(repo, "ffprobe", ffprobe_out, tmp_p)

if not is_valid_arm64_elf(ffmpeg_out):
    print("ERROR: Could not fetch a valid ARM64 FFmpeg binary from any mirror.", file=sys.stderr)
    sys.exit(1)

print("Bundled ARM64 FFmpeg successfully!", flush=True)
PYEOF

ls -lh "$OUT_DIR"
file "$OUT_DIR/libffmpeg.so"
