#!/usr/bin/env python3
import hashlib
import html as html_lib
import http.cookiejar
import json
import mimetypes
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlparse

PORT = 8000
VERSION = "v20.13"


def _resolve_app_data_dir() -> Path:
    env_dir = (os.environ.get("OMNISAVE_DATA_DIR") or "").strip()
    if env_dir:
        p = Path(env_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p
    try:
        return Path.home()
    except Exception:
        return Path("/tmp")


APP_DATA_DIR = _resolve_app_data_dir()
DOWNLOAD_DIR = Path(os.environ.get("OMNISAVE_DOWNLOAD_DIR") or "/storage/emulated/0/Download")
WA_SUBDIR_NAME = "WhatsApp"
COOKIES_FILE = APP_DATA_DIR / ".omnisave_cookies.txt"
BROWSER_COOKIES_FILE = APP_DATA_DIR / ".omnisave_browser_cookies.txt"
CONFIG_FILE = APP_DATA_DIR / ".omnisave_config.json"
STAGE_DIR = APP_DATA_DIR / ".omnisave_stage"
YTDLP_OVERRIDE_ZIP = APP_DATA_DIR / "yt-dlp-latest.zip"
_ANDROID_FFMPEG_DIR = ""

if YTDLP_OVERRIDE_ZIP.exists():
    try:
        if YTDLP_OVERRIDE_ZIP.stat().st_size > 100000 and str(YTDLP_OVERRIDE_ZIP) not in sys.path:
            sys.path.insert(0, str(YTDLP_OVERRIDE_ZIP))
    except Exception:
        pass
COOKIES_LOCK = threading.Lock()
SUPPORTED_BROWSERS = ("firefox", "edge", "chrome", "brave", "opera", "vivaldi", "chromium")
BROWSER_COOKIE_TTL = 300  # seconds

BROWSER_CK_LOCK = threading.Lock()
BROWSER_CK_STATE = {
    "browser": "",
    "profile": "",
    "synced_at": 0.0,
    "lines": 0,
    "error": "",
    "cli_fallback": False,
}

BROWSER_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

WA_CANDIDATE_DIRS = [
    ("WhatsApp", Path("/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media/.Statuses")),
    ("WA Business", Path("/storage/emulated/0/Android/media/com.whatsapp.w4b/WhatsApp Business/Media/.Statuses")),
    ("WhatsApp", Path("/sdcard/Android/media/com.whatsapp/WhatsApp/Media/.Statuses")),
    ("WA Business", Path("/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business/Media/.Statuses")),
    ("WhatsApp", Path.home() / "storage/shared/Android/media/com.whatsapp/WhatsApp/Media/.Statuses"),
    ("WA Business", Path.home() / "storage/shared/Android/media/com.whatsapp.w4b/WhatsApp Business/Media/.Statuses"),
    ("WhatsApp", Path("/storage/emulated/0/WhatsApp/Media/.Statuses")),
    ("WA Business", Path("/storage/emulated/0/WhatsApp Business/Media/.Statuses")),
    ("WhatsApp", Path("/sdcard/WhatsApp/Media/.Statuses")),
    ("WA Business", Path("/sdcard/WhatsApp Business/Media/.Statuses")),
]

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXTS = {".mp4", ".mkv", ".3gp", ".webm", ".mov", ".m4v"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac"}
STREAM_EXTS = {".m3u8", ".mpd"}

AD_IFRAME_BLOCKLIST = (
    "googlesyndication", "doubleclick", "googletagmanager", "google-analytics",
    "recaptcha", "hcaptcha", "facebook.com/plugins/like", "platform.twitter.com/widgets",
    "ads.", "adservice.", "popads", "exoclick", "juicyads",
)

JOBS = {}
JOBS_LOCK = threading.Lock()

INFO_CACHE = {}
INFO_CACHE_LOCK = threading.Lock()
INFO_CACHE_TTL = 90  # seconds

_YTDLP_MOD = None
_YTDLP_IMPORT_TRIED = False
_YTDLP_USE_CLI_ONLY = False
_YTDLP_IMPORT_LOCK = threading.Lock()


class _SilentYtdlpLogger:
    def __init__(self):
        self.errors = []

    def debug(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        if msg:
            self.errors.append(str(msg))


def _get_ytdlp_module():
    global _YTDLP_MOD, _YTDLP_IMPORT_TRIED
    if _YTDLP_USE_CLI_ONLY:
        return None
    if _YTDLP_IMPORT_TRIED:
        return _YTDLP_MOD
    with _YTDLP_IMPORT_LOCK:
        if not _YTDLP_IMPORT_TRIED:
            try:
                import yt_dlp as _ytdlp_imported
                _YTDLP_MOD = _ytdlp_imported
            except Exception:
                _YTDLP_MOD = None
            _YTDLP_IMPORT_TRIED = True
    return _YTDLP_MOD


def _warmup_ytdlp_async():
    def _bg():
        _get_ytdlp_module()
    threading.Thread(target=_bg, daemon=True).start()


def _clear_info_cache():
    with INFO_CACHE_LOCK:
        INFO_CACHE.clear()

WA_MAP = {}
WA_LOCK = threading.Lock()

FIXED_CHROME_EXT_ID = 'blholaaggcinjiflgfehbpebfgodijce'
LAST_ACTIVITY_TS = time.time()
IDLE_TIMEOUT_SEC = 0
SERVER_INSTANCE = None
SHUTDOWN_REQUESTED = False


def _touch_activity():
    global LAST_ACTIVITY_TS
    LAST_ACTIVITY_TS = time.time()


def _has_active_jobs() -> bool:
    with JOBS_LOCK:
        for job in JOBS.values():
            if job.get("status") in ("starting", "downloading", "postprocessing"):
                return True
    return False


def _win_popen_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}

HTML = r"""<!doctype html>
<html lang="en" dir="ltr" data-theme="dark">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover" />
<title>OmniSave Studio</title>
<style>
  :root{
    --bg:#09090f;
    --orb1:rgba(124,58,237,.34);
    --orb2:rgba(14,165,233,.28);
    --orb3:rgba(236,72,153,.20);
    --glass:rgba(20,22,34,.66);
    --glass-strong:rgba(25,28,44,.88);
    --surface:rgba(255,255,255,.045);
    --surface-hover:rgba(255,255,255,.085);
    --input-bg:rgba(9,11,18,.68);
    --border:rgba(255,255,255,.11);
    --border-top:rgba(255,255,255,.22);
    --txt:#f8fafc;
    --txt-soft:#cbd5e1;
    --muted:#94a3b8;
    --accent:#8b5cf6;
    --accent2:#06b6d4;
    --accent-grad:linear-gradient(135deg,#8b5cf6 0%,#6366f1 48%,#06b6d4 100%);
    --dl-grad:linear-gradient(135deg,#10b981 0%,#06b6d4 100%);
    --wa-grad:linear-gradient(135deg,#22c55e 0%,#10b981 100%);
    --shadow:0 24px 60px rgba(0,0,0,.48);
    --chip-bg:rgba(255,255,255,.05);
    --chip-active-bg:rgba(139,92,246,.22);
    --chip-active-border:rgba(167,139,250,.65);
    --ok-bg:rgba(16,185,129,.14);
    --ok-border:rgba(16,185,129,.35);
    --ok-txt:#6ee7b7;
    --err-bg:rgba(244,63,94,.14);
    --err-border:rgba(244,63,94,.36);
    --err-txt:#fda4af;
    --info-bg:rgba(56,189,248,.13);
    --info-border:rgba(56,189,248,.34);
    --info-txt:#7dd3fc;
  }

  [data-theme="light"]{
    --bg:#eef2f9;
    --orb1:rgba(139,92,246,.22);
    --orb2:rgba(14,165,233,.20);
    --orb3:rgba(244,114,182,.16);
    --glass:rgba(255,255,255,.78);
    --glass-strong:rgba(255,255,255,.94);
    --surface:rgba(15,23,42,.04);
    --surface-hover:rgba(15,23,42,.075);
    --input-bg:rgba(255,255,255,.88);
    --border:rgba(15,23,42,.09);
    --border-top:rgba(255,255,255,.95);
    --txt:#0f172a;
    --txt-soft:#334155;
    --muted:#64748b;
    --shadow:0 20px 50px rgba(15,23,42,.09);
    --chip-bg:rgba(15,23,42,.045);
    --chip-active-bg:rgba(99,102,241,.13);
    --chip-active-border:rgba(99,102,241,.55);
    --ok-bg:rgba(16,185,129,.12);
    --ok-border:rgba(16,185,129,.30);
    --ok-txt:#047857;
    --err-bg:rgba(244,63,94,.10);
    --err-border:rgba(244,63,94,.28);
    --err-txt:#be123c;
    --info-bg:rgba(14,165,233,.10);
    --info-border:rgba(14,165,233,.28);
    --info-txt:#0369a1;
  }

  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}

  body{
    margin:0;
    min-height:100vh;
    font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Inter","Segoe UI",Roboto,sans-serif;
    background:var(--bg);
    color:var(--txt);
    padding:22px 15px 56px;
    overflow-x:hidden;
    transition:background .3s ease,color .3s ease;
  }

  .ambient{position:fixed;inset:0;z-index:-1;pointer-events:none;overflow:hidden}
  .orb{
    position:absolute;border-radius:50%;filter:blur(85px);opacity:.95;
    animation:float 16s ease-in-out infinite alternate;
  }
  .orb.o1{width:340px;height:340px;background:var(--orb1);top:-110px;left:-90px}
  .orb.o2{width:320px;height:320px;background:var(--orb2);top:18%;right:-110px;animation-delay:-5s}
  .orb.o3{width:280px;height:280px;background:var(--orb3);bottom:-90px;left:22%;animation-delay:-10s}
  @keyframes float{
    0%{transform:translate3d(0,0,0) scale(1)}
    100%{transform:translate3d(18px,24px,0) scale(1.08)}
  }

  .wrap{max-width:580px;margin:0 auto}

  .topbar{
    display:flex;align-items:center;justify-content:space-between;
    margin-bottom:16px;padding:0 2px;
  }
  .brand{display:flex;align-items:center;gap:12px}
  .logo{
    width:40px;height:40px;border-radius:13px;
    background:var(--accent-grad);
    display:grid;place-items:center;color:#fff;
    box-shadow:0 10px 24px rgba(124,58,237,.35);
    flex-shrink:0;
  }
  .brand-name{
    margin:0;font-size:18px;font-weight:800;letter-spacing:-.35px;
    display:flex;align-items:center;gap:7px;
  }
  .ver{
    font-size:10px;font-weight:700;letter-spacing:.4px;
    padding:2px 7px;border-radius:999px;
    background:var(--surface);border:1px solid var(--border);
    color:var(--muted);text-transform:uppercase;
  }
  .top-actions{display:flex;align-items:center;gap:8px}
  .icon-pill{
    position:relative;
    width:38px;height:38px;border-radius:12px;
    border:1px solid var(--border);
    background:var(--glass);
    backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);
    color:var(--txt);cursor:pointer;
    display:grid;place-items:center;
    transition:transform .15s ease,background .2s ease;
  }
  .icon-pill:active{transform:scale(.93)}
  .dot{
    position:absolute;top:7px;right:7px;width:7px;height:7px;border-radius:50%;
    background:#10b981;display:none;box-shadow:0 0 8px rgba(16,185,129,.8);
  }

  .nav-tabs{
    display:grid;grid-template-columns:1fr 1fr;gap:6px;
    padding:5px;border-radius:18px;margin-bottom:14px;
    background:var(--glass);
    backdrop-filter:blur(22px);-webkit-backdrop-filter:blur(22px);
    border:1px solid var(--border);
    box-shadow:0 10px 28px rgba(0,0,0,.16);
  }
  .nav-tab{
    height:42px;border:none;border-radius:13px;
    background:transparent;color:var(--muted);
    font-size:13.5px;font-weight:700;cursor:pointer;
    display:flex;align-items:center;justify-content:center;gap:8px;
    transition:all .2s ease;
  }
  .nav-tab.active{
    background:var(--glass-strong);color:var(--txt);
    border:1px solid var(--border);
    box-shadow:0 6px 16px rgba(0,0,0,.18);
  }
  .count-pill{
    font-size:10.5px;font-weight:700;padding:1px 7px;border-radius:999px;
    background:rgba(16,185,129,.18);color:var(--ok-txt);
    display:none;font-variant-numeric:tabular-nums;
  }

  .panel{
    background:var(--glass);
    backdrop-filter:blur(26px);-webkit-backdrop-filter:blur(26px);
    border:1px solid var(--border);
    border-top-color:var(--border-top);
    border-radius:26px;
    padding:18px;
    box-shadow:var(--shadow);
  }

  .tab-pane{display:none;animation:fadeUp .24s cubic-bezier(.16,1,.3,1)}
  .tab-pane.active{display:block}

  .input-shell{
    display:flex;align-items:center;gap:6px;
    background:var(--input-bg);
    border:1px solid var(--border);
    border-radius:16px;
    padding:5px 6px 5px 14px;
    transition:border-color .2s ease,box-shadow .2s ease;
  }
  .input-shell:focus-within{
    border-color:var(--accent);
    box-shadow:0 0 0 3.5px rgba(139,92,246,.20);
  }
  .input-icon{color:var(--muted);flex-shrink:0;display:flex}
  .input-shell input{
    flex:1;min-width:0;border:none;background:transparent;
    color:var(--txt);font-size:14px;padding:9px 4px;outline:none;
  }
  .input-shell input::placeholder{color:var(--muted)}

  .mini-btn{
    height:36px;padding:0 11px;border-radius:11px;
    border:1px solid transparent;background:var(--surface);
    color:var(--txt-soft);font-size:12.5px;font-weight:600;
    cursor:pointer;display:inline-flex;align-items:center;gap:5px;
    transition:background .15s ease,transform .12s ease;
    flex-shrink:0;
  }
  .mini-btn:hover{background:var(--surface-hover)}
  .mini-btn:active{transform:scale(.95)}
  .mini-btn.icon-only{width:36px;padding:0;justify-content:center;color:var(--muted)}

  .btn-primary{
    width:100%;margin-top:12px;height:48px;border-radius:15px;
    border:none;color:#fff;font-size:14.5px;font-weight:700;
    letter-spacing:.15px;cursor:pointer;
    background:var(--accent-grad);
    box-shadow:0 10px 24px rgba(99,102,241,.30);
    display:flex;align-items:center;justify-content:center;gap:8px;
    transition:transform .14s ease,opacity .2s ease;
  }
  .btn-primary:active{transform:scale(.985)}
  .btn-primary:disabled{opacity:.52;cursor:not-allowed;box-shadow:none}

  .btn-download{
    background:var(--dl-grad);
    box-shadow:0 10px 24px rgba(16,185,129,.28);
    margin-top:14px;
  }

  .btn-secondary{
    height:40px;padding:0 14px;border-radius:12px;
    border:1px solid var(--border);background:var(--surface);
    color:var(--txt);font-size:12.5px;font-weight:700;cursor:pointer;
    display:inline-flex;align-items:center;justify-content:center;gap:6px;
  }
  .btn-secondary:disabled{opacity:.5;cursor:not-allowed}

  .btn-cancel{
    width:100%;margin-top:12px;height:38px;border-radius:12px;
    border:1px solid var(--err-border);background:var(--err-bg);
    color:var(--err-txt);font-size:12.5px;font-weight:700;cursor:pointer;
  }

  .loader-card{
    display:none;margin-top:14px;padding:13px 14px;border-radius:16px;
    background:var(--surface);border:1px solid var(--border);
    animation:fadeUp .28s cubic-bezier(.16,1,.3,1);
  }
  .loader-row{display:flex;align-items:center;gap:10px;font-size:13px;font-weight:600;color:var(--txt-soft)}
  .spinner{
    width:17px;height:17px;border-radius:50%;
    border:2.3px solid var(--border);border-top-color:var(--accent2);
    animation:spin .7s linear infinite;flex-shrink:0;
  }
  .indet-bar{margin-top:10px;height:4px;border-radius:999px;background:var(--surface-hover);overflow:hidden}
  .indet-bar i{display:block;height:100%;width:42%;border-radius:999px;background:var(--accent-grad);animation:slide 1.15s ease-in-out infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  @keyframes slide{0%{transform:translateX(-110%)}100%{transform:translateX(260%)}}
  @keyframes fadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}

  .preview{
    display:none;margin-top:18px;padding-top:18px;
    border-top:1px solid var(--border);
    animation:fadeUp .32s cubic-bezier(.16,1,.3,1);
  }
  .media-card{
    border-radius:18px;border:1px solid var(--border);
    background:var(--surface);
    display:grid;grid-template-columns:146px 1fr;gap:14px;align-items:center;
    padding:10px;
  }
  @media(max-width:470px){.media-card{grid-template-columns:1fr}}
  .thumb-wrap{
    position:relative;border-radius:12px;overflow:hidden;
    background:#0b0f1a;aspect-ratio:16/9;border:1px solid var(--border);
  }
  .thumb-wrap img{width:100%;height:100%;object-fit:cover;display:block}
  .dur-badge{
    position:absolute;bottom:7px;right:7px;
    background:rgba(9,11,18,.78);backdrop-filter:blur(8px);
    color:#fff;font-size:11px;font-weight:700;
    padding:2px 7px;border-radius:7px;font-variant-numeric:tabular-nums;
  }
  .media-info{min-width:0;padding:2px 4px 2px 0}
  .media-title{
    margin:0 0 8px;font-size:14.5px;font-weight:700;line-height:1.4;
    display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
  }
  .meta-row{display:flex;flex-wrap:wrap;gap:6px}
  .badge{
    font-size:11px;font-weight:600;color:var(--txt-soft);
    background:var(--glass-strong);border:1px solid var(--border);
    padding:3px 9px;border-radius:999px;max-width:100%;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  }
  .badge.source{color:var(--accent);border-color:var(--chip-active-border);background:var(--chip-active-bg)}
  .badge.sniffer{color:var(--ok-txt);border-color:var(--ok-border);background:var(--ok-bg)}

  .section-lbl{
    font-size:11px;font-weight:700;text-transform:uppercase;
    letter-spacing:.65px;color:var(--muted);margin:16px 0 8px 2px;
  }
  .seg-control{
    display:grid;grid-template-columns:1fr 1fr;gap:6px;
    padding:5px;border-radius:15px;
    background:var(--input-bg);border:1px solid var(--border);
  }
  .seg-btn{
    height:38px;border:none;border-radius:11px;
    background:transparent;color:var(--muted);
    font-size:13px;font-weight:700;cursor:pointer;
    display:flex;align-items:center;justify-content:center;gap:7px;
    transition:all .18s ease;
  }
  .seg-btn.active{
    background:var(--glass-strong);color:var(--txt);
    box-shadow:0 4px 12px rgba(0,0,0,.16);
    border:1px solid var(--border);
  }

  .chips{display:flex;flex-wrap:wrap;gap:8px}
  .chip{
    padding:9px 13px;border-radius:12px;
    border:1px solid var(--border);background:var(--chip-bg);
    color:var(--txt-soft);font-size:12.5px;font-weight:600;
    cursor:pointer;transition:all .16s ease;
    display:inline-flex;align-items:center;gap:6px;
  }
  .chip:hover{background:var(--surface-hover)}
  .chip.active{
    background:var(--chip-active-bg);
    border-color:var(--chip-active-border);
    color:var(--txt);
    box-shadow:0 4px 14px rgba(139,92,246,.22);
  }
  .chip .sz{font-size:11px;color:var(--muted);font-weight:600}
  .chip.active .sz{color:var(--txt-soft)}

  .entries-box{
    display:none;margin-top:12px;padding:10px;border-radius:15px;
    background:var(--surface);border:1px solid var(--border);
    max-height:210px;overflow-y:auto;
  }
  .entry-item{
    display:flex;align-items:center;justify-content:space-between;gap:10px;
    padding:8px 10px;border-radius:10px;cursor:pointer;
    font-size:12.5px;font-weight:600;color:var(--txt-soft);
    transition:background .15s ease;
  }
  .entry-item:hover{background:var(--surface-hover)}
  .entry-item.active{background:var(--chip-active-bg);color:var(--txt)}
  .entry-idx{
    font-size:11px;font-weight:700;color:var(--muted);
    min-width:22px;font-variant-numeric:tabular-nums;
  }
  .entry-title{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

  .prog-card{
    display:none;margin-top:16px;padding:15px;border-radius:18px;
    background:var(--glass-strong);border:1px solid var(--chip-active-border);
    box-shadow:0 12px 30px rgba(99,102,241,.12);
    animation:fadeUp .28s cubic-bezier(.16,1,.3,1);
  }
  .prog-top{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px}
  .prog-stage{font-size:13px;font-weight:700;color:var(--txt);display:flex;align-items:center;gap:8px;min-width:0}
  .prog-pct{
    font-size:20px;font-weight:800;letter-spacing:-.4px;
    background:var(--accent-grad);
    -webkit-background-clip:text;background-clip:text;color:transparent;
    font-variant-numeric:tabular-nums;flex-shrink:0;
  }
  .prog-track{width:100%;height:10px;border-radius:999px;background:var(--surface-hover);overflow:hidden}
  .prog-fill{height:100%;width:0%;border-radius:999px;background:var(--dl-grad);position:relative;transition:width .25s ease}
  .prog-fill::after{
    content:"";position:absolute;inset:0;
    background:linear-gradient(90deg,transparent,rgba(255,255,255,.35),transparent);
    animation:shimmer 1.5s infinite;
  }
  @keyframes shimmer{0%{transform:translateX(-100%)}100%{transform:translateX(100%)}}

  .stats-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px}
  @media(max-width:440px){.stats-grid{grid-template-columns:1fr 1fr}}
  .stat-tile{background:var(--surface);border:1px solid var(--border);border-radius:13px;padding:9px 11px}
  .stat-lbl{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin-bottom:3px}
  .stat-val{font-size:13px;font-weight:700;color:var(--txt);font-variant-numeric:tabular-nums}

  .status{
    display:none;margin-top:14px;padding:12px 14px;border-radius:14px;
    font-size:12.5px;font-weight:600;line-height:1.5;
    white-space:pre-wrap;word-break:break-word;
    animation:fadeUp .22s ease;
  }
  .status.info{display:block;background:var(--info-bg);border:1px solid var(--info-border);color:var(--info-txt)}
  .status.ok{display:block;background:var(--ok-bg);border:1px solid var(--ok-border);color:var(--ok-txt)}
  .status.err{display:block;background:var(--err-bg);border:1px solid var(--err-border);color:var(--err-txt)}

  /* WhatsApp Status Tab */
  .wa-toolbar{
    display:flex;align-items:center;justify-content:space-between;gap:8px;
    flex-wrap:wrap;margin-bottom:14px;
  }
  .wa-filters{display:flex;gap:6px}
  .f-pill{
    height:34px;padding:0 12px;border-radius:11px;
    border:1px solid var(--border);background:var(--chip-bg);
    color:var(--muted);font-size:12px;font-weight:700;cursor:pointer;
    transition:all .16s ease;
  }
  .f-pill.active{
    background:var(--chip-active-bg);border-color:var(--chip-active-border);color:var(--txt);
  }
  .wa-actions{display:flex;gap:6px;align-items:center}
  .wa-save-all{
    height:34px;padding:0 12px;border-radius:11px;border:none;
    background:var(--wa-grad);color:#fff;font-size:12px;font-weight:700;
    cursor:pointer;display:none;align-items:center;gap:5px;
  }
  .wa-save-all:disabled{opacity:.55;cursor:not-allowed}

  .wa-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:11px}
  @media(min-width:540px){.wa-grid{grid-template-columns:repeat(3,1fr)}}

  .wa-card{
    position:relative;border-radius:16px;overflow:hidden;
    background:var(--input-bg);border:1px solid var(--border);
    aspect-ratio:3/4;cursor:pointer;
    transition:transform .16s ease;
  }
  .wa-card:active{transform:scale(.98)}
  .wa-card img,.wa-card video{width:100%;height:100%;object-fit:cover;display:block;background:#05080f}
  .wa-top-badges{
    position:absolute;top:8px;left:8px;right:8px;
    display:flex;align-items:center;justify-content:space-between;pointer-events:none;
  }
  .wa-pill{
    font-size:10px;font-weight:700;letter-spacing:.3px;
    padding:3px 7px;border-radius:8px;
    background:rgba(9,11,18,.72);backdrop-filter:blur(8px);
    color:#fff;display:inline-flex;align-items:center;gap:4px;
  }
  .wa-play{
    position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
    width:42px;height:42px;border-radius:50%;
    background:rgba(9,11,18,.68);backdrop-filter:blur(8px);
    border:1px solid rgba(255,255,255,.28);color:#fff;
    display:grid;place-items:center;pointer-events:none;
  }
  .wa-foot{
    position:absolute;left:0;right:0;bottom:0;
    padding:26px 9px 9px;
    background:linear-gradient(to top,rgba(5,7,14,.92) 15%,transparent 100%);
    display:flex;align-items:center;justify-content:space-between;gap:6px;
  }
  .wa-sz{font-size:11px;font-weight:700;color:rgba(255,255,255,.85);font-variant-numeric:tabular-nums}
  .wa-save-btn{
    height:30px;padding:0 11px;border-radius:9px;border:none;
    background:var(--wa-grad);color:#fff;font-size:11.5px;font-weight:700;
    cursor:pointer;display:inline-flex;align-items:center;gap:4px;
    box-shadow:0 4px 12px rgba(16,185,129,.35);
  }
  .wa-save-btn.saved{
    background:rgba(16,185,129,.22);border:1px solid rgba(52,211,153,.5);
    color:#6ee7b7;box-shadow:none;
  }

  .wa-empty{
    display:none;text-align:center;padding:34px 16px;
    border-radius:18px;background:var(--surface);border:1px dashed var(--border);
    color:var(--muted);font-size:13px;line-height:1.6;
  }
  .wa-empty strong{display:block;color:var(--txt);font-size:14px;margin-bottom:5px}

  /* Modals */
  .modal{
    display:none;position:fixed;inset:0;z-index:50;
    background:rgba(5,7,14,.84);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);
    padding:16px;align-items:center;justify-content:center;
  }
  .modal.open{display:flex;animation:fadeUp .2s ease}
  .modal-box{
    width:100%;max-width:480px;max-height:90vh;
    background:var(--glass-strong);border:1px solid var(--border);
    border-radius:22px;overflow:hidden;
    display:flex;flex-direction:column;box-shadow:0 28px 70px rgba(0,0,0,.6);
  }
  .modal-head{
    display:flex;align-items:center;justify-content:space-between;
    padding:13px 16px;border-bottom:1px solid var(--border);
  }
  .modal-title{font-size:14.5px;font-weight:800;color:var(--txt)}
  .modal-meta{font-size:12.5px;font-weight:700;color:var(--txt-soft)}
  .modal-body{
    background:#05070d;display:grid;place-items:center;
    max-height:66vh;overflow:hidden;
  }
  .modal-body img,.modal-body video{max-width:100%;max-height:66vh;object-fit:contain;display:block}
  .modal-foot{
    display:grid;grid-template-columns:1fr auto;gap:8px;
    padding:12px 14px;border-top:1px solid var(--border);
  }
  .modal-save{
    height:42px;border-radius:12px;border:none;
    background:var(--wa-grad);color:#fff;font-size:13.5px;font-weight:700;
    cursor:pointer;display:flex;align-items:center;justify-content:center;gap:7px;
  }
  .modal-save.saved{background:var(--ok-bg);border:1px solid var(--ok-border);color:var(--ok-txt)}
  .modal-dl-link{
    height:42px;padding:0 14px;border-radius:12px;
    border:1px solid var(--border);background:var(--surface);
    color:var(--txt);text-decoration:none;font-size:13px;font-weight:700;
    display:inline-flex;align-items:center;justify-content:center;
  }

  /* Settings Content */
  .set-body{padding:16px;overflow-y:auto;display:flex;flex-direction:column;gap:14px}
  .set-card{
    background:var(--surface);border:1px solid var(--border);
    border-radius:16px;padding:13px;
  }
  .set-row{display:flex;align-items:center;justify-content:space-between;gap:10px}
  .set-h{font-size:13px;font-weight:700;color:var(--txt);margin:0}
  .set-sub{font-size:11.5px;color:var(--muted);margin:3px 0 0;line-height:1.4}
  .set-area{
    width:100%;min-height:92px;margin-top:10px;padding:10px 12px;
    border-radius:12px;border:1px solid var(--border);
    background:var(--input-bg);color:var(--txt);
    font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;
    resize:vertical;outline:none;
  }
  .set-btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
  .set-select,.set-input{
    width:100%;height:38px;padding:0 11px;border-radius:11px;
    border:1px solid var(--border);background:var(--input-bg);
    color:var(--txt);font-size:12.5px;font-weight:600;outline:none;
  }
  .set-grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
  .set-seg{
    display:grid;grid-template-columns:1fr 1fr;gap:5px;margin-top:10px;
    padding:4px;border-radius:12px;background:var(--input-bg);border:1px solid var(--border);
  }
  .set-seg-btn{
    height:32px;border:none;border-radius:9px;background:transparent;
    color:var(--muted);font-size:12px;font-weight:700;cursor:pointer;transition:all .16s ease;
  }
  .set-seg-btn.active{background:var(--glass-strong);color:var(--txt);border:1px solid var(--border)}
  .set-warn{
    display:none;margin-top:9px;padding:9px 11px;border-radius:11px;
    background:var(--err-bg);border:1px solid var(--err-border);color:var(--err-txt);
    font-size:11.5px;font-weight:600;line-height:1.45;
  }
  .action-row{display:flex;gap:8px;margin-top:12px}
  .action-row > *{flex:1;margin-top:0 !important}
  .switch{position:relative;display:inline-block;width:44px;height:24px;flex-shrink:0}
  .switch input{opacity:0;width:0;height:0}
  .slider{
    position:absolute;cursor:pointer;inset:0;border-radius:999px;
    background:var(--surface-hover);border:1px solid var(--border);transition:.2s;
  }
  .slider:before{
    content:"";position:absolute;height:18px;width:18px;left:2px;top:2px;
    border-radius:50%;background:#fff;transition:.2s;
  }
  .switch input:checked + .slider{background:#10b981;border-color:#10b981}
  .switch input:checked + .slider:before{transform:translateX(20px)}

  /* Quick Share Bottom Sheet Mode (?quick=1) */
  body.quick-mode{
    min-height:auto;
    padding:8px 12px 18px;
    background:#121420;
  }
  body.quick-mode .ambient,
  body.quick-mode .topbar,
  body.quick-mode .nav-tabs,
  body.quick-mode .input-shell,
  body.quick-mode #infoBtn{
    display:none !important;
  }
  body.quick-mode .wrap{max-width:100%}
  body.quick-mode .panel{
    background:transparent;
    border:none;
    box-shadow:none;
    backdrop-filter:none;-webkit-backdrop-filter:none;
    padding:4px 2px 6px;
    border-radius:0;
  }
  body.quick-mode .preview{
    margin-top:6px;
    padding-top:6px;
    border-top:none;
  }
  body.quick-mode .media-card{
    grid-template-columns:108px 1fr;
    gap:10px;
    padding:8px;
  }
  body.quick-mode .section-lbl{margin:11px 0 6px 2px}
  body.quick-mode .btn-download{margin-top:12px;height:46px}

  .quick-bar{
    display:none;
    align-items:center;
    justify-content:space-between;
    gap:8px;
    padding:2px 2px 10px;
    margin-bottom:4px;
    border-bottom:1px solid var(--border);
  }
  body.quick-mode .quick-bar{display:flex}
  .quick-handle{
    display:none;
    width:40px;height:4px;border-radius:999px;
    background:rgba(255,255,255,.24);
    margin:0 auto 10px;
  }
  body.quick-mode .quick-handle{display:block}
  .quick-title{
    font-size:14px;font-weight:800;color:var(--txt);
    display:flex;align-items:center;gap:7px;
  }
  .quick-acts{display:flex;align-items:center;gap:6px}
  .quick-pill{
    height:30px;padding:0 10px;border-radius:9px;
    border:1px solid var(--border);background:var(--surface);
    color:var(--txt-soft);font-size:11.5px;font-weight:700;cursor:pointer;
  }
  .quick-login-bar{
    display:none;margin-top:10px;gap:8px;flex-wrap:wrap;
  }
  .android-acct-grid{
    display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px;
  }
  .android-login-btn{
    height:40px !important;
    border-radius:12px !important;
    font-weight:700 !important;
    justify-content:center !important;
  }
  .manual-toggle{
    margin-top:10px;background:none;border:none;padding:4px 0;
    color:var(--muted);font-size:11.5px;font-weight:700;cursor:pointer;
    text-decoration:underline;display:none;
  }
</style>
</head>
<body>
<div class="ambient">
  <div class="orb o1"></div>
  <div class="orb o2"></div>
  <div class="orb o3"></div>
</div>

<div class="wrap">
  <div class="quick-handle"></div>
  <div class="quick-bar">
    <span class="quick-title">OmniSave <span class="ver">Quick</span></span>
    <div class="quick-acts">
      <button id="quickFullBtn" class="quick-pill" type="button">Full App</button>
      <button id="quickCloseBtn" class="quick-pill" type="button">✕</button>
    </div>
  </div>
  <header class="topbar">
    <div class="brand">
      <div class="logo">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
      </div>
      <h1 class="brand-name">OmniSave <span class="ver">v20.13</span></h1>
    </div>
    <div class="top-actions">
      <button id="acctBtn" class="icon-pill" type="button" title="Sign In (Facebook / Instagram / YouTube)" aria-label="Accounts" style="display:none">
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
      </button>
      <button id="settingsBtn" class="icon-pill" type="button" title="Engine & Cookies" aria-label="Settings">
        <span id="cookieDot" class="dot"></span>
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
      </button>
      <button id="themeBtn" class="icon-pill" type="button" title="Switch Theme" aria-label="Switch Theme"></button>
    </div>
  </header>

  <nav id="navTabs" class="nav-tabs">
    <button id="tabDl" class="nav-tab active" type="button">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
      <span>Downloader</span>
    </button>
    <button id="tabWa" class="nav-tab" type="button">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="4"/></svg>
      <span>WhatsApp Status</span>
      <span id="waCount" class="count-pill">0</span>
    </button>
  </nav>

  <main class="panel">
    <!-- TAB 1: UNIVERSAL DOWNLOADER -->
    <div id="paneDl" class="tab-pane active">
      <div class="input-shell">
        <span class="input-icon">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
        </span>
        <input id="url" type="text" autocomplete="off" placeholder="Any Media or Page URL" />
        <button id="pasteBtn" class="mini-btn" type="button">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
          <span>Paste</span>
        </button>
        <button id="clearBtn" class="mini-btn icon-only" type="button" title="Clear" aria-label="Clear">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>

      <button id="infoBtn" class="btn-primary" type="button">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        <span>Fetch</span>
      </button>

      <div id="loader" class="loader-card">
        <div class="loader-row">
          <div class="spinner"></div>
          <span id="loaderText">Scanning URL...</span>
        </div>
        <div class="indet-bar"><i></i></div>
      </div>

      <section id="preview" class="preview">
        <div class="media-card">
          <div class="thumb-wrap" id="thumbWrap">
            <img id="thumb" src="" alt="Preview" />
            <span id="dur" class="dur-badge" style="display:none"></span>
          </div>
          <div class="media-info">
            <p id="title" class="media-title"></p>
            <div class="meta-row">
              <span id="ext" class="badge source"></span>
              <span id="methodBadge" class="badge sniffer" style="display:none"></span>
              <span id="up" class="badge" style="display:none"></span>
              <span id="alb" class="badge" style="display:none"></span>
            </div>
          </div>
        </div>

        <div id="entriesWrap" style="display:none">
          <div id="entriesLbl" class="section-lbl">Items</div>
          <div id="entriesBox" class="entries-box"></div>
        </div>

        <div id="formatWrap">
          <div class="section-lbl">Format</div>
          <div class="seg-control">
            <button id="segVideo" class="seg-btn active" type="button">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2"/></svg>
              <span>Video · MP4</span>
            </button>
            <button id="segAudio" class="seg-btn" type="button">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
              <span>Audio · Music</span>
            </button>
          </div>
        </div>

        <div id="qualityWrap">
          <div id="qualityLbl" class="section-lbl">Quality</div>
          <div id="qualityChips" class="chips"></div>
        </div>

        <button id="dlBtn" class="btn-primary btn-download" type="button">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          <span id="dlBtnText">Download</span>
        </button>
      </section>

      <section id="progBox" class="prog-card">
        <div class="prog-top">
          <div class="prog-stage">
            <div id="progSpin" class="spinner"></div>
            <span id="progStage">Starting...</span>
          </div>
          <div id="progPct" class="prog-pct">0%</div>
        </div>
        <div class="prog-track">
          <div id="progFill" class="prog-fill"></div>
        </div>
        <div class="stats-grid">
          <div class="stat-tile">
            <div class="stat-lbl">Transferred</div>
            <div id="statMb" class="stat-val">0 MB</div>
          </div>
          <div class="stat-tile">
            <div class="stat-lbl">Speed</div>
            <div id="statSpeed" class="stat-val">—</div>
          </div>
          <div class="stat-tile">
            <div class="stat-lbl">ETA</div>
            <div id="statEta" class="stat-val">—</div>
          </div>
        </div>
        <button id="cancelBtn" class="btn-cancel" type="button">Cancel</button>
        <div id="doneActions" class="action-row" style="display:none">
          <button id="openFolderBtn" class="btn-secondary" type="button" style="display:none;height:40px;font-size:13px">Open Folder</button>
          <a id="syncGalleryBtn" class="btn-primary btn-download" href="#" download style="display:none;text-decoration:none;height:40px;font-size:13px">Save to Gallery (WhatsApp Ready)</a>
        </div>
      </section>

      <div id="status" class="status"></div>
      <div id="quickLoginBar" class="quick-login-bar">
        <button class="btn-secondary quick-site-login" data-site="facebook" type="button">Sign in Facebook</button>
        <button class="btn-secondary quick-site-login" data-site="instagram" type="button">Sign in Instagram</button>
        <button class="btn-secondary quick-site-login" data-site="youtube" type="button">Sign in YouTube</button>
        <button class="btn-secondary quick-site-login" data-site="tiktok" type="button">Sign in TikTok</button>
      </div>
    </div>

    <!-- TAB 2: WHATSAPP STATUS -->
    <div id="paneWa" class="tab-pane">
      <div class="wa-toolbar">
        <div class="wa-filters">
          <button class="f-pill active" data-filter="all" type="button">All</button>
          <button class="f-pill" data-filter="image" type="button">Photos</button>
          <button class="f-pill" data-filter="video" type="button">Videos</button>
        </div>
        <div class="wa-actions">
          <button id="waSaveAllBtn" class="wa-save-all" type="button">Save All</button>
          <button id="waRefreshBtn" class="mini-btn icon-only" type="button" title="Refresh" aria-label="Refresh">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
          </button>
        </div>
      </div>

      <div id="waLoader" class="loader-card">
        <div class="loader-row">
          <div class="spinner"></div>
          <span>Scanning statuses...</span>
        </div>
      </div>

      <div id="waGrid" class="wa-grid"></div>
      <div id="waEmpty" class="wa-empty"></div>
      <div id="waStatus" class="status"></div>
    </div>
  </main>
</div>

<!-- WhatsApp Preview Modal -->
<div id="waModal" class="modal">
  <div class="modal-box">
    <div class="modal-head">
      <span id="modalMeta" class="modal-meta">Preview</span>
      <button id="modalClose" class="mini-btn icon-only" type="button" aria-label="Close">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div id="modalBody" class="modal-body"></div>
    <div class="modal-foot">
      <button id="modalSave" class="modal-save" type="button">Save to Gallery</button>
      <a id="modalDl" class="modal-dl-link" href="#" download>File</a>
    </div>
  </div>
</div>

<!-- Settings Modal (Engine & Cookies) -->
<div id="setModal" class="modal">
  <div class="modal-box">
    <div class="modal-head">
      <span class="modal-title">Engine &amp; Access</span>
      <button id="setClose" class="mini-btn icon-only" type="button" aria-label="Close">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="set-body">
      <div class="set-card">
        <div class="set-row">
          <div>
            <p class="set-h">Extractor Engine</p>
            <p id="engVer" class="set-sub">Loading...</p>
          </div>
          <button id="updBtn" class="btn-secondary" type="button">Update yt-dlp</button>
        </div>
      </div>

      <div class="set-card">
        <div class="set-row">
          <div>
            <p class="set-h">Session Cookies</p>
            <p id="ckInfo" class="set-sub">Unlocks Facebook Stories, private Instagram, TikTok &amp; age-gated YouTube.</p>
          </div>
          <label class="switch" title="Use Cookies">
            <input id="ckToggle" type="checkbox" />
            <span class="slider"></span>
          </label>
        </div>

        <div id="ckSrcSeg" class="set-seg" style="display:none">
          <button id="srcBrowserBtn" class="set-seg-btn active" type="button">Auto Browser</button>
          <button id="srcFileBtn" class="set-seg-btn" type="button">Manual .txt</button>
        </div>

        <div id="ckBrowserWrap" style="display:none">
          <div class="set-grid2">
            <select id="ckBrowserSel" class="set-select" aria-label="Browser">
              <option value="auto">Auto-Detect</option>
              <option value="firefox">Firefox</option>
              <option value="edge">Microsoft Edge</option>
              <option value="chrome">Google Chrome</option>
              <option value="brave">Brave</option>
              <option value="opera">Opera</option>
              <option value="vivaldi">Vivaldi</option>
              <option value="chromium">Chromium</option>
            </select>
            <input id="ckProfileInp" class="set-input" type="text" placeholder="Profile (optional)" />
          </div>
          <div id="ckBrowserWarn" class="set-warn"></div>
          <div class="set-btns">
            <button id="ckSyncBtn" class="btn-secondary" type="button">Sync Browser Now</button>
          </div>
        </div>

        <div id="ckAndroidWrap" style="display:none;margin-top:10px">
          <p class="set-sub" style="margin:0 0 6px;color:var(--txt-soft)">Sign in inside OmniSave to capture session cookies automatically:</p>
          <div class="android-acct-grid">
            <button class="btn-secondary android-login-btn" data-site="facebook" type="button">Facebook Login</button>
            <button class="btn-secondary android-login-btn" data-site="instagram" type="button">Instagram Login</button>
            <button class="btn-secondary android-login-btn" data-site="youtube" type="button">YouTube Login</button>
            <button class="btn-secondary android-login-btn" data-site="tiktok" type="button">TikTok Login</button>
          </div>
          <button id="toggleManualCkBtn" class="manual-toggle" type="button">Manual cookies.txt (Advanced) ▾</button>
        </div>

        <div id="ckManualWrap">
          <textarea id="ckArea" class="set-area" placeholder="Paste Netscape cookies.txt content here..."></textarea>
          <input id="ckFile" type="file" accept=".txt,text/plain" style="display:none" />
          <div class="set-btns">
            <button id="ckUploadBtn" class="btn-secondary" type="button">Load .txt</button>
            <button id="ckSaveBtn" class="btn-secondary" type="button">Save Cookies</button>
            <button id="ckClearBtn" class="btn-secondary" type="button">Clear</button>
          </div>
        </div>
      </div>

      <div class="set-card">
        <p class="set-h">Save Directory</p>
        <p id="saveDirTxt" class="set-sub">Downloads</p>
        <input id="saveDirInp" class="set-input" type="text" style="margin-top:9px" placeholder="Enter folder path..." />
        <div class="set-btns">
          <button id="dirBrowseBtn" class="btn-secondary" type="button" style="display:none">Browse</button>
          <button id="dirSaveBtn" class="btn-secondary" type="button">Save Path</button>
          <button id="dirResetBtn" class="btn-secondary" type="button">Reset</button>
          <button id="dirOpenBtn" class="btn-secondary" type="button" style="display:none">Open</button>
        </div>
      </div>

      <div id="setStatus" class="status"></div>
    </div>
  </div>
</div>

<script>
(function(){
  var meta = null;
  var activeMode = "video";
  var activeQuality = "best";
  var activeTargetUrl = "";
  var activeReferer = "";
  var activeDirect = false;
  var activeAudioUrl = "";
  var activeQualityLabel = "";
  var activeExtraCookies = "";
  var activePlaylistItem = null;
  var activeJobId = null;
  var pollTimer = null;

  var isDesktop = false;
  var isAndroidApp = !!(window.OmniSaveAndroid);
  var isQuickMode = false;
  var cookieSource = "file";
  try {
    var _initQs = new URLSearchParams(window.location.search || "");
    if(_initQs.get("quick") === "1"){
      isQuickMode = true;
      document.body.classList.add("quick-mode");
    }
  } catch(e) {}
  var lastSavedDir = "";
  var waItems = [];
  var waFilter = "all";
  var waLoadedOnce = false;
  var modalItem = null;

  var SUN_SVG = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>';
  var MOON_SVG = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';

  function $(id){ return document.getElementById(id); }

  function applyTheme(theme){
    document.documentElement.setAttribute("data-theme", theme);
    $("themeBtn").innerHTML = theme === "dark" ? SUN_SVG : MOON_SVG;
    try { localStorage.setItem("omnisave_theme", theme); } catch(e) {}
  }

  var savedTheme = "dark";
  try { savedTheme = localStorage.getItem("omnisave_theme") || "dark"; } catch(e) {}
  applyTheme(savedTheme);

  $("themeBtn").onclick = function(){
    var cur = document.documentElement.getAttribute("data-theme") || "dark";
    applyTheme(cur === "dark" ? "light" : "dark");
  };

  /* Top Tabs */
  function switchTab(tab){
    $("tabDl").className = "nav-tab" + (tab === "dl" ? " active" : "");
    $("tabWa").className = "nav-tab" + (tab === "wa" ? " active" : "");
    $("paneDl").className = "tab-pane" + (tab === "dl" ? " active" : "");
    $("paneWa").className = "tab-pane" + (tab === "wa" ? " active" : "");
    if(tab === "wa" && !waLoadedOnce){ loadWaStatuses(); }
  }
  $("tabDl").onclick = function(){ switchTab("dl"); };
  $("tabWa").onclick = function(){ switchTab("wa"); };

  function setStatusEl(elId, type, text){
    var el = $(elId);
    if(!type){ el.className = "status"; el.textContent = ""; return; }
    el.className = "status " + type;
    el.textContent = text;
  }
  function clearStatus(){ setStatusEl("status", "", ""); }
  function setStatus(type, text){ setStatusEl("status", type, text); }
  function setWaStatus(type, text){ setStatusEl("waStatus", type, text); }
  function setSetStatus(type, text){ setStatusEl("setStatus", type, text); }

  window.onerror = function(msg){ setStatus("err", "UI Error: " + msg); };

  function setInspectBusy(busy, text){
    $("infoBtn").disabled = busy;
    $("dlBtn").disabled = busy;
    $("loader").style.display = busy ? "block" : "none";
    if(text) $("loaderText").textContent = text;
  }

  function setDownloadButtonsDisabled(disabled){
    $("infoBtn").disabled = disabled;
    $("dlBtn").disabled = disabled;
  }

  function fmtDur(sec){
    if(!sec || isNaN(sec)) return "";
    sec = Math.floor(sec);
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var s = sec % 60;
    var mm = m < 10 ? "0" + m : "" + m;
    var ss = s < 10 ? "0" + s : "" + s;
    return h > 0 ? (h + ":" + mm + ":" + ss) : (m + ":" + ss);
  }

  function setMode(mode){
    activeMode = mode;
    $("segVideo").className = "seg-btn" + (mode === "video" ? " active" : "");
    $("segAudio").className = "seg-btn" + (mode === "audio" ? " active" : "");
    renderQualityChips();
  }

  function renderQualityChips(){
    var box = $("qualityChips");
    box.innerHTML = "";
    var list = [];

    if(meta && meta.kind === "sniffed"){
      list = meta.streams || [];
      $("qualityLbl").textContent = "Detected Streams";
    } else if(meta && meta.kind === "direct"){
      list = meta.formats || [];
      $("qualityLbl").textContent = "File";
    } else {
      $("qualityLbl").textContent = "Quality";
      var audSz = (meta && meta.audio_size) ? (meta.audio_size + " · Tags") : "Cover + Tags";
      list = activeMode === "video"
        ? (meta && meta.formats && meta.formats.length ? meta.formats : [{id:"best", name:"Best", size:""}])
        : [
            {id:"mp3_320", name:"MP3 · 320k", size:audSz},
            {id:"mp3_192", name:"MP3 · 192k", size:"Balanced"},
            {id:"mp3_128", name:"MP3 · 128k", size:"Compact"},
            {id:"m4a_best", name:"M4A · Best", size:"Original + Tags"}
          ];
    }

    if(!list.length){
      list = [{id:"best", name:"Best", size:""}];
    }

    activeQuality = list[0].id;
    activeQualityLabel = list[0].name || "";
    if(meta && meta.kind === "sniffed" && list[0].url){
      activeTargetUrl = list[0].url;
      activeDirect = !!list[0].direct;
      activeAudioUrl = list[0].audio_url || "";
    } else {
      activeAudioUrl = "";
    }

    for(var i = 0; i < list.length; i++){
      (function(item, idx){
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "chip" + (idx === 0 ? " active" : "");
        var lbl = document.createElement("span");
        lbl.textContent = item.name || "Best";
        btn.appendChild(lbl);
        if(item.size){
          var sz = document.createElement("span");
          sz.className = "sz";
          sz.textContent = item.size;
          btn.appendChild(sz);
        }
        btn.onclick = function(){
          activeQuality = item.id;
          activeQualityLabel = item.name || "";
          if(meta && meta.kind === "sniffed" && item.url){
            activeTargetUrl = item.url;
            activeDirect = !!item.direct;
            activeAudioUrl = item.audio_url || "";
          }
          var all = box.querySelectorAll(".chip");
          for(var j = 0; j < all.length; j++){
            all[j].className = "chip" + (all[j] === btn ? " active" : "");
          }
        };
        box.appendChild(btn);
      })(list[i], i);
    }
  }

  function renderEntries(entries, totalCount){
    var wrap = $("entriesWrap");
    var box = $("entriesBox");
    box.innerHTML = "";
    activePlaylistItem = null;

    if(!entries || entries.length <= 1){
      wrap.style.display = "none";
      box.style.display = "none";
      $("dlBtnText").textContent = isQuickMode ? "Download in Background" : "Download";
      return;
    }

    var total = (totalCount && totalCount > entries.length) ? totalCount : entries.length;
    wrap.style.display = "block";
    box.style.display = "block";
    $("entriesLbl").textContent = "Playlist / Album (" + total + ")";

    // Option 0: All items
    var allRow = document.createElement("div");
    allRow.className = "entry-item active";
    allRow.innerHTML = '<span class="entry-idx">ALL</span><span class="entry-title">Download all ' + total + ' items</span>';
    allRow.onclick = function(){
      activePlaylistItem = "ALL";
      $("dlBtnText").textContent = "Download All (" + total + ")";
      var rows = box.querySelectorAll(".entry-item");
      for(var k = 0; k < rows.length; k++) rows[k].className = "entry-item" + (rows[k] === allRow ? " active" : "");
    };
    box.appendChild(allRow);
    activePlaylistItem = "ALL";
    $("dlBtnText").textContent = "Download All (" + total + ")";

    for(var i = 0; i < entries.length; i++){
      (function(ent, idx){
        var row = document.createElement("div");
        row.className = "entry-item";
        var idxSpan = document.createElement("span");
        idxSpan.className = "entry-idx";
        idxSpan.textContent = "#" + (idx + 1);
        var tSpan = document.createElement("span");
        tSpan.className = "entry-title";
        tSpan.textContent = ent.title || ("Item " + (idx + 1));
        row.appendChild(idxSpan);
        row.appendChild(tSpan);

        if(ent.duration){
          var dSpan = document.createElement("span");
          dSpan.className = "entry-idx";
          dSpan.textContent = fmtDur(ent.duration);
          row.appendChild(dSpan);
        }

        row.onclick = function(){
          activePlaylistItem = String(idx + 1);
          $("dlBtnText").textContent = "Download Item #" + (idx + 1);
          if(ent.thumbnail){ $("thumb").src = ent.thumbnail; $("thumbWrap").style.display = "block"; }
          var rows = box.querySelectorAll(".entry-item");
          for(var k = 0; k < rows.length; k++) rows[k].className = "entry-item" + (rows[k] === row ? " active" : "");
        };
        box.appendChild(row);
      })(entries[i], i);
    }
  }

  $("segVideo").onclick = function(){ setMode("video"); };
  $("segAudio").onclick = function(){ setMode("audio"); };

  function updateProgressUI(job){
    $("progBox").style.display = "block";
    var pct = typeof job.percent === "number" ? job.percent : 0;
    if(pct < 0) pct = 0;
    if(pct > 100) pct = 100;

    $("progStage").textContent = job.stage || "Downloading...";
    $("progPct").textContent = pct.toFixed(1).replace(/\.0$/, "") + "%";
    $("progFill").style.width = pct + "%";

    var mbText = job.downloaded_text || "0 MB";
    if(job.total_text){ mbText = mbText + " / " + job.total_text; }
    $("statMb").textContent = mbText;
    $("statSpeed").textContent = job.speed_text || "—";
    $("statEta").textContent = job.eta_text || "—";

    var running = (job.status === "starting" || job.status === "downloading" || job.status === "postprocessing");
    $("progSpin").style.display = running ? "block" : "none";
    $("cancelBtn").style.display = running ? "block" : "none";
  }

  function stopPolling(){
    if(pollTimer){ clearTimeout(pollTimer); pollTimer = null; }
  }

  function pollProgress(jobId){
    if(activeJobId !== jobId) return;
    fetch("/api/progress?id=" + encodeURIComponent(jobId), {cache: "no-store"})
      .then(function(r){ return r.json(); })
      .then(function(job){
        if(activeJobId !== jobId) return;
        updateProgressUI(job);
        if(job.status === "done"){
          stopPolling();
          var finishedId = jobId;
          activeJobId = null;
          setDownloadButtonsDisabled(false);
          var out = job.saved_to || "";
          if(job.filename) out += "/" + job.filename;
          setStatus("ok", "Saved: " + out);
          lastSavedDir = job.saved_to || "";
          var isLocalHost = (window.location.hostname === "127.0.0.1" || window.location.hostname === "localhost");
          var showOpen = isDesktop && isLocalHost;
          var showFileDl = !!job.has_file;
          var isAndroidBridge = !!(window.OmniSaveAndroid);
          if(isAndroidBridge && job.filename && job.saved_to){
            var fullNativePath = job.saved_to.replace(/\/+$/, "") + "/" + job.filename;
            $("doneActions").style.display = "flex";
            $("openFolderBtn").textContent = "Share";
            $("openFolderBtn").style.display = "inline-flex";
            $("openFolderBtn").onclick = function(){ window.OmniSaveAndroid.shareFile(fullNativePath); };
            $("syncGalleryBtn").href = "#";
            $("syncGalleryBtn").removeAttribute("download");
            $("syncGalleryBtn").textContent = "Open Video / Play";
            $("syncGalleryBtn").style.display = "flex";
            $("syncGalleryBtn").onclick = function(ev){ ev.preventDefault(); window.OmniSaveAndroid.openFile(fullNativePath); };
          } else if(showOpen || showFileDl){
            $("doneActions").style.display = "flex";
            $("openFolderBtn").textContent = "Open Folder";
            $("openFolderBtn").style.display = showOpen ? "inline-flex" : "none";
            $("openFolderBtn").onclick = function(){
              fetch("/api/open-folder", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({path: lastSavedDir})
              }).catch(function(){});
            };
            $("syncGalleryBtn").onclick = null;
            if(showFileDl){
              $("syncGalleryBtn").href = "/api/dl/file?id=" + encodeURIComponent(finishedId) + "&_t=" + Date.now();
              if(job.filename){ $("syncGalleryBtn").setAttribute("download", job.filename); }
              else { $("syncGalleryBtn").removeAttribute("download"); }
              $("syncGalleryBtn").textContent = isDesktop ? "Download File" : "Save to Gallery (WhatsApp Ready)";
              $("syncGalleryBtn").style.display = "flex";
            } else {
              $("syncGalleryBtn").style.display = "none";
            }
          }
        } else if(job.status === "error"){
          stopPolling();
          activeJobId = null;
          setDownloadButtonsDisabled(false);
          setStatus("err", job.error || "Download failed");
        } else if(job.status === "cancelled"){
          stopPolling();
          activeJobId = null;
          setDownloadButtonsDisabled(false);
          setStatus("info", "Cancelled");
        } else {
          pollTimer = setTimeout(function(){ pollProgress(jobId); }, 350);
        }
      })
      .catch(function(){
        if(activeJobId === jobId){
          pollTimer = setTimeout(function(){ pollProgress(jobId); }, 900);
        }
      });
  }

  function resetMediaState(){
    meta = null;
    activeTargetUrl = "";
    activeReferer = "";
    activeDirect = false;
    activeAudioUrl = "";
    activeQualityLabel = "";
    activeExtraCookies = "";
    activePlaylistItem = null;
    $("doneActions").style.display = "none";
    $("syncGalleryBtn").style.display = "none";
    $("syncGalleryBtn").href = "#";
    $("openFolderBtn").style.display = "none";
  }

  $("pasteBtn").onclick = function(){
    if(navigator.clipboard && navigator.clipboard.readText){
      navigator.clipboard.readText().then(function(t){
        if(t){
          resetMediaState();
          $("preview").style.display = "none";
          $("url").value = t.trim();
          $("url").focus();
        }
      }).catch(function(){ setStatus("info", "Long-press input to paste."); });
    } else {
      setStatus("info", "Long-press input to paste.");
    }
  };

  $("clearBtn").onclick = function(){
    $("url").value = "";
    resetMediaState();
    if($("quickLoginBar")) $("quickLoginBar").style.display = "none";
    $("preview").style.display = "none";
    $("progBox").style.display = "none";
    clearStatus();
    $("url").focus();
  };

  $("url").oninput = function(){
    resetMediaState();
  };

  $("url").onkeydown = function(e){
    if(e.key === "Enter"){ e.preventDefault(); $("infoBtn").click(); }
  };

  $("infoBtn").onclick = function(){
    var url = ($("url").value || "").trim();
    if(!url){ setStatus("err", "URL is required."); return; }
    resetMediaState();
    $("preview").style.display = "none";
    $("progBox").style.display = "none";
    clearStatus();
    setInspectBusy(true, "Scanning URL...");

    fetch("/api/info", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({url: url, force: false})
    })
    .then(function(r){
      return r.text().then(function(raw){
        var d = {};
        try { d = JSON.parse(raw); } catch(e) { throw new Error("Invalid server response"); }
        if(!r.ok) throw new Error(d.error || "Fetch failed");
        return d;
      });
    })
    .then(function(d){
      meta = d;
      if(d.clean_url) $("url").value = d.clean_url;
      activeTargetUrl = d.target_url || d.clean_url || url;
      activeReferer = d.referer || "";
      activeDirect = (d.kind === "direct");
      activeAudioUrl = "";
      activeExtraCookies = d.extra_cookies || "";

      $("title").textContent = d.title || "Untitled";
      $("ext").textContent = d.extractor || "Web";

      if(d.method_badge){
        $("methodBadge").style.display = "inline-block";
        $("methodBadge").textContent = d.method_badge;
      } else {
        $("methodBadge").style.display = "none";
      }

      var artistLabel = d.artist || d.uploader || "";
      if(artistLabel){ $("up").style.display = "inline-block"; $("up").textContent = artistLabel; }
      else { $("up").style.display = "none"; }
      if(d.album){ $("alb").style.display = "inline-block"; $("alb").textContent = "Album: " + d.album; }
      else { $("alb").style.display = "none"; }

      var dStr = fmtDur(d.duration);
      if(dStr){ $("dur").style.display = "block"; $("dur").textContent = dStr; }
      else { $("dur").style.display = "none"; }

      if(d.thumbnail){ $("thumb").src = d.thumbnail; $("thumbWrap").style.display = "block"; }
      else { $("thumbWrap").style.display = "none"; }

      var showFormatToggle = (d.kind === "ytdlp");
      $("formatWrap").style.display = showFormatToggle ? "block" : "none";

      renderEntries(d.entries || [], d.total_entries || 0);
      if(showFormatToggle){
        setMode(d.prefer_audio ? "audio" : "video");
      } else {
        activeMode = d.media_type === "audio" ? "audio" : "video";
        renderQualityChips();
      }
      $("preview").style.display = "block";
    })
    .catch(function(e){
      var em = e.message || String(e);
      setStatus("err", em);
      if(isQuickMode && window.OmniSaveAndroid && /login|cookie|sign in|story|stories|private|age/i.test(em)){
        $("quickLoginBar").style.display = "flex";
      }
    })
    .finally(function(){ setInspectBusy(false); });
  };

  $("dlBtn").onclick = function(){
    var url = activeTargetUrl || ($("url").value || "").trim();
    if(!url){ setStatus("err", "URL is required."); return; }
    stopPolling();
    $("doneActions").style.display = "none";
    $("syncGalleryBtn").style.display = "none";
    $("openFolderBtn").style.display = "none";
    setDownloadButtonsDisabled(true);
    clearStatus();
    updateProgressUI({
      status: "starting", stage: "Starting...", percent: 0,
      downloaded_text: "0 MB", total_text: "", speed_text: "—", eta_text: "—"
    });

    fetch("/api/download", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        url: url,
        mode: activeMode,
        quality: activeQuality,
        quality_label: activeQualityLabel,
        referer: activeReferer,
        direct: activeDirect,
        audio_url: activeAudioUrl,
        extra_cookies: activeExtraCookies,
        filename_hint: (meta && meta.title) ? meta.title : "",
        playlist_item: activePlaylistItem
      })
    })
    .then(function(r){
      return r.text().then(function(raw){
        var d = {};
        try { d = JSON.parse(raw); } catch(e) { throw new Error("Invalid server response"); }
        if(!r.ok) throw new Error(d.error || "Failed to start download");
        return d;
      });
    })
    .then(function(d){
      if(isQuickMode && window.OmniSaveAndroid && window.OmniSaveAndroid.dismissAfterDownload){
        var ttl = (meta && meta.title) ? meta.title : "Media";
        window.OmniSaveAndroid.dismissAfterDownload(ttl);
        return;
      }
      activeJobId = d.job_id;
      pollProgress(activeJobId);
    })
    .catch(function(e){
      setDownloadButtonsDisabled(false);
      $("progBox").style.display = "none";
      setStatus("err", e.message || String(e));
    });
  };

  $("cancelBtn").onclick = function(){
    if(!activeJobId) return;
    $("cancelBtn").disabled = true;
    fetch("/api/cancel", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({job_id: activeJobId})
    }).finally(function(){ $("cancelBtn").disabled = false; });
  };

  /* Settings Modal */
  $("openFolderBtn").onclick = function(){
    fetch("/api/open-folder", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({path: lastSavedDir})
    }).catch(function(){});
  };

  function applyCookieSourceUI(srcMode){
    cookieSource = srcMode;
    $("srcBrowserBtn").className = "set-seg-btn" + (srcMode === "browser" ? " active" : "");
    $("srcFileBtn").className = "set-seg-btn" + (srcMode === "file" ? " active" : "");
    if(isDesktop){
      $("ckSrcSeg").style.display = "grid";
      $("ckBrowserWrap").style.display = (srcMode === "browser") ? "block" : "none";
      $("ckManualWrap").style.display = (srcMode === "file") ? "block" : "none";
    } else {
      $("ckSrcSeg").style.display = "none";
      $("ckBrowserWrap").style.display = "none";
      if(!isAndroidApp){
        $("ckManualWrap").style.display = "block";
      }
    }
  }

  function refreshSettingsUI(){
    fetch("/api/settings", {cache: "no-store"})
      .then(function(r){ return r.json(); })
      .then(function(d){
        isDesktop = !!d.is_desktop;
        if(d.is_android_app || window.OmniSaveAndroid){
          isAndroidApp = true;
          showAndroidAccountControls();
        }
        if(isDesktop){
          $("navTabs").style.display = "none";
          switchTab("dl");
        } else {
          $("navTabs").style.display = "grid";
        }

        var syncPart = isDesktop
          ? (" · Desktop (" + (d.os_name || "PC") + ")")
          : (" · Gallery Sync " + (d.media_scan ? "Auto" : "Manual"));
        $("engVer").textContent = "yt-dlp " + (d.ytdlp_version || "unknown") + " · FFmpeg " + (d.ffmpeg ? "OK" : "Missing") + syncPart;

        $("ckToggle").checked = !!d.use_cookies;
        $("cookieDot").style.display = (d.use_cookies && d.has_cookies) ? "block" : "none";

        var srcMode = (isDesktop && d.cookie_source === "browser") ? "browser" : "file";
        applyCookieSourceUI(srcMode);

        // Populate browser dropdown labels with detected indicators
        if(isDesktop && d.detected_browsers){
          var det = d.detected_browsers;
          var sel = $("ckBrowserSel");
          var labels = {
            auto: "Auto-Detect" + (det.length ? (" (" + det.join(", ") + ")") : ""),
            firefox: "Firefox",
            edge: "Microsoft Edge",
            chrome: "Google Chrome",
            brave: "Brave",
            opera: "Opera",
            vivaldi: "Vivaldi",
            chromium: "Chromium"
          };
          for(var i = 0; i < sel.options.length; i++){
            var val = sel.options[i].value;
            var base = labels[val] || val;
            if(val !== "auto" && det.indexOf(val) !== -1){
              sel.options[i].text = base + " ✓";
            } else {
              sel.options[i].text = base;
            }
          }
          sel.value = d.cookie_browser || "auto";
          if(document.activeElement !== $("ckProfileInp")){
            $("ckProfileInp").value = d.cookie_profile || "";
          }
        }

        var fbTag = d.fb_cookies ? " · Facebook Ready" : "";
        if(srcMode === "browser"){
          if(d.browser_active){
            var cntTxt = d.cookie_lines > 0 ? (d.cookie_lines + " entries" + fbTag) : "CLI Direct Mode";
            $("ckInfo").textContent = "Browser: " + d.browser_active + " (" + cntTxt + ")" + (d.use_cookies ? "" : " · Paused");
          } else {
            $("ckInfo").textContent = "Reads cookies automatically from your installed desktop browser.";
          }
          if(d.browser_error){
            $("ckBrowserWarn").textContent = d.browser_error;
            $("ckBrowserWarn").style.display = "block";
          } else {
            $("ckBrowserWarn").style.display = "none";
          }
        } else {
          $("ckBrowserWarn").style.display = "none";
          $("ckInfo").textContent = d.has_cookies
            ? ("Loaded (" + d.cookie_lines + " entries" + fbTag + ")." + (d.use_cookies ? "" : " Toggle ON to activate."))
            : "Unlocks Facebook Stories, private Instagram, TikTok & age-gated YouTube.";
        }
        if(d.save_dir){
          $("saveDirTxt").textContent = d.save_dir + (d.is_custom_save_dir ? " (Custom)" : " (Default)");
          if(document.activeElement !== $("saveDirInp")){
            $("saveDirInp").value = d.save_dir;
          }
        }
        var isLocalPc = isDesktop && (window.location.hostname === "127.0.0.1" || window.location.hostname === "localhost");
        $("dirBrowseBtn").style.display = isLocalPc ? "inline-flex" : "none";
        $("dirOpenBtn").style.display = isLocalPc ? "inline-flex" : "none";
      })
      .catch(function(){});
  }

  $("settingsBtn").onclick = function(){
    setSetStatus("", "");
    $("setModal").className = "modal open";
    refreshSettingsUI();
  };
  $("setClose").onclick = function(){ $("setModal").className = "modal"; };
  $("setModal").onclick = function(e){ if(e.target === $("setModal")) $("setModal").className = "modal"; };

  $("updBtn").onclick = function(){
    $("updBtn").disabled = true;
    setSetStatus("info", "Updating yt-dlp...");
    fetch("/api/engine/update", {method: "POST"})
      .then(function(r){ return r.json().then(function(d){ if(!r.ok) throw new Error(d.error || "Update failed"); return d; }); })
      .then(function(d){
        setSetStatus("ok", "Updated: yt-dlp " + (d.ytdlp_version || ""));
        refreshSettingsUI();
      })
      .catch(function(e){ setSetStatus("err", e.message || String(e)); })
      .finally(function(){ $("updBtn").disabled = false; });
  };

  $("ckToggle").onchange = function(){
    fetch("/api/cookies", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action: "toggle", use_cookies: $("ckToggle").checked})
    })
    .then(function(){ refreshSettingsUI(); })
    .catch(function(){});
  };

  function saveBrowserSettings(triggerSync){
    $("ckSyncBtn").disabled = true;
    if(triggerSync) setSetStatus("info", "Reading browser cookies...");
    fetch("/api/cookies", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        action: triggerSync ? "browser_sync" : "set_source",
        cookie_source: "browser",
        cookie_browser: $("ckBrowserSel").value || "auto",
        cookie_profile: ($("ckProfileInp").value || "").trim(),
        use_cookies: true
      })
    })
    .then(function(r){ return r.json().then(function(d){ if(!r.ok) throw new Error(d.error || "Sync failed"); return d; }); })
    .then(function(d){
      if(d.browser_error){
        setSetStatus("err", d.browser_error);
      } else if(triggerSync){
        var extra = d.cookie_lines > 0 ? (" (" + d.cookie_lines + " entries)") : " (CLI mode)";
        setSetStatus("ok", "Synced from " + (d.browser_active || "browser") + extra);
      }
      refreshSettingsUI();
    })
    .catch(function(e){ setSetStatus("err", e.message || String(e)); })
    .finally(function(){ $("ckSyncBtn").disabled = false; });
  }

  $("srcBrowserBtn").onclick = function(){
    applyCookieSourceUI("browser");
    saveBrowserSettings(true);
  };
  $("srcFileBtn").onclick = function(){
    applyCookieSourceUI("file");
    fetch("/api/cookies", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action: "set_source", cookie_source: "file"})
    }).then(function(){ refreshSettingsUI(); }).catch(function(){});
  };
  $("ckBrowserSel").onchange = function(){ saveBrowserSettings(true); };
  $("ckSyncBtn").onclick = function(){ saveBrowserSettings(true); };

  $("ckUploadBtn").onclick = function(){ $("ckFile").click(); };
  $("ckFile").onchange = function(){
    var f = $("ckFile").files && $("ckFile").files[0];
    if(!f) return;
    var reader = new FileReader();
    reader.onload = function(){ $("ckArea").value = reader.result || ""; };
    reader.readAsText(f);
  };

  $("ckSaveBtn").onclick = function(){
    var txt = ($("ckArea").value || "").trim();
    if(!txt){ setSetStatus("err", "Paste or load cookies.txt first."); return; }
    fetch("/api/cookies", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action: "save", content: txt, use_cookies: true, cookie_source: "file"})
    })
    .then(function(r){ return r.json().then(function(d){ if(!r.ok) throw new Error(d.error || "Failed"); return d; }); })
    .then(function(d){
      $("ckArea").value = "";
      setSetStatus("ok", "Cookies saved (" + d.cookie_lines + " entries).");
      refreshSettingsUI();
    })
    .catch(function(e){ setSetStatus("err", e.message || String(e)); });
  };

  $("ckClearBtn").onclick = function(){
    fetch("/api/cookies", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action: "clear"})
    })
    .then(function(){
      $("ckArea").value = "";
      setSetStatus("info", "Cookies cleared.");
      refreshSettingsUI();
    })
    .catch(function(){});
  };

  function saveCustomDir(){
    var p = ($("saveDirInp").value || "").trim();
    if(!p){ setSetStatus("err", "Enter a folder path first."); return; }
    $("dirSaveBtn").disabled = true;
    fetch("/api/save-dir", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action: "save", path: p})
    })
    .then(function(r){ return r.json().then(function(d){ if(!r.ok) throw new Error(d.error || "Failed to save folder"); return d; }); })
    .then(function(d){
      $("saveDirInp").value = d.save_dir || p;
      setSetStatus("ok", "Save folder updated: " + (d.save_dir || p));
      refreshSettingsUI();
    })
    .catch(function(e){ setSetStatus("err", e.message || String(e)); })
    .finally(function(){ $("dirSaveBtn").disabled = false; });
  }

  $("dirSaveBtn").onclick = saveCustomDir;
  $("saveDirInp").onkeydown = function(e){
    if(e.key === "Enter"){ e.preventDefault(); saveCustomDir(); }
  };

  $("dirResetBtn").onclick = function(){
    $("dirResetBtn").disabled = true;
    fetch("/api/save-dir", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action: "reset"})
    })
    .then(function(r){ return r.json().then(function(d){ if(!r.ok) throw new Error(d.error || "Failed"); return d; }); })
    .then(function(d){
      $("saveDirInp").value = d.save_dir || "";
      setSetStatus("info", "Reset to default: " + (d.save_dir || ""));
      refreshSettingsUI();
    })
    .catch(function(e){ setSetStatus("err", e.message || String(e)); })
    .finally(function(){ $("dirResetBtn").disabled = false; });
  };

  $("dirBrowseBtn").onclick = function(){
    $("dirBrowseBtn").disabled = true;
    setSetStatus("info", "Choose a folder in the popup window...");
    fetch("/api/save-dir", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action: "browse"})
    })
    .then(function(r){ return r.json().then(function(d){ if(!r.ok) throw new Error(d.error || "Folder picker unavailable"); return d; }); })
    .then(function(d){
      if(d.cancelled){
        setSetStatus("", "");
      } else {
        $("saveDirInp").value = d.save_dir || "";
        setSetStatus("ok", "Save folder updated: " + (d.save_dir || ""));
        refreshSettingsUI();
      }
    })
    .catch(function(e){ setSetStatus("err", e.message || String(e)); })
    .finally(function(){ $("dirBrowseBtn").disabled = false; });
  };

  $("dirOpenBtn").onclick = function(){
    fetch("/api/open-folder", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({path: ($("saveDirInp").value || "").trim()})
    }).catch(function(){});
  };

  /* WhatsApp Status Logic */
  function updateWaCountBadge(){
    var c = $("waCount");
    if(waItems.length > 0){ c.textContent = String(waItems.length); c.style.display = "inline-block"; }
    else { c.style.display = "none"; }
    var unsaved = waItems.filter(function(it){ return !it.saved; }).length;
    var btn = $("waSaveAllBtn");
    if(unsaved > 0){ btn.style.display = "inline-flex"; btn.textContent = "Save All (" + unsaved + ")"; }
    else { btn.style.display = "none"; }
  }

  function renderWaGrid(extraState){
    var grid = $("waGrid");
    var empty = $("waEmpty");
    grid.innerHTML = "";
    var list = waItems.filter(function(it){ return waFilter === "all" || it.type === waFilter; });
    updateWaCountBadge();

    if(list.length === 0){
      empty.style.display = "block";
      if(extraState && extraState.permission_denied){
        empty.innerHTML = "<strong>Storage Permission Required</strong>Grant <b>All files access</b> to Termux in Android Settings, then tap Refresh.";
      } else if(waItems.length === 0){
        empty.innerHTML = "<strong>No Statuses Found</strong>View statuses inside WhatsApp first so they cache locally, then tap Refresh.";
      } else {
        empty.innerHTML = "<strong>No Matching Media</strong>No items in this category.";
      }
      return;
    }

    empty.style.display = "none";
    for(var i = 0; i < list.length; i++){
      (function(item){
        var card = document.createElement("div");
        card.className = "wa-card";
        var mediaUrl = "/api/wa/media?id=" + encodeURIComponent(item.id);
        if(item.type === "video"){
          var vid = document.createElement("video");
          vid.src = mediaUrl + "#t=0.2";
          vid.muted = true; vid.playsInline = true; vid.preload = "metadata";
          card.appendChild(vid);
          var play = document.createElement("div");
          play.className = "wa-play";
          play.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><polygon points="6 4 20 12 6 20 6 4"/></svg>';
          card.appendChild(play);
        } else {
          var img = document.createElement("img");
          img.src = mediaUrl; img.loading = "lazy"; img.alt = "Status";
          card.appendChild(img);
        }
        var top = document.createElement("div");
        top.className = "wa-top-badges";
        top.innerHTML = '<span class="wa-pill">' + (item.type === "video" ? "VIDEO" : "PHOTO") + '</span><span class="wa-pill">' + item.age + '</span>';
        card.appendChild(top);

        var foot = document.createElement("div");
        foot.className = "wa-foot";
        var sz = document.createElement("span");
        sz.className = "wa-sz"; sz.textContent = item.size;
        foot.appendChild(sz);

        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "wa-save-btn" + (item.saved ? " saved" : "");
        btn.textContent = item.saved ? "Saved ✓" : "Save";
        btn.onclick = function(ev){ ev.stopPropagation(); saveSingleStatus(item, btn); };
        foot.appendChild(btn);

        card.appendChild(foot);
        card.onclick = function(){ openWaModal(item); };
        grid.appendChild(card);
      })(list[i]);
    }
  }

  function saveSingleStatus(item, btnEl){
    if(btnEl) btnEl.disabled = true;
    fetch("/api/wa/save", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({id: item.id})
    })
    .then(function(r){ return r.json().then(function(d){ if(!r.ok) throw new Error(d.error || "Save failed"); return d; }); })
    .then(function(d){
      item.saved = true;
      if(btnEl){ btnEl.className = "wa-save-btn saved"; btnEl.textContent = "Saved ✓"; }
      if(modalItem && modalItem.id === item.id){
        $("modalSave").className = "modal-save saved";
        $("modalSave").textContent = "Saved to Gallery ✓";
      }
      updateWaCountBadge();
      setWaStatus("ok", "Saved: " + d.saved_to);
    })
    .catch(function(e){ setWaStatus("err", e.message || String(e)); })
    .finally(function(){ if(btnEl) btnEl.disabled = false; });
  }

  function loadWaStatuses(){
    waLoadedOnce = true;
    setWaStatus("", "");
    $("waLoader").style.display = "block";
    $("waEmpty").style.display = "none";
    fetch("/api/wa/list", {cache: "no-store"})
      .then(function(r){ return r.json(); })
      .then(function(d){ waItems = d.items || []; renderWaGrid(d); })
      .catch(function(e){ setWaStatus("err", e.message || String(e)); })
      .finally(function(){ $("waLoader").style.display = "none"; });
  }

  var fBtns = document.querySelectorAll(".f-pill");
  for(var i = 0; i < fBtns.length; i++){
    (function(b){
      b.onclick = function(){
        waFilter = b.getAttribute("data-filter") || "all";
        for(var j = 0; j < fBtns.length; j++){
          fBtns[j].className = "f-pill" + (fBtns[j] === b ? " active" : "");
        }
        renderWaGrid();
      };
    })(fBtns[i]);
  }

  $("waRefreshBtn").onclick = loadWaStatuses;
  $("waSaveAllBtn").onclick = function(){
    var unsavedIds = waItems.filter(function(it){ return !it.saved; }).map(function(it){ return it.id; });
    if(!unsavedIds.length) return;
    $("waSaveAllBtn").disabled = true;
    fetch("/api/wa/save", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ids: unsavedIds})
    })
    .then(function(r){ return r.json().then(function(d){ if(!r.ok) throw new Error(d.error || "Save failed"); return d; }); })
    .then(function(d){
      for(var k = 0; k < waItems.length; k++){ waItems[k].saved = true; }
      renderWaGrid();
      setWaStatus("ok", "Saved " + d.count + " file(s) to " + d.saved_to);
    })
    .catch(function(e){ setWaStatus("err", e.message || String(e)); })
    .finally(function(){ $("waSaveAllBtn").disabled = false; });
  };

  function openWaModal(item){
    modalItem = item;
    $("modalMeta").textContent = (item.source || "WhatsApp") + " · " + item.size + " · " + item.age;
    var body = $("modalBody");
    body.innerHTML = "";
    var url = "/api/wa/media?id=" + encodeURIComponent(item.id);
    if(item.type === "video"){
      var v = document.createElement("video");
      v.src = url; v.controls = true; v.autoplay = true; v.playsInline = true;
      body.appendChild(v);
    } else {
      var im = document.createElement("img");
      im.src = url; im.alt = "Status";
      body.appendChild(im);
    }
    $("modalSave").className = "modal-save" + (item.saved ? " saved" : "");
    $("modalSave").textContent = item.saved ? "Saved to Gallery ✓" : "Save to Gallery";
    $("modalDl").href = url + "&download=1";
    $("waModal").className = "modal open";
  }

  function closeWaModal(){
    $("waModal").className = "modal";
    $("modalBody").innerHTML = "";
    modalItem = null;
  }

  $("modalClose").onclick = closeWaModal;
  $("waModal").onclick = function(e){ if(e.target === $("waModal")) closeWaModal(); };
  $("modalSave").onclick = function(){
    if(!modalItem) return;
    saveSingleStatus(modalItem, null);
    renderWaGrid();
  };

  var androidControlsBound = false;
  function showAndroidAccountControls(){
    var aw = $("ckAndroidWrap");
    if(aw) aw.style.display = "block";
    var ab = $("acctBtn");
    if(ab) ab.style.display = "grid";
    var tm = $("toggleManualCkBtn");
    if(tm && !androidControlsBound){
      tm.style.display = "inline-block";
      $("ckManualWrap").style.display = "none";
      tm.onclick = function(){
        var cur = $("ckManualWrap").style.display;
        $("ckManualWrap").style.display = (cur === "none") ? "block" : "none";
      };
    }
    if(androidControlsBound) return;
    androidControlsBound = true;
    if(ab){
      ab.onclick = function(){
        setSetStatus("", "");
        $("setModal").className = "modal open";
        refreshSettingsUI();
      };
    }
    var lBtns = document.querySelectorAll(".android-login-btn, .quick-site-login");
    for(var li = 0; li < lBtns.length; li++){
      (function(btn){
        btn.onclick = function(){
          var site = btn.getAttribute("data-site") || "facebook";
          if(window.OmniSaveAndroid && window.OmniSaveAndroid.openCookieLogin){
            window.OmniSaveAndroid.openCookieLogin(site);
          }
        };
      })(lBtns[li]);
    }
    window.__onOmniCookiesSynced = function(cnt){
      setSetStatus("ok", "Synced " + cnt + " session cookies from login!");
      refreshSettingsUI();
      if(isQuickMode && ($("url").value || "").trim()){
        $("quickLoginBar").style.display = "none";
        $("infoBtn").click();
      }
    };
  }

  if(window.OmniSaveAndroid){
    showAndroidAccountControls();
  }
  if($("quickCloseBtn")){
    $("quickCloseBtn").onclick = function(){
      if(window.OmniSaveAndroid && window.OmniSaveAndroid.closeSheet){
        window.OmniSaveAndroid.closeSheet();
      }
    };
  }
  if($("quickFullBtn")){
    $("quickFullBtn").onclick = function(){
      if(window.OmniSaveAndroid && window.OmniSaveAndroid.openFullApp){
        window.OmniSaveAndroid.openFullApp(($("url").value || "").trim());
      }
    };
  }

  // Init quiet status checks
  refreshSettingsUI();
  fetch("/api/wa/list", {cache: "no-store"})
    .then(function(r){ return r.json(); })
    .then(function(d){ if(d && d.items){ waItems = d.items; updateWaCountBadge(); } })
    .catch(function(){});

  // Auto-load & fetch shared URL when opened via Share -> Termux (?url=...)
  try {
    var qs = new URLSearchParams(window.location.search || "");
    var sharedUrl = (qs.get("url") || qs.get("text") || qs.get("q") || "").trim();
    if(sharedUrl){
      switchTab("dl");
      $("url").value = sharedUrl;
      if(window.history && window.history.replaceState){
        window.history.replaceState({}, document.title, "/");
      }
      setTimeout(function(){ $("infoBtn").click(); }, 60);
    }
  } catch(e) {}
})();
</script>
</body>
</html>
"""


def clean_env():
    env = os.environ.copy()
    for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
        env.pop(k, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def sanitize_netscape_cookies(raw_text: str) -> str:
    out = [
        "# Netscape HTTP Cookie File",
        "# https://curl.haxx.se/rfc/cookie_spec.html",
        "# This is a generated file! Do not edit.",
        "",
    ]
    for line in (raw_text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split("\t")
        if len(parts) < 7:
            parts = [p.strip() for p in re.split(r"\s{2,}|\t", s)]
        if len(parts) < 7:
            continue
        domain = parts[0].strip()
        flag = parts[1].strip().upper()
        path = parts[2].strip() or "/"
        secure = parts[3].strip().upper()
        expiry = parts[4].strip()
        name = parts[5].strip()
        val = "\t".join(parts[6:]).strip()

        if not domain or not name:
            continue
        if flag not in ("TRUE", "FALSE"):
            flag = "TRUE" if domain.startswith(".") else "FALSE"
        if domain.startswith(".") and flag != "TRUE":
            flag = "TRUE"
        if secure not in ("TRUE", "FALSE"):
            secure = "FALSE"
        try:
            exp_int = int(float(expiry))
            if exp_int < 0:
                exp_int = 0
        except Exception:
            exp_int = 0

        out.append(f"{domain}\t{flag}\t{path}\t{secure}\t{exp_int}\t{name}\t{val}")
    return "\n".join(out) + "\n"


def _atomic_write_text(dest_path: Path, text: str):
    tmp_p = dest_path.with_name(f".{dest_path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp_p.write_text(text, encoding="utf-8")
    os.replace(tmp_p, dest_path)


def _move_file_safely(src_file: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / src_file.name
    if target.exists():
        stem, suf = src_file.stem, src_file.suffix
        idx = 1
        while (dest_dir / f"{stem} ({idx}){suf}").exists():
            idx += 1
        target = dest_dir / f"{stem} ({idx}){suf}"
    shutil.move(str(src_file), str(target))
    return target


def _rescue_staged_files_to_downloads(only_done_jobs: bool = False) -> int:
    """Safely move any un-handed-off completed files from STAGE_DIR into Downloads so zero files are ever stranded."""
    if not STAGE_DIR.exists():
        return 0
    rescued = 0
    default_dl = get_base_download_dir()

    if only_done_jobs:
        with JOBS_LOCK:
            candidates = [
                (jid, j.get("_stage_dir") or "", j.get("saved_to") or str(default_dl))
                for jid, j in JOBS.items()
                if j.get("status") == "done" and j.get("has_file") and j.get("_stage_dir")
            ]
        for jid, sd_str, target_dir_str in candidates:
            sd = Path(sd_str)
            if not sd.exists():
                continue
            dest_dir = Path(target_dir_str) if target_dir_str else default_dl
            try:
                for f in list(sd.iterdir()):
                    if f.is_file() and not f.name.startswith(".") and not f.name.endswith((".part", ".ytdl", ".temp")):
                        moved = _move_file_safely(f, dest_dir)
                        _update_job(
                            jid,
                            saved_to=str(moved.parent),
                            filename=moved.name,
                            _filepath=str(moved),
                            _stage_dir="",
                            via_chrome=False,
                            has_file=True,
                        )
                        rescued += 1
                shutil.rmtree(sd, ignore_errors=True)
            except Exception:
                pass
        return rescued

    # Startup sweep: rescue any completed leftover media files from previous runs into Downloads
    try:
        for child in list(STAGE_DIR.iterdir()):
            try:
                if child.is_dir():
                    for f in list(child.iterdir()):
                        if f.is_file() and not f.name.startswith(".") and not f.name.endswith((".part", ".ytdl", ".temp")):
                            if f.stat().st_size > 0:
                                _move_file_safely(f, default_dl)
                                rescued += 1
                    shutil.rmtree(child, ignore_errors=True)
                elif child.is_file() and not child.name.startswith(".") and not child.name.endswith((".part", ".ytdl", ".temp")):
                    if child.stat().st_size > 0:
                        _move_file_safely(child, default_dl)
                        rescued += 1
            except Exception:
                pass
    except Exception:
        pass
    # Also sweep any leftover .omni_tmp_* directories inside Downloads (e.g. after force-close on Termux)
    try:
        if default_dl.exists():
            for child in list(default_dl.iterdir()):
                if child.is_dir() and child.name.startswith(".omni_tmp_"):
                    try:
                        primary_left = _pick_primary_staged_file(child)
                        if primary_left is not None and primary_left.stat().st_size > 0:
                            _move_file_safely(primary_left, default_dl)
                            rescued += 1
                    except Exception:
                        pass
                    shutil.rmtree(child, ignore_errors=True)
    except Exception:
        pass
    return rescued


def merge_extension_cookies(chrome_cookies: list, dest_path: Path = None) -> int:
    """Merge Chrome extension cookie objects into Netscape COOKIES_FILE atomically by (domain, path, name)."""
    if dest_path is None:
        dest_path = COOKIES_FILE
    with COOKIES_LOCK:
        existing = {}
        if dest_path.exists():
            try:
                for ln in dest_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    s = ln.strip()
                    if not s or s.startswith("#"):
                        continue
                    parts = s.split("\t")
                    if len(parts) < 7:
                        continue
                    dom = parts[0].strip()
                    pth = parts[2].strip() or "/"
                    nm = parts[5].strip()
                    if dom and nm:
                        existing[(dom.lower(), pth, nm)] = (dom, parts[1].strip().upper(), pth, parts[3].strip().upper(), parts[4].strip(), nm, "\t".join(parts[6:]).strip())
            except Exception:
                pass

        added = 0
        for c in (chrome_cookies or []):
            if not isinstance(c, dict):
                continue
            dom = str(c.get("domain") or "").strip()
            nm = str(c.get("name") or "").strip()
            if not dom or not nm:
                continue
            if not c.get("hostOnly") and not dom.startswith("."):
                dom = "." + dom
            flag = "TRUE" if dom.startswith(".") else "FALSE"
            pth = str(c.get("path") or "/").strip() or "/"
            sec = "TRUE" if c.get("secure") else "FALSE"
            exp_raw = c.get("expirationDate")
            try:
                exp_int = int(float(exp_raw)) if exp_raw else 0
                if exp_int < 0:
                    exp_int = 0
            except Exception:
                exp_int = 0
            val = str(c.get("value") or "").replace("\r", "").replace("\n", "").replace("\t", " ")
            existing[(dom.lower(), pth, nm)] = (dom, flag, pth, sec, str(exp_int), nm, val)
            added += 1

        out_lines = [
            "# Netscape HTTP Cookie File",
            "# https://curl.haxx.se/rfc/cookie_spec.html",
            "# Synced locally via OmniSave Chrome Extension",
            "",
        ]
        for tup in existing.values():
            out_lines.append("\t".join(tup))
        new_blob = "\n".join(out_lines) + "\n"
        try:
            if dest_path.exists() and dest_path.read_text(encoding="utf-8", errors="ignore") == new_blob:
                return added
        except Exception:
            pass
        _atomic_write_text(dest_path, new_blob)
        _clear_info_cache()
        return added


def _is_android_app() -> bool:
    return os.environ.get("OMNISAVE_ANDROID_APP") == "1"


def _is_termux_env() -> bool:
    if _is_android_app():
        return False
    return (
        os.environ.get("PREFIX", "").startswith("/data/data/com.termux")
        or Path("/data/data/com.termux/files/usr").exists()
    )


def _is_mobile_env() -> bool:
    return _is_android_app() or _is_termux_env()


def _is_desktop_env() -> bool:
    return not _is_mobile_env()


def _setup_android_ffmpeg() -> str:
    global _ANDROID_FFMPEG_DIR
    native_dir_s = (os.environ.get("OMNISAVE_NATIVE_LIB_DIR") or "").strip()
    if not native_dir_s:
        return ""
    native_dir = Path(native_dir_s)
    if not native_dir.exists():
        return ""

    # Ensure shared libraries bundled in nativeLibraryDir are discoverable by libffmpeg.so
    cur_ld = os.environ.get("LD_LIBRARY_PATH") or ""
    if native_dir_s not in cur_ld.split(":"):
        os.environ["LD_LIBRARY_PATH"] = f"{native_dir_s}:{cur_ld}" if cur_ld else native_dir_s

    bin_dir = APP_DATA_DIR / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    linked_any = False
    for bin_name, so_name in (("ffmpeg", "libffmpeg.so"), ("ffprobe", "libffprobe.so")):
        src_so = native_dir / so_name
        if not src_so.exists():
            continue
        dst_link = bin_dir / bin_name
        try:
            if dst_link.is_symlink() or dst_link.exists():
                dst_link.unlink()
            os.symlink(str(src_so), str(dst_link))
            linked_any = True
        except Exception:
            pass

    if linked_any:
        bin_s = str(bin_dir)
        cur_path = os.environ.get("PATH") or ""
        if bin_s not in cur_path.split(os.pathsep):
            os.environ["PATH"] = f"{bin_s}{os.pathsep}{cur_path}" if cur_path else bin_s
        _ANDROID_FFMPEG_DIR = bin_s
        return bin_s
    return ""


def _get_ffmpeg_location() -> str:
    if _ANDROID_FFMPEG_DIR and (Path(_ANDROID_FFMPEG_DIR) / "ffmpeg").exists():
        return _ANDROID_FFMPEG_DIR
    ff = shutil.which("ffmpeg")
    if ff:
        return str(Path(ff).parent)
    return ""


def _get_android_bridge():
    if not _is_android_app():
        return None
    try:
        from com.omnisave.studio import AndroidBridge
        return AndroidBridge
    except Exception:
        return None


def _notify_android_progress(title: str, stage: str, percent: float, active: bool):
    br = _get_android_bridge()
    if br is None:
        return
    try:
        br.onDownloadProgress(str(title or ""), str(stage or ""), int(round(percent or 0)), bool(active))
    except Exception:
        pass


def _notify_android_complete(title: str, filepath: str):
    br = _get_android_bridge()
    if br is None or not filepath:
        return
    try:
        br.onDownloadComplete(str(title or ""), str(filepath))
    except Exception:
        pass


def detect_installed_browsers() -> list:
    if not _is_desktop_env():
        return []

    found = []
    home = Path.home()
    sys_name = sys.platform

    if sys_name == "win32":
        local_app = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData/Local"))
        roaming_app = Path(os.environ.get("APPDATA") or (home / "AppData/Roaming"))
        checks = [
            ("firefox", [roaming_app / "Mozilla/Firefox/Profiles"]),
            ("edge", [local_app / "Microsoft/Edge/User Data"]),
            ("chrome", [local_app / "Google/Chrome/User Data"]),
            ("brave", [local_app / "BraveSoftware/Brave-Browser/User Data"]),
            ("opera", [roaming_app / "Opera Software/Opera Stable", roaming_app / "Opera Software/Opera GX Stable"]),
            ("vivaldi", [local_app / "Vivaldi/User Data"]),
            ("chromium", [local_app / "Chromium/User Data"]),
        ]
    elif sys_name == "darwin":
        app_sup = home / "Library/Application Support"
        checks = [
            ("firefox", [app_sup / "Firefox/Profiles"]),
            ("chrome", [app_sup / "Google/Chrome"]),
            ("edge", [app_sup / "Microsoft Edge"]),
            ("brave", [app_sup / "BraveSoftware/Brave-Browser"]),
            ("opera", [app_sup / "com.operasoftware.Opera"]),
            ("vivaldi", [app_sup / "Vivaldi"]),
            ("chromium", [app_sup / "Chromium"]),
        ]
    else:
        cfg_dir = home / ".config"
        checks = [
            ("firefox", [home / ".mozilla/firefox", home / "snap/firefox/common/.mozilla/firefox"]),
            ("chrome", [cfg_dir / "google-chrome"]),
            ("edge", [cfg_dir / "microsoft-edge"]),
            ("brave", [cfg_dir / "BraveSoftware/Brave-Browser"]),
            ("opera", [cfg_dir / "opera"]),
            ("vivaldi", [cfg_dir / "vivaldi"]),
            ("chromium", [cfg_dir / "chromium"]),
        ]

    for bname, paths in checks:
        for p in paths:
            try:
                if p.exists():
                    found.append(bname)
                    break
            except OSError:
                continue
    return found


def load_config() -> dict:
    default_src = "browser" if _is_desktop_env() else "file"
    defaults = {
        "use_cookies": True,
        "cookie_source": default_src,
        "cookie_browser": "auto",
        "cookie_profile": "",
        "save_dir": "",
    }
    try:
        if CONFIG_FILE.exists():
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                defaults.update(saved)
    except Exception:
        pass
    if not _is_desktop_env():
        defaults["cookie_source"] = "file"
    if defaults.get("cookie_source") not in ("browser", "file"):
        defaults["cookie_source"] = default_src
    if defaults.get("cookie_browser") not in ("auto",) + SUPPORTED_BROWSERS:
        defaults["cookie_browser"] = "auto"
    return defaults


def save_config(cfg: dict):
    try:
        CONFIG_FILE.write_text(json.dumps(cfg), encoding="utf-8")
    except Exception:
        pass


def _write_cookiejar_netscape(jar, dest_path: Path) -> int:
    lines = [
        "# Netscape HTTP Cookie File",
        "# https://curl.haxx.se/rfc/cookie_spec.html",
        "# Auto-synced from desktop browser by OmniSave Studio",
        "",
    ]
    count = 0
    for c in jar:
        domain = (getattr(c, "domain", "") or "").strip()
        name = (getattr(c, "name", "") or "").strip()
        if not domain or not name:
            continue
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        path = (getattr(c, "path", "/") or "/").strip() or "/"
        secure = "TRUE" if getattr(c, "secure", False) else "FALSE"
        exp = getattr(c, "expires", None)
        try:
            exp_int = int(exp) if (exp is not None and int(exp) > 0) else 0
        except Exception:
            exp_int = 0
        val = (getattr(c, "value", "") or "").replace("\r", "").replace("\n", "")
        lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{exp_int}\t{name}\t{val}")
        count += 1
    dest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return count


def _friendly_browser_error(browser_name: str, raw_err: str) -> str:
    low = (raw_err or "").lower()
    b_title = browser_name.capitalize() if browser_name else "Browser"
    if any(k in low for k in ("could not copy", "permission denied", "database is locked", "being used by another process")):
        return (
            f"{b_title} is currently locking its cookie database on Windows. "
            f"Close {b_title} completely and click Sync, or select Firefox / Manual .txt."
        )
    if any(k in low for k in ("dpapi", "app-bound", "decrypt", "v20", "cryptunprotectdata")):
        return (
            f"{b_title} uses Windows App-Bound cookie encryption that blocks direct reading. "
            f"Switch to Firefox (works seamlessly), or export cookies.txt via 'Get cookies.txt LOCALLY' in Manual .txt mode."
        )
    if "could not find" in low or "no such file" in low:
        return f"Could not find {b_title} profile cookies on this PC."
    return f"{b_title}: {raw_err}"


def sync_browser_cookies(force: bool = False, browser_override: str = None, profile_override: str = None) -> dict:
    if not _is_desktop_env():
        return {"ok": False, "browser": "", "lines": 0, "error": "Desktop only", "cli_fallback": False}

    cfg = load_config()
    pref = (browser_override if browser_override is not None else cfg.get("cookie_browser") or "auto").strip().lower()
    profile = (profile_override if profile_override is not None else cfg.get("cookie_profile") or "").strip()

    with BROWSER_CK_LOCK:
        now = time.time()
        if (
            not force
            and BROWSER_CK_STATE["synced_at"] > 0
            and (now - BROWSER_CK_STATE["synced_at"]) < BROWSER_COOKIE_TTL
            and BROWSER_CK_STATE["profile"] == profile
            and (pref == "auto" or BROWSER_CK_STATE["browser"] == pref)
            and (BROWSER_CK_STATE["lines"] > 0 or BROWSER_CK_STATE["cli_fallback"])
        ):
            return {
                "ok": True,
                "browser": BROWSER_CK_STATE["browser"],
                "lines": BROWSER_CK_STATE["lines"],
                "error": BROWSER_CK_STATE["error"],
                "cli_fallback": BROWSER_CK_STATE["cli_fallback"],
            }

        detected = detect_installed_browsers()
        if pref == "auto":
            candidates = detected if detected else list(SUPPORTED_BROWSERS)
        else:
            candidates = [pref]

        # Try in-process yt_dlp Python API first (fast, feeds both yt-dlp and custom FB Story/Sniffer)
        try:
            from yt_dlp.cookies import extract_cookies_from_browser
        except Exception:
            chosen = candidates[0] if candidates else ""
            BROWSER_CK_STATE.update({
                "browser": chosen,
                "profile": profile,
                "synced_at": now,
                "lines": 0,
                "error": "" if chosen else "No supported desktop browser detected.",
                "cli_fallback": bool(chosen),
            })
            return {
                "ok": bool(chosen),
                "browser": chosen,
                "lines": 0,
                "error": BROWSER_CK_STATE["error"],
                "cli_fallback": bool(chosen),
            }

        last_err = ""
        for bname in candidates:
            try:
                jar = extract_cookies_from_browser(bname, profile=profile or None)
                cnt = _write_cookiejar_netscape(jar, BROWSER_COOKIES_FILE)
                if cnt > 0:
                    BROWSER_CK_STATE.update({
                        "browser": bname,
                        "profile": profile,
                        "synced_at": now,
                        "lines": cnt,
                        "error": "",
                        "cli_fallback": False,
                    })
                    return {"ok": True, "browser": bname, "lines": cnt, "error": "", "cli_fallback": False}
                last_err = f"{bname.capitalize()} profile had 0 cookies."
            except Exception as exc:
                last_err = _friendly_browser_error(bname, str(exc))

        BROWSER_CK_STATE.update({
            "browser": "",
            "profile": profile,
            "synced_at": now,
            "lines": 0,
            "error": last_err or "No readable browser cookies found.",
            "cli_fallback": False,
        })
        return {"ok": False, "browser": "", "lines": 0, "error": BROWSER_CK_STATE["error"], "cli_fallback": False}


def _active_cookie_file_path() -> Path:
    cfg = load_config()
    if _is_desktop_env() and cfg.get("cookie_source") == "browser":
        return BROWSER_COOKIES_FILE
    return COOKIES_FILE


def _count_cookie_lines(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
        return sum(1 for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith("#") and "\t" in ln)
    except Exception:
        return 0


def get_ytdlp_cookie_cli_args(use_cookies: bool) -> list:
    if not use_cookies:
        return []
    cfg = load_config()
    if _is_desktop_env() and cfg.get("cookie_source") == "browser":
        res = sync_browser_cookies(force=False)
        if res.get("ok") and not res.get("cli_fallback") and BROWSER_COOKIES_FILE.exists() and res.get("lines", 0) > 0:
            return ["--cookies", str(BROWSER_COOKIES_FILE)]
        if res.get("cli_fallback") and res.get("browser"):
            spec = res["browser"]
            prof = (cfg.get("cookie_profile") or "").strip()
            if prof:
                spec = f"{spec}:{prof}"
            return ["--cookies-from-browser", spec]
        return []

    if COOKIES_FILE.exists():
        return ["--cookies", str(COOKIES_FILE)]
    return []


def get_cookie_info() -> dict:
    cfg = load_config()
    src_mode = cfg.get("cookie_source", "file")
    enabled_cfg = bool(cfg.get("use_cookies", True))

    if _is_desktop_env() and src_mode == "browser":
        with BROWSER_CK_LOCK:
            b_active = BROWSER_CK_STATE["browser"]
            b_err = BROWSER_CK_STATE["error"]
            b_cli = BROWSER_CK_STATE["cli_fallback"]
        lines = _count_cookie_lines(BROWSER_COOKIES_FILE)
        has_ck = (lines > 0) or bool(b_cli and b_active)
        return {
            "has_cookies": has_ck,
            "cookie_lines": lines,
            "use_cookies": enabled_cfg and (has_ck or src_mode == "browser"),
            "cookie_source": "browser",
            "cookie_browser": cfg.get("cookie_browser", "auto"),
            "cookie_profile": cfg.get("cookie_profile", ""),
            "browser_active": b_active,
            "browser_error": b_err,
            "detected_browsers": detect_installed_browsers(),
        }

    lines = _count_cookie_lines(COOKIES_FILE)
    has_ck = lines > 0
    return {
        "has_cookies": has_ck,
        "cookie_lines": lines,
        "use_cookies": enabled_cfg and has_ck,
        "cookie_source": "file",
        "cookie_browser": cfg.get("cookie_browser", "auto"),
        "cookie_profile": cfg.get("cookie_profile", ""),
        "browser_active": "",
        "browser_error": "",
        "detected_browsers": detect_installed_browsers() if _is_desktop_env() else [],
    }


def _is_cookies_enabled() -> bool:
    cfg = load_config()
    if not bool(cfg.get("use_cookies", True)):
        return False
    if _is_desktop_env() and cfg.get("cookie_source") == "browser":
        return True
    try:
        return COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 20
    except OSError:
        return False


def _cookies_cache_sig() -> str:
    cfg = load_config()
    if not bool(cfg.get("use_cookies", True)):
        return "off"
    ck_p = _active_cookie_file_path()
    try:
        st = ck_p.stat()
        return f"{cfg.get('cookie_source')}:{int(st.st_mtime)}:{st.st_size}"
    except OSError:
        return f"{cfg.get('cookie_source')}:none"


def cookie_header_for_url(url: str) -> str:
    cfg = load_config()
    if not bool(cfg.get("use_cookies", True)):
        return ""
    if _is_desktop_env() and cfg.get("cookie_source") == "browser":
        sync_browser_cookies(force=False)
    ck_file = _active_cookie_file_path()
    if not ck_file.exists():
        return ""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return ""
    pairs = []
    try:
        for ln in ck_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not ln.strip() or ln.startswith("#"):
                continue
            parts = ln.split("\t")
            if len(parts) < 7:
                continue
            domain = parts[0].lstrip(".").lower()
            if host == domain or host.endswith("." + domain):
                pairs.append(f"{parts[5]}={parts[6]}")
    except Exception:
        return ""
    return "; ".join(pairs)


def get_ytdlp_version() -> str:
    ymod = _get_ytdlp_module()
    if ymod is not None:
        try:
            ver_m = getattr(ymod, "version", None)
            v = getattr(ver_m, "__version__", "") if ver_m else ""
            if v:
                return str(v).strip()
        except Exception:
            pass
    try:
        p = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, env=clean_env(), timeout=15, **_win_popen_kwargs())
        if p.returncode == 0:
            return p.stdout.strip()
    except Exception:
        pass
    return "not found"


def _update_ytdlp_zipapp(release_url: str = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp") -> dict:
    """Download official pure-Python yt-dlp zipapp into APP_DATA_DIR and hot-reload it via zipimport."""
    import zipfile as _zf
    tmp_zip = YTDLP_OVERRIDE_ZIP.with_name(f".ytdlp_upd_{uuid.uuid4().hex[:8]}.tmp")
    try:
        req = urllib.request.Request(
            release_url,
            headers={"User-Agent": DESKTOP_UA, "Accept": "application/octet-stream,*/*"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=90, context=_http_context()) as resp:
            with open(tmp_zip, "wb") as fp:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    fp.write(chunk)
        if not tmp_zip.exists() or tmp_zip.stat().st_size < 100000:
            raise RuntimeError("Downloaded yt-dlp package is incomplete.")
        with _zf.ZipFile(tmp_zip, "r") as zchk:
            names = set(zchk.namelist())
            if "yt_dlp/__init__.py" not in names:
                raise RuntimeError("Downloaded file is not a valid yt-dlp Python archive.")
        os.replace(tmp_zip, YTDLP_OVERRIDE_ZIP)
    except Exception:
        if tmp_zip.exists():
            try:
                tmp_zip.unlink()
            except Exception:
                pass
        raise

    zip_str = str(YTDLP_OVERRIDE_ZIP)
    if zip_str in sys.path:
        sys.path.remove(zip_str)
    sys.path.insert(0, zip_str)

    import importlib
    global _YTDLP_MOD, _YTDLP_IMPORT_TRIED, _YTDLP_USE_CLI_ONLY
    with _YTDLP_IMPORT_LOCK:
        for mod_name in list(sys.modules.keys()):
            if mod_name == "yt_dlp" or mod_name.startswith("yt_dlp."):
                sys.modules.pop(mod_name, None)
        importlib.invalidate_caches()
        _YTDLP_MOD = None
        _YTDLP_IMPORT_TRIED = False
        _YTDLP_USE_CLI_ONLY = False

    _clear_info_cache()
    ver = get_ytdlp_version()
    return {"ok": True, "ytdlp_version": ver}


def update_ytdlp() -> dict:
    if _is_android_app():
        return _update_ytdlp_zipapp()
    cmds = [
        [sys.executable, "-m", "pip", "install", "--break-system-packages", "-U", "yt-dlp"],
        [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
    ]
    last_err = ""
    for cmd in cmds:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=clean_env(), timeout=180, **_win_popen_kwargs())
        if proc.returncode == 0:
            global _YTDLP_USE_CLI_ONLY
            _YTDLP_USE_CLI_ONLY = True
            _clear_info_cache()
            return {"ok": True, "ytdlp_version": get_ytdlp_version()}
        last_err = proc.stderr.strip() or proc.stdout.strip()
    raise RuntimeError(last_err or "Failed to update yt-dlp")


def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def extract_url(raw_text: str) -> str:
    s = (raw_text or "").strip()
    if not s:
        return ""
    m = re.search(r"https?://[^\s<>\"']+", s)
    if m:
        url = m.group(0).rstrip(".,);]}>\"'")
        return url
    if re.match(r"^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(/.*)?$", s):
        return "https://" + s
    return s


def safe_float(val):
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in ("na", "n/a", "none", "unknown"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fmt_mb(num_bytes, approx=False):
    val = safe_float(num_bytes)
    if val is None or val < 0:
        return ""
    mb = val / (1024.0 * 1024.0)
    prefix = "~" if approx else ""
    if mb >= 1024.0:
        return f"{prefix}{mb / 1024.0:.2f} GB"
    if mb >= 10.0:
        return f"{prefix}{mb:.1f} MB"
    if mb >= 1.0:
        return f"{prefix}{mb:.2f} MB"
    return f"{prefix}{val / 1024.0:.0f} KB"


def fmt_speed(bps):
    val = safe_float(bps)
    if val is None or val <= 0:
        return "—"
    mbps = val / (1024.0 * 1024.0)
    if mbps >= 1.0:
        return f"{mbps:.2f} MB/s"
    return f"{val / 1024.0:.0f} KB/s"


def fmt_eta(seconds):
    val = safe_float(seconds)
    if val is None or val < 0:
        return "—"
    sec = int(val)
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def fmt_age(mtime: float) -> str:
    diff = max(0, int(time.time() - mtime))
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{diff // 60}m"
    if diff < 86400:
        return f"{diff // 3600}h"
    return f"{diff // 86400}d"


def sanitize_filename(name: str, fallback: str = "media") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned[:85] or fallback)


def get_default_download_dir() -> Path:
    env_dl = (os.environ.get("OMNISAVE_DOWNLOAD_DIR") or "").strip()
    if env_dl:
        p_env = Path(env_dl)
        try:
            p_env.mkdir(parents=True, exist_ok=True)
            if os.access(str(p_env), os.W_OK):
                return p_env
        except Exception:
            pass
    for cand in (Path("/storage/emulated/0/Download"), Path("/sdcard/Download")):
        if cand.exists() and os.access(str(cand), os.W_OK):
            return cand
    if _is_android_app():
        fallback = APP_DATA_DIR / "Downloads"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
    dl_root = Path.home() / "Downloads"
    if not dl_root.exists() and (Path.home() / "downloads").exists():
        dl_root = Path.home() / "downloads"
    return dl_root


def _verify_writable_dir(target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    if not target.is_dir():
        raise RuntimeError("Selected path is not a folder.")
    probe = target / f".omnisave_test_{uuid.uuid4().hex[:8]}"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
    except Exception as exc:
        raise RuntimeError(f"Folder is not writable: {exc}")
    return target


def set_custom_download_dir(raw_path: str) -> Path:
    cleaned = (raw_path or "").strip().strip('"').strip("'")
    if not cleaned:
        raise RuntimeError("Folder path cannot be empty.")
    expanded = os.path.expandvars(os.path.expanduser(cleaned))
    target = Path(expanded)
    if not target.is_absolute():
        target = (Path.home() / target)
    target = _verify_writable_dir(target)
    cfg = load_config()
    cfg["save_dir"] = str(target)
    save_config(cfg)
    return target


def reset_custom_download_dir() -> Path:
    cfg = load_config()
    cfg["save_dir"] = ""
    save_config(cfg)
    return get_default_download_dir()


def get_base_download_dir() -> Path:
    custom = (load_config().get("save_dir") or "").strip()
    if custom:
        try:
            p = Path(os.path.expandvars(os.path.expanduser(custom)))
            p.mkdir(parents=True, exist_ok=True)
            if p.is_dir():
                return p
        except Exception:
            pass
    return get_default_download_dir()


def pick_local_folder_dialog(initial_dir: str = "") -> str:
    if not _is_desktop_env():
        raise RuntimeError("Folder picker is only available on Desktop.")
    init_p = str(Path(initial_dir)) if (initial_dir and Path(initial_dir).exists()) else str(get_base_download_dir())

    # 1. Cross-platform Tkinter folder dialog in a short-lived subprocess (thread-safe!)
    tk_script = (
        "import sys, tkinter as tk\n"
        "from tkinter import filedialog\n"
        "r = tk.Tk()\n"
        "r.withdraw()\n"
        "try:\n"
        "    r.attributes('-topmost', True)\n"
        "except Exception:\n"
        "    pass\n"
        "p = filedialog.askdirectory(initialdir=sys.argv[1], title='Select Save Directory')\n"
        "r.destroy()\n"
        "print(p or '')\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", tk_script, init_p],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=180,
        )
        if proc.returncode == 0:
            chosen = (proc.stdout or "").strip()
            if chosen:
                return str(Path(chosen))
            return ""
    except Exception:
        pass

    # 2. Windows PowerShell FolderBrowserDialog fallback
    if sys.platform == "win32":
        safe_init = init_p.replace("'", "''")
        ps_cmd = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$f = New-Object System.Windows.Forms.Form; $f.TopMost = $true; "
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$d.Description = 'Select Save Directory'; "
            f"$d.SelectedPath = '{safe_init}'; "
            "if ($d.ShowDialog($f) -eq 'OK') { Write-Output $d.SelectedPath }"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode == 0:
            return (proc.stdout or "").strip()

    # 3. macOS AppleScript fallback
    if sys.platform == "darwin":
        proc = subprocess.run(
            ["osascript", "-e", 'POSIX path of (choose folder with prompt "Select Save Directory")'],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode == 0:
            return (proc.stdout or "").strip()

    # 4. Linux zenity fallback
    if shutil.which("zenity"):
        proc = subprocess.run(
            ["zenity", "--file-selection", "--directory", "--title=Select Save Directory"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode == 0:
            return (proc.stdout or "").strip()

    raise RuntimeError("Could not open folder picker. Type or paste the folder path manually and click Save Path.")


def open_local_folder(folder_path: str = "") -> bool:
    if not _is_desktop_env():
        return False
    target = Path(folder_path) if folder_path else get_base_download_dir()
    if not target.exists():
        target = get_base_download_dir()
    target.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform == "win32":
            os.startfile(str(target))
            return True
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
            return True
        if shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(target)])
            return True
    except Exception:
        pass
    return False


def get_wa_save_dir() -> Path:
    return get_base_download_dir() / WA_SUBDIR_NAME


def _http_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


MAJOR_PLATFORMS = (
    "instagram.com", "youtube.com", "youtu.be", "tiktok.com",
    "facebook.com", "fb.watch", "twitter.com", "x.com",
    "drive.google.com", "docs.google.com",
)


def _is_major_platform(url: str) -> bool:
    host_low = (urlparse(url).hostname or "").lower()
    return any(host_low == d or host_low.endswith("." + d) for d in MAJOR_PLATFORMS)


def _origin_from_url(url: str) -> str:
    p = urlparse(url or "")
    if p.scheme in ("http", "https") and p.netloc:
        return f"{p.scheme}://{p.netloc}"
    return ""


def _active_http_ua() -> str:
    return DESKTOP_UA if _is_desktop_env() else BROWSER_UA


def _merge_cookie_headers(base_ck: str, extra_ck: str) -> str:
    if not base_ck:
        return extra_ck or ""
    if not extra_ck:
        return base_ck
    merged = {}
    for blob in (base_ck, extra_ck):
        for part in blob.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                merged[k.strip()] = v.strip()
    return "; ".join(f"{k}={v}" for k, v in merged.items())


def _cookiejar_header_for_url(cj: http.cookiejar.CookieJar, url: str) -> str:
    if not cj:
        return ""
    host = (urlparse(url).hostname or "").lower()
    pairs = []
    for c in cj:
        dom = (c.domain or "").lstrip(".").lower()
        if not dom or host == dom or host.endswith("." + dom):
            pairs.append(f"{c.name}={c.value}")
    return "; ".join(pairs)


def _build_request(url: str, referer: str = "", method: str = "GET", extra_cookies: str = ""):
    headers = {
        "User-Agent": _active_http_ua(),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
        orig = _origin_from_url(referer)
        if orig:
            headers["Origin"] = orig
    ck = _merge_cookie_headers(cookie_header_for_url(url), extra_cookies)
    if ck:
        headers["Cookie"] = ck
    return urllib.request.Request(url, headers=headers, method=method)


def probe_direct_media(url: str):
    parsed = urlparse(url)
    ext = Path(parsed.path).suffix.lower()
    if ext in STREAM_EXTS:
        return {
            "kind": "sniffed",
            "clean_url": url,
            "target_url": url,
            "referer": "",
            "title": sanitize_filename(Path(parsed.path).stem, "Stream"),
            "thumbnail": "",
            "duration": None,
            "uploader": parsed.hostname or "",
            "extractor": ext.lstrip(".").upper(),
            "method_badge": "Stream",
            "media_type": "video",
            "streams": [{"id": "best", "name": f"{ext.lstrip('.').upper()} Stream", "size": "", "url": url, "direct": False}],
            "entries": [],
        }

    looks_direct = ext in VIDEO_EXTS or ext in AUDIO_EXTS or ext in IMAGE_EXTS
    if not looks_direct:
        return None

    size_bytes = None
    ctype = ""
    final_url = url
    try:
        req = _build_request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=15, context=_http_context()) as resp:
            final_url = resp.geturl() or url
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit():
                size_bytes = int(cl)
    except Exception:
        pass

    if ctype.startswith("text/html"):
        return None

    mtype = "image" if ext in IMAGE_EXTS or ctype.startswith("image/") else (
        "audio" if ext in AUDIO_EXTS or ctype.startswith("audio/") else "video"
    )
    fname = Path(urlparse(final_url).path).name or f"file{ext}"
    size_txt = fmt_mb(size_bytes) if size_bytes else ""
    return {
        "kind": "direct",
        "clean_url": url,
        "target_url": final_url,
        "referer": "",
        "title": sanitize_filename(Path(fname).stem, "Media"),
        "thumbnail": final_url if mtype == "image" else "",
        "duration": None,
        "uploader": parsed.hostname or "",
        "extractor": (ext.lstrip(".") or mtype).upper(),
        "method_badge": "Direct File",
        "media_type": mtype,
        "formats": [{"id": "direct", "name": f"Original ({ext.lstrip('.').upper()})", "size": size_txt}],
        "entries": [],
    }


def _parse_quality_from_text(text: str):
    if not text:
        return None, ""
    low = text.lower()
    m_dim = re.search(r"(?<!\d)(\d{3,4})\s*[x×]\s*(\d{3,4})(?!\d)", low)
    if m_dim:
        w, h = int(m_dim.group(1)), int(m_dim.group(2))
        q = min(w, h)
        if 140 <= q <= 4320:
            lbl = f"{q}p (4K)" if q >= 2160 else f"{q}p"
            return q, lbl
    m_p = re.search(r"(?<![a-z0-9])(4320|2160|1440|1080|960|720|576|540|480|360|270|240|144)p(?![a-z0-9])", low)
    if m_p:
        q = int(m_p.group(1))
        lbl = f"{q}p (4K)" if q >= 2160 else f"{q}p"
        return q, lbl
    if re.fullmatch(r"(4320|2160|1440|1080|960|720|576|540|480|360|270|240|144)", low.strip()):
        q = int(low.strip())
        lbl = f"{q}p (4K)" if q >= 2160 else f"{q}p"
        return q, lbl
    m_q = re.search(r"(?:quality|res|size|height)[=_:/-]?(2160|1440|1080|720|540|480|360|240)\b", low)
    if m_q:
        q = int(m_q.group(1))
        lbl = f"{q}p (4K)" if q >= 2160 else f"{q}p"
        return q, lbl
    m_file_q = re.search(r"[_/-](2160|1440|1080|720|480|360|240)\.(?:mp4|webm|m3u8|mov|m4v)", low)
    if m_file_q:
        q = int(m_file_q.group(1))
        lbl = f"{q}p (4K)" if q >= 2160 else f"{q}p"
        return q, lbl
    if re.search(r"(?<![a-z0-9])(4k|uhd)(?![a-z0-9])", low):
        return 2160, "4K"
    if re.search(r"(?<![a-z0-9])(fhd|full\s*hd)(?![a-z0-9])", low):
        return 1080, "1080p"
    if re.search(r"(?<![a-z0-9])hd(?![a-z0-9])", low):
        return 720, "HD"
    if re.search(r"(?<![a-z0-9])sd(?![a-z0-9])", low):
        return 480, "SD"
    return None, ""


def _fmt_bitrate(kbps):
    val = safe_float(kbps)
    if val is None or val <= 0:
        return ""
    if val >= 1000:
        return f"{val / 1000.0:.1f} Mbps"
    return f"{int(round(val))} kbps"


def _clean_extracted_url(raw_u: str) -> str:
    if not raw_u:
        return ""
    u = raw_u
    for old, new in (
        ("\\\\/", "/"),
        ("\\/", "/"),
        ("\\u002f", "/"), ("\\u002F", "/"),
        ("\\u0026", "&"),
        ("\\u003d", "="), ("\\u003D", "="),
        ("\\u003f", "?"), ("\\u003F", "?"),
        ("\\u0025", "%"),
        ("&amp;", "&"), ("&#38;", "&"), ("&#x26;", "&"),
    ):
        u = u.replace(old, new)
    u = html_lib.unescape(u).strip().strip("\"'")
    u = u.rstrip(".,;)]}\"'\\")
    return u


def _unescape_html_for_urls(html_text: str) -> str:
    s = html_text
    for old, new in (
        ("\\\\/", "/"),
        ("\\/", "/"),
        ("\\u002f", "/"), ("\\u002F", "/"),
        ("\\u0026", "&"),
        ("\\u003d", "="), ("\\u003D", "="),
        ("\\u003f", "?"), ("\\u003F", "?"),
        ("\\u0025", "%"),
        ("&amp;", "&"), ("&#38;", "&"), ("&#x26;", "&"),
    ):
        s = s.replace(old, new)
    return s


JUNK_STREAM_LABELS = {
    "false", "true", "null", "none", "undefined", "nan",
    "auto", "default", "video", "audio", "stream", "media", "file", "url",
}


def _looks_like_direct_media_url(u: str) -> bool:
    low = (u or "").lower()
    ext = Path(urlparse(low).path).suffix
    if ext in VIDEO_EXTS or ext in AUDIO_EXTS or ext in STREAM_EXTS:
        return True
    return any(tok in low for tok in (".m3u8", ".mpd", ".mp4", ".webm", ".mov", ".m4v", ".mp3", ".m4a", ".aac"))


def _looks_like_json_media_api(u: str) -> bool:
    low = (u or "").lower()
    if _looks_like_direct_media_url(low):
        return False
    return any(tok in low for tok in ("get_media", "get_video", "/media?", "sources.json", "playlist.json", "media_definitions"))


def _sanitize_stream_label(label: str) -> str:
    s = (label or "").strip().strip("\"'")
    if not s or s.lower() in JUNK_STREAM_LABELS:
        return ""
    return s


def _extract_json_media_objects(html_text: str) -> tuple:
    """Parse structured player arrays (e.g. mediaDefinitions) into (media_items, api_urls)."""
    media_items = []
    api_urls = []
    seen_api = set()

    # Match common player config arrays like "mediaDefinitions": [...]
    for m_arr in re.finditer(r'["\']?mediaDefinitions["\']?\s*:\s*(\[[\s\S]{10,40000}?\])', html_text, re.I):
        raw_arr = m_arr.group(1)
        try:
            arr = json.loads(raw_arr)
        except Exception:
            continue
        if not isinstance(arr, list):
            continue
        for obj in arr:
            if not isinstance(obj, dict):
                continue
            vurl = _clean_extracted_url(str(obj.get("videoUrl") or obj.get("url") or obj.get("src") or obj.get("file") or ""))
            if not vurl.startswith(("http://", "https://")):
                continue
            q_raw = obj.get("quality")
            if isinstance(q_raw, list):
                q_str = str(q_raw[0]) if q_raw else ""
            else:
                q_str = str(q_raw or obj.get("height") or "")
            is_remote = bool(obj.get("remote"))
            if is_remote or _looks_like_json_media_api(vurl):
                if vurl not in seen_api:
                    seen_api.add(vurl)
                    api_urls.append(vurl)
            else:
                media_items.append((vurl, q_str, str(obj.get("format") or "")))

    return media_items, api_urls


def extract_media_from_html(html_text: str, base_url: str) -> dict:
    title = ""
    m_og_t = re.search(r'<meta[^>]+(?:property|name)=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html_text, re.I)
    if m_og_t:
        title = html_lib.unescape(m_og_t.group(1)).strip()
    if not title:
        m_t = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.I | re.S)
        if m_t:
            title = html_lib.unescape(re.sub(r"\s+", " ", m_t.group(1))).strip()

    thumb = ""
    m_og_i = re.search(r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)["\']', html_text, re.I)
    if m_og_i:
        thumb = urljoin(base_url, html_lib.unescape(m_og_i.group(1)).strip())

    candidates = []
    seen = {}
    api_urls = []
    seen_api = set()
    iframes = []
    seen_if = set()

    def add_api(raw_u: str):
        u = _clean_extracted_url(raw_u)
        if u.startswith("//"):
            u = "https:" + u
        elif u and not re.match(r"^https?://", u, re.I):
            u = urljoin(base_url, u)
        if re.match(r"^https?://", u, re.I) and u not in seen_api:
            seen_api.add(u)
            api_urls.append(u)

    def add_iframe(raw_u: str):
        iu = _clean_extracted_url(raw_u)
        if iu.startswith("//"):
            iu = "https:" + iu
        elif iu and not re.match(r"^https?://", iu, re.I):
            iu = urljoin(base_url, iu)
        low = iu.lower()
        if re.match(r"^https?://", iu, re.I) and not any(b in low for b in AD_IFRAME_BLOCKLIST):
            if iu not in seen_if:
                seen_if.add(iu)
                iframes.append(iu)

    def add_candidate(raw_u: str, label: str = "", q_hint=None):
        if not raw_u:
            return
        u = _clean_extracted_url(raw_u)
        if u.startswith("//"):
            u = "https:" + u
        elif not re.match(r"^https?://", u, re.I):
            u = urljoin(base_url, u)
        if not re.match(r"^https?://", u, re.I):
            return
        low = u.lower()
        if any(bad in low for bad in ("favicon", "sprite", "logo", "avatar", ".css", ".js", "blank.mp4", "preview.mp4")):
            return
        if _looks_like_json_media_api(u):
            add_api(u)
            return
        if not _looks_like_direct_media_url(u):
            return

        ext = Path(urlparse(u).path).suffix.lower()
        is_stream = ext in STREAM_EXTS or ".m3u8" in low or ".mpd" in low
        is_audio = ext in AUDIO_EXTS
        is_direct = not is_stream

        clean_lbl = _sanitize_stream_label(label)
        clean_hint = _sanitize_stream_label(str(q_hint or ""))

        q_num, q_lbl = _parse_quality_from_text(clean_hint)
        if not q_num:
            q_num, q_lbl = _parse_quality_from_text(clean_lbl)
        if not q_num:
            q_num, q_lbl = _parse_quality_from_text(u)

        final_label = q_lbl or clean_lbl
        if not final_label:
            if ".m3u8" in low:
                final_label = "HLS Stream"
            elif ".mpd" in low:
                final_label = "DASH Stream"
            elif ext:
                final_label = ext.lstrip(".").upper()
            else:
                final_label = "Video"

        if u in seen:
            existing = seen[u]
            if (q_num or 0) > (existing.get("q_num") or 0):
                existing["q_num"] = q_num or 0
                existing["name"] = final_label
            return

        # Priority: highest resolution first; prefer direct MP4 slightly ahead of HLS at equal resolution
        if is_audio:
            kind_rank = 3
        elif is_direct:
            kind_rank = 1
        else:
            kind_rank = 2

        item = {
            "url": u,
            "name": final_label,
            "direct": is_direct,
            "is_stream": is_stream,
            "q_num": q_num or 0,
            "kind_rank": kind_rank,
        }
        seen[u] = item
        candidates.append(item)

    unescaped_html = _unescape_html_for_urls(html_text)

    # 0. Structured mediaDefinitions JSON arrays (e.g. Pornhub / Tube8 / RedTube / YouPorn)
    md_items, md_apis = _extract_json_media_objects(html_text)
    for au in md_apis:
        add_api(au)
    for vu, qs, fmt in md_items:
        add_candidate(vu, label=qs, q_hint=qs)

    # 1. HTML5 <source> / <video> / <audio> tags with quality attributes
    for m in re.finditer(r"<(?:source|video|audio)\b([^>]+)>", html_text, re.I):
        attrs = m.group(1)
        m_src = re.search(r'\bsrc=["\']([^"\']+)["\']', attrs, re.I)
        if not m_src:
            continue
        m_lbl = re.search(r'\b(?:label|size|res|data-res|data-quality|title)=["\']([^"\']+)["\']', attrs, re.I)
        add_candidate(m_src.group(1), label=m_lbl.group(1) if m_lbl else "")

    # 2. JS/JSON player source objects (strict key names so defaultQuality=false NEVER matches quality!)
    json_obj_pats = [
        r'(?<![A-Za-z0-9_])["\']?(?:file|src|url|videoUrl|video_url)["\']?\s*:\s*["\'](https?://[^"\']+)["\'][^\}]{0,160}(?<![A-Za-z0-9_])["\']?(?:label|res|size|quality)["\']?\s*:\s*["\']?([A-Za-z0-9_ .-]{1,20})["\']?',
        r'(?<![A-Za-z0-9_])["\']?(?:label|res|size|quality)["\']?\s*:\s*["\']?([A-Za-z0-9_ .-]{1,20})["\']?[^\}]{0,160}(?<![A-Za-z0-9_])["\']?(?:file|src|url|videoUrl|video_url)["\']?\s*:\s*["\'](https?://[^"\']+)["\']',
    ]
    for idx_pat, pat in enumerate(json_obj_pats):
        for m in re.finditer(pat, unescaped_html, re.I):
            u_str = m.group(1) if idx_pat == 0 else m.group(2)
            q_str = m.group(2) if idx_pat == 0 else m.group(1)
            if _looks_like_json_media_api(u_str):
                add_api(u_str)
            elif _looks_like_direct_media_url(u_str):
                add_candidate(u_str, label=q_str, q_hint=q_str)

    # Also pick up standalone get_media API URLs if embedded in JS
    for m_api in re.finditer(r'["\'](https?://[^"\']+?(?:get_media|get_video)[^"\']*)["\']', unescaped_html, re.I):
        add_api(m_api.group(1))

    # 3. OpenGraph / Twitter video meta tags: only add as direct candidate if real media URL; otherwise treat as embed iframe
    for pat in (
        r'<meta[^>]+(?:property|name)=["\'](?:og:video:secure_url|og:video:url|og:video|twitter:player:stream)["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:video:secure_url|og:video:url|og:video|twitter:player:stream)["\']',
    ):
        for m in re.finditer(pat, html_text, re.I):
            og_u = _clean_extracted_url(m.group(1))
            if _looks_like_direct_media_url(og_u):
                add_candidate(og_u, "Embedded Video")
            elif og_u.startswith(("http://", "https://")) and "/embed" in og_u.lower():
                add_iframe(og_u)

    # 4. Raw & JSON-escaped direct stream/media URLs
    media_pat = re.compile(
        r'(https?://[^\s"\'<>\\]+?\.(?:m3u8|mpd|mp4|webm|mov|m4v|mp3|m4a|aac|ogg)(?:\?[^\s"\'<>\\]*)?)',
        re.I,
    )
    for m in media_pat.finditer(unescaped_html):
        add_candidate(m.group(1))

    # 5. Iframes (for embedded video players)
    for m in re.finditer(r'<iframe[^>]+src=["\']([^"\']+)["\']', html_text, re.I):
        add_iframe(m.group(1))

    # Sort: highest numeric resolution first (1080p -> 720p -> 480p -> 240p), then direct MP4 before HLS
    candidates.sort(key=lambda x: (x["kind_rank"] == 3, -(x.get("q_num") or 0), x["kind_rank"]))
    return {
        "title": title,
        "thumbnail": thumb,
        "candidates": candidates[:16],
        "api_urls": api_urls[:4],
        "iframes": iframes[:4],
    }


def _fetch_json_media_list(api_url: str, referer: str, opener, extra_cookies: str = "") -> list:
    """Fetch a JSON media endpoint (e.g. get_media) using the same session cookies and parse stream entries."""
    out = []
    try:
        req = _build_request(api_url, referer=referer, method="GET", extra_cookies=extra_cookies)
        req.add_header("Accept", "application/json, text/javascript, */*; q=0.01")
        req.add_header("X-Requested-With", "XMLHttpRequest")
        with opener.open(req, timeout=15) as resp:
            raw = resp.read(512000).decode("utf-8", errors="ignore")
        data = json.loads(raw)
    except Exception:
        return []

    rows = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("mediaDefinitions", "sources", "files", "videos", "data", "streams"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break

    for obj in rows:
        if not isinstance(obj, dict):
            continue
        vurl = _clean_extracted_url(str(obj.get("videoUrl") or obj.get("url") or obj.get("src") or obj.get("file") or ""))
        if not vurl.startswith(("http://", "https://")):
            continue
        if not _looks_like_direct_media_url(vurl):
            continue
        q_raw = obj.get("quality")
        if isinstance(q_raw, list):
            q_str = str(q_raw[0]) if q_raw else ""
        else:
            q_str = str(q_raw or obj.get("height") or obj.get("label") or "")
        q_num, q_lbl = _parse_quality_from_text(_sanitize_stream_label(q_str))
        if not q_num:
            q_num, q_lbl = _parse_quality_from_text(vurl)
        low = vurl.lower()
        is_stream = ".m3u8" in low or ".mpd" in low
        lbl = q_lbl or ("HLS Stream" if ".m3u8" in low else ("DASH Stream" if ".mpd" in low else "MP4"))
        out.append({
            "url": vurl,
            "name": lbl,
            "direct": not is_stream,
            "is_stream": is_stream,
            "q_num": q_num or 0,
            "kind_rank": 1 if (not is_stream) else 2,
        })
    return out


def _probe_url_http_status(url: str, referer: str = "", opener=None, extra_cookies: str = "") -> int:
    """Use a tiny ranged GET (avoiding HEAD, which many video CDNs reject with 403/405/410)."""
    try:
        req = _build_request(url, referer=referer, method="GET", extra_cookies=extra_cookies)
        req.add_header("Range", "bytes=0-1")
        if opener is not None:
            with opener.open(req, timeout=7) as resp:
                return int(getattr(resp, "status", 200) or 200)
        with urllib.request.urlopen(req, timeout=7, context=_http_context()) as resp:
            return int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as he:
        return int(he.code)
    except Exception:
        return 0


def _resolve_hls_uri(base_url: str, uri: str, inherit_query: bool = False) -> str:
    resolved = urljoin(base_url, uri)
    if inherit_query:
        base_q = urlparse(base_url).query
        if base_q and not urlparse(resolved).query:
            sep = "&" if "?" in resolved else "?"
            resolved = f"{resolved}{sep}{base_q}"
    return resolved


def parse_hls_master(m3u8_url: str, referer: str = "", opener=None, extra_cookies: str = ""):
    try:
        req = _build_request(m3u8_url, referer=referer, method="GET", extra_cookies=extra_cookies)
        if opener is not None:
            with opener.open(req, timeout=15) as resp:
                final_url = resp.geturl() or m3u8_url
                text = resp.read(512000).decode("utf-8", errors="ignore")
        else:
            with urllib.request.urlopen(req, timeout=15, context=_http_context()) as resp:
                final_url = resp.geturl() or m3u8_url
                text = resp.read(512000).decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as he:
        if he.code in (404, 410):
            return {"dead_code": int(he.code), "variants": []}
        return []
    except Exception:
        return []

    if "#EXT-X-STREAM-INF" not in text:
        return []

    low_txt = text.lower()
    is_drm = (
        "method=sample-aes" in low_txt
        or "com.apple.streamingkeydelivery" in low_txt
        or "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed" in low_txt
        or "urn:uuid:9a04f079-9840-4286-ab92-e65be0885f95" in low_txt
    )

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    master_has_query = bool(urlparse(final_url).query)

    # 1. Parse #EXT-X-MEDIA:TYPE=AUDIO separate audio renditions (e.g. AWS MediaConvert / MBC VOD)
    audio_groups = {}
    default_audio_uri = ""
    for ln in lines:
        if not ln.startswith("#EXT-X-MEDIA:"):
            continue
        attrs = ln.split(":", 1)[1]
        if "TYPE=AUDIO" not in attrs.upper():
            continue
        m_uri = re.search(r'URI="([^"]+)"', attrs, re.I)
        if not m_uri:
            continue
        a_uri = m_uri.group(1).strip()
        m_gid = re.search(r'GROUP-ID="([^"]+)"', attrs, re.I)
        gid = m_gid.group(1).strip() if m_gid else ""
        is_def = bool(re.search(r"DEFAULT=YES", attrs, re.I))
        if gid:
            if gid not in audio_groups or is_def:
                audio_groups[gid] = a_uri
        if is_def or not default_audio_uri:
            default_audio_uri = a_uri

    # 2. Parse #EXT-X-STREAM-INF video variants
    raw_variants = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("#EXT-X-STREAM-INF:"):
            attrs = ln.split(":", 1)[1]
            uri = ""
            j = i + 1
            while j < len(lines):
                if not lines[j].startswith("#"):
                    uri = lines[j]
                    break
                j += 1
            if uri:
                q_num = 0
                label = ""
                m_res = re.search(r"RESOLUTION=(\d+)x(\d+)", attrs, re.I)
                if m_res:
                    w, h = int(m_res.group(1)), int(m_res.group(2))
                    q_num = min(w, h)
                    label = f"{q_num}p (4K)" if q_num >= 2160 else f"{q_num}p"
                m_bw = re.search(r"BANDWIDTH=(\d+)", attrs, re.I)
                bw_kbps = (int(m_bw.group(1)) / 1000.0) if m_bw else 0.0
                if not label:
                    m_name = re.search(r'NAME="([^"]+)"', attrs, re.I)
                    if m_name:
                        label = m_name.group(1)
                    elif bw_kbps:
                        label = _fmt_bitrate(bw_kbps)
                    else:
                        label = "HLS Variant"
                m_ag = re.search(r'AUDIO="([^"]+)"', attrs, re.I)
                ag = m_ag.group(1).strip() if m_ag else ""
                a_uri = audio_groups.get(ag) or (default_audio_uri if audio_groups else "")
                raw_variants.append({
                    "q_num": q_num,
                    "bw": bw_kbps,
                    "name": label,
                    "size": _fmt_bitrate(bw_kbps) if (q_num and bw_kbps) else "",
                    "raw_uri": uri,
                    "raw_audio_uri": a_uri,
                })
            i = j + 1
        else:
            i += 1

    if not raw_variants:
        return []

    raw_variants.sort(key=lambda x: (x["q_num"], x["bw"]), reverse=True)

    # Deduplicate by resolution/label first
    dedup_raw = []
    seen_labels = set()
    for v in raw_variants:
        key = v["q_num"] or v["name"]
        if key in seen_labels:
            continue
        seen_labels.add(key)
        dedup_raw.append(v)

    # 3. Detect whether relative URIs require inheriting the master's signed query string (?token= / ?hdnts=)
    inherit_q = False
    if master_has_query:
        first_bare = _resolve_hls_uri(final_url, dedup_raw[0]["raw_uri"], inherit_query=False)
        if not urlparse(first_bare).query:
            st_bare = _probe_url_http_status(first_bare, referer=referer, opener=opener, extra_cookies=extra_cookies)
            if st_bare in (401, 403, 404, 410):
                first_with_q = _resolve_hls_uri(final_url, dedup_raw[0]["raw_uri"], inherit_query=True)
                st_q = _probe_url_http_status(first_with_q, referer=referer, opener=opener, extra_cookies=extra_cookies)
                if st_q in (200, 206):
                    inherit_q = True

    # 4. Resolve final URLs and drop confirmed 404/410 ghost variants (probing top 4)
    dedup = []
    alive_count = 0
    for idx, v in enumerate(dedup_raw[:12], 1):
        v_url = _resolve_hls_uri(final_url, v["raw_uri"], inherit_query=inherit_q)
        a_url = _resolve_hls_uri(final_url, v["raw_audio_uri"], inherit_query=inherit_q) if v["raw_audio_uri"] else ""
        if idx <= 4 and len(dedup_raw) > 1:
            st_v = _probe_url_http_status(v_url, referer=referer, opener=opener, extra_cookies=extra_cookies)
            if st_v in (404, 410):
                # Try once with inherited query if we didn't already
                if master_has_query and not inherit_q and not urlparse(v_url).query:
                    alt_v = _resolve_hls_uri(final_url, v["raw_uri"], inherit_query=True)
                    if _probe_url_http_status(alt_v, referer=referer, opener=opener, extra_cookies=extra_cookies) in (200, 206):
                        v_url = alt_v
                        if v["raw_audio_uri"]:
                            a_url = _resolve_hls_uri(final_url, v["raw_audio_uri"], inherit_query=True)
                        st_v = 200
                if st_v in (404, 410):
                    continue
            if st_v in (200, 206):
                alive_count += 1
        dedup.append({
            "id": f"stream_{len(dedup) + 1}",
            "name": v["name"],
            "size": v["size"],
            "url": v_url,
            "audio_url": a_url,
            "master_url": final_url,
            "direct": False,
            "has_separate_audio": bool(a_url),
            "is_drm": is_drm,
        })

    # If ALL probed variants were 404, fall back to offering the master playlist directly via yt-dlp/ffmpeg
    if not dedup:
        dedup.append({
            "id": "stream_1",
            "name": "Master HLS",
            "size": "Auto",
            "url": final_url,
            "audio_url": "",
            "master_url": final_url,
            "direct": False,
            "has_separate_audio": bool(audio_groups),
            "is_drm": is_drm,
        })
    return dedup[:12]


def inspect_dash_mpd(mpd_url: str, referer: str = "", opener=None, extra_cookies: str = "") -> dict:
    """Fetch a DASH .mpd manifest to detect DRM (Widevine/PlayReady/CENC) and live/VOD status quickly."""
    try:
        req = _build_request(mpd_url, referer=referer, method="GET", extra_cookies=extra_cookies)
        if opener is not None:
            with opener.open(req, timeout=15) as resp:
                final_url = resp.geturl() or mpd_url
                text = resp.read(512000).decode("utf-8", errors="ignore")
        else:
            with urllib.request.urlopen(req, timeout=15, context=_http_context()) as resp:
                final_url = resp.geturl() or mpd_url
                text = resp.read(512000).decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as he:
        if he.code in (404, 410):
            return {"dead_code": int(he.code)}
        return {}
    except Exception:
        return {}

    low = text.lower()
    if "<mpd" not in low:
        return {}

    drm_systems = []
    if "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed" in low or "widevine" in low:
        drm_systems.append("Widevine")
    if "9a04f079-9840-4286-ab92-e65be0885f95" in low or "playready" in low:
        drm_systems.append("PlayReady")
    if "clearkey" in low or "e2719d58-a985-b3c9-781a-b030af78d30e" in low:
        drm_systems.append("ClearKey")
    has_cenc = (
        "cenc:default_kid" in low
        or 'value="cenc"' in low
        or "value='cenc'" in low
        or "urn:mpeg:dash:mp4protection:2011" in low
        or ("<contentprotection" in low and ("pssh" in low or "default_kid" in low))
    )
    is_drm = bool(drm_systems or has_cenc)
    return {
        "final_url": final_url,
        "is_drm": is_drm,
        "drm_label": " / ".join(drm_systems) if drm_systems else ("DRM (CENC)" if is_drm else ""),
        "is_live": 'type="dynamic"' in low or "type='dynamic'" in low,
    }


def expand_stream_url(stream_url: str, clean_url: str, referer: str, title: str = "", thumb: str = "", host: str = "", opener=None, extra_cookies: str = ""):
    # 0. Inspect DASH .mpd for DRM (Widevine/PlayReady/CENC) before spending time in yt-dlp/FFmpeg
    if ".mpd" in stream_url.lower():
        dash_info = inspect_dash_mpd(stream_url, referer=referer, opener=opener, extra_cookies=extra_cookies)
        if dash_info.get("dead_code"):
            return {"_dead_code": dash_info["dead_code"]}
        if dash_info.get("is_drm"):
            drm_lbl = dash_info.get("drm_label") or "DRM"
            raise RuntimeError(
                f"This video is encrypted with {drm_lbl} DRM protection and cannot be downloaded directly.\n"
                "(Encrypted streaming platforms like Shahid / Netflix / OSN lock DASH streams with browser DRM.)"
            )

    # 1. Parse HLS master playlist (preserves tokens, detects separate audio & filters 404 variants)
    parsed_variants = None
    if ".m3u8" in stream_url.lower():
        parsed_variants = parse_hls_master(stream_url, referer=referer, opener=opener, extra_cookies=extra_cookies)
        if isinstance(parsed_variants, dict) and parsed_variants.get("dead_code"):
            return {"_dead_code": parsed_variants["dead_code"]}

    # If the HLS master has separate audio tracks (#EXT-X-MEDIA:TYPE=AUDIO), try yt-dlp -J on the master URL first
    # because yt-dlp natively merges separate video + audio HLS tracks with exact resolution selection!
    has_sep_audio = bool(parsed_variants and any(v.get("has_separate_audio") for v in parsed_variants))
    is_drm_hls = bool(parsed_variants and any(v.get("is_drm") for v in parsed_variants))
    if is_drm_hls:
        raise RuntimeError(
            "This HLS stream is protected with DRM encryption (FairPlay / Widevine / SAMPLE-AES) and cannot be downloaded directly."
        )

    if has_sep_audio or not parsed_variants:
        try:
            sub = _run_ytdlp_info_raw(
                stream_url,
                allow_playlist=False,
                use_cookies=_is_cookies_enabled(),
                referer=referer,
            )
            if sub and len(sub.get("formats") or []) > 1:
                sub["clean_url"] = clean_url
                sub["target_url"] = stream_url
                sub["referer"] = referer
                sub["extra_cookies"] = extra_cookies
                if title:
                    sub["title"] = title
                if thumb and not sub.get("thumbnail"):
                    sub["thumbnail"] = thumb
                if host and (not sub.get("extractor") or sub.get("extractor").lower() == "generic"):
                    sub["extractor"] = host
                sub["method_badge"] = "DRM Protected" if is_drm_hls else "HLS / DASH"
                return sub
        except Exception:
            pass

    if parsed_variants:
        return {
            "kind": "sniffed",
            "clean_url": clean_url,
            "target_url": parsed_variants[0]["url"],
            "referer": referer,
            "extra_cookies": extra_cookies,
            "title": title or host or "Stream",
            "thumbnail": thumb,
            "duration": None,
            "uploader": host,
            "extractor": host or "HLS",
            "method_badge": "DRM Protected" if is_drm_hls else "HLS Variants",
            "media_type": "video",
            "streams": parsed_variants,
            "entries": [],
        }

    return None


def sniff_page_media(url: str, depth: int = 0):
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_http_context()),
        urllib.request.HTTPCookieProcessor(cj),
    )

    try:
        req = _build_request(url, method="GET")
        with opener.open(req, timeout=20) as resp:
            final_url = resp.geturl() or url
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            cl = resp.headers.get("Content-Length")
            size_bytes = int(cl) if (cl and cl.isdigit()) else None

            session_ck = _cookiejar_header_for_url(cj, final_url)

            # Direct media response after redirects
            if ctype.startswith(("video/", "audio/", "image/")) or ctype in (
                "application/vnd.apple.mpegurl",
                "application/x-mpegurl",
                "application/dash+xml",
            ):
                is_stream = "mpegurl" in ctype or "dash" in ctype
                if is_stream:
                    expanded = expand_stream_url(
                        final_url,
                        clean_url=url,
                        referer=url,
                        title=sanitize_filename(Path(urlparse(final_url).path).stem, "Stream"),
                        host=urlparse(final_url).hostname or "",
                        opener=opener,
                        extra_cookies=session_ck,
                    )
                    if expanded and not (isinstance(expanded, dict) and expanded.get("_dead_code")):
                        return expanded
                ext_guess = mimetypes.guess_extension(ctype) or ".mp4"
                mtype = "image" if ctype.startswith("image/") else ("audio" if ctype.startswith("audio/") else "video")
                return {
                    "kind": "sniffed" if is_stream else "direct",
                    "clean_url": url,
                    "target_url": final_url,
                    "referer": url,
                    "extra_cookies": session_ck,
                    "title": sanitize_filename(Path(urlparse(final_url).path).stem, "Media"),
                    "thumbnail": final_url if mtype == "image" else "",
                    "duration": None,
                    "uploader": urlparse(final_url).hostname or "",
                    "extractor": ext_guess.lstrip(".").upper(),
                    "method_badge": "Deep Sniffer",
                    "media_type": mtype,
                    "streams": [{"id": "best", "name": ctype, "size": fmt_mb(size_bytes) if size_bytes else "", "url": final_url, "direct": not is_stream}],
                    "formats": [{"id": "direct", "name": f"Original ({ext_guess.lstrip('.').upper()})", "size": fmt_mb(size_bytes) if size_bytes else ""}],
                    "entries": [],
                }

            raw_bytes = resp.read(1500000)
            html_text = raw_bytes.decode("utf-8", errors="ignore")
    except Exception:
        return None

    session_ck = _cookiejar_header_for_url(cj, final_url)
    parsed = extract_media_from_html(html_text, final_url)
    host = urlparse(final_url).hostname or "Web"

    # 1. Expand any discovered JSON media endpoints (e.g. get_media) using the SAME session CookieJar!
    api_candidates = []
    for api_u in parsed.get("api_urls") or []:
        api_candidates.extend(_fetch_json_media_list(api_u, referer=final_url, opener=opener, extra_cookies=session_ck))

    # Merge API-discovered streams (higher priority since they carry fresh tokens) with HTML candidates
    all_cands = []
    seen_u = set()
    for c in api_candidates + (parsed.get("candidates") or []):
        if c["url"] not in seen_u:
            seen_u.add(c["url"])
            all_cands.append(c)

    all_cands.sort(key=lambda x: (x.get("kind_rank", 2) == 3, -(x.get("q_num") or 0), x.get("kind_rank", 2)))

    if all_cands:
        # If we already have high-res direct MP4s from get_media/HTML (>=720p), prefer listing them directly
        # instead of immediately expanding an HLS master that might return 410.
        has_hd_direct = any((c.get("direct") and (c.get("q_num") or 0) >= 720) for c in all_cands)
        dead_urls = set()

        if not has_hd_direct:
            tried_streams = 0
            for c in all_cands:
                if c.get("is_stream") and tried_streams < 3:
                    tried_streams += 1
                    expanded = expand_stream_url(
                        c["url"],
                        clean_url=url,
                        referer=final_url,
                        title=parsed["title"] or host,
                        thumb=parsed["thumbnail"],
                        host=host,
                        opener=opener,
                        extra_cookies=session_ck,
                    )
                    if isinstance(expanded, dict) and expanded.get("_dead_code"):
                        dead_urls.add(c["url"])
                        continue
                    if expanded:
                        return expanded

        alive_cands = [c for c in all_cands if c["url"] not in dead_urls]
        if not alive_cands:
            alive_cands = list(all_cands)

        # Light ranged-GET check on top candidates to drop confirmed 404/410 dead links
        if len(alive_cands) > 1:
            verified = []
            for idx_p, c in enumerate(alive_cands):
                if idx_p < 4:
                    st_code = _probe_url_http_status(c["url"], referer=final_url, opener=opener, extra_cookies=session_ck)
                    if st_code in (404, 410):
                        continue
                verified.append(c)
            if verified:
                alive_cands = verified

        streams = []
        used_names = {}
        for idx, c in enumerate(alive_cands[:12], 1):
            base_lbl = c["name"]
            cnt = used_names.get(base_lbl, 0) + 1
            used_names[base_lbl] = cnt
            lbl = base_lbl if cnt == 1 else f"{base_lbl} #{cnt}"
            ext_hint = "HLS" if ".m3u8" in c["url"].lower() else ("DASH" if ".mpd" in c["url"].lower() else ("MP4" if ".mp4" in c["url"].lower() else ""))
            if ext_hint and base_lbl.upper() == ext_hint.upper():
                ext_hint = ""
            streams.append({
                "id": f"stream_{idx}",
                "name": lbl,
                "size": ext_hint,
                "url": c["url"],
                "direct": c["direct"],
            })
        return {
            "kind": "sniffed",
            "clean_url": url,
            "target_url": streams[0]["url"],
            "referer": final_url,
            "extra_cookies": session_ck,
            "title": parsed["title"] or host,
            "thumbnail": parsed["thumbnail"],
            "duration": None,
            "uploader": host,
            "extractor": host,
            "method_badge": "Deep Sniffer",
            "media_type": "video",
            "streams": streams,
            "entries": [],
        }

    # Check embedded iframes recursively (depth 0 -> 1)
    if depth < 1 and parsed["iframes"]:
        for iframe_url in parsed["iframes"]:
            try:
                sub = _run_ytdlp_info_raw(iframe_url, allow_playlist=False, use_cookies=_is_cookies_enabled(), referer=final_url)
                if sub:
                    sub["clean_url"] = url
                    sub["target_url"] = iframe_url
                    sub["referer"] = final_url
                    sub["method_badge"] = "Iframe Extract"
                    return sub
            except Exception:
                pass
            sub_sniff = sniff_page_media(iframe_url, depth=depth + 1)
            if sub_sniff:
                sub_sniff["clean_url"] = url
                if not sub_sniff.get("title") or sub_sniff["title"] == urlparse(iframe_url).hostname:
                    sub_sniff["title"] = parsed["title"] or sub_sniff.get("title") or host
                if not sub_sniff.get("thumbnail") and parsed["thumbnail"]:
                    sub_sniff["thumbnail"] = parsed["thumbnail"]
                return sub_sniff

    return None


def _is_facebook_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("facebook.com", "fb.watch", "fb.com") or host.endswith((".facebook.com", ".fb.watch", ".fb.com"))


def _is_youtube_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("youtube.com", "youtu.be", "youtube-nocookie.com") or host.endswith((".youtube.com", ".youtu.be", ".youtube-nocookie.com"))


def _is_google_drive_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("drive.google.com", "docs.google.com", "drive.usercontent.google.com")


def _parse_gdrive_url(url: str):
    """Extract (file_id, authuser) from Google Drive / Docs URLs."""
    parsed = urlparse(url)
    path = parsed.path or ""
    qs = parse_qs(parsed.query)

    authuser = (qs.get("authuser") or [""])[0].strip()
    if not authuser:
        m_u = re.search(r"/u/(\d+)/", path)
        if m_u:
            authuser = m_u.group(1)

    file_id = ""
    for pat in (
        r"/file/d/([A-Za-z0-9_-]{10,})",
        r"/d/([A-Za-z0-9_-]{10,})",
        r"/uc\b",
    ):
        if pat.startswith("/uc"):
            continue
        m_id = re.search(pat, path)
        if m_id:
            file_id = m_id.group(1)
            break
    if not file_id:
        for key in ("id", "docid"):
            val = (qs.get(key) or [""])[0].strip()
            if re.fullmatch(r"[A-Za-z0-9_-]{10,}", val):
                file_id = val
                break

    return file_id, authuser


def _extract_filename_from_cd(cd_header: str) -> str:
    if not cd_header:
        return ""
    m_star = re.search(r"filename\*\s*=\s*(?:UTF-8''|utf-8'')([^;]+)", cd_header)
    if m_star:
        try:
            from urllib.parse import unquote
            return unquote(m_star.group(1).strip().strip('"'))
        except Exception:
            pass
    m_plain = re.search(r'filename\s*=\s*"([^"]+)"', cd_header)
    if m_plain:
        return m_plain.group(1).strip()
    m_bare = re.search(r"filename\s*=\s*([^;\s]+)", cd_header)
    if m_bare:
        return m_bare.group(1).strip().strip('"')
    return ""


def _parse_gdrive_confirm_form(html_text: str, fallback_action: str = "https://drive.usercontent.google.com/download") -> str:
    """Parse Google Drive's large-file virus-scan confirmation HTML form into a direct download URL."""
    if not html_text:
        return ""
    m_form = re.search(r'<form[^>]+id=["\']download-form["\'][^>]*>([\s\S]*?)</form>', html_text, re.I)
    if not m_form:
        m_form = re.search(r'<form[^>]+action=["\']([^"\']*download[^"\']*)["\'][^>]*>([\s\S]*?)</form>', html_text, re.I)
        if not m_form:
            return ""
        action = html_lib.unescape(m_form.group(1)).strip() or fallback_action
        inner = m_form.group(2)
    else:
        full_tag = m_form.group(0)
        m_act = re.search(r'action=["\']([^"\']+)["\']', full_tag, re.I)
        action = html_lib.unescape(m_act.group(1)).strip() if m_act else fallback_action
        inner = m_form.group(1)

    params = []
    for m_inp in re.finditer(r"<input\b([^>]+)>", inner, re.I):
        attrs = m_inp.group(1)
        m_n = re.search(r'name=["\']([^"\']+)["\']', attrs, re.I)
        m_v = re.search(r'value=["\']([^"\']*)["\']', attrs, re.I)
        if m_n:
            params.append(f"{quote(m_n.group(1), safe='')}={quote(html_lib.unescape(m_v.group(1)) if m_v else '', safe='')}")
    if not params:
        return ""
    sep = "&" if "?" in action else "?"
    return f"{action}{sep}{'&'.join(params)}"


def _parse_gdrive_fmt_stream_map(raw_map: str) -> list:
    itag_map = {
        "37": (1080, "1080p"),
        "22": (720, "720p"),
        "59": (480, "480p"),
        "18": (360, "360p"),
        "36": (240, "240p"),
    }
    out = []
    seen = set()
    for part in (raw_map or "").split(","):
        if "|" not in part:
            continue
        itag, s_url = part.split("|", 1)
        itag = itag.strip()
        s_url = s_url.strip()
        if not s_url.startswith(("http://", "https://")) or s_url in seen:
            continue
        seen.add(s_url)
        q_num, q_lbl = itag_map.get(itag, (0, f"Stream ({itag})"))
        out.append({"q_num": q_num, "name": q_lbl, "url": s_url, "size": "MP4"})
    out.sort(key=lambda x: x["q_num"], reverse=True)
    return out


def extract_google_drive_media(url: str, base_host: str = ""):
    """Fallback extractor for Google Drive when yt-dlp fails with HTTP 403 on JSON metadata.
    Tries:
      1) get_video_info (preserving /u/N/ authuser & capturing DRIVE_STREAM session cookie)
      2) Direct original file download via drive.usercontent.google.com (including virus-scan form confirm)
    """
    file_id, authuser = _parse_gdrive_url(url)
    if not file_id:
        return None

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_http_context()),
        urllib.request.HTTPCookieProcessor(cj),
    )

    u_prefix = f"/u/{authuser}" if authuser else ""
    auth_qs = f"&authuser={quote(authuser, safe='')}" if authuser else ""
    drive_origin = base_host.rstrip("/") if base_host else "https://drive.google.com"
    usercontent_origin = base_host.rstrip("/") if base_host else "https://drive.usercontent.google.com"
    view_referer = f"{drive_origin}{u_prefix}/file/d/{file_id}/view"

    title = ""
    duration = None
    streams = []
    extra_ck = ""
    saw_403 = False
    saw_quota = False
    saw_login = False

    # 1. Try get_video_info (works even when direct download is disabled / View-Only if cookies grant view access)
    info_urls = [
        f"{drive_origin}{u_prefix}/get_video_info?docid={file_id}",
    ]
    if not base_host:
        info_urls.append(f"https://docs.google.com{u_prefix}/get_video_info?docid={file_id}")

    for info_u in info_urls:
        try:
            req = _build_request(info_u, referer=view_referer, method="GET")
            req.add_header("User-Agent", DESKTOP_UA)
            with opener.open(req, timeout=15) as resp:
                raw_txt = resp.read(512000).decode("utf-8", errors="ignore")
            kv = parse_qs(raw_txt, keep_blank_values=True)
            status_v = (kv.get("status") or [""])[0].lower()
            reason_v = (kv.get("reason") or [""])[0]
            if "quota" in reason_v.lower() or "too many" in reason_v.lower():
                saw_quota = True
            if status_v == "ok":
                title = (kv.get("title") or [""])[0].strip() or title
                dur_s = safe_float((kv.get("length_seconds") or [""])[0])
                if dur_s and dur_s > 0:
                    duration = int(dur_s)
                fmt_map = (kv.get("fmt_stream_map") or [""])[0]
                parsed_streams = _parse_gdrive_fmt_stream_map(fmt_map)
                if parsed_streams:
                    extra_ck = _cookiejar_header_for_url(cj, parsed_streams[0]["url"])
                    for idx, st in enumerate(parsed_streams, 1):
                        streams.append({
                            "id": f"stream_{idx}",
                            "name": st["name"],
                            "size": st["size"],
                            "url": st["url"],
                            "direct": True,
                        })
                    break
        except urllib.error.HTTPError as he:
            if he.code == 403:
                saw_403 = True
        except Exception:
            pass

    # 2. Probe direct original file download (often works when yt-dlp JSON metadata returns 403!)
    direct_candidates = [
        f"{usercontent_origin}/download?id={file_id}&export=download&confirm=t{auth_qs}",
        f"{drive_origin}{u_prefix}/uc?export=download&id={file_id}&confirm=t",
    ]
    for dl_u in direct_candidates:
        try:
            req = _build_request(dl_u, referer=view_referer, method="GET", extra_cookies=extra_ck)
            req.add_header("User-Agent", DESKTOP_UA)
            req.add_header("Range", "bytes=0-65535")
            with opener.open(req, timeout=15) as resp:
                final_u = resp.geturl() or dl_u
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                cd = resp.headers.get("Content-Disposition") or ""
                cr = resp.headers.get("Content-Range") or ""
                cl = resp.headers.get("Content-Length") or ""

                if "accounts.google.com" in final_u.lower():
                    saw_login = True
                    continue

                if ctype.startswith("text/html"):
                    html_chunk = resp.read(256000).decode("utf-8", errors="ignore")
                    low_h = html_chunk.lower()
                    if "quota exceeded" in low_h or "too many users have viewed or downloaded" in low_h:
                        saw_quota = True
                        continue
                    confirm_u = _parse_gdrive_confirm_form(html_chunk, fallback_action=f"{usercontent_origin}/download")
                    if confirm_u:
                        fname_m = re.search(r'<a[^>]+href=["\']/open\?id=[^"\']+["\'][^>]*>([^<]+)</a>', html_chunk, re.I)
                        if fname_m and not title:
                            title = html_lib.unescape(fname_m.group(1)).strip()
                        size_m = re.search(r"\((\d+(?:\.\d+)?\s*[KMGT]?B)\)", html_chunk, re.I)
                        sz_txt = size_m.group(1) if size_m else "Original"
                        sess_ck = _cookiejar_header_for_url(cj, confirm_u)
                        extra_ck = _merge_cookie_headers(extra_ck, sess_ck)
                        streams.insert(0, {
                            "id": "stream_orig",
                            "name": "Original File",
                            "size": sz_txt,
                            "url": confirm_u,
                            "direct": True,
                        })
                        break
                else:
                    # Direct binary media response!
                    total_bytes = None
                    m_cr = re.search(r"/(\d+)$", cr.strip())
                    if m_cr:
                        total_bytes = int(m_cr.group(1))
                    elif cl.isdigit() and int(cl) > 65536:
                        total_bytes = int(cl)
                    fname = _extract_filename_from_cd(cd)
                    if fname and not title:
                        title = Path(fname).stem
                    sz_txt = fmt_mb(total_bytes) if total_bytes else "Original"
                    sess_ck = _cookiejar_header_for_url(cj, final_u)
                    extra_ck = _merge_cookie_headers(extra_ck, sess_ck)
                    streams.insert(0, {
                        "id": "stream_orig",
                        "name": "Original File",
                        "size": sz_txt,
                        "url": final_u,
                        "direct": True,
                    })
                    break
        except urllib.error.HTTPError as he:
            if he.code == 403:
                saw_403 = True
        except Exception:
            pass

    if streams:
        # Re-index stream IDs cleanly
        for i, s in enumerate(streams, 1):
            s["id"] = f"stream_{i}"
        return {
            "kind": "sniffed",
            "clean_url": url,
            "target_url": streams[0]["url"],
            "referer": view_referer,
            "extra_cookies": extra_ck,
            "title": sanitize_filename(title, f"GoogleDrive_{file_id[:8]}"),
            "thumbnail": f"https://drive.google.com/thumbnail?id={file_id}&sz=w640" if not base_host else "",
            "duration": duration,
            "uploader": "Google Drive",
            "extractor": "GoogleDrive",
            "method_badge": "Drive Direct" if streams[0]["name"] == "Original File" else "Drive Stream",
            "media_type": "video",
            "streams": streams[:10],
            "entries": [],
        }

    # Build a clear, actionable Google Drive error message instead of raw yt-dlp JSON 403
    if saw_quota:
        raise RuntimeError(
            "Google Drive download quota exceeded for this file.\n"
            "Fix: Open the file in Google Drive -> click 'Add shortcut to Drive' or 'Make a copy' into your own Drive, then fetch your copy."
        )
    if saw_login:
        raise RuntimeError(
            "Google Drive requires login for this file.\n"
            "Fix: Make sure you are signed into the right Google account in Chrome and 'Auto Chrome Cookies' is ON in Settings (⚙)."
        )
    if saw_403:
        raise RuntimeError(
            "Google Drive returned 403 Forbidden (access denied or view-only protected).\n"
            "Check:\n"
            "1) Are you using a secondary Google account (/u/1/)? Open the file directly in Chrome and retry.\n"
            "2) Did the owner disable downloading (View-Only)?\n"
            "3) In Settings (⚙), verify 'Auto Chrome Cookies' is ON and click 'Update yt-dlp'."
        )
    return None


def _unwrap_facebook_login_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if (host == "facebook.com" or host.endswith(".facebook.com")) and "login" in (parsed.path or "").lower():
        qs = parse_qs(parsed.query)
        nxt = (qs.get("next") or [""])[0].strip()
        if nxt.startswith(("http://", "https://")):
            return nxt
    return url


def _has_facebook_auth_cookies() -> bool:
    hdr = cookie_header_for_url("https://www.facebook.com/")
    if not hdr:
        return False
    names = {part.split("=", 1)[0].strip() for part in hdr.split(";") if "=" in part}
    return "c_user" in names and "xs" in names


def _build_desktop_fb_request(url: str, referer: str = "https://www.facebook.com/"):
    headers = {
        "User-Agent": DESKTOP_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    }
    if referer:
        headers["Referer"] = referer
    ck = cookie_header_for_url(url)
    if ck:
        headers["Cookie"] = ck
    return urllib.request.Request(url, headers=headers, method="GET")


def _unescape_fb_url(raw: str) -> str:
    if not raw:
        return ""
    return (
        raw.replace("\\/", "/")
        .replace("\\u0026", "&")
        .replace("\\u0025", "%")
        .replace("\\u003d", "=")
        .replace("\\u003D", "=")
        .replace("&amp;", "&")
        .strip()
        .strip("\"'")
    )


def _parse_fb_dash_manifest(xml_text: str):
    """Parse Facebook DASH MPD XML into (video_tracks, best_audio_url)."""
    if not xml_text or "<Representation" not in xml_text:
        return [], ""

    videos = []
    audios = []

    try:
        root = ET.fromstring(xml_text.strip())
        for aset in root.iter():
            tag_a = aset.tag.split("}")[-1] if "}" in aset.tag else aset.tag
            if tag_a != "AdaptationSet":
                continue
            aset_mime = (aset.attrib.get("mimeType") or "").lower()
            aset_ct = (aset.attrib.get("contentType") or "").lower()

            for rep in aset:
                tag_r = rep.tag.split("}")[-1] if "}" in rep.tag else rep.tag
                if tag_r != "Representation":
                    continue
                base_url = ""
                for child in rep:
                    tag_c = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if tag_c == "BaseURL" and child.text:
                        base_url = html_lib.unescape(child.text.strip())
                        break
                if not base_url.startswith(("http://", "https://")):
                    continue

                mime = (rep.attrib.get("mimeType") or aset_mime).lower()
                codecs = (rep.attrib.get("codecs") or "").lower()
                bw = int(safe_float(rep.attrib.get("bandwidth")) or 0)
                w = int(safe_float(rep.attrib.get("width")) or 0)
                h = int(safe_float(rep.attrib.get("height")) or 0)
                q_lbl = (rep.attrib.get("FBQualityLabel") or "").strip()

                is_audio = (
                    mime.startswith("audio/")
                    or aset_ct == "audio"
                    or (not w and not h and ("mp4a" in codecs or "opus" in codecs or "audio" in mime))
                )
                if is_audio:
                    is_aac = ("mp4a" in codecs or "aac" in codecs or "mp4" in mime)
                    audios.append({"url": base_url, "bw": bw, "is_aac": is_aac})
                else:
                    q_num = min(w, h) if (w and h) else (h or 0)
                    if not q_num and q_lbl:
                        q_num, _ = _parse_quality_from_text(q_lbl)
                        q_num = q_num or 0
                    is_h264 = ("avc" in codecs or "h264" in codecs)
                    videos.append({
                        "url": base_url,
                        "q_num": q_num,
                        "bw": bw,
                        "is_h264": is_h264,
                        "label": q_lbl,
                    })
    except Exception:
        # Regex fallback if XML has minor namespace/entity issues
        for m in re.finditer(r"<Representation\b([^>]*)>([\s\S]*?)</Representation>", xml_text, re.I):
            attrs, body = m.group(1), m.group(2)
            m_u = re.search(r"<BaseURL[^>]*>(https?://[^<]+)</BaseURL>", body, re.I)
            if not m_u:
                continue
            base_url = html_lib.unescape(m_u.group(1).strip())
            m_mime = re.search(r'mimeType=["\']([^"\']+)["\']', attrs, re.I)
            m_cod = re.search(r'codecs=["\']([^"\']+)["\']', attrs, re.I)
            m_w = re.search(r'width=["\'](\d+)["\']', attrs, re.I)
            m_h = re.search(r'height=["\'](\d+)["\']', attrs, re.I)
            m_bw = re.search(r'bandwidth=["\'](\d+)["\']', attrs, re.I)
            m_ql = re.search(r'FBQualityLabel=["\']([^"\']+)["\']', attrs, re.I)
            mime = (m_mime.group(1) if m_mime else "").lower()
            codecs = (m_cod.group(1) if m_cod else "").lower()
            w = int(m_w.group(1)) if m_w else 0
            h = int(m_h.group(1)) if m_h else 0
            bw = int(m_bw.group(1)) if m_bw else 0
            q_lbl = m_ql.group(1) if m_ql else ""
            if mime.startswith("audio/") or (not w and not h and ("mp4a" in codecs or "opus" in codecs)):
                audios.append({"url": base_url, "bw": bw, "is_aac": ("mp4a" in codecs or "mp4" in mime)})
            else:
                q_num = min(w, h) if (w and h) else h
                videos.append({
                    "url": base_url,
                    "q_num": q_num,
                    "bw": bw,
                    "is_h264": ("avc" in codecs or "h264" in codecs),
                    "label": q_lbl,
                })

    best_audio = ""
    if audios:
        audios.sort(key=lambda a: (1 if a["is_aac"] else 0, a["bw"]), reverse=True)
        best_audio = audios[0]["url"]

    return videos, best_audio


def parse_facebook_story_html(html_text: str, page_url: str) -> dict:
    story_id = ""
    m_sid = re.search(r"/stories/[^/?#]+/([^/?#]+)", page_url)
    if m_sid:
        story_id = m_sid.group(1)

    search_scopes = []
    if story_id and story_id in html_text:
        idx = html_text.find(story_id)
        search_scopes.append(html_text[max(0, idx - 12000): min(len(html_text), idx + 95000)])
    search_scopes.append(html_text)

    title = ""
    m_og_t = re.search(r'<meta[^>]+(?:property|name)=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html_text, re.I)
    if m_og_t:
        title = html_lib.unescape(m_og_t.group(1)).strip()
    if not title or title.lower() in ("facebook", "log in or sign up to view"):
        m_owner = re.search(r'"story_bucket_owner"\s*:\s*\{[^{}]{0,400}"name"\s*:\s*"([^"]+)"', html_text)
        if m_owner:
            try:
                title = json.loads(f'"{m_owner.group(1)}"') + " — Story"
            except Exception:
                title = m_owner.group(1) + " — Story"
    if not title or title.lower() == "facebook":
        title = "Facebook Story"

    # Collect all video candidates across DASH MPD, modern Relay progressive_urls, and legacy HD/SD fields
    buckets = {}

    def _add_video_cand(url: str, q_num: int, label: str, bw: int = 0, is_h264: bool = True, audio_url: str = "", is_muxed: bool = False):
        if not url or not url.startswith(("http://", "https://")):
            return
        if q_num >= 2160:
            lbl = f"{q_num}p (4K)"
        elif q_num > 0:
            lbl = f"{q_num}p"
        else:
            lbl = label or "MP4"

        b_key = f"q_{q_num}" if q_num > 0 else f"l_{lbl.lower()}"
        score = (
            1 if is_h264 else 0,
            1 if is_muxed else 0,
            bw,
        )
        size_hint = _fmt_bitrate(bw / 1000.0) if (q_num > 0 and bw > 0) else ("Muxed" if is_muxed else "")
        cand = {
            "url": url,
            "audio_url": audio_url,
            "name": lbl,
            "size": size_hint,
            "q_num": q_num,
            "bw": bw,
            "score": score,
        }
        prev = buckets.get(b_key)
        if prev is None or cand["score"] > prev["score"]:
            buckets[b_key] = cand

    for scope in search_scopes:
        # 1. DASH MPD Manifests (where Facebook puts real 1080p / 720p HD on Stories!)
        for m_dm in re.finditer(r'"(?:dash_manifest|dash_manifest_xml_string)"\s*:\s*"((?:\\.|[^"\\])+)"', scope):
            raw_xml = m_dm.group(1)
            try:
                xml_decoded = json.loads(f'"{raw_xml}"')
            except Exception:
                xml_decoded = _unescape_fb_url(raw_xml).replace("\\n", "\n").replace('\\"', '"')
            v_tracks, best_aud = _parse_fb_dash_manifest(xml_decoded)
            for vt in v_tracks:
                _add_video_cand(
                    url=vt["url"],
                    q_num=vt["q_num"],
                    label=vt["label"] or "HD · MP4",
                    bw=vt["bw"],
                    is_h264=vt["is_h264"],
                    audio_url=best_aud,
                    is_muxed=(not best_aud),
                )

        # 2. Modern Facebook Relay progressive_urls (HD / SD)
        prog_pats = [
            (0, r'"progressive_url"\s*:\s*"(https?:[^"]+)"[^\}]{0,160}"quality"\s*:\s*"(HD|SD)"'),
            (1, r'"quality"\s*:\s*"(HD|SD)"[^\}]{0,160}"progressive_url"\s*:\s*"(https?:[^"]+)"'),
        ]
        for order, pat in prog_pats:
            for m_pr in re.finditer(pat, scope, re.I):
                u = _unescape_fb_url(m_pr.group(1) if order == 0 else m_pr.group(2))
                ql = (m_pr.group(2) if order == 0 else m_pr.group(1)).upper()
                q_num = 720 if ql == "HD" else 360
                _add_video_cand(url=u, q_num=0, label=f"{ql} · MP4", bw=2500000 if ql == "HD" else 600000, is_h264=True, audio_url="", is_muxed=True)

        # 3. Progressive HD & SD fields (legacy & fallback)
        video_patterns = [
            ("HD · MP4", 2600000, r'"browser_native_hd_url"\s*:\s*"(https?:[^"]+)"'),
            ("HD · MP4", 2600000, r'"playable_url_quality_hd"\s*:\s*"(https?:[^"]+)"'),
            ("HD · MP4", 2600000, r'"hd_src(?:_no_ratelimit)?"\s*:\s*"(https?:[^"]+)"'),
            ("SD · MP4", 550000, r'"browser_native_sd_url"\s*:\s*"(https?:[^"]+)"'),
            ("SD · MP4", 550000, r'"playable_url"\s*:\s*"(https?:[^"]+)"'),
            ("SD · MP4", 550000, r'"sd_src(?:_no_ratelimit)?"\s*:\s*"(https?:[^"]+)"'),
        ]
        for label, est_bw, pat in video_patterns:
            for m in re.finditer(pat, scope):
                u = _unescape_fb_url(m.group(1))
                _add_video_cand(url=u, q_num=0, label=label, bw=est_bw, is_h264=True, audio_url="", is_muxed=True)

        # Only stop at narrow scope if we actually found HD/DASH quality (>=720p or HD · MP4);
        # otherwise continue to full HTML so we never settle for SD when HD is outside the narrow window!
        has_hd_here = any((c["q_num"] >= 720 or "HD" in c["name"]) for c in buckets.values())
        if has_hd_here:
            break

    # Sort all discovered video qualities: highest numeric resolution first, then HD before SD
    def _sort_key(c):
        rank_q = c["q_num"]
        if rank_q == 0:
            rank_q = 715 if "HD" in c["name"] else 350
        return (rank_q, c["bw"])

    sorted_vids = sorted(buckets.values(), key=_sort_key, reverse=True)
    streams = []
    seen_stream_urls = set()
    for v in sorted_vids[:10]:
        if v["url"] in seen_stream_urls:
            continue
        seen_stream_urls.add(v["url"])
        streams.append({
            "id": f"stream_{len(streams) + 1}",
            "name": v["name"],
            "size": v["size"],
            "url": v["url"],
            "audio_url": v["audio_url"],
            "direct": True,
        })

    # Extract preview/thumbnail or photo story image
    thumb = ""
    photo_patterns = [
        r'"__typename"\s*:\s*"Photo"[^{]{0,1200}?"image"\s*:\s*\{\s*"uri"\s*:\s*"(https?:[^"]+)"',
        r'"full_width_image"\s*:\s*\{\s*"uri"\s*:\s*"(https?:[^"]+)"',
        r'"previewImage"\s*:\s*\{\s*"uri"\s*:\s*"(https?:[^"]+)"',
        r'"preferred_thumbnail"\s*:\s*\{[\s\S]{0,300}?"uri"\s*:\s*"(https?:[^"]+)"',
    ]
    photo_candidates = []
    seen_imgs = set()
    for scope in search_scopes:
        for pat in photo_patterns:
            for m in re.finditer(pat, scope):
                u = _unescape_fb_url(m.group(1))
                low = u.lower()
                if not u.startswith(("http://", "https://")) or u in seen_imgs:
                    continue
                if any(bad in low for bad in ("static.xx.fbcdn", "emoji", "p50x50", "s40x40", "s60x60", "p100x100", "s100x100", "c0.0.")):
                    continue
                seen_imgs.add(u)
                photo_candidates.append(u)
        if photo_candidates:
            break

    if photo_candidates:
        thumb = photo_candidates[0]

    if not streams and photo_candidates:
        for idx, pu in enumerate(photo_candidates[:6], 1):
            lbl = "Photo · Original" if len(photo_candidates) == 1 else f"Photo #{idx}"
            streams.append({
                "id": f"stream_{idx}",
                "name": lbl,
                "size": "",
                "url": pu,
                "audio_url": "",
                "direct": True,
            })

    if not streams:
        return None

    is_photo_only = all(s["name"].startswith("Photo") for s in streams)
    has_dash_hd = any(bool(s.get("audio_url")) for s in streams)
    return {
        "kind": "sniffed",
        "clean_url": page_url,
        "target_url": streams[0]["url"],
        "referer": "https://www.facebook.com/",
        "title": title,
        "thumbnail": thumb,
        "duration": None,
        "uploader": "Facebook",
        "extractor": "Facebook Story",
        "method_badge": "Photo Story" if is_photo_only else ("FB Story HD" if has_dash_hd else "FB Story"),
        "media_type": "image" if is_photo_only else "video",
        "streams": streams[:10],
        "entries": [],
    }


def extract_facebook_story_or_resolve(url: str):
    clean_u = _unwrap_facebook_login_url(url)
    has_fb_ck = _has_facebook_auth_cookies()

    try:
        req = _build_desktop_fb_request(clean_u)
        with urllib.request.urlopen(req, timeout=15, context=_http_context()) as resp:
            final_url = resp.geturl() or clean_u
            low_f = final_url.lower()
            # Only download the full 2.5 MB HTML body if this actually resolved to a Facebook Story!
            # For /share/r/ (Reels) and /share/v/ (Videos), reading 0 bytes cuts 2-4 seconds immediately.
            is_story_dest = ("/stories/" in low_f) or ("/stories/" in clean_u.lower() and "/login" not in low_f and "/checkpoint/" not in low_f)
            if is_story_dest:
                html_text = resp.read(2500000).decode("utf-8", errors="ignore")
            else:
                html_text = ""
    except Exception as exc:
        if "/stories/" in clean_u.lower() and not has_fb_ck:
            raise RuntimeError(
                "Facebook Stories require login cookies (even for public pages).\n\n"
                "Open Settings (⚙) -> enable Session Cookies and save fresh Facebook cookies (c_user & xs)."
            )
        return {"resolved_url": clean_u, "story_result": None, "login_required": False, "error": str(exc)}

    low_final = final_url.lower()
    if "/login.php" in low_final or "/login/" in low_final or "/checkpoint/" in low_final:
        unwrapped = _unwrap_facebook_login_url(final_url)
        if not has_fb_ck:
            msg = (
                "Facebook requires login to view Stories (even on public pages).\n\n"
                "Open Settings (⚙) -> enable Session Cookies and save Facebook cookies (c_user & xs)."
            )
        else:
            msg = (
                "Your Facebook session cookies expired or were rejected by Facebook.\n\n"
                "Re-export fresh cookies from facebook.com (while logged in) and save them in Settings (⚙)."
            )
        return {"resolved_url": unwrapped, "story_result": None, "login_required": True, "error": msg}

    story_res = None
    if "/stories/" in low_final or "/stories/" in clean_u.lower():
        story_res = parse_facebook_story_html(html_text, final_url)

    return {
        "resolved_url": final_url,
        "story_result": story_res,
        "login_required": False,
        "error": "",
    }


def _is_explicit_playlist_url(url: str) -> bool:
    parsed = urlparse(url)
    low = url.lower()
    path = (parsed.path or "").lower()
    qs = parse_qs(parsed.query)
    list_id = (qs.get("list") or [""])[0].strip()

    if "/playlist" in path or "/sets/" in path or "/album/" in path:
        return True
    if "music.youtube.com" in low and ("/browse/" in path or bool(list_id)):
        if list_id.upper().startswith("RD"):
            return False
        return True
    # YouTube watch URL containing a real playlist or album (PL..., OLAK..., UU..., LL..., FL...)
    if list_id and not list_id.upper().startswith(("RD", "WL")):
        if "youtube.com" in low or "youtu.be" in low:
            return True
    return False


def _should_allow_playlist(url: str) -> bool:
    if _is_explicit_playlist_url(url):
        return True
    low = url.lower()
    if any(k in low for k in ("instagram.com/", "x.com/", "twitter.com/", "tiktok.com/", "soundcloud.com/", "reddit.com/")):
        return True
    return False


def _extract_ytdlp_video_formats(primary: dict):
    raw_formats = primary.get("formats") or []
    best_audio_bytes = 0
    has_separate_audio = False

    for f in raw_formats:
        if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none"):
            has_separate_audio = True
            sz = f.get("filesize") or f.get("filesize_approx") or 0
            if sz and sz > best_audio_bytes:
                best_audio_bytes = sz

    # Group video candidates by quality key so every available resolution/tier is preserved and best codec/bitrate wins
    buckets = {}

    for f in raw_formats:
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        ext = (f.get("ext") or "").lower()
        fid = str(f.get("format_id") or "").strip()
        fnote = str(f.get("format_note") or "").strip()
        fmt_str = str(f.get("format") or "").strip()

        # Skip audio-only, thumbnails, storyboards, subtitles
        if vcodec == "none":
            continue
        if ext in ("mhtml", "jpg", "jpeg", "png", "webp", "vtt", "srt", "ttml"):
            continue
        if "storyboard" in fnote.lower() or "storyboard" in fid.lower():
            continue
        if not fid and not f.get("height") and not f.get("width"):
            continue

        w = safe_float(f.get("width"))
        h = safe_float(f.get("height"))
        w = int(w) if (w and w > 0) else None
        h = int(h) if (h and h > 0) else None

        if not h and f.get("resolution"):
            m_r = re.search(r"(\d{3,4})\s*[x×]\s*(\d{3,4})", str(f.get("resolution")))
            if m_r:
                w, h = int(m_r.group(1)), int(m_r.group(2))

        tbr = safe_float(f.get("tbr")) or safe_float(f.get("vbr")) or 0.0
        fps = safe_float(f.get("fps")) or 0.0

        q_num = None
        label = ""
        if w and h:
            q_num = min(w, h)
            label = f"{q_num}p (4K)" if q_num >= 2160 else f"{q_num}p"
        elif h:
            q_num = h
            label = f"{q_num}p (4K)" if q_num >= 2160 else f"{q_num}p"
        else:
            q_num, label = _parse_quality_from_text(f"{fnote} {fid} {fmt_str}")
            if not label:
                if fnote and fnote.lower() not in ("default", "video"):
                    label = fnote
                elif tbr > 0:
                    label = _fmt_bitrate(tbr)
                elif fid:
                    label = fid.upper() if len(fid) <= 6 else fid[:14]
                else:
                    label = ext.upper() or "Video"

        is_video_only = (acodec == "none")
        is_mp4 = (ext in ("mp4", "m4v"))
        is_h264 = "avc" in str(vcodec or "").lower() or "h264" in str(vcodec or "").lower()

        sz = f.get("filesize") or f.get("filesize_approx") or 0
        if sz and is_video_only and best_audio_bytes:
            sz += best_audio_bytes

        # Build an exact, reliable yt-dlp format selector for this specific quality (prefer AAC/m4a audio for WhatsApp/Gallery)
        fid_safe = bool(fid and re.fullmatch(r"[A-Za-z0-9._-]+", fid))
        if fid_safe:
            if is_video_only and has_separate_audio:
                selector = f"{fid}+bestaudio[ext=m4a]/{fid}+bestaudio/{fid}/best"
            else:
                selector = f"{fid}/best"
        elif h:
            selector = f"bestvideo[height<={h}]+bestaudio[ext=m4a]/bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
        else:
            selector = "best"

        # Score candidate within its quality bucket: prioritize H.264 (avc1) + MP4 for WhatsApp/Android compatibility
        is_aac = "mp4a" in str(acodec or "").lower() or "aac" in str(acodec or "").lower()
        score = (
            1 if is_h264 else 0,
            1 if is_mp4 else 0,
            1 if (not is_video_only and is_aac) else 0,
            1 if (not is_video_only) else 0,
            1 if fps >= 50 else 0,
            tbr,
            sz or 0,
        )

        # Bucket key: numeric resolution if known, otherwise normalized label
        bucket_key = f"res_{q_num}" if q_num else f"lbl_{label.lower()}"

        size_hint = fmt_mb(sz, approx=True) if sz else (_fmt_bitrate(tbr) if (q_num and tbr > 0) else "")
        cand = {
            "id": selector,
            "name": label,
            "size": size_hint,
            "sort_q": q_num or 0,
            "sort_tbr": tbr,
            "score": score,
        }

        prev = buckets.get(bucket_key)
        if prev is None or cand["score"] > prev["score"]:
            buckets[bucket_key] = cand

    sorted_items = sorted(
        buckets.values(),
        key=lambda x: (x["sort_q"], x["sort_tbr"]),
        reverse=True,
    )

    formats = [{"id": "best", "name": "Best", "size": sorted_items[0]["name"] if sorted_items else ""}]
    for item in sorted_items[:12]:
        formats.append({
            "id": item["id"],
            "name": item["name"],
            "size": item["size"],
        })

    return formats, best_audio_bytes


GENERIC_MEDIA_TITLES = {
    "facebook", "facebook video", "facebook watch", "facebook reel", "watch",
    "log in or sign up to view", "instagram", "instagram video", "reel", "reels",
    "tiktok", "video", "untitled", "media", "stream", "download", "x", "twitter",
    "shorts", "status", "story", "facebook story",
}


def _is_generic_title(name: str) -> bool:
    s = re.sub(r"\s+", " ", (name or "").strip()).lower()
    if not s or s in GENERIC_MEDIA_TITLES:
        return True
    if re.fullmatch(r"(?:facebook|instagram|tiktok|video|reel|watch|media|stream)(?:\s*\(\d+\))?", s):
        return True
    return False


def _pick_smart_media_title(raw_title: str, primary: dict, data: dict, fallback_extractor: str = "") -> str:
    title = (raw_title or "").strip()
    if title and not _is_generic_title(title):
        return title

    # Try first meaningful line from description / fulltitle
    for field in ("description", "fulltitle", "alt_title"):
        val = (primary.get(field) or data.get(field) or "").strip()
        if not val:
            continue
        for ln in val.splitlines():
            cand = re.sub(r"https?://\S+", "", ln).strip(" -_|·•#")
            cand = re.sub(r"\s+", " ", cand).strip()
            if len(cand) >= 4 and not _is_generic_title(cand):
                return cand[:75]

    vid_id = str(primary.get("id") or data.get("id") or "").strip()
    uploader = _clean_artist_name(
        primary.get("uploader") or data.get("uploader") or primary.get("creator") or data.get("channel") or ""
    )
    ext_lbl = (fallback_extractor or primary.get("extractor_key") or data.get("extractor_key") or "Video").strip()
    if uploader and not _is_generic_title(uploader):
        return f"{uploader}_{vid_id[:12]}" if vid_id else f"{uploader}_{time.strftime('%H%M%S')}"
    if vid_id:
        return f"{ext_lbl}_{vid_id[:16]}"
    return f"{title or ext_lbl}_{time.strftime('%Y%m%d_%H%M%S')}"


def _clean_artist_name(name: str) -> str:
    if not name:
        return ""
    return re.sub(r"\s*-\s*Topic$", "", str(name).strip(), flags=re.I)


def _format_ytdlp_payload(data: dict, clean_url: str, referer: str = "") -> dict:
    entries_raw = data.get("entries")
    entries_out = []
    total_entries = 0
    primary = data

    if isinstance(entries_raw, list) and len(entries_raw) > 0:
        valid_entries = [e for e in entries_raw if isinstance(e, dict)]
        total_entries = len(valid_entries)
        if len(valid_entries) == 1:
            primary = valid_entries[0]
        elif len(valid_entries) > 1:
            primary = valid_entries[0]
            for idx, e in enumerate(valid_entries[:200], 1):
                e_thumb = e.get("thumbnail")
                if not e_thumb and e.get("thumbnails"):
                    e_thumb = e["thumbnails"][-1].get("url")
                entries_out.append({
                    "index": idx,
                    "title": e.get("title") or f"Item {idx}",
                    "duration": e.get("duration"),
                    "thumbnail": e_thumb,
                })

    formats, best_audio_bytes = _extract_ytdlp_video_formats(primary)

    # Flat-playlist fetch does not include per-video formats -> expose standard selectable presets
    if len(entries_out) > 1 and len(formats) <= 1:
        formats = [
            {"id": "best", "name": "Best", "size": "Auto"},
            {"id": "bestvideo[height<=1080]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best", "name": "1080p", "size": ""},
            {"id": "bestvideo[height<=720]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best", "name": "720p", "size": ""},
            {"id": "bestvideo[height<=480]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]/best", "name": "480p", "size": ""},
            {"id": "bestvideo[height<=360]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best[height<=360]/best", "name": "360p", "size": ""},
        ]

    thumb = data.get("thumbnail") or primary.get("thumbnail")
    if not thumb and data.get("thumbnails"):
        thumb = data["thumbnails"][-1].get("url")
    if not thumb and primary.get("thumbnails"):
        thumb = primary["thumbnails"][-1].get("url")

    raw_artist = (
        primary.get("artist")
        or (primary.get("artists")[0] if isinstance(primary.get("artists"), list) and primary.get("artists") else "")
        or primary.get("creator")
        or data.get("uploader")
        or primary.get("uploader")
        or data.get("channel")
        or primary.get("channel")
        or ""
    )
    artist = _clean_artist_name(raw_artist)
    album = primary.get("album") or (data.get("title") if len(entries_out) > 1 else "") or ""
    track_title = primary.get("track") if (len(entries_out) <= 1 and primary.get("track")) else ""
    raw_disp = track_title or data.get("title") or primary.get("title") or ""
    ext_key = data.get("extractor_key") or primary.get("extractor_key") or data.get("extractor") or "Web"
    display_title = _pick_smart_media_title(raw_disp, primary, data, fallback_extractor=ext_key)

    low_u = (clean_url or "").lower()
    prefer_audio = ("music.youtube.com" in low_u) or ("soundcloud.com" in low_u) or bool(primary.get("album") and primary.get("track"))

    badge = f"{total_entries} Items" if total_entries > 1 else ("Music Tags" if (primary.get("artist") or primary.get("album")) else "")

    return {
        "kind": "ytdlp",
        "clean_url": clean_url,
        "target_url": clean_url,
        "referer": referer,
        "title": display_title,
        "artist": artist,
        "album": album if album != display_title else "",
        "prefer_audio": prefer_audio,
        "thumbnail": thumb or "",
        "duration": primary.get("duration") if not entries_out else data.get("duration"),
        "uploader": _clean_artist_name(data.get("uploader") or primary.get("uploader") or data.get("channel") or primary.get("channel") or ""),
        "extractor": data.get("extractor_key") or primary.get("extractor_key") or data.get("extractor") or "Web",
        "method_badge": badge,
        "media_type": "audio" if prefer_audio else "video",
        "formats": formats,
        "audio_size": fmt_mb(best_audio_bytes, approx=True) if best_audio_bytes else "",
        "entries": entries_out,
        "total_entries": total_entries,
    }


def _parse_extractor_args_for_api(extra_args: list):
    """Translate known CLI extra_args (like --extractor-args youtube:player_client=...) to yt_dlp Python API dict.
    Returns (extractor_args_dict, supported: bool).
    """
    if not extra_args:
        return {}, True
    out = {}
    i = 0
    while i < len(extra_args):
        tok = extra_args[i]
        if tok == "--extractor-args" and i + 1 < len(extra_args):
            spec = extra_args[i + 1]
            if ":" not in spec:
                return {}, False
            ie_key, rest = spec.split(":", 1)
            ie_key = ie_key.strip().lower()
            ie_dict = out.setdefault(ie_key, {})
            for part in rest.split(";"):
                if not part.strip():
                    continue
                if "=" in part:
                    k, v = part.split("=", 1)
                    ie_dict[k.strip()] = [x.strip() for x in v.split(",") if x.strip()]
                else:
                    ie_dict[part.strip()] = [""]
            i += 2
        else:
            return {}, False
    return out, True


def _run_ytdlp_info_inprocess(ymod, url: str, allow_playlist: bool, use_cookies: bool, referer: str = "", extra_args: list = None):
    ext_args_dict, ok_extra = _parse_extractor_args_for_api(extra_args)
    if not ok_extra:
        return None  # Caller falls back to CLI

    logger = _SilentYtdlpLogger()
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "proxy": "",
        "socket_timeout": 15,
        "retries": 2,
        "extractor_retries": 2,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "ignoreerrors": False,
        "logger": logger,
        "noplaylist": not allow_playlist,
    }
    if _is_mobile_env():
        ydl_opts["source_address"] = "0.0.0.0"

    if allow_playlist:
        if _is_explicit_playlist_url(url):
            ydl_opts["extract_flat"] = "in_playlist"
            ydl_opts["noplaylist"] = False
        else:
            ydl_opts["playlistend"] = 25

    if referer:
        hdrs = {"Referer": referer}
        if not _is_major_platform(url):
            hdrs["User-Agent"] = _active_http_ua()
            orig = _origin_from_url(referer)
            if orig:
                hdrs["Origin"] = orig
        ydl_opts["http_headers"] = hdrs

    if ext_args_dict:
        ydl_opts["extractor_args"] = ext_args_dict

    ck_cli = get_ytdlp_cookie_cli_args(use_cookies)
    if len(ck_cli) >= 2:
        if ck_cli[0] == "--cookies":
            ydl_opts["cookiefile"] = ck_cli[1]
        elif ck_cli[0] == "--cookies-from-browser":
            spec = ck_cli[1]
            if ":" in spec:
                b, prof = spec.split(":", 1)
                ydl_opts["cookiesfrombrowser"] = (b, prof or None, None, None)
            else:
                ydl_opts["cookiesfrombrowser"] = (spec, None, None, None)

    try:
        with ymod.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise RuntimeError(logger.errors[-1] if logger.errors else "Failed to fetch metadata.")
            # sanitize_info converts lazy playlist generators into lists — exact same shape as `yt-dlp -J`
            data = ydl.sanitize_info(info)
    except Exception as exc:
        err_txt = str(exc).strip() or (logger.errors[-1] if logger.errors else "Failed to fetch metadata.")
        raise RuntimeError(err_txt)

    return _format_ytdlp_payload(data, url, referer=referer)


def _run_ytdlp_info_raw(url: str, allow_playlist: bool, use_cookies: bool, referer: str = "", extra_args: list = None):
    # Fast path: In-process yt_dlp.YoutubeDL (eliminates 1.5-3.5s Python/extractor cold-start on every Fetch!)
    ymod = _get_ytdlp_module()
    if ymod is not None:
        inproc_res = _run_ytdlp_info_inprocess(ymod, url, allow_playlist, use_cookies, referer=referer, extra_args=extra_args)
        if inproc_res is not None:
            return inproc_res

    # Fallback: CLI subprocess (used if yt_dlp isn't importable by sys.executable or after in-session pip update)
    cmd = [
        "yt-dlp",
        "--proxy", "",
        "--socket-timeout", "15",
        "--retries", "2",
        "--no-warnings",
        "--geo-bypass",
        "--no-check-certificates",
        "-J",
    ]
    if _is_termux_env():
        cmd.append("--force-ipv4")
    if allow_playlist:
        if _is_explicit_playlist_url(url):
            cmd += ["--flat-playlist", "--yes-playlist"]
        else:
            cmd += ["--playlist-end", "25"]
    else:
        cmd += ["--no-playlist"]
    if referer:
        cmd += ["--referer", referer]
        if not _is_major_platform(url):
            cmd += ["--user-agent", _active_http_ua()]
            orig = _origin_from_url(referer)
            if orig:
                cmd += ["--add-header", f"Origin:{orig}"]
    if extra_args:
        cmd += list(extra_args)
    cmd += get_ytdlp_cookie_cli_args(use_cookies)
    cmd.append(url)

    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=clean_env(), timeout=120, **_win_popen_kwargs())
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Failed to fetch metadata.")
    data = json.loads(proc.stdout)
    return _format_ytdlp_payload(data, url, referer=referer)


def run_info(raw_input: str, referer: str = "", extra_cookies: str = "", title_hint: str = "", force: bool = False):
    url = extract_url(raw_input)
    if not url:
        raise RuntimeError("URL is required.")

    cache_key = (url, referer or "", _cookies_cache_sig())
    if not force and not extra_cookies:
        with INFO_CACHE_LOCK:
            cached = INFO_CACHE.get(cache_key)
            if cached and (time.time() - cached[0]) < INFO_CACHE_TTL:
                return json.loads(json.dumps(cached[1]))

    res = _run_info_uncached(url, referer=referer, extra_cookies=extra_cookies, title_hint=title_hint)

    if res and not extra_cookies:
        with INFO_CACHE_LOCK:
            if len(INFO_CACHE) >= 30:
                oldest_k = min(INFO_CACHE.keys(), key=lambda k: INFO_CACHE[k][0])
                INFO_CACHE.pop(oldest_k, None)
            INFO_CACHE[cache_key] = (time.time(), json.loads(json.dumps(res)))
    return res


def _run_info_uncached(url: str, referer: str = "", extra_cookies: str = "", title_hint: str = ""):

    # Stage 0: Direct file/stream URL (expand .m3u8/.mpd into all qualities)
    parsed_u = urlparse(url)
    ext_u = Path(parsed_u.path).suffix.lower()
    if ext_u in STREAM_EXTS or ".m3u8" in url.lower() or ".mpd" in url.lower():
        expanded = expand_stream_url(
            url,
            clean_url=url,
            referer=referer or "",
            title=title_hint or sanitize_filename(Path(parsed_u.path).stem, "Stream"),
            host=parsed_u.hostname or "",
            extra_cookies=extra_cookies or "",
        )
        if expanded and not (isinstance(expanded, dict) and expanded.get("_dead_code")):
            return expanded

    direct_info = probe_direct_media(url)
    if direct_info:
        return direct_info

    # Unwrap accidental facebook.com/login.php?next=... links immediately
    if _is_facebook_url(url):
        url = _unwrap_facebook_login_url(url)

    allow_pl = _should_allow_playlist(url)
    use_ck = _is_cookies_enabled()
    last_error = ""
    ytdlp_res = None

    # Stage 0.5: Facebook Stories & /share/ links (FB Stories always require login & often fail in yt-dlp)
    fb_probe = None
    if _is_facebook_url(url) and ("/stories/" in url.lower() or "/share/" in url.lower()):
        fb_probe = extract_facebook_story_or_resolve(url)
        if fb_probe.get("story_result"):
            return fb_probe["story_result"]
        if fb_probe.get("resolved_url") and fb_probe["resolved_url"] != url:
            url = fb_probe["resolved_url"]
        if fb_probe.get("login_required") and "/stories/" in url.lower():
            raise RuntimeError(fb_probe["error"])

    is_yt = _is_youtube_url(url)

    def _ytdlp_has_playable_formats(res: dict) -> bool:
        if not res:
            return False
        if len(res.get("entries") or []) > 1:
            return True
        real_v = [f for f in (res.get("formats") or []) if f.get("id") != "best"]
        return bool(real_v or res.get("audio_size"))

    # Stage 1: Primary yt-dlp extraction
    # On YouTube/YT Music, try without cookies FIRST (Chrome cookies disable android_vr/tv clients & trigger SABR/PO-Token format errors on public videos)
    first_use_ck = False if is_yt else use_ck
    try:
        ytdlp_res = _run_ytdlp_info_raw(url, allow_playlist=allow_pl, use_cookies=first_use_ck)
        if is_yt and not _ytdlp_has_playable_formats(ytdlp_res):
            ytdlp_res = None
    except Exception as exc:
        last_error = str(exc)

    # Stage 1.5 (YouTube): Try cookie-less android_vr/tv client chain if default returned no playable formats
    if ytdlp_res is None and is_yt:
        try:
            ytdlp_res = _run_ytdlp_info_raw(
                url,
                allow_playlist=allow_pl,
                use_cookies=False,
                extra_args=["--extractor-args", "youtube:player_client=android_vr,tv,web_safari,web"],
            )
            if not _ytdlp_has_playable_formats(ytdlp_res):
                ytdlp_res = None
        except Exception as exc:
            if not last_error:
                last_error = str(exc)

    # Stage 2: Toggle cookies fallback (with cookies on YT for age-gated/private/bot-check; without cookies on other sites)
    if ytdlp_res is None and use_ck and not (_is_facebook_url(url) and "/stories/" in url.lower()):
        second_use_ck = True if is_yt else False
        try:
            ytdlp_res = _run_ytdlp_info_raw(url, allow_playlist=allow_pl, use_cookies=second_use_ck)
        except Exception as exc:
            if not last_error:
                last_error = str(exc)

    # Stage 2.5: If yt-dlp failed on a Facebook URL that redirected to login.php or is a story, give clear FB guidance
    if ytdlp_res is None and _is_facebook_url(url):
        if "facebook.com/login" in last_error.lower() or "/stories/" in last_error.lower():
            if fb_probe is None:
                fb_probe = extract_facebook_story_or_resolve(url)
                if fb_probe.get("story_result"):
                    return fb_probe["story_result"]
                if fb_probe.get("login_required"):
                    raise RuntimeError(fb_probe["error"])
            if not _has_facebook_auth_cookies():
                raise RuntimeError(
                    "Facebook requires login cookies for this link (Stories always need login, even on public pages).\n\n"
                    "Open Settings (⚙) -> enable Session Cookies and save Facebook cookies (c_user & xs)."
                )
            raise RuntimeError(
                "Facebook rejected the session or this Story could not be parsed.\n"
                "Re-export fresh Facebook cookies in Settings (⚙) and try again."
            )

    # Stage 2.6: Google Drive dedicated fallback when yt-dlp fails with HTTP 403 on JSON metadata
    if ytdlp_res is None and _is_google_drive_url(url):
        gd_res = extract_google_drive_media(url)
        if gd_res is not None:
            return gd_res

    is_major = _is_major_platform(url)

    # If yt-dlp succeeded via Generic extractor with only <=1 quality, check Deep Sniffer
    # because many streaming sites expose multiple qualities or master HLS playlists in JS/HTML!
    if ytdlp_res is not None:
        ext_name = (ytdlp_res.get("extractor") or "").lower()
        real_q = [f for f in (ytdlp_res.get("formats") or []) if f.get("id") != "best"]
        if (not is_major) and ext_name == "generic" and len(real_q) <= 1:
            sniffed = sniff_page_media(url)
            if sniffed:
                sniff_list = sniffed.get("streams") if sniffed.get("kind") == "sniffed" else [
                    f for f in (sniffed.get("formats") or []) if f.get("id") != "best"
                ]
                if len(sniff_list or []) > len(real_q):
                    return sniffed
        return ytdlp_res

    # Stage 3: Deep Web Media Sniffer (skip on major platforms so real yt-dlp errors aren't hidden)
    if not is_major:
        sniffed = sniff_page_media(url)
        if sniffed:
            return sniffed

    # Clean, actionable error message
    low_err = last_error.lower()
    if any(k in low_err for k in ("login required", "not available to everyone", "sign in", "cookies", "private")):
        raise RuntimeError(last_error + "\n\nTip: Open Settings (⚙) and add cookies.txt for login-gated content.")
    raise RuntimeError(last_error or "No downloadable media found on this page.")


def _update_job(job_id: str, **kwargs):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job.update(kwargs)


def _public_job(job: dict) -> dict:
    return {k: v for k, v in job.items() if not k.startswith("_")}


def _cleanup_old_jobs():
    with JOBS_LOCK:
        if len(JOBS) > 25:
            sorted_ids = sorted(JOBS.keys(), key=lambda jid: JOBS[jid].get("created_at", 0))
            for jid in sorted_ids[:-20]:
                if JOBS[jid].get("status") in ("done", "error", "cancelled"):
                    JOBS.pop(jid, None)


def parse_progress_line(line: str, state: dict):
    s = line.strip()
    if not s:
        return None

    if s.startswith("PROG|"):
        parts = s.split("|")
        while len(parts) < 8:
            parts.append("")
        status, dl_b, tot_b, est_b, speed_b, eta_s, pct_str = parts[1:8]

        dl_val = safe_float(dl_b) or 0.0
        tot_val = safe_float(tot_b)
        est_val = safe_float(est_b)
        approx = False
        effective_total = tot_val
        if effective_total is None and est_val is not None:
            effective_total = est_val
            approx = True

        pct = None
        if effective_total and effective_total > 0:
            pct = min(100.0, max(0.0, (dl_val / effective_total) * 100.0))
        else:
            m_pct = re.search(r"(\d+(?:\.\d+)?)\s*%", pct_str)
            if m_pct:
                pct = float(m_pct.group(1))

        prefix = state.get("item_prefix") or ""
        stage = prefix + (state.get("stream_stage") or "Downloading...")
        if status == "finished":
            pct = 100.0
            stage = prefix + "Finalizing..."

        updates = {
            "status": "downloading",
            "stage": stage,
            "downloaded_text": fmt_mb(dl_val) or "0 MB",
            "total_text": fmt_mb(effective_total, approx=approx) if effective_total else "",
            "speed_text": fmt_speed(speed_b),
            "eta_text": fmt_eta(eta_s),
        }
        if pct is not None:
            updates["percent"] = round(pct, 1)
        return updates

    if s.startswith("PP|"):
        parts = s.split("|")
        pp_name = parts[2] if len(parts) > 2 else ""
        # MetadataParser runs in pre_process BEFORE downloading starts; do not jump progress to 99%!
        if "MetadataParser" in pp_name or "MetadataFromField" in pp_name or "ModifyChapters" in pp_name:
            return None
        prefix = state.get("item_prefix") or ""
        stage = prefix + "Processing..."
        if "ExtractAudio" in pp_name:
            stage = prefix + "Converting audio..."
        elif "ThumbnailsConvertor" in pp_name:
            stage = prefix + "Preparing cover art..."
        elif "EmbedThumbnail" in pp_name:
            stage = prefix + "Embedding cover art..."
        elif "Metadata" in pp_name:
            stage = prefix + "Writing ID3 tags..."
        elif "Merger" in pp_name:
            stage = prefix + "Merging streams..."
        return {
            "status": "postprocessing",
            "stage": stage,
            "percent": 99.0,
            "speed_text": "FFmpeg",
            "eta_text": "—",
        }

    m_std = re.search(
        r"\[download\]\s+(\d+(?:\.\d+)?)%\s+of\s+(~?\s*[\d.]+\w+)(?:\s+at\s+([\d.]+\w+/s))?(?:\s+ETA\s+(\S+))?",
        s,
    )
    if m_std:
        pct = float(m_std.group(1))
        total_raw = m_std.group(2).replace("MiB", "MB").replace("KiB", "KB").replace("GiB", "GB")
        speed_raw = (m_std.group(3) or "—").replace("MiB", "MB").replace("KiB", "KB")
        eta_raw = m_std.group(4) or "—"
        return {
            "status": "downloading",
            "stage": state.get("stream_stage") or "Downloading...",
            "percent": round(pct, 1),
            "total_text": total_raw,
            "speed_text": speed_raw,
            "eta_text": eta_raw,
        }

    return None


def _trigger_media_scan(paths):
    valid = []
    for p in (paths or []):
        try:
            pp = Path(p)
            if pp.exists():
                os.utime(pp, None)
                valid.append(pp)
        except Exception:
            pass
    if not valid:
        return False

    br = _get_android_bridge()
    if br is not None:
        try:
            for pp in valid:
                br.scanFile(str(pp))
            return True
        except Exception:
            pass

    if shutil.which("termux-media-scan"):
        try:
            subprocess.Popen(
                ["termux-media-scan"] + [str(p) for p in valid],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            pass

    # Fallback Android broadcast (works on some builds without Termux:API)
    if shutil.which("am"):
        for p in valid:
            try:
                uri = "file://" + quote(str(p))
                subprocess.Popen(
                    ["am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE", "-d", uri],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass
    return False


def _gdrive_clen_from_url(url: str) -> int:
    try:
        p = urlparse(url)
        if "videoplayback" not in (p.path or "").lower():
            return 0
        qs = parse_qs(p.query)
        val = (qs.get("clen") or [""])[0].strip()
        if val.isdigit():
            return int(val)
    except Exception:
        pass
    return 0


def _append_range_param(url: str, start: int, end: int) -> str:
    clean = re.sub(r"([?&])range=[^&]*&?", r"\1", url).rstrip("?&")
    sep = "&" if "?" in clean else "?"
    return f"{clean}{sep}range={start}-{end}"


def _download_single_direct_stream(job_id: str, url: str, referer: str, dest: Path, stage_label: str, extra_cookies: str = ""):
    expected_clen = _gdrive_clen_from_url(url)
    downloaded = 0
    final_url = url
    ctype = ""
    start_t = time.time()
    last_ui = 0.0

    def _emit_ui(total_bytes):
        nonlocal last_ui
        now = time.time()
        if now - last_ui >= 0.2:
            elapsed = max(0.05, now - start_t)
            speed = downloaded / elapsed
            pct = min(100.0, (downloaded / total_bytes) * 100.0) if total_bytes else 0.0
            eta = ((total_bytes - downloaded) / speed) if (total_bytes and speed > 0) else None
            _update_job(
                job_id,
                percent=round(pct, 1),
                downloaded_text=fmt_mb(downloaded) or "0 MB",
                speed_text=fmt_speed(speed),
                eta_text=fmt_eta(eta),
            )
            last_ui = now

    # 1. Try single direct GET first (fast path)
    need_chunked = False
    first_err = None
    try:
        req = _build_request(url, referer=referer, method="GET", extra_cookies=extra_cookies)
        with urllib.request.urlopen(req, timeout=30, context=_http_context()) as resp:
            final_url = resp.geturl() or url
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            cl = resp.headers.get("Content-Length")
            total = int(cl) if (cl and cl.isdigit()) else (expected_clen or None)

            _update_job(
                job_id,
                status="downloading",
                stage=stage_label,
                total_text=fmt_mb(total) if total else "",
                filename=dest.name,
            )

            with open(dest, "wb") as fp:
                while True:
                    with JOBS_LOCK:
                        if JOBS.get(job_id, {}).get("status") == "cancelled":
                            return downloaded, final_url, ctype, True
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    fp.write(chunk)
                    downloaded += len(chunk)
                    _emit_ui(total)

        if expected_clen > 0 and downloaded < expected_clen:
            need_chunked = True
    except Exception as exc:
        first_err = exc
        if expected_clen > 0:
            need_chunked = True
        else:
            raise

    # 2. Chunked &range=start-end fallback for Google Drive videoplayback streams
    if need_chunked and expected_clen > 0:
        chunk_size = 4 * 1024 * 1024  # 4 MB slices
        mode_open = "ab" if (downloaded > 0 and dest.exists()) else "wb"
        if mode_open == "wb":
            downloaded = 0
        _update_job(
            job_id,
            status="downloading",
            stage=stage_label,
            total_text=fmt_mb(expected_clen),
            filename=dest.name,
        )
        with open(dest, mode_open) as fp:
            while downloaded < expected_clen:
                with JOBS_LOCK:
                    if JOBS.get(job_id, {}).get("status") == "cancelled":
                        return downloaded, final_url, ctype, True
                end_b = min(expected_clen - 1, downloaded + chunk_size - 1)
                slice_u = _append_range_param(url, downloaded, end_b)
                req_s = _build_request(slice_u, referer=referer, method="GET", extra_cookies=extra_cookies)
                got_slice = 0
                with urllib.request.urlopen(req_s, timeout=30, context=_http_context()) as resp_s:
                    final_url = resp_s.geturl() or final_url
                    if not ctype:
                        ctype = (resp_s.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    while True:
                        with JOBS_LOCK:
                            if JOBS.get(job_id, {}).get("status") == "cancelled":
                                return downloaded, final_url, ctype, True
                        blk = resp_s.read(65536)
                        if not blk:
                            break
                        fp.write(blk)
                        downloaded += len(blk)
                        got_slice += len(blk)
                        _emit_ui(expected_clen)
                if got_slice == 0:
                    if first_err:
                        raise first_err
                    break

    return downloaded, final_url, ctype, False


def _chrome_staging_info(via_chrome: bool, playlist_item=None):
    """Return (should_stage: bool, chrome_subdir: str).
    Stages for Chrome Downloads handoff when save_dir is empty, equals default Downloads,
    or is a subfolder inside default Downloads (which Chrome downloads API supports cleanly!).
    """
    if not via_chrome or not _is_desktop_env():
        return False, ""
    if playlist_item == "ALL":
        return False, ""
    custom = (load_config().get("save_dir") or "").strip()
    if not custom:
        return True, ""
    try:
        def_dir = get_default_download_dir().resolve()
        cus_dir = Path(os.path.expandvars(os.path.expanduser(custom))).resolve()
        if cus_dir == def_dir:
            return True, ""
        rel = cus_dir.relative_to(def_dir)
        rel_posix = rel.as_posix().strip("/")
        if rel_posix and ".." not in rel.parts:
            return True, rel_posix
    except Exception:
        pass
    return False, ""


def _should_stage_for_chrome(via_chrome: bool, playlist_item=None) -> bool:
    ok, _ = _chrome_staging_info(via_chrome, playlist_item)
    return ok


def _pick_primary_staged_file(stage_dir: Path):
    if not stage_dir or not stage_dir.exists():
        return None
    all_files = []
    for p in stage_dir.iterdir():
        try:
            if not p.is_file():
                continue
            low = p.name.lower()
            if p.name.startswith(".") or low.endswith((".part", ".ytdl", ".temp", ".tmp", ".aria2")):
                continue
            if p.stat().st_size <= 0:
                continue
            all_files.append(p)
        except OSError:
            continue
    if not all_files:
        return None
    # Prefer actual video/audio over leftover thumbnail images (.jpg/.webp/.png)
    media_files = [p for p in all_files if p.suffix.lower() in (VIDEO_EXTS | AUDIO_EXTS)]
    pool = media_files if media_files else all_files
    pool.sort(key=lambda p: (p.stat().st_size, p.stat().st_mtime), reverse=True)
    return pool[0]


def start_direct_download_job(
    url: str,
    referer: str,
    filename_hint: str,
    audio_url: str = "",
    extra_cookies: str = "",
    via_chrome: bool = False,
    mode: str = "video",
    quality_label: str = "",
) -> str:
    _cleanup_old_jobs()
    job_id = uuid.uuid4().hex[:12]
    stage_chrome, chrome_subdir = _chrome_staging_info(via_chrome, None)
    if stage_chrome:
        out_dir = STAGE_DIR / job_id
        display_dir = str(get_default_download_dir() / chrome_subdir) if chrome_subdir else str(get_default_download_dir())
    else:
        out_dir = get_base_download_dir()
        display_dir = str(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    display_title = (filename_hint or "").strip() or (urlparse(url).hostname or "Media")

    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "title": display_title,
            "source_url": url,
            "mode": mode or "video",
            "quality_label": quality_label or "",
            "via_chrome": stage_chrome,
            "chrome_subdir": chrome_subdir,
            "has_file": False,
            "status": "starting",
            "stage": "Connecting...",
            "percent": 0.0,
            "downloaded_text": "0 MB",
            "total_text": "",
            "speed_text": "—",
            "eta_text": "—",
            "saved_to": display_dir,
            "filename": "",
            "error": "",
            "created_at": time.time(),
            "_proc": None,
            "_stage_dir": str(out_dir) if stage_chrome else "",
        }

    def worker():
        dest = None
        tmp_vid = None
        tmp_aud = None
        try:
            # Separate DASH HD Video + Audio (e.g. Facebook 1080p/720p Stories) -> download both & mux via FFmpeg
            if audio_url and shutil.which("ffmpeg"):
                base = sanitize_filename(filename_hint or f"story_{int(time.time())}", f"story_{int(time.time())}")
                dest = out_dir / f"{base}.mp4"
                if dest.exists():
                    dest = out_dir / f"{base}_{int(time.time())}.mp4"
                tmp_vid = out_dir / f".omni_v_{job_id}.bin"
                tmp_aud = out_dir / f".omni_a_{job_id}.bin"

                dl_v, _, _, cancelled = _download_single_direct_stream(job_id, url, referer, tmp_vid, "Downloading video stream...", extra_cookies=extra_cookies)
                if cancelled:
                    return
                dl_a, _, _, cancelled = _download_single_direct_stream(job_id, audio_url, referer, tmp_aud, "Downloading audio stream...", extra_cookies=extra_cookies)
                if cancelled:
                    return

                _update_job(
                    job_id,
                    status="postprocessing",
                    stage="Merging video + audio...",
                    percent=99.0,
                    speed_text="FFmpeg",
                    eta_text="—",
                    filename=dest.name,
                )
                mux_cmds = [
                    [
                        "ffmpeg", "-y", "-loglevel", "error",
                        "-i", str(tmp_vid), "-i", str(tmp_aud),
                        "-c", "copy", "-movflags", "+faststart",
                        str(dest),
                    ],
                    [
                        "ffmpeg", "-y", "-loglevel", "error",
                        "-i", str(tmp_vid), "-i", str(tmp_aud),
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                        "-movflags", "+faststart",
                        str(dest),
                    ],
                    [
                        "ffmpeg", "-y", "-loglevel", "error",
                        "-i", str(tmp_vid), "-i", str(tmp_aud),
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                        "-c:a", "aac", "-b:a", "192k",
                        "-movflags", "+faststart",
                        str(dest),
                    ],
                ]
                last_mux_err = ""
                mux_ok = False
                for mcmd in mux_cmds:
                    proc = subprocess.Popen(mcmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=clean_env(), **_win_popen_kwargs())
                    _update_job(job_id, _proc=proc)
                    _, err_txt = proc.communicate()
                    if proc.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
                        mux_ok = True
                        break
                    last_mux_err = (err_txt or "").strip() or last_mux_err
                if not mux_ok:
                    raise RuntimeError(last_mux_err or "FFmpeg merge failed")

                scanned = _trigger_media_scan([dest])
                _update_job(
                    job_id,
                    status="done",
                    stage="Completed",
                    percent=100.0,
                    downloaded_text=fmt_mb(dest.stat().st_size) or fmt_mb(dl_v + dl_a) or "Done",
                    speed_text="Done",
                    eta_text="00:00",
                    filename=dest.name,
                    _filepath=str(dest),
                    has_file=True,
                    media_scanned=scanned,
                )
                _notify_android_complete(display_title, str(dest))
                return

            # Standard single-file direct download (with automatic chunked &range= support for videoplayback!)
            ext_guess = Path(urlparse(url).path).suffix.lower()
            if not ext_guess or len(ext_guess) > 6:
                ext_guess = ".m4a" if mode == "audio" else ".mp4"
            base = sanitize_filename(filename_hint or Path(urlparse(url).path).stem, f"media_{int(time.time())}")
            dest = out_dir / f"{base}{ext_guess}"
            if dest.exists():
                dest = out_dir / f"{base}_{int(time.time())}{ext_guess}"

            downloaded, final_url, ctype, cancelled = _download_single_direct_stream(
                job_id, url, referer, dest, "Downloading file...", extra_cookies=extra_cookies
            )
            if cancelled:
                if dest and dest.exists():
                    try:
                        dest.unlink()
                    except Exception:
                        pass
                return

            # Refine file extension if server returned a clearer extension/content-type
            real_ext = Path(urlparse(final_url).path).suffix.lower()
            if not real_ext or len(real_ext) > 6:
                real_ext = mimetypes.guess_extension(ctype) or ext_guess
            if real_ext == ".jpe":
                real_ext = ".jpg"
            if real_ext and real_ext != dest.suffix.lower() and dest.exists():
                renamed = dest.with_suffix(real_ext)
                if not renamed.exists():
                    dest.rename(renamed)
                    dest = renamed

            scanned = _trigger_media_scan([dest])
            _update_job(
                job_id,
                status="done",
                stage="Completed",
                percent=100.0,
                downloaded_text=fmt_mb(downloaded) or "0 MB",
                speed_text="Done",
                eta_text="00:00",
                filename=dest.name,
                _filepath=str(dest),
                has_file=True,
                media_scanned=scanned,
            )
            _notify_android_complete(display_title, str(dest))
        except Exception as exc:
            _update_job(job_id, status="error", stage="Failed", error=str(exc))
        finally:
            _update_job(job_id, _proc=None)
            for tp in (tmp_vid, tmp_aud):
                if tp and tp.exists():
                    try:
                        tp.unlink()
                    except Exception:
                        pass
            if stage_chrome:
                with JOBS_LOCK:
                    st_now = JOBS.get(job_id, {}).get("status")
                if st_now in ("cancelled", "error"):
                    shutil.rmtree(out_dir, ignore_errors=True)

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def _run_ffmpeg_stream_fallback(job_id: str, url: str, out_dir: Path, mode: str, referer: str, filename_hint: str, extra_cookies: str = "", audio_url: str = ""):
    if not shutil.which("ffmpeg"):
        return False, None, "FFmpeg is not installed."

    ext = ".mp3" if mode == "audio" else ".mp4"
    base = sanitize_filename(filename_hint or Path(urlparse(url).path).stem, f"stream_{int(time.time())}")
    dest = out_dir / f"{base}{ext}"
    if dest.exists():
        dest = out_dir / f"{base}_{int(time.time())}{ext}"

    hdr_lines = ["Accept: */*"]
    if referer:
        hdr_lines.append(f"Referer: {referer}")
        orig = _origin_from_url(referer)
        if orig:
            hdr_lines.append(f"Origin: {orig}")
    ck = _merge_cookie_headers(cookie_header_for_url(url), extra_cookies)
    if ck:
        hdr_lines.append(f"Cookie: {ck}")
    headers_blob = "\r\n".join(hdr_lines) + "\r\n"

    cmd = [
        "ffmpeg", "-y",
        "-loglevel", "error",
        "-user_agent", _active_http_ua(),
        "-headers", headers_blob,
        "-rw_timeout", "30000000",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", url,
    ]
    is_dash_input = ".mpd" in url.lower()
    if audio_url and mode != "audio":
        cmd += [
            "-user_agent", _active_http_ua(),
            "-headers", headers_blob,
            "-rw_timeout", "30000000",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", audio_url,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c", "copy",
        ]
        if not is_dash_input:
            cmd += ["-bsf:a", "aac_adtstoasc"]
        cmd += ["-movflags", "+faststart"]
    elif mode == "audio":
        cmd += ["-vn", "-c:a", "libmp3lame", "-b:a", "192k"]
    else:
        cmd += ["-c", "copy"]
        if not is_dash_input:
            cmd += ["-bsf:a", "aac_adtstoasc"]
        cmd += ["-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", str(dest)]

    _update_job(
        job_id,
        status="downloading",
        stage="Downloading via FFmpeg...",
        percent=0.0,
        downloaded_text="0 MB",
        total_text="",
        speed_text="—",
        eta_text="—",
        filename=dest.name,
    )

    err_buf = []
    start_t = time.time()
    last_bytes = 0
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=clean_env(),
        **_win_popen_kwargs(),
    )
    _update_job(job_id, _proc=proc)

    def _drain_err():
        try:
            for ln in proc.stderr:
                s = ln.strip()
                if s:
                    err_buf.append(s)
        except Exception:
            pass

    t_err = threading.Thread(target=_drain_err, daemon=True)
    t_err.start()

    stalled_zero = False
    last_progress_ts = time.time()
    done_event = threading.Event()

    def _stall_guard():
        nonlocal stalled_zero
        while not done_event.wait(2.0):
            if proc.poll() is not None:
                return
            if last_bytes == 0 and (time.time() - last_progress_ts) >= 25.0:
                stalled_zero = True
                try:
                    proc.terminate()
                    time.sleep(1.0)
                    if proc.poll() is None:
                        proc.kill()
                except Exception:
                    pass
                return

    threading.Thread(target=_stall_guard, daemon=True).start()

    for raw_ln in proc.stdout:
        ln = raw_ln.strip()
        if ln.startswith("total_size="):
            val = safe_float(ln.split("=", 1)[1])
            if val is not None and val >= 0:
                if val > last_bytes:
                    last_progress_ts = time.time()
                last_bytes = val
                elapsed = max(0.1, time.time() - start_t)
                speed = last_bytes / elapsed
                _update_job(
                    job_id,
                    downloaded_text=fmt_mb(last_bytes) or "0 MB",
                    speed_text=fmt_speed(speed),
                )
        elif ln.startswith("out_time="):
            t_str = ln.split("=", 1)[1].split(".")[0]
            if t_str and t_str != "N/A":
                if t_str != "00:00:00":
                    last_progress_ts = time.time()
                _update_job(job_id, stage=f"FFmpeg stream ({t_str})")

    done_event.set()
    rc = proc.wait()
    t_err.join(timeout=2)
    if stalled_zero:
        if dest.exists():
            try:
                dest.unlink()
            except Exception:
                pass
        extra_e = (" — " + err_buf[-1]) if err_buf else ""
        return False, None, "Stream stalled at 0 MB for 25s (likely DRM-encrypted or blocked by CDN)" + extra_e

    with JOBS_LOCK:
        cancelled = JOBS.get(job_id, {}).get("status") == "cancelled"
    if cancelled:
        if dest.exists():
            try:
                dest.unlink()
            except Exception:
                pass
        return False, None, "Cancelled"

    if rc == 0 and dest.exists() and dest.stat().st_size > 0:
        return True, dest, ""

    if dest.exists() and dest.stat().st_size == 0:
        try:
            dest.unlink()
        except Exception:
            pass
    err_msg = "\n".join(err_buf[-3:]) if err_buf else f"FFmpeg exited with code {rc}"
    return False, None, err_msg


def _refresh_expired_stream_url(referer_url: str, quality_label: str, failed_url: str):
    if not referer_url or not referer_url.startswith(("http://", "https://")):
        return None
    try:
        fresh = sniff_page_media(referer_url)
    except Exception:
        return None
    if not fresh:
        return None
    streams = fresh.get("streams") or []
    if not streams:
        return None
    # Match same quality label first (e.g. "1080p"), otherwise pick first working stream
    wanted = (quality_label or "").strip().lower()
    if wanted:
        for s in streams:
            if (s.get("name") or "").strip().lower() == wanted and s.get("url") != failed_url:
                return s
        wanted_base = re.sub(r"\s*#\d+$", "", wanted)
        for s in streams:
            s_base = re.sub(r"\s*#\d+$", "", (s.get("name") or "").strip().lower())
            if s_base == wanted_base and s.get("url") != failed_url:
                return s
    for s in streams:
        if s.get("url") and s.get("url") != failed_url:
            return s
    return streams[0]


def _build_ytdlp_download_cmd(
    url: str,
    mode: str,
    quality: str,
    out_tpl: str,
    referer: str,
    is_major: bool,
    use_cookies: bool,
    playlist_item,
    extra_cookies: str = "",
    extra_args: list = None,
    force_broad_format: bool = False,
) -> list:
    common = [
        "yt-dlp",
        "--proxy", "",
        "--no-mtime",
        "--force-overwrites",
        "--socket-timeout", "30",
        "--retries", "10",
        "--fragment-retries", "10",
        "--concurrent-fragments", "4",
        "--geo-bypass",
        "--no-check-certificates",
        "--progress",
        "--newline",
        "--progress-template",
        "download:PROG|%(progress.status)s|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s|%(progress._percent_str)s",
        "--progress-template",
        "postprocess:PP|%(progress.status)s|%(progress.postprocessor)s",
    ]
    if _is_termux_env():
        common.append("--force-ipv4")
    if referer:
        common += ["--referer", referer]
        if not is_major:
            common += ["--user-agent", _active_http_ua()]
            orig = _origin_from_url(referer)
            if orig:
                common += ["--add-header", f"Origin:{orig}"]
    if extra_args:
        common += list(extra_args)
    if use_cookies:
        common += get_ytdlp_cookie_cli_args(True)
    if extra_cookies and not is_major:
        common += ["--add-header", f"Cookie:{extra_cookies}"]

    if playlist_item == "ALL":
        common += ["--yes-playlist", "--ignore-errors"]
    elif playlist_item and str(playlist_item).isdigit():
        common += ["--yes-playlist", "--playlist-items", str(playlist_item)]
    else:
        common += ["--no-playlist"]

    if mode == "audio":
        audio_fmt = "mp3"
        audio_q = "320K"
        audio_sel = ["-f", "bestaudio/best"]
        if quality == "m4a_best":
            audio_fmt = "m4a"
            audio_q = "0"
            audio_sel = ["-f", "bestaudio[ext=m4a]/bestaudio/best"]
        elif quality == "mp3_192":
            audio_q = "192K"
        elif quality == "mp3_128":
            audio_q = "128K"

        if force_broad_format:
            audio_sel = ["-f", "bestaudio*/best"]

        meta_args = [
            "--embed-metadata",
            "--embed-thumbnail",
            "--convert-thumbnails", "jpg",
            "--replace-in-metadata", "artist,uploader,channel,album_artist", r" - Topic$", "",
            "--parse-metadata", "%(track,title)s:%(meta_title)s",
            "--parse-metadata", "%(artist,creator,uploader,channel)s:%(meta_artist)s",
            "--parse-metadata", "%(album_artist,artist,uploader,channel)s:%(meta_album_artist)s",
        ]
        if playlist_item:
            meta_args += [
                "--parse-metadata", "%(album,playlist_title)s:%(meta_album)s",
                "--parse-metadata", "%(track_number,playlist_index)s:%(meta_track)s",
            ]
        return common + audio_sel + [
            "-x",
            "--audio-format", audio_fmt,
            "--audio-quality", audio_q,
        ] + meta_args + ["-o", out_tpl, url]

    if (not force_broad_format) and quality and quality != "best" and not quality.startswith("stream_"):
        return common + ["-f", quality, "--merge-output-format", "mp4", "-o", out_tpl, url]
    return common + [
        "-S", "res,vcodec:h264,acodec:aac,ext:mp4:m4a",
        "-f", "bestvideo*+bestaudio[ext=m4a]/bestvideo*+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", out_tpl, url,
    ]


def start_download_job(
    raw_url: str,
    mode: str,
    quality: str,
    referer: str = "",
    direct: bool = False,
    audio_url: str = "",
    filename_hint: str = "",
    playlist_item=None,
    quality_label: str = "",
    extra_cookies: str = "",
    via_chrome: bool = False,
    master_url: str = "",
) -> str:
    url = extract_url(raw_url)
    if not url:
        raise RuntimeError("URL is required.")

    if direct:
        return start_direct_download_job(
            url,
            referer=referer,
            filename_hint=filename_hint,
            audio_url=audio_url,
            extra_cookies=extra_cookies,
            via_chrome=via_chrome,
            mode=mode,
            quality_label=quality_label,
        )

    _cleanup_old_jobs()
    job_id = uuid.uuid4().hex[:12]
    stage_chrome, chrome_subdir = _chrome_staging_info(via_chrome, playlist_item)
    isolated_local_tmp = False
    if stage_chrome:
        final_out_dir = (get_default_download_dir() / chrome_subdir) if chrome_subdir else get_default_download_dir()
        out_dir = STAGE_DIR / job_id
        display_dir = str(final_out_dir)
    else:
        final_out_dir = get_base_download_dir()
        final_out_dir.mkdir(parents=True, exist_ok=True)
        display_dir = str(final_out_dir)
        if playlist_item != "ALL":
            # Isolate single-item downloads in a fresh empty temp folder so yt-dlp NEVER collides with old files
            # (e.g., previous Facebook.mp4) and NEVER serves an old video via "has already been downloaded"!
            candidate_tmp = final_out_dir / f".omni_tmp_{job_id}"
            try:
                candidate_tmp.mkdir(parents=True, exist_ok=True)
                out_dir = candidate_tmp
                isolated_local_tmp = True
            except Exception:
                out_dir = STAGE_DIR / job_id
                out_dir.mkdir(parents=True, exist_ok=True)
                isolated_local_tmp = True
        else:
            out_dir = final_out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    is_major = _is_major_platform(url)
    is_yt = _is_youtube_url(url)
    low_url = url.lower()
    is_stream_url = (
        ".m3u8" in low_url
        or ".mpd" in low_url
        or (quality and quality.startswith("stream_"))
    )

    if playlist_item == "ALL":
        out_tpl = str(out_dir / "%(playlist_title,uploader|Playlist).60s" / "%(playlist_index)02d - %(title).75s.%(ext)s")
    elif is_stream_url and filename_hint and not playlist_item:
        safe_title = sanitize_filename(filename_hint, f"stream_{int(time.time())}")
        out_tpl = str(out_dir / f"{safe_title}.%(ext)s")
    else:
        out_tpl = str(out_dir / "%(title).80s.%(ext)s")

    has_saved_cookies = _is_cookies_enabled()

    # Build ordered attempt plan:
    # - On YouTube/YT Music: try cookie-less first (avoids SABR/PO-Token 'Requested format is not available'),
    #   then explicit android_vr/tv clients + broad format, then with cookies if available (for age-gated/private/bot-check).
    # - On other sites: try with saved cookies first, then without cookies / broad format if format or auth fails.
    attempts = []
    if is_yt:
        attempts.append({"label": "Starting...", "use_cookies": False, "extra_args": None, "broad": False})
        attempts.append({
            "label": "Retrying with YouTube compatibility mode...",
            "use_cookies": False,
            "extra_args": ["--extractor-args", "youtube:player_client=android_vr,tv,web_safari,web"],
            "broad": True,
        })
        if has_saved_cookies:
            attempts.append({"label": "Retrying with session cookies...", "use_cookies": True, "extra_args": None, "broad": True})
    else:
        attempts.append({"label": "Starting...", "use_cookies": has_saved_cookies, "extra_args": None, "broad": False})
        if has_saved_cookies:
            attempts.append({"label": "Retrying without cookies...", "use_cookies": False, "extra_args": None, "broad": False})
        if quality and quality != "best" and not quality.startswith("stream_"):
            attempts.append({"label": "Retrying with compatible format...", "use_cookies": has_saved_cookies, "extra_args": None, "broad": True})

    display_title = (filename_hint or "").strip() or (urlparse(url).hostname or "Media")
    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "title": display_title,
            "source_url": url,
            "mode": mode or "video",
            "quality_label": quality_label or "",
            "via_chrome": stage_chrome,
            "chrome_subdir": chrome_subdir,
            "has_file": False,
            "status": "starting",
            "stage": "Starting...",
            "percent": 0.0,
            "downloaded_text": "0 MB",
            "total_text": "",
            "speed_text": "—",
            "eta_text": "—",
            "saved_to": display_dir,
            "filename": "",
            "error": "",
            "created_at": time.time(),
            "_proc": None,
            "_stage_dir": str(out_dir) if stage_chrome else "",
        }

    class _JobCancelledError(Exception):
        pass

    def _run_ytdlp_inprocess_download(
        dl_url: str,
        use_ck_flag: bool,
        extra_args_list,
        broad_flag: bool,
        state: dict,
        custom_quality: str = None,
    ):
        ymod = _get_ytdlp_module()
        if ymod is None:
            return None

        ext_args_dict, ok_ea = _parse_extractor_args_for_api(extra_args_list)
        if not ok_ea:
            return None

        logger = _SilentYtdlpLogger()
        saved_files = []
        last_file = ""
        eff_quality = custom_quality if custom_quality is not None else quality
        ff_loc = _get_ffmpeg_location()
        has_ff = bool(ff_loc or shutil.which("ffmpeg"))

        def _prog_hook(d):
            nonlocal last_file
            with JOBS_LOCK:
                if JOBS.get(job_id, {}).get("status") == "cancelled":
                    raise _JobCancelledError("Cancelled")

            fn = d.get("filename") or ""
            if fn:
                p_fn = Path(fn)
                if p_fn.name != last_file:
                    last_file = p_fn.name
                    saved_files.append(p_fn)
                    state["stream_count"] += 1
                    if mode == "audio":
                        state["stream_stage"] = "Downloading audio..."
                    elif state["stream_count"] == 1:
                        state["stream_stage"] = "Downloading video..."
                    else:
                        state["stream_stage"] = "Downloading audio..."

            st_d = d.get("status") or ""
            dl_b = safe_float(d.get("downloaded_bytes")) or 0.0
            tot_b = safe_float(d.get("total_bytes"))
            est_b = safe_float(d.get("total_bytes_estimate"))
            approx = False
            eff_tot = tot_b
            if eff_tot is None and est_b is not None:
                eff_tot = est_b
                approx = True

            pct = 0.0
            if eff_tot and eff_tot > 0:
                pct = min(100.0, max(0.0, (dl_b / eff_tot) * 100.0))
            elif st_d == "finished":
                pct = 100.0

            prefix = state.get("item_prefix") or ""
            stg = prefix + ("Finalizing..." if st_d == "finished" else (state.get("stream_stage") or "Downloading..."))
            _update_job(
                job_id,
                status="downloading",
                stage=stg,
                percent=round(pct, 1),
                downloaded_text=fmt_mb(dl_b) or "0 MB",
                total_text=fmt_mb(eff_tot, approx=approx) if eff_tot else "",
                speed_text=fmt_speed(d.get("speed")),
                eta_text=fmt_eta(d.get("eta")),
                filename=last_file,
            )
            _notify_android_progress(display_title, stg, pct, True)

        def _pp_hook(d):
            nonlocal last_file
            with JOBS_LOCK:
                if JOBS.get(job_id, {}).get("status") == "cancelled":
                    raise _JobCancelledError("Cancelled")
            pp_name = str(d.get("postprocessor") or "")
            if "MetadataParser" in pp_name or "MetadataFromField" in pp_name or "ModifyChapters" in pp_name:
                return
            info_d = d.get("info_dict") or {}
            fp_out = info_d.get("filepath") or info_d.get("_filename") or ""
            if fp_out:
                p_o = Path(fp_out)
                last_file = p_o.name
                saved_files.append(p_o)
            prefix = state.get("item_prefix") or ""
            stg = prefix + "Processing..."
            if "ExtractAudio" in pp_name:
                stg = prefix + "Converting audio..."
            elif "ThumbnailsConvertor" in pp_name:
                stg = prefix + "Preparing cover art..."
            elif "EmbedThumbnail" in pp_name:
                stg = prefix + "Embedding cover art..."
            elif "Metadata" in pp_name:
                stg = prefix + "Writing ID3 tags..."
            elif "Merger" in pp_name:
                stg = prefix + "Merging streams..."
            _update_job(
                job_id,
                status="postprocessing",
                stage=stg,
                percent=99.0,
                speed_text="FFmpeg",
                eta_text="—",
                filename=last_file,
            )
            _notify_android_progress(display_title, stg, 99.0, True)

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "proxy": "",
            "updatetime": False,
            "overwrites": True,
            "socket_timeout": 30,
            "retries": 10,
            "fragment_retries": 10,
            "concurrent_fragment_downloads": 4,
            "geo_bypass": True,
            "nocheckcertificate": True,
            "outtmpl": out_tpl,
            "logger": logger,
            "progress_hooks": [_prog_hook],
            "postprocessor_hooks": [_pp_hook],
        }
        if _is_mobile_env():
            ydl_opts["source_address"] = "0.0.0.0"
        if ff_loc:
            ydl_opts["ffmpeg_location"] = ff_loc

        hdrs = {}
        if referer:
            hdrs["Referer"] = referer
            if not is_major:
                hdrs["User-Agent"] = _active_http_ua()
                orig = _origin_from_url(referer)
                if orig:
                    hdrs["Origin"] = orig
        if extra_cookies and not is_major:
            hdrs["Cookie"] = extra_cookies
        if hdrs:
            ydl_opts["http_headers"] = hdrs

        if ext_args_dict:
            ydl_opts["extractor_args"] = ext_args_dict

        if use_ck_flag:
            ck_cli = get_ytdlp_cookie_cli_args(True)
            if len(ck_cli) >= 2 and ck_cli[0] == "--cookies":
                ydl_opts["cookiefile"] = ck_cli[1]

        if playlist_item == "ALL":
            ydl_opts["noplaylist"] = False
            ydl_opts["ignoreerrors"] = True
        elif playlist_item and str(playlist_item).isdigit():
            ydl_opts["noplaylist"] = False
            ydl_opts["playlist_items"] = str(playlist_item)
        else:
            ydl_opts["noplaylist"] = True

        if mode == "audio":
            audio_fmt = "mp3"
            audio_q = "320"
            audio_sel = "bestaudio/best"
            if eff_quality == "m4a_best":
                audio_fmt = "m4a"
                audio_q = "0"
                audio_sel = "bestaudio[ext=m4a]/bestaudio/best"
            elif eff_quality == "mp3_192":
                audio_q = "192"
            elif eff_quality == "mp3_128":
                audio_q = "128"
            if broad_flag:
                audio_sel = "bestaudio*/best"

            ydl_opts["format"] = audio_sel
            if has_ff:
                ydl_opts["writethumbnail"] = True
                ydl_opts["postprocessors"] = [
                    {"key": "FFmpegExtractAudio", "preferredcodec": audio_fmt, "preferredquality": audio_q},
                    {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
                    {"key": "FFmpegMetadata", "add_metadata": True},
                    {"key": "EmbedThumbnail", "already_have_thumbnail": False},
                ]
        else:
            if (not broad_flag) and eff_quality and eff_quality != "best" and not eff_quality.startswith("stream_"):
                ydl_opts["format"] = eff_quality
            else:
                ydl_opts["format_sort"] = ["res", "vcodec:h264", "acodec:aac", "ext:mp4:m4a"]
                if has_ff:
                    ydl_opts["format"] = "bestvideo*+bestaudio[ext=m4a]/bestvideo*+bestaudio/best"
                else:
                    ydl_opts["format"] = "best[ext=mp4]/best"
            if has_ff:
                ydl_opts["merge_output_format"] = "mp4"

        try:
            with ymod.YoutubeDL(ydl_opts) as ydl:
                rc = ydl.download([dl_url])
            return int(rc or 0), logger.errors, saved_files, last_file
        except _JobCancelledError:
            return 1, ["Cancelled"], saved_files, last_file
        except Exception as exc:
            errs = list(logger.errors)
            msg_e = str(exc).strip()
            if msg_e and msg_e not in errs:
                errs.append(msg_e)
            return 1, errs, saved_files, last_file

    def _run_ytdlp_once(cmd_to_run: list, state: dict):
        error_lines = []
        saved_files = []
        last_file = ""
        proc = subprocess.Popen(
            cmd_to_run,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=clean_env(),
            **_win_popen_kwargs(),
        )
        _update_job(job_id, _proc=proc)

        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue

            if line.startswith("[download] Downloading item "):
                m_it = re.search(r"Downloading item\s+(\d+)\s+of\s+(\d+)", line)
                if m_it:
                    lbl_kind = "Track" if mode == "audio" else "Item"
                    state["item_prefix"] = f"{lbl_kind} {m_it.group(1)}/{m_it.group(2)} · "
                    _update_job(job_id, stage=state["item_prefix"] + "Starting...", percent=0.0)
                else:
                    _update_job(job_id, stage=line.replace("[download] ", ""))
                continue

            if line.startswith("[download] Destination:"):
                dest = line.split("Destination:", 1)[1].strip()
                last_file = Path(dest).name
                saved_files.append(Path(dest))
                state["stream_count"] += 1
                if mode == "audio":
                    state["stream_stage"] = "Downloading audio..."
                elif state["stream_count"] == 1:
                    state["stream_stage"] = "Downloading video..."
                else:
                    state["stream_stage"] = "Downloading audio..."
                _update_job(job_id, stage=state["stream_stage"], filename=last_file)
                continue

            if "[Merger] Merging formats into" in line:
                m_f = re.search(r'"([^"]+)"', line)
                if m_f:
                    p_m = Path(m_f.group(1))
                    last_file = p_m.name
                    saved_files.append(p_m)
                _update_job(
                    job_id,
                    status="postprocessing",
                    stage="Merging streams...",
                    percent=99.0,
                    filename=last_file,
                )
                continue

            if "[ExtractAudio] Destination:" in line:
                dest = line.split("Destination:", 1)[1].strip()
                p_a = Path(dest)
                last_file = p_a.name
                saved_files.append(p_a)
                _update_job(
                    job_id,
                    status="postprocessing",
                    stage=(state.get("item_prefix") or "") + "Converting audio...",
                    percent=99.0,
                    filename=last_file,
                )
                continue

            if "[Metadata] Adding metadata to" in line:
                _update_job(
                    job_id,
                    status="postprocessing",
                    stage=(state.get("item_prefix") or "") + "Writing ID3 tags...",
                    percent=99.0,
                )
                continue

            if "[EmbedThumbnail]" in line:
                _update_job(
                    job_id,
                    status="postprocessing",
                    stage=(state.get("item_prefix") or "") + "Embedding cover art...",
                    percent=99.0,
                )
                continue

            if "has already been downloaded" in line:
                m_a = re.search(r"\[download\]\s+(.+?)\s+has already been downloaded", line)
                if m_a:
                    p_ex = Path(m_a.group(1).strip())
                    last_file = p_ex.name
                    saved_files.append(p_ex)
                _update_job(job_id, percent=100.0, stage="Already downloaded", filename=last_file)
                continue

            if "ERROR:" in line:
                error_lines.append(line)

            updates = parse_progress_line(line, state)
            if updates:
                _update_job(job_id, **updates)

        rc = proc.wait()
        return rc, error_lines, saved_files, last_file

    def _finalize_single_file(cand_files: list, fallback_name: str = ""):
        """Pick the real freshly downloaded media file, fix generic titles like 'Facebook.mp4',
        and move it safely into final_out_dir (with unique (1)/(2) deduplication) when isolated_local_tmp is active."""
        chosen = None
        if (stage_chrome or isolated_local_tmp) and playlist_item != "ALL":
            chosen = _pick_primary_staged_file(out_dir)
        if chosen is None:
            for p in reversed(cand_files or []):
                try:
                    if p and Path(p).exists() and Path(p).is_file():
                        chosen = Path(p)
                        break
                except Exception:
                    pass
        if chosen is None and fallback_name and (out_dir / fallback_name).exists():
            chosen = out_dir / fallback_name
        if chosen is None:
            return None

        # Fix generic filename stems (e.g. "Facebook.mp4") before moving to Downloads
        if _is_generic_title(chosen.stem):
            hint_clean = sanitize_filename(filename_hint, "")
            if hint_clean and not _is_generic_title(hint_clean):
                new_stem = hint_clean
            else:
                # Extract numeric/alphanumeric ID from URL if possible, else timestamp
                m_uid = re.search(r"(?:/(?:videos|reel|reels|posts|watch|status|p|v)/|[?&]v=)([A-Za-z0-9_-]{6,20})", url)
                suffix_tok = m_uid.group(1)[:14] if m_uid else time.strftime("%Y%m%d_%H%M%S")
                new_stem = sanitize_filename(f"{chosen.stem}_{suffix_tok}", f"video_{suffix_tok}")
            renamed = chosen.with_name(f"{new_stem}{chosen.suffix}")
            if renamed != chosen and not renamed.exists():
                try:
                    chosen.rename(renamed)
                    chosen = renamed
                except Exception:
                    pass

        if isolated_local_tmp and not stage_chrome:
            try:
                chosen = _move_file_safely(chosen, final_out_dir)
            except Exception:
                pass
        return chosen

    def worker():
        nonlocal url
        state = {"stream_count": 0, "stream_stage": "Downloading...", "item_prefix": ""}
        last_cmd = None
        rc = 1
        error_lines = []
        saved_files = []
        last_file = ""

        try:
            # Instant DRM pre-check for DASH .mpd streams so encrypted VOD never hangs at 0 MB
            if ".mpd" in low_url:
                d_chk = inspect_dash_mpd(url, referer=referer, extra_cookies=extra_cookies)
                if d_chk.get("is_drm"):
                    drm_lbl = d_chk.get("drm_label") or "DRM"
                    _update_job(
                        job_id,
                        status="error",
                        stage="DRM Protected",
                        error=f"This DASH stream uses {drm_lbl} encryption and cannot be downloaded directly.",
                    )
                    return

            # If this HLS stream has a separate audio_url track (e.g. AWS MediaConvert / MBC VOD), mux both via FFmpeg directly!
            if is_stream_url and audio_url and mode != "audio" and shutil.which("ffmpeg"):
                ok_ff0, ff_dest0, _ = _run_ffmpeg_stream_fallback(
                    job_id, url, out_dir, mode, referer, filename_hint, extra_cookies=extra_cookies, audio_url=audio_url
                )
                with JOBS_LOCK:
                    if JOBS.get(job_id, {}).get("status") == "cancelled":
                        return
                if ok_ff0 and ff_dest0 is not None:
                    final_p = _finalize_single_file([ff_dest0], ff_dest0.name) or ff_dest0
                    scanned = _trigger_media_scan([final_p]) if not stage_chrome else False
                    _update_job(
                        job_id, status="done", stage="Completed", percent=100.0,
                        downloaded_text=fmt_mb(final_p.stat().st_size) or "Done",
                        speed_text="Done", eta_text="00:00",
                        saved_to=display_dir,
                        filename=final_p.name, _filepath=str(final_p), has_file=True, media_scanned=scanned,
                    )
                    return

            for att_idx, att in enumerate(attempts):
                with JOBS_LOCK:
                    if JOBS.get(job_id, {}).get("status") == "cancelled":
                        return
                if att_idx > 0:
                    state = {"stream_count": 0, "stream_stage": "Downloading...", "item_prefix": ""}
                    _update_job(
                        job_id,
                        status="downloading",
                        stage=att["label"],
                        percent=0.0,
                        downloaded_text="0 MB",
                        total_text="",
                        speed_text="—",
                        eta_text="—",
                    )

                use_inproc_dl = (_is_android_app() or not shutil.which("yt-dlp")) and (_get_ytdlp_module() is not None)
                if use_inproc_dl:
                    inproc_out = _run_ytdlp_inprocess_download(
                        dl_url=url,
                        use_ck_flag=att["use_cookies"],
                        extra_args_list=att["extra_args"],
                        broad_flag=att["broad"],
                        state=state,
                    )
                    if inproc_out is not None:
                        rc, error_lines, saved_files, last_file = inproc_out
                    else:
                        use_inproc_dl = False
                if not use_inproc_dl:
                    cmd_run = _build_ytdlp_download_cmd(
                        url=url,
                        mode=mode,
                        quality=quality,
                        out_tpl=out_tpl,
                        referer=referer,
                        is_major=is_major,
                        use_cookies=att["use_cookies"],
                        playlist_item=playlist_item,
                        extra_cookies=extra_cookies,
                        extra_args=att["extra_args"],
                        force_broad_format=att["broad"],
                    )
                    last_cmd = cmd_run
                    rc, error_lines, saved_files, last_file = _run_ytdlp_once(cmd_run, state)

                with JOBS_LOCK:
                    if JOBS.get(job_id, {}).get("status") == "cancelled":
                        return

                if last_file and (out_dir / last_file).exists():
                    saved_files.append(out_dir / last_file)
                existing_now = []
                seen_p = set()
                for p in saved_files:
                    try:
                        if p.exists() and str(p) not in seen_p:
                            seen_p.add(str(p))
                            existing_now.append(p)
                    except Exception:
                        pass

                if rc == 0 or (playlist_item == "ALL" and len(existing_now) > 0):
                    if playlist_item != "ALL":
                        final_single = _finalize_single_file(existing_now, last_file)
                        if final_single is not None:
                            existing_now = [final_single]
                    scanned = _trigger_media_scan(existing_now) if (existing_now and not stage_chrome) else False
                    final_fp = str(existing_now[-1]) if existing_now else ""
                    final_dir = str(existing_now[-1].parent) if (playlist_item == "ALL" and existing_now) else display_dir
                    done_stage = "Completed"
                    if playlist_item == "ALL":
                        done_stage = f"Completed ({len(existing_now)} saved)" if rc == 0 else f"Completed ({len(existing_now)} saved, some skipped)"
                    sz_done = ""
                    if playlist_item != "ALL" and existing_now and existing_now[-1].exists():
                        sz_done = fmt_mb(existing_now[-1].stat().st_size) or ""
                    upd_done = {
                        "status": "done",
                        "stage": done_stage,
                        "percent": 100.0,
                        "speed_text": "Done",
                        "eta_text": "00:00",
                        "saved_to": final_dir,
                        "filename": "" if playlist_item == "ALL" else (existing_now[-1].name if existing_now else last_file),
                        "_filepath": final_fp,
                        "has_file": bool(final_fp and playlist_item != "ALL"),
                        "media_scanned": scanned,
                    }
                    if sz_done:
                        upd_done["downloaded_text"] = sz_done
                    _update_job(job_id, **upd_done)
                    _notify_android_complete(display_title, final_fp)
                    if not _has_active_jobs():
                        _notify_android_progress("OmniSave Studio", "Ready", 0.0, False)
                    return

                # Only retry next attempt if error is format/auth/cookie/client related (or YouTube)
                low_e = "\n".join(error_lines).lower()
                retryable = is_yt or any(
                    k in low_e
                    for k in (
                        "requested format is not available",
                        "format is not available",
                        "only images are available",
                        "sign in",
                        "login",
                        "cookies",
                        "confirm your age",
                        "not a bot",
                        "403",
                    )
                )
                if not retryable:
                    break

            err_msg = "\n".join(error_lines[-3:]) if error_lines else f"Exit code {rc}"
            # If a variant HLS URL failed (404/403/410) and we have the master playlist URL, retry via master_url first!
            low_em = err_msg.lower()
            if is_stream_url and master_url and master_url != url and any(k in low_em for k in ("404", "not found", "403", "410", "gone")):
                _update_job(job_id, status="downloading", stage="Retrying via master playlist...", percent=0.0)
                q_num_m, _ = _parse_quality_from_text(quality_label)
                m_fmt = f"bestvideo[height<={q_num_m}]+bestaudio/best[height<={q_num_m}]/best" if q_num_m else "best"
                m_cmd = _build_ytdlp_download_cmd(
                    url=master_url,
                    mode=mode,
                    quality=m_fmt,
                    out_tpl=out_tpl,
                    referer=referer,
                    is_major=is_major,
                    use_cookies=has_saved_cookies,
                    playlist_item=playlist_item,
                    extra_cookies=extra_cookies,
                    force_broad_format=(not q_num_m),
                )
                try:
                    rc_m, _, m_files, m_last = _run_ytdlp_once(m_cmd, state)
                    if rc_m == 0:
                        final_m = _finalize_single_file([p for p in m_files if p.exists()], m_last)
                        ex_m = [final_m] if final_m is not None else []
                        scanned = _trigger_media_scan(ex_m) if (ex_m and not stage_chrome) else False
                        fp_m = str(ex_m[-1]) if ex_m else ""
                        _update_job(
                            job_id, status="done", stage="Completed", percent=100.0,
                            speed_text="Done", eta_text="00:00",
                            saved_to=display_dir,
                            filename=ex_m[-1].name if ex_m else m_last,
                            _filepath=fp_m, has_file=bool(fp_m), media_scanned=scanned,
                        )
                        return
                except Exception:
                    pass

            # If a sniffed stream URL returned 404 / 410 Gone / 403 Forbidden (expired/broken link), re-fetch fresh URL from page & retry once!
            if is_stream_url and referer and any(k in low_em for k in ("404", "not found", "410", "gone", "403")):
                _update_job(job_id, status="downloading", stage="Refreshing expired stream link...", percent=0.0)
                refreshed = _refresh_expired_stream_url(referer, quality_label, url)
                if refreshed and refreshed.get("url"):
                    new_u = refreshed["url"]
                    if refreshed.get("direct"):
                        base_f = sanitize_filename(filename_hint or "video", f"video_{int(time.time())}")
                        dest_f = out_dir / f"{base_f}.mp4"
                        if dest_f.exists():
                            dest_f = out_dir / f"{base_f}_{int(time.time())}.mp4"
                        try:
                            dl_b, _, _, was_c = _download_single_direct_stream(job_id, new_u, referer, dest_f, "Downloading refreshed stream...")
                            if was_c:
                                return
                            if dest_f.exists() and dest_f.stat().st_size > 0:
                                final_df = _finalize_single_file([dest_f], dest_f.name) or dest_f
                                scanned = _trigger_media_scan([final_df]) if not stage_chrome else False
                                _update_job(
                                    job_id, status="done", stage="Completed", percent=100.0,
                                    downloaded_text=fmt_mb(final_df.stat().st_size) or "Done",
                                    speed_text="Done", eta_text="00:00",
                                    saved_to=display_dir,
                                    filename=final_df.name, _filepath=str(final_df), has_file=True, media_scanned=scanned,
                                )
                                return
                        except Exception:
                            pass
                    elif last_cmd:
                        retry_cmd = list(last_cmd[:-1]) + [new_u]
                        try:
                            rc_r, _, r_files, r_last = _run_ytdlp_once(retry_cmd, state)
                            if rc_r == 0:
                                final_r = _finalize_single_file([p for p in r_files if p.exists()], r_last)
                                ex_r = [final_r] if final_r is not None else []
                                scanned = _trigger_media_scan(ex_r) if (ex_r and not stage_chrome) else False
                                fp_r = str(ex_r[-1]) if ex_r else ""
                                _update_job(
                                    job_id, status="done", stage="Completed", percent=100.0,
                                    speed_text="Done", eta_text="00:00",
                                    saved_to=display_dir,
                                    filename=ex_r[-1].name if ex_r else r_last,
                                    _filepath=fp_r, has_file=bool(fp_r), media_scanned=scanned,
                                )
                                return
                        except Exception:
                            pass
                        url = new_u

            # Never fall back to FFmpeg when yt-dlp already identified DRM / encryption
            is_drm_err = any(k in low_em for k in ("drm", "encrypted", "widevine", "fairplay", "playready"))
            if is_drm_err:
                _update_job(
                    job_id,
                    status="error",
                    stage="DRM Protected",
                    error=err_msg + "\n\nThis stream is DRM-encrypted by the site and cannot be downloaded directly.",
                )
                return

            # Automatic FFmpeg fallback for HLS/DASH streams when yt-dlp times out or fails transport
            if is_stream_url and shutil.which("ffmpeg"):
                ok_ff, ff_dest, ff_err = _run_ffmpeg_stream_fallback(
                    job_id, url, out_dir, mode, referer, filename_hint, extra_cookies=extra_cookies, audio_url=audio_url
                )
                with JOBS_LOCK:
                    if JOBS.get(job_id, {}).get("status") == "cancelled":
                        return
                if ok_ff and ff_dest is not None:
                    final_ff = _finalize_single_file([ff_dest], ff_dest.name) or ff_dest
                    scanned = _trigger_media_scan([final_ff]) if not stage_chrome else False
                    _update_job(
                        job_id,
                        status="done",
                        stage="Completed",
                        percent=100.0,
                        downloaded_text=fmt_mb(final_ff.stat().st_size) or "Done",
                        speed_text="Done",
                        eta_text="00:00",
                        saved_to=display_dir,
                        filename=final_ff.name,
                        _filepath=str(final_ff),
                        has_file=True,
                        media_scanned=scanned,
                    )
                    return
                if ff_err:
                    err_msg = err_msg + "\nFFmpeg fallback: " + ff_err

            _update_job(job_id, status="error", stage="Failed", error=err_msg)
        except Exception as exc:
            _update_job(job_id, status="error", stage="Failed", error=str(exc))
        finally:
            _update_job(job_id, _proc=None)
            if not _has_active_jobs():
                _notify_android_progress("OmniSave Studio", "Ready", 0.0, False)
            if isolated_local_tmp and not stage_chrome:
                shutil.rmtree(out_dir, ignore_errors=True)
            elif stage_chrome:
                with JOBS_LOCK:
                    st_now = JOBS.get(job_id, {}).get("status")
                if st_now in ("cancelled", "error"):
                    shutil.rmtree(out_dir, ignore_errors=True)

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def scan_wa_statuses() -> dict:
    save_dir = get_wa_save_dir()
    seen_dirs = set()
    seen_files = set()
    items = []
    new_map = {}
    found_dirs = 0
    permission_denied = False

    for source_label, dpath in WA_CANDIDATE_DIRS:
        try:
            if not dpath.exists():
                continue
            real_dir = str(dpath.resolve())
            if real_dir in seen_dirs:
                continue
            seen_dirs.add(real_dir)
            entries = list(dpath.iterdir())
            found_dirs += 1
        except PermissionError:
            permission_denied = True
            continue
        except OSError:
            continue

        for entry in entries:
            try:
                if not entry.is_file():
                    continue
                ext = entry.suffix.lower()
                if ext in IMAGE_EXTS:
                    mtype = "image"
                elif ext in VIDEO_EXTS:
                    mtype = "video"
                else:
                    continue
                real_file = str(entry.resolve())
                if real_file in seen_files:
                    continue
                seen_files.add(real_file)

                st = entry.stat()
                if st.st_size <= 0:
                    continue
                fid = hashlib.sha1(real_file.encode("utf-8", errors="ignore")).hexdigest()[:12]
                mime = mimetypes.guess_type(entry.name)[0] or (
                    "video/mp4" if mtype == "video" else "image/jpeg"
                )
                is_saved = (save_dir / entry.name).exists()

                new_map[fid] = {
                    "path": entry,
                    "name": entry.name,
                    "type": mtype,
                    "mime": mime,
                    "source": source_label,
                }
                items.append({
                    "id": fid,
                    "name": entry.name,
                    "type": mtype,
                    "size": fmt_mb(st.st_size),
                    "mtime": st.st_mtime,
                    "age": fmt_age(st.st_mtime),
                    "source": source_label,
                    "saved": is_saved,
                })
            except OSError:
                continue

    items.sort(key=lambda x: x["mtime"], reverse=True)
    with WA_LOCK:
        WA_MAP.clear()
        WA_MAP.update(new_map)

    return {
        "items": items,
        "found_dirs": found_dirs,
        "permission_denied": permission_denied and found_dirs == 0,
        "save_dir": str(save_dir),
    }


def save_wa_statuses(item_ids):
    if not item_ids:
        raise RuntimeError("No status selected.")
    save_dir = get_wa_save_dir()
    save_dir.mkdir(parents=True, exist_ok=True)
    saved_paths = []

    with WA_LOCK:
        records = [WA_MAP.get(fid) for fid in item_ids]

    for rec in records:
        if not rec or not rec["path"].exists():
            continue
        dest = save_dir / rec["name"]
        shutil.copy2(rec["path"], dest)
        saved_paths.append(dest)

    if not saved_paths:
        raise RuntimeError("Status file not found. Tap Refresh and try again.")

    _trigger_media_scan(saved_paths)
    return {
        "ok": True,
        "count": len(saved_paths),
        "saved_to": str(saved_paths[0]) if len(saved_paths) == 1 else str(save_dir),
    }


def parse_byte_range(range_header: str, file_size: int):
    if not range_header or not range_header.startswith("bytes="):
        return None
    m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not m:
        return None
    start_s, end_s = m.group(1), m.group(2)
    if start_s == "" and end_s == "":
        return None
    if start_s == "":
        suffix = int(end_s)
        start = max(0, file_size - suffix)
        end = file_size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else file_size - 1
    if start >= file_size or start > end:
        return None
    end = min(end, file_size - 1)
    return start, end


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _cors_origin(self) -> str:
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return ""
        allowed_exact = {
            f"chrome-extension://{FIXED_CHROME_EXT_ID}",
            f"http://127.0.0.1:{PORT}",
            f"http://localhost:{PORT}",
        }
        if origin in allowed_exact:
            return origin
        return ""

    def _send_cors_headers(self):
        orig = self._cors_origin()
        if orig:
            self.send_header("Access-Control-Allow-Origin", orig)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        _touch_activity()
        self.send_response(204)
        self._send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(self, code: int, obj: dict):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _stream_wa_media(self, fid: str, as_download: bool):
        with WA_LOCK:
            rec = WA_MAP.get(fid)
        if not rec or not rec["path"].exists():
            self.send_response(404)
            self.end_headers()
            return

        fpath = rec["path"]
        fsize = fpath.stat().st_size
        mime = rec["mime"]
        rng = parse_byte_range(self.headers.get("Range"), fsize)

        if rng is not None:
            start, end = rng
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{fsize}")
        else:
            start, length = 0, fsize
            self.send_response(200)

        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, max-age=300")
        if as_download:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(rec['name'])}")
        self.end_headers()

        try:
            with open(fpath, "rb") as fp:
                fp.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fp.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_dl_file(self, qs: dict, head_only: bool = False):
        job_id = (qs.get("id") or [""])[0].strip()
        do_cleanup = (qs.get("cleanup") or ["0"])[0] == "1"
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            fp_str = job.get("_filepath") if job else ""
            stage_dir_str = job.get("_stage_dir") if job else ""
        if (not fp_str or not Path(fp_str).exists()) and stage_dir_str:
            alt = _pick_primary_staged_file(Path(stage_dir_str))
            if alt is not None:
                fp_str = str(alt)
                _update_job(job_id, _filepath=fp_str, filename=alt.name, has_file=True)
        if not fp_str or not Path(fp_str).exists():
            self.send_response(404)
            self._send_cors_headers()
            self.end_headers()
            return

        fpath = Path(fp_str)
        fsize = fpath.stat().st_size
        mime = mimetypes.guess_type(fpath.name)[0] or "application/octet-stream"
        rng = parse_byte_range(self.headers.get("Range"), fsize)

        if rng is not None:
            start, end = rng
            length = end - start + 1
            self.send_response(206)
            self._send_cors_headers()
            self.send_header("Content-Range", f"bytes {start}-{end}/{fsize}")
        else:
            start, length = 0, fsize
            self.send_response(200)
            self._send_cors_headers()

        ascii_fallback = re.sub(r'[^A-Za-z0-9._ -]+', '_', fpath.name).strip(' .') or f"download{fpath.suffix or '.bin'}"
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f'attachment; filename="{ascii_fallback}"; filename*=UTF-8''{quote(fpath.name)}')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if head_only:
            return

        sent_all = False
        try:
            with open(fpath, "rb") as fp:
                fp.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fp.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            sent_all = True
        except (BrokenPipeError, ConnectionResetError):
            pass

        if sent_all and do_cleanup and stage_dir_str and rng is None:
            try:
                sd = Path(stage_dir_str).resolve()
                if str(sd).startswith(str(STAGE_DIR.resolve())):
                    shutil.rmtree(sd, ignore_errors=True)
                    _update_job(job_id, has_file=False, _filepath="", _stage_dir="", chrome_handed_off=True)
            except Exception:
                pass

    def do_HEAD(self):
        _touch_activity()
        parsed = urlparse(self.path)
        if parsed.path == "/api/dl/file":
            self._serve_dl_file(parse_qs(parsed.query), head_only=True)
        elif parsed.path == "/api/dl/test":
            payload = b"OmniSave Studio Chrome Downloads test file - OK!\n"
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Disposition", 'attachment; filename="OmniSave_Test.txt"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        _touch_activity()
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            raw = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
        elif path == "/api/progress":
            qs = parse_qs(parsed.query)
            job_id = (qs.get("id") or [""])[0].strip()
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                data = _public_job(job) if job else None
            if data is None:
                self._send_json(404, {"error": "Job not found"})
            else:
                self._send_json(200, data)
        elif path == "/api/settings":
            ck = get_cookie_info()
            self._send_json(200, {
                "version": VERSION,
                "is_desktop": _is_desktop_env(),
                "os_name": platform.system() or sys.platform,
                "ytdlp_version": get_ytdlp_version(),
                "ffmpeg": bool(shutil.which("ffmpeg")),
                "media_scan": bool(_is_android_app() or shutil.which("termux-media-scan")),
                "is_android_app": _is_android_app(),
                "fb_cookies": _has_facebook_auth_cookies(),
                "save_dir": str(get_base_download_dir()),
                "is_custom_save_dir": bool((load_config().get("save_dir") or "").strip()),
                "chrome_tray_compatible": _should_stage_for_chrome(True, None),
                **ck,
            })
        elif path == "/api/wa/list":
            self._send_json(200, scan_wa_statuses())
        elif path == "/api/wa/media":
            qs = parse_qs(parsed.query)
            fid = (qs.get("id") or [""])[0].strip()
            as_dl = (qs.get("download") or ["0"])[0] == "1"
            self._stream_wa_media(fid, as_dl)
        elif path == "/api/dl/file":
            self._serve_dl_file(parse_qs(parsed.query), head_only=False)
        elif path == "/api/dl/test":
            payload = b"OmniSave Studio Chrome Downloads test file - OK!\n"
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Disposition", 'attachment; filename="OmniSave_Test.txt"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
        elif path == "/api/jobs":
            with JOBS_LOCK:
                all_jobs = sorted(
                    [_public_job(j) for j in JOBS.values()],
                    key=lambda x: x.get("created_at", 0),
                    reverse=True,
                )
            self._send_json(200, {"jobs": all_jobs[:20]})
        elif path == "/api/ping":
            self._send_json(200, {
                "ok": True,
                "version": VERSION,
                "idle_timeout": IDLE_TIMEOUT_SEC,
                "active_jobs": _has_active_jobs(),
            })
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        _touch_activity()
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw_body = self.rfile.read(length) if length > 0 else b"{}"
            body = json.loads(raw_body.decode("utf-8", errors="replace") or "{}")

            if path == "/api/info":
                self._send_json(200, run_info(
                    body.get("url") or "",
                    referer=(body.get("referer") or "").strip(),
                    extra_cookies=(body.get("extra_cookies") or "").strip(),
                    title_hint=(body.get("title_hint") or "").strip(),
                    force=bool(body.get("force")),
                ))
            elif path == "/api/download":
                job_id = start_download_job(
                    raw_url=body.get("url") or "",
                    mode=(body.get("mode") or "video").strip(),
                    quality=(body.get("quality") or "best").strip(),
                    quality_label=(body.get("quality_label") or "").strip(),
                    referer=(body.get("referer") or "").strip(),
                    direct=bool(body.get("direct")),
                    audio_url=(body.get("audio_url") or "").strip(),
                    filename_hint=(body.get("filename_hint") or "").strip(),
                    playlist_item=body.get("playlist_item"),
                    extra_cookies=(body.get("extra_cookies") or "").strip(),
                    via_chrome=bool(body.get("via_chrome")),
                    master_url=(body.get("master_url") or "").strip(),
                )
                self._send_json(200, {"job_id": job_id})
            elif path == "/api/dl/cleanup":
                job_id = (body.get("job_id") or "").strip()
                cleaned = False
                with JOBS_LOCK:
                    job = JOBS.get(job_id)
                    sd_s = job.get("_stage_dir") if job else ""
                if sd_s:
                    try:
                        sd = Path(sd_s).resolve()
                        if str(sd).startswith(str(STAGE_DIR.resolve())):
                            shutil.rmtree(sd, ignore_errors=True)
                            _update_job(job_id, has_file=False, _filepath="", _stage_dir="", chrome_handed_off=True)
                            cleaned = True
                    except Exception:
                        pass
                self._send_json(200, {"ok": True, "cleaned": cleaned})
            elif path == "/api/jobs/clear":
                _rescue_staged_files_to_downloads(only_done_jobs=True)
                removed = 0
                with JOBS_LOCK:
                    for jid in list(JOBS.keys()):
                        st = JOBS[jid].get("status")
                        if st in ("done", "error", "cancelled"):
                            sd_s = JOBS[jid].get("_stage_dir")
                            if sd_s:
                                try:
                                    shutil.rmtree(Path(sd_s), ignore_errors=True)
                                except Exception:
                                    pass
                            JOBS.pop(jid, None)
                            removed += 1
                self._send_json(200, {"ok": True, "removed": removed})
            elif path == "/api/cancel":
                job_id = (body.get("job_id") or "").strip()
                with JOBS_LOCK:
                    job = JOBS.get(job_id)
                    proc = job.get("_proc") if job else None
                    if job:
                        job["status"] = "cancelled"
                        job["stage"] = "Cancelled"
                if proc is not None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                self._send_json(200, {"ok": True})
            elif path == "/api/engine/update":
                self._send_json(200, update_ytdlp())
            elif path == "/api/open-folder":
                job_id = (body.get("job_id") or "").strip()
                folder_p = (body.get("path") or "").strip()
                if job_id:
                    with JOBS_LOCK:
                        j_open = JOBS.get(job_id)
                        sd_open = j_open.get("_stage_dir") if j_open else ""
                        st_open = j_open.get("status") if j_open else ""
                    if st_open == "done" and sd_open and Path(sd_open).exists():
                        _rescue_staged_files_to_downloads(only_done_jobs=True)
                        with JOBS_LOCK:
                            j_after = JOBS.get(job_id)
                            if j_after and j_after.get("saved_to"):
                                folder_p = j_after["saved_to"]
                ok = open_local_folder(folder_p)
                self._send_json(200, {"ok": ok})
            elif path == "/api/shutdown":
                _rescue_staged_files_to_downloads(only_done_jobs=True)
                self._send_json(200, {"ok": True})
                def _do_shut():
                    time.sleep(0.15)
                    if SERVER_INSTANCE is not None:
                        try:
                            SERVER_INSTANCE.shutdown()
                        except Exception:
                            pass
                    os._exit(0)
                threading.Thread(target=_do_shut, daemon=True).start()
            elif path == "/api/save-dir":
                action = (body.get("action") or "save").strip().lower()
                if action == "save":
                    saved_p = set_custom_download_dir(body.get("path") or "")
                    self._send_json(200, {"ok": True, "save_dir": str(saved_p), "is_custom_save_dir": True})
                elif action == "reset":
                    def_p = reset_custom_download_dir()
                    self._send_json(200, {"ok": True, "save_dir": str(def_p), "is_custom_save_dir": False})
                elif action == "browse":
                    chosen = pick_local_folder_dialog(str(get_base_download_dir()))
                    if not chosen:
                        self._send_json(200, {"ok": True, "cancelled": True, "save_dir": str(get_base_download_dir())})
                    else:
                        saved_p = set_custom_download_dir(chosen)
                        self._send_json(200, {"ok": True, "cancelled": False, "save_dir": str(saved_p), "is_custom_save_dir": True})
                else:
                    self._send_json(400, {"error": "Unknown action"})
            elif path == "/api/cookies":
                action = (body.get("action") or "").strip()
                cfg = load_config()
                if action == "save":
                    content = (body.get("content") or "").strip()
                    if not content:
                        raise RuntimeError("Empty cookie content.")
                    sanitized = sanitize_netscape_cookies(content)
                    COOKIES_FILE.write_text(sanitized, encoding="utf-8")
                    _clear_info_cache()
                    cfg["use_cookies"] = bool(body.get("use_cookies", True))
                    cfg["cookie_source"] = "file"
                    save_config(cfg)
                    self._send_json(200, get_cookie_info())
                elif action == "ext_sync":
                    ck_list = body.get("cookies") or []
                    if isinstance(ck_list, list) and len(ck_list) > 0:
                        synced = merge_extension_cookies(ck_list, COOKIES_FILE)
                        cfg["use_cookies"] = True
                        cfg["cookie_source"] = "file"
                        save_config(cfg)
                        info = get_cookie_info()
                        info["synced_count"] = synced
                        self._send_json(200, info)
                    else:
                        info = get_cookie_info()
                        info["synced_count"] = 0
                        self._send_json(200, info)
                elif action == "set_source":
                    src_m = (body.get("cookie_source") or "file").strip().lower()
                    if src_m in ("browser", "file") and (_is_desktop_env() or src_m == "file"):
                        cfg["cookie_source"] = src_m
                    if "cookie_browser" in body:
                        b = (body.get("cookie_browser") or "auto").strip().lower()
                        if b in ("auto",) + SUPPORTED_BROWSERS:
                            cfg["cookie_browser"] = b
                    if "cookie_profile" in body:
                        cfg["cookie_profile"] = (body.get("cookie_profile") or "").strip()
                    save_config(cfg)
                    self._send_json(200, get_cookie_info())
                elif action == "browser_sync":
                    b = (body.get("cookie_browser") or cfg.get("cookie_browser") or "auto").strip().lower()
                    prof = (body.get("cookie_profile") if "cookie_profile" in body else cfg.get("cookie_profile") or "").strip()
                    if b in ("auto",) + SUPPORTED_BROWSERS:
                        cfg["cookie_browser"] = b
                    cfg["cookie_profile"] = prof
                    cfg["cookie_source"] = "browser"
                    cfg["use_cookies"] = True
                    save_config(cfg)
                    sync_browser_cookies(force=True, browser_override=cfg["cookie_browser"], profile_override=prof)
                    self._send_json(200, get_cookie_info())
                elif action == "toggle":
                    cfg["use_cookies"] = bool(body.get("use_cookies", False))
                    save_config(cfg)
                    _clear_info_cache()
                    if cfg["use_cookies"] and _is_desktop_env() and cfg.get("cookie_source") == "browser":
                        sync_browser_cookies(force=False)
                    self._send_json(200, get_cookie_info())
                elif action == "clear":
                    if COOKIES_FILE.exists():
                        COOKIES_FILE.unlink()
                    if BROWSER_COOKIES_FILE.exists():
                        BROWSER_COOKIES_FILE.unlink()
                    with BROWSER_CK_LOCK:
                        BROWSER_CK_STATE.update({"browser": "", "synced_at": 0.0, "lines": 0, "error": "", "cli_fallback": False})
                    cfg["use_cookies"] = False
                    save_config(cfg)
                    _clear_info_cache()
                    self._send_json(200, get_cookie_info())
                else:
                    self._send_json(400, {"error": "Unknown action"})
            elif path == "/api/wa/save":
                ids = body.get("ids")
                if not ids and body.get("id"):
                    ids = [body.get("id")]
                self._send_json(200, save_wa_statuses(ids or []))
            else:
                self._send_json(404, {"error": "Not found"})
        except Exception as exc:
            self._send_json(400, {"error": str(exc)})


def _open_browser_url(target_url: str) -> bool:
    if not target_url:
        return False
    if _is_desktop_env():
        try:
            return bool(webbrowser.open(target_url))
        except Exception:
            return False
    prefix = os.environ.get("PREFIX", "")
    opener_bin = shutil.which("termux-open-url") or (
        str(Path(prefix) / "bin/termux-open-url") if prefix and (Path(prefix) / "bin/termux-open-url").exists() else ""
    )
    if opener_bin:
        try:
            rc = subprocess.call(
                [opener_bin, target_url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if rc == 0:
                return True
        except Exception:
            pass
    am_bin = shutil.which("am")
    if am_bin:
        try:
            rc = subprocess.call(
                [am_bin, "start", "--user", "0", "-a", "android.intent.action.VIEW", "-d", target_url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if rc == 0:
                return True
        except Exception:
            pass
    return False


def install_termux_url_opener(script_path: Path, opener_path: Path = None) -> Path:
    if opener_path is None:
        opener_path = Path.home() / "bin" / "termux-url-opener"
    opener_path.parent.mkdir(parents=True, exist_ok=True)

    py_bin = sys.executable or "/data/data/com.termux/files/usr/bin/python3"
    resolved_script = str(script_path.resolve())

    opener_code = f"""#!{py_bin}
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import quote

PORT = {PORT}
SERVER_SCRIPT = {resolved_script!r}


def extract_shared_url(raw_text: str) -> str:
    s = (raw_text or "").strip()
    if not s:
        return ""
    m = re.search(r"https?://[^\\s<>\\\"']+", s)
    if m:
        return m.group(0).rstrip(".,);]}}>\\\"'")
    return s


def server_is_running() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{{PORT}}/api/ping", timeout=1.2) as r:
            return r.status == 200
    except Exception:
        return False


def open_in_browser(url: str) -> bool:
    prefix = os.environ.get("PREFIX", "")
    opener = shutil.which("termux-open-url") or (
        str(Path(prefix) / "bin/termux-open-url") if prefix and (Path(prefix) / "bin/termux-open-url").exists() else ""
    )
    if opener:
        try:
            if subprocess.call([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
                return True
        except Exception:
            pass
    am_bin = shutil.which("am")
    if am_bin:
        try:
            if subprocess.call(
                [am_bin, "start", "--user", "0", "-a", "android.intent.action.VIEW", "-d", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ) == 0:
                return True
        except Exception:
            pass
    return False


def main():
    raw = " ".join(sys.argv[1:]).strip()
    shared_url = extract_shared_url(raw)
    target = f"http://127.0.0.1:{{PORT}}/"
    if shared_url:
        target = f"http://127.0.0.1:{{PORT}}/?url={{quote(shared_url, safe='')}}"

    if server_is_running():
        open_in_browser(target)
        return

    if not Path(SERVER_SCRIPT).exists():
        print(f"OmniSave script not found: {{SERVER_SCRIPT}}", flush=True)
        sys.exit(1)

    print("Starting OmniSave Studio...", flush=True)
    os.execv(sys.executable, [sys.executable, SERVER_SCRIPT, "--open-url", target])


if __name__ == "__main__":
    main()
"""
    opener_path.write_text(opener_code, encoding="utf-8")
    os.chmod(opener_path, 0o755)
    return opener_path


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_android_server(port: int = 18980):
    """Entrypoint called by OmniSaveService (Kotlin + Chaquopy) on a background daemon thread."""
    global SERVER_INSTANCE, PORT
    PORT = int(port or 8000)
    _setup_android_ffmpeg()
    _rescue_staged_files_to_downloads(only_done_jobs=False)
    if COOKIES_FILE.exists():
        try:
            COOKIES_FILE.write_text(
                sanitize_netscape_cookies(COOKIES_FILE.read_text(encoding="utf-8", errors="ignore")),
                encoding="utf-8",
            )
        except Exception:
            pass
    _warmup_ytdlp_async()
    srv = ReusableThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    SERVER_INSTANCE = srv
    srv.serve_forever()


def stop_android_server():
    global SERVER_INSTANCE
    srv = SERVER_INSTANCE
    SERVER_INSTANCE = None
    if srv is not None:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    _rescue_staged_files_to_downloads(only_done_jobs=False)
    if COOKIES_FILE.exists():
        try:
            COOKIES_FILE.write_text(sanitize_netscape_cookies(COOKIES_FILE.read_text(encoding="utf-8", errors="ignore")), encoding="utf-8")
        except Exception:
            pass

    open_target = ""
    local_only = False
    args = sys.argv[1:]
    if "--open-url" in args:
        idx_o = args.index("--open-url")
        if idx_o + 1 < len(args):
            open_target = args[idx_o + 1].strip()
    elif "--open" in args:
        open_target = f"http://127.0.0.1:{PORT}/"
    if "--local-only" in args:
        local_only = True
    if "--idle-timeout" in args:
        idx_it = args.index("--idle-timeout")
        if idx_it + 1 < len(args):
            try:
                IDLE_TIMEOUT_SEC = max(0, int(args[idx_it + 1]))
            except Exception:
                IDLE_TIMEOUT_SEC = 0

    if _is_termux_env():
        try:
            op_path = install_termux_url_opener(Path(__file__))
            print(f"Share -> Termux: Ready ({op_path})", flush=True)
        except Exception as exc:
            print(f"Share -> Termux setup skipped: {exc}", flush=True)
    elif not local_only:
        cfg_boot = load_config()
        if cfg_boot.get("use_cookies", True) and cfg_boot.get("cookie_source") == "browser":
            def _bg_cookie_sync():
                r = sync_browser_cookies(force=True)
                if r.get("ok"):
                    extra = f"{r['lines']} entries" if r.get("lines") else "CLI mode"
                    print(f"Browser Cookies: {r['browser']} ({extra})", flush=True)
                elif r.get("error"):
                    print(f"Browser Cookies: {r['error']}", flush=True)
            threading.Thread(target=_bg_cookie_sync, daemon=True).start()

    bind_host = "127.0.0.1" if local_only else "0.0.0.0"
    print(f"OmniSave Studio {VERSION}: http://127.0.0.1:{PORT}", flush=True)
    if not local_only:
        lan_ip = get_lan_ip()
        if lan_ip:
            print(f"LAN: http://{lan_ip}:{PORT}", flush=True)
    if IDLE_TIMEOUT_SEC > 0:
        print(f"Auto-sleep enabled: {IDLE_TIMEOUT_SEC}s idle timeout", flush=True)

    _warmup_ytdlp_async()
    server = ThreadingHTTPServer((bind_host, PORT), Handler)
    SERVER_INSTANCE = server

    if IDLE_TIMEOUT_SEC > 0:
        def _idle_watchdog():
            while True:
                time.sleep(5)
                if _has_active_jobs():
                    _touch_activity()
                    continue
                if (time.time() - LAST_ACTIVITY_TS) >= IDLE_TIMEOUT_SEC:
                    _rescue_staged_files_to_downloads(only_done_jobs=True)
                    try:
                        server.shutdown()
                    except Exception:
                        pass
                    os._exit(0)
        threading.Thread(target=_idle_watchdog, daemon=True).start()

    if open_target:
        def _delayed_open(u):
            time.sleep(0.18)
            _open_browser_url(u)
        threading.Thread(target=_delayed_open, args=(open_target,), daemon=True).start()
    server.serve_forever()
