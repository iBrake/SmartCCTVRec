#!/usr/bin/env python3
"""
cctv_viewer.py - a tiny local web viewer for CCTV mp4 recordings.

HOW TO USE
----------
1. Put this file in the folder that CONTAINS your camera folders, e.g.

       CCTV/
       |- cctv_viewer.py   <- here
       |- FrontDoor/
       |   |- 2026-06-20_08-15.mp4
       |   `- ...
       `- BackDoor/
           `- ...

2. Open a terminal in that folder and run:

       python cctv_viewer.py

   (On some systems use "python3" instead of "python".)

3. It prints a link like http://localhost:4322 - open that in your browser.
   Press Ctrl+C in the terminal to stop the server.

Options:
   python cctv_viewer.py --port 9000      # use a different port
   python cctv_viewer.py --dir /path/to/CCTV   # point at another folder
   python cctv_viewer.py --host 0.0.0.0   # allow other machines on the LAN
                                          # (default is localhost-only)

ON-THE-FLY TRANSCODING (optional, needs ffmpeg + ffprobe on PATH)
-----------------------------------------------------------------
If ffmpeg is installed, clips that browsers can't play natively (e.g. H.265 /
HEVC) are transcoded to a complete 720p H.264 file the first time you open them,
cached, and served with full seeking. Later views are instant. Clips that are
already H.264 are streamed untouched. The cache lives in a temp folder by
default (see --cache-dir) and never modifies your original recordings.

   python cctv_viewer.py --encoder auto   # default: auto-pick a working HW
                                          # encoder (VAAPI/QSV/NVENC/AMF),
                                          # else CPU libx264
   python cctv_viewer.py --encoder libx264      # force CPU encoding
   python cctv_viewer.py --encoder h264_vaapi   # force a specific encoder
   python cctv_viewer.py --scale-height 480     # transcode to 480p instead
   python cctv_viewer.py --force-transcode      # transcode every clip
   python cctv_viewer.py --no-transcode         # never transcode (raw only)
   python cctv_viewer.py --max-transcodes 2     # cap concurrent transcodes

No installation needed for the viewer itself - it uses only Python's standard
library. ffmpeg is only required if you want transcoding.
"""

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".avi", ".hevc", ".h265"}
ROOT = os.getcwd()

# --- transcoding config (filled in by main) -------------------------------
FFMPEG = None
FFPROBE = None
ENCODER = "libx264"
SCALE_HEIGHT = 720
FORCE_TRANSCODE = False
NO_TRANSCODE = False
DEBUG = False
VAAPI_DEVICE = "/dev/dri/renderD128"
CACHE_DIR = None          # where finalized transcodes are stored
CACHE_LIMIT_MB = 4096     # evict oldest cached files past this total (0 = no limit)
_sem = None               # optional concurrency limiter
_probe_cache = {}         # path -> (video_codec, fps)
_probe_lock = threading.Lock()
_key_locks = {}           # cache-file path -> Lock (avoid transcoding same clip twice)
_key_locks_lock = threading.Lock()

# Try to pull a timestamp out of common CCTV filename patterns.
_TS_PATTERNS = [
    # 2026-06-20_08-15-30  or 2026-06-20 08-15-30 etc.
    re.compile(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})[ _T-]+(\d{2})[-_.:]?(\d{2})[-_.:]?(\d{2})?"),
    # 20260620_081530
    re.compile(r"(20\d{2})(\d{2})(\d{2})[ _T-]?(\d{2})(\d{2})(\d{2})?"),
]


def parse_timestamp(name):
    for pat in _TS_PATTERNS:
        m = pat.search(name)
        if m:
            y, mo, d, h, mi, s = m.groups()
            try:
                return _dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0))
            except ValueError:
                continue
    return None


def scan(root):
    """Return {camera_name: [clip, ...]} for every subfolder holding videos."""
    cameras = {}
    for entry in sorted(os.listdir(root)):
        folder = os.path.join(root, entry)
        if not os.path.isdir(folder) or entry.startswith("."):
            continue
        clips = []
        for fname in os.listdir(folder):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in VIDEO_EXTS:
                continue
            fpath = os.path.join(folder, fname)
            try:
                st = os.stat(fpath)
            except OSError:
                continue
            ts = parse_timestamp(fname)
            sort_time = ts.timestamp() if ts else st.st_mtime
            clips.append({
                "name": fname,
                "src": "stream/" + entry + "/" + fname,   # player uses this
                "url": "video/" + entry + "/" + fname,     # raw file (download)
                "size": st.st_size,
                "mtime": st.st_mtime,
                "timestamp": ts.isoformat() if ts else None,
                "sort_time": sort_time,
            })
        if clips:
            clips.sort(key=lambda c: c["sort_time"], reverse=True)
            cameras[entry] = clips
    return cameras


# --- codec detection / transcode command ----------------------------------

def probe_source(path):
    """Return (video_codec, fps) for a file, cached. fps falls back to 15."""
    with _probe_lock:
        if path in _probe_cache:
            return _probe_cache[path]
    codec, fps = "", 0.0
    if FFPROBE:
        try:
            r = subprocess.run(
                [FFPROBE, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name,r_frame_rate",
                 "-of", "default=nk=1:nw=1", path],
                capture_output=True, text=True, timeout=15)
            lines = [x.strip() for x in r.stdout.splitlines() if x.strip()]
            if lines:
                codec = lines[0].lower()
            if len(lines) > 1 and "/" in lines[1]:
                num, den = lines[1].split("/")
                if float(den) != 0:
                    fps = float(num) / float(den)
        except Exception:
            pass
    if not (1.0 <= fps <= 60.0):
        fps = 15.0
    res = (codec, fps)
    with _probe_lock:
        _probe_cache[path] = res
    return res


def needs_transcode(path):
    if NO_TRANSCODE:
        return False
    if not (FFMPEG and FFPROBE):
        return False
    if FORCE_TRANSCODE:
        return True
    codec, _ = probe_source(path)
    # h264 plays natively everywhere; transcode anything else we could identify.
    return codec not in ("", "h264")


def transcode_file_cmd(src, dst, enc=None):
    # Many CCTV clips are two recordings spliced into one file, which leaves an
    # edit list + a timestamp discontinuity at the join. We defuse it by ignoring
    # the edit list and resampling to a clean, zero-based, constant frame rate.
    # Output is a COMPLETE faststart mp4 (moov at the front) so the browser gets
    # a real duration and full seeking - no live-stream guesswork.
    enc = enc or ENCODER
    _, fps = probe_source(src)
    fps_s = ("%.4f" % fps).rstrip("0").rstrip(".")
    loglevel = "warning" if DEBUG else "error"
    base = [FFMPEG, "-hide_banner", "-loglevel", loglevel, "-ignore_editlist", "1"]
    if enc == "h264_vaapi":
        # Scale + frame-rate + pixel-format on the CPU, then upload uniform nv12
        # frames to the GPU for ENCODING only. Doing the scale with scale_vaapi
        # on-GPU triggers "Impossible to convert between formats / Error
        # reinitializing filters" on spliced clips, so we avoid it.
        cmd = list(base) + ["-vaapi_device", VAAPI_DEVICE, "-i", src,
                            "-vf", "scale=-2:%d,fps=%s,setpts=PTS-STARTPTS,format=nv12,hwupload"
                            % (SCALE_HEIGHT, fps_s),
                            "-c:v", "h264_vaapi", "-qp", "24"]
    else:
        cmd = list(base) + ["-i", src,
                            "-vf", "scale=-2:%d,fps=%s,setpts=PTS-STARTPTS" % (SCALE_HEIGHT, fps_s),
                            "-c:v", enc]
        if enc == "libx264":
            cmd += ["-preset", "veryfast", "-crf", "23"]
        else:  # generic hardware encoder (qsv / nvenc / amf)
            cmd += ["-b:v", "3M", "-maxrate", "3M", "-bufsize", "6M"]
    cmd += ["-af", "aresample=async=1:first_pts=0",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            "-f", "mp4", dst, "-y"]
    return cmd


def cache_path(src):
    st = os.stat(src)
    key = "%s|%d|%d|%d|%s" % (os.path.abspath(src), int(st.st_mtime), st.st_size,
                             SCALE_HEIGHT, ENCODER)
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return os.path.join(CACHE_DIR, h + ".mp4")


def _key_lock(dst):
    with _key_locks_lock:
        lk = _key_locks.get(dst)
        if lk is None:
            lk = threading.Lock()
            _key_locks[dst] = lk
        return lk


def evict_cache():
    if CACHE_LIMIT_MB <= 0:
        return
    try:
        names = [f for f in os.listdir(CACHE_DIR) if f.endswith(".mp4")]
    except OSError:
        return
    entries, total = [], 0
    for f in names:
        p = os.path.join(CACHE_DIR, f)
        try:
            stt = os.stat(p)
        except OSError:
            continue
        entries.append((stt.st_mtime, stt.st_size, p))
        total += stt.st_size
    limit = CACHE_LIMIT_MB * 1024 * 1024
    if total <= limit:
        return
    entries.sort()  # oldest first
    for _mtime, size, p in entries:
        if total <= limit:
            break
        try:
            os.remove(p)
            total -= size
        except OSError:
            pass


def ensure_transcoded(src):
    """Transcode src to a complete cached mp4 (once) and return its path, or None."""
    dst = cache_path(src)
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        try:
            os.utime(dst, None)  # bump mtime so it survives LRU eviction
        except OSError:
            pass
        return dst
    with _key_lock(dst):
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            return dst  # another thread finished it while we waited
        tmp = dst + ".part"
        # Try the configured encoder first; if a hardware encoder fails (some
        # spliced clips make VAAPI's filter graph reinitialize, which it can't
        # do), fall back to CPU libx264, which handles these files reliably.
        attempts = [ENCODER]
        if ENCODER != "libx264":
            attempts.append("libx264")
        err = ""
        ok = False
        for i, enc in enumerate(attempts):
            if _sem is not None:
                _sem.acquire()
            try:
                proc = subprocess.run(transcode_file_cmd(src, tmp, enc=enc),
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            finally:
                if _sem is not None:
                    _sem.release()
            err = proc.stderr.decode("utf-8", "replace").strip() if proc.stderr else ""
            ok = proc.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0
            if ok:
                if i > 0:
                    sys.stderr.write("[cctv] %s failed on %s; succeeded on CPU (libx264)\n"
                                     % (ENCODER, os.path.basename(src)))
                    sys.stderr.flush()
                break
            # failed this encoder
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            if i + 1 < len(attempts):
                sys.stderr.write("[cctv] %s failed on %s, retrying on CPU...\n"
                                 % (enc, os.path.basename(src)))
                sys.stderr.flush()
        if not ok:
            sys.stderr.write("[cctv] transcode FAILED: %s\n        %s\n"
                             % (os.path.basename(src), (err[-1500:] or "(no ffmpeg output)")))
            sys.stderr.flush()
            return None
        if DEBUG and err:
            sys.stderr.write("[cctv] ffmpeg ok: %s\n%s\n" % (os.path.basename(src), err))
            sys.stderr.flush()
        os.replace(tmp, dst)
    evict_cache()
    return dst


def _encoder_listed(name):
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=15).stdout
        return name in out
    except Exception:
        return False


def _encoder_works(name):
    """Do a tiny throwaway encode to confirm the encoder actually runs."""
    if name == "h264_vaapi":
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-vaapi_device", VAAPI_DEVICE,
               "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.1:r=5",
               "-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-f", "null", "-"]
    else:
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error",
               "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.1:r=5",
               "-c:v", name, "-f", "null", "-"]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


def detect_encoder(preferred):
    if preferred != "auto":
        return preferred
    if not FFMPEG:
        return "libx264"
    for cand in ("h264_vaapi", "h264_qsv", "h264_nvenc", "h264_amf"):
        if _encoder_listed(cand) and _encoder_works(cand):
            return cand
    return "libx264"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep the terminal quiet
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            self._send_html()
        elif path == "/api/clips":
            self._send_clips()
        elif path.startswith("/stream/"):
            self._send_stream(path[len("/stream/"):])
        elif path.startswith("/video/"):
            self._send_video(path[len("/video/"):])
        else:
            self.send_error(404)

    def _send_html(self):
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_clips(self):
        data = json.dumps(scan(ROOT)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _resolve(self, rel):
        rel = unquote(rel)
        full = os.path.normpath(os.path.join(ROOT, rel))
        if not full.startswith(os.path.abspath(ROOT) + os.sep):
            return None
        if not os.path.isfile(full):
            return None
        return full

    def _send_video(self, rel):
        full = self._resolve(rel)
        if full is None:
            self.send_error(404)
            return
        self._serve_file_range(full)

    def _send_stream(self, rel):
        full = self._resolve(rel)
        if full is None:
            self.send_error(404)
            return
        if not needs_transcode(full):
            # Already browser-friendly: serve the raw file with full seeking.
            self._serve_file_range(full)
            return
        # Finalize the clip to a complete cached mp4, then serve it like any
        # other file (real duration, full seeking). First view waits for the
        # encode; later views are instant cache hits.
        dst = ensure_transcoded(full)
        if dst is None:
            self.send_error(500, "Transcode failed - run the server with --debug to see ffmpeg output")
            return
        self._serve_file_range(dst)

    def _serve_file_range(self, full):
        size = os.path.getsize(full)
        ctype = "video/mp4"
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            start = int(m.group(1)) if m and m.group(1) else 0
            end = int(m.group(2)) if m and m.group(2) else size - 1
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.end_headers()
                return
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Content-Length", str(length))
            self.end_headers()
            self._copy_range(full, start, length)
        else:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            self._copy_range(full, 0, size)

    def _copy_range(self, full, start, length):
        try:
            with open(full, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser closed/seeked - normal for video


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CCTV Viewer</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 15px/1.4 system-ui, sans-serif;
         background: #0e1116; color: #e6e6e6; }
  header { padding: 14px 18px; background: #161b22; border-bottom: 1px solid #2a2f37;
           display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
  header h1 { font-size: 17px; margin: 0; font-weight: 600; }
  #tabs { display: flex; gap: 8px; flex-wrap: wrap; }
  .tab { padding: 6px 14px; border-radius: 999px; background: #21262d; cursor: pointer;
         border: 1px solid #30363d; font-size: 14px; }
  .tab.active { background: #2f81f7; border-color: #2f81f7; color: #fff; }
  .layout { display: flex; height: calc(100vh - 59px); }
  #list { width: 320px; overflow-y: auto; border-right: 1px solid #2a2f37; flex: none; }
  .clip { padding: 10px 16px; cursor: pointer; border-bottom: 1px solid #1c2128; }
  .clip:hover { background: #161b22; }
  .clip.active { background: #1f6feb22; border-left: 3px solid #2f81f7; padding-left: 13px; }
  .clip .t { font-weight: 600; }
  .clip .s { color: #8b949e; font-size: 12px; margin-top: 2px; }
  #stage { flex: 1; display: flex; flex-direction: column; align-items: center;
           justify-content: center; padding: 18px; min-width: 0; }
  video { width: 100%; max-width: 1000px; max-height: 70vh; background: #000;
          border-radius: 8px; }
  #now { margin-top: 12px; color: #8b949e; font-size: 13px; text-align: center; }
  #empty { color: #8b949e; padding: 40px; text-align: center; }
  .err { color: #f0883e; max-width: 560px; text-align: center; font-size: 13px; }
  a { color: #2f81f7; }
</style>
</head>
<body>
<header>
  <h1>CCTV Viewer</h1>
  <div id="tabs"></div>
  <span id="count" style="color:#8b949e;font-size:13px;margin-left:auto"></span>
</header>
<div class="layout">
  <div id="list"></div>
  <div id="stage">
    <video id="player" controls preload="metadata"></video>
    <div id="now"></div>
  </div>
</div>
<script>
let data = {}, current = null;

function fmtSize(b){ const u=['B','KB','MB','GB']; let i=0; while(b>=1024&&i<u.length-1){b/=1024;i++;} return b.toFixed(i?1:0)+' '+u[i]; }
function fmtTime(c){
  if(c.timestamp){ const d=new Date(c.timestamp); return d.toLocaleString([], {dateStyle:'medium', timeStyle:'short'}); }
  const d=new Date(c.mtime*1000); return d.toLocaleString([], {dateStyle:'medium', timeStyle:'short'})+' (file date)';
}

async function load(){
  try {
    const r = await fetch('api/clips');
    data = await r.json();
  } catch(e){ document.getElementById('list').innerHTML='<div id="empty">Could not reach server.</div>'; return; }
  const cams = Object.keys(data);
  const tabs = document.getElementById('tabs');
  tabs.innerHTML='';
  if(!cams.length){
    document.getElementById('list').innerHTML='<div id="empty">No video files found in any subfolder.<br>Put this script in the folder that contains your camera folders.</div>';
    return;
  }
  cams.forEach((c,i)=>{
    const el=document.createElement('div');
    el.className='tab'+(i===0?' active':'');
    el.textContent=c+' ('+data[c].length+')';
    el.onclick=()=>{ document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active')); el.classList.add('active'); showCam(c); };
    tabs.appendChild(el);
  });
  showCam(cams[0]);
}

function showCam(cam){
  const list=document.getElementById('list');
  list.innerHTML='';
  document.getElementById('count').textContent=cam+': '+data[cam].length+' clips';
  data[cam].forEach((c,idx)=>{
    const el=document.createElement('div');
    el.className='clip';
    el.innerHTML='<div class="t">'+fmtTime(c)+'</div><div class="s">'+c.name+' &middot; '+fmtSize(c.size)+'</div>';
    el.onclick=()=>{ document.querySelectorAll('.clip').forEach(x=>x.classList.remove('active')); el.classList.add('active'); play(c); };
    list.appendChild(el);
    if(idx===0){ el.classList.add('active'); play(c); }
  });
}

function play(c){
  current=c;
  const v=document.getElementById('player');
  v.src=c.src;
  v.load();
  document.getElementById('now').textContent=c.name+'  -  '+fmtTime(c)+'  -  '+fmtSize(c.size);
  v.onerror=()=>{
    document.getElementById('now').innerHTML='<span class="err">This clip would not play. If it is H.265/HEVC, install ffmpeg on the server (and leave transcoding on) so it can be converted to H.264 for the browser.</span>';
  };
}

load();
</script>
</body>
</html>
"""


def pick_free_port(host, preferred):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, preferred))
        s.close()
        return preferred
    except OSError:
        s.close()
        s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s2.bind((host, 0))
        port = s2.getsockname()[1]
        s2.close()
        return port


def main():
    global ROOT, FFMPEG, FFPROBE, ENCODER, SCALE_HEIGHT, FORCE_TRANSCODE
    global NO_TRANSCODE, VAAPI_DEVICE, _sem, DEBUG, CACHE_DIR, CACHE_LIMIT_MB
    ap = argparse.ArgumentParser(description="Tiny local CCTV mp4 viewer with optional on-the-fly transcoding.")
    ap.add_argument("--port", type=int, default=4322)
    ap.add_argument("--host", default="127.0.0.1",
                    help="Interface to bind. Default 127.0.0.1 (this machine only). "
                         "Use 0.0.0.0 to allow other machines on your network.")
    ap.add_argument("--dir", default=os.getcwd(), help="Folder containing the camera subfolders.")
    ap.add_argument("--no-open", action="store_true", help="Don't auto-open the browser.")
    ap.add_argument("--encoder", default="auto",
                    help="auto (default), libx264, h264_vaapi, h264_qsv, h264_nvenc, h264_amf.")
    ap.add_argument("--scale-height", type=int, default=720, help="Transcode output height (default 720).")
    ap.add_argument("--force-transcode", action="store_true", help="Transcode every clip, even H.264.")
    ap.add_argument("--no-transcode", action="store_true", help="Never transcode; serve raw files only.")
    ap.add_argument("--vaapi-device", default="/dev/dri/renderD128", help="VAAPI render node (Linux).")
    ap.add_argument("--max-transcodes", type=int, default=0,
                    help="Max simultaneous transcodes (0 = unlimited).")
    ap.add_argument("--debug", action="store_true",
                    help="Show ffmpeg output in the terminal (helps diagnose playback issues).")
    ap.add_argument("--cache-dir", default=None,
                    help="Where finalized transcodes are stored (default: a temp folder).")
    ap.add_argument("--cache-limit-mb", type=int, default=4096,
                    help="Evict oldest cached transcodes past this total size (0 = no limit).")
    args = ap.parse_args()

    ROOT = os.path.abspath(args.dir)
    SCALE_HEIGHT = args.scale_height
    FORCE_TRANSCODE = args.force_transcode
    NO_TRANSCODE = args.no_transcode
    DEBUG = args.debug
    VAAPI_DEVICE = args.vaapi_device
    CACHE_LIMIT_MB = args.cache_limit_mb
    CACHE_DIR = args.cache_dir or os.path.join(tempfile.gettempdir(), "cctv_viewer_cache")
    FFMPEG = shutil.which("ffmpeg")
    FFPROBE = shutil.which("ffprobe")

    if NO_TRANSCODE:
        ENCODER = "libx264"
        transcode_status = "off (--no-transcode)"
    elif not (FFMPEG and FFPROBE):
        transcode_status = "off (ffmpeg/ffprobe not found on PATH)"
    else:
        ENCODER = detect_encoder(args.encoder)
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
        except OSError as e:
            print("Warning: could not create cache dir %s (%s)" % (CACHE_DIR, e))
        # Hardware encoders (VAAPI/QSV/NVENC/AMF) typically allow only one or two
        # simultaneous sessions, so default them to one transcode at a time.
        # libx264 (CPU) has no such limit. User --max-transcodes always wins.
        if args.max_transcodes > 0:
            _sem = threading.BoundedSemaphore(args.max_transcodes)
            limit_note = "max %d at once" % args.max_transcodes
        elif ENCODER != "libx264":
            _sem = threading.BoundedSemaphore(1)
            limit_note = "1 at a time (hardware encoder)"
        else:
            limit_note = "unlimited"
        how = "every clip" if FORCE_TRANSCODE else "non-H.264 clips only"
        transcode_status = "on -> %s, %dp, encoder=%s, %s (finalize+cache in %s)" % (
            how, SCALE_HEIGHT, ENCODER, limit_note, CACHE_DIR)

    port = pick_free_port(args.host, args.port)
    open_host = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    url = "http://%s:%d" % (open_host, port)

    cams = scan(ROOT)
    print("Serving CCTV in:", ROOT)
    if cams:
        print("Found cameras:", ", ".join("%s (%d)" % (k, len(v)) for k, v in cams.items()))
    else:
        print("No video subfolders found yet - check this script sits next to FrontDoor/BackDoor folders.")
    print("Transcoding:", transcode_status)
    print("\nBound to %s:%d" % (args.host, port))
    if args.host == "0.0.0.0":
        print("Reachable from other machines at:  http://<this-machine-ip>:%d" % port)
    print("Open this in your browser:  %s" % url)
    print("Press Ctrl+C to stop.\n")

    if not args.no_open and args.host != "0.0.0.0":
        try:
            webbrowser.open(url)
        except Exception:
            pass

    httpd = ThreadingHTTPServer((args.host, port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
