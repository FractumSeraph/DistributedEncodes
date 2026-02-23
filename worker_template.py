import argparse
import time
import requests
import subprocess
import os
import re
import shutil
import threading
import sys
import traceback
import platform
import json
import signal
import zipfile
import tarfile
import io
import gzip
from datetime import datetime, timedelta

# ==============================================================================
# CONFIGURATION
# ==============================================================================
DEFAULT_MANAGER_URL = "https://encode.fractumseraph.net/"
DEFAULT_USERNAME = "Anonymous"
DEFAULT_WORKERNAME = f"Node-{int(time.time())}"
WORKER_VERSION = "2.7.0" # Incremented for linux exit fix

WORKER_SECRET = os.environ.get("WORKER_SECRET", "DefaultInsecureSecret")

SHUTDOWN_EVENT = threading.Event()
UPDATE_AVAILABLE = False
LAST_UPDATE_CHECK = 0
CHECK_LOCK = threading.Lock()
CONSOLE_LOCK = threading.Lock()
PROGRESS_LOCK = threading.Lock()
MONITOR_PAUSED = threading.Event()
WORKER_PROGRESS = {} 
PAUSE_REQUESTED = False
ACTIVE_PROCS = {}
PROC_LOCK = threading.Lock()

# Global paths for executables
FFMPEG_CMD = "ffmpeg"
FFPROBE_CMD = "ffprobe"

# Detect OS to handle Fonts
_script_dir = os.path.dirname(os.path.abspath(__file__))

ENCODING_CONFIG = {
    "VIDEO_CODEC": "libsvtav1",
    "VIDEO_PRESET": "2",
    "VIDEO_CRF": "63",           
    "VIDEO_PIX_FMT": "yuv420p",
    "VIDEO_SCALE": "scale=-2:480",
    "AUDIO_CODEC": "libopus",
    "AUDIO_BITRATE": "24k",      # Perfect for Mono Speech
    "AUDIO_CHANNELS": "1",       # CHANGED: 2 -> 1 (Mono) for better quality
    "SUBTITLE_CODEC": "mov_text", 
    "OUTPUT_EXT": ".mp4"
}


class QuotaTracker:
    def __init__(self, limit_gb, worker_name):
        self.limit_bytes = int(limit_gb * 1024**3) if limit_gb > 0 else 0
        self.filename = f"usage_{re.sub(r'[^a-zA-Z0-9]', '', worker_name)}.json"
        self.lock = threading.Lock()
        self.current_usage = 0
        self.last_save = 0
        self._load()

    def _load(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r') as f:
                    data = json.load(f)
                    if data.get('date') == today:
                        self.current_usage = data.get('bytes', 0)
                    else:
                        self.current_usage = 0
                        self._save()
            except:
                self.current_usage = 0
        else:
            self.current_usage = 0

    def _save(self):
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            with open(self.filename, 'w') as f:
                json.dump({"date": today, "bytes": self.current_usage}, f)
        except: pass

    def check_cap(self):
        if self.limit_bytes <= 0: return False
        today = datetime.now().strftime("%Y-%m-%d")
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r') as f:
                    data = json.load(f)
                    if data.get('date') != today:
                        with self.lock:
                            self.current_usage = 0
                            self._save()
                        return False
            except: pass
        return self.current_usage >= self.limit_bytes

    def add_usage(self, num_bytes):
        if self.limit_bytes <= 0: return
        with self.lock:
            self.current_usage += num_bytes
            if time.time() - self.last_save > 30:
                self._save()
                self.last_save = time.time()
    
    def force_save(self):
        with self.lock: self._save()

    def get_remaining_str(self):
        if self.limit_bytes <= 0: return "Unlimited"
        rem = self.limit_bytes - self.current_usage
        if rem < 0: rem = 0
        return f"{rem / 1024**3:.2f} GB"
    
    def get_wait_time(self):
        now = datetime.now()
        tomorrow = now + timedelta(days=1)
        midnight = datetime(year=tomorrow.year, month=tomorrow.month, day=tomorrow.day, hour=0, minute=0, second=1)
        return (midnight - now).total_seconds()


def get_auth_headers():
    headers = {'User-Agent': f'FractumWorker/{WORKER_VERSION}'}
    if WORKER_SECRET:
        headers['X-Worker-Token'] = WORKER_SECRET
    return headers

def get_term_width():
    try: return shutil.get_terminal_size((80, 20)).columns
    except: return 80

def safe_print(message):
    with CONSOLE_LOCK:
        try:
            width = get_term_width()
            # Truncate to avoid wrapping
            if len(message) > width - 1:
                message = message[:width-1]
            
            # Use spaces to clear the line instead of ANSI \033[2K which breaks some cmd.exe
            padded = message.ljust(width - 1)
            sys.stdout.write(f'\r{padded}\n')
            sys.stdout.flush()
        except:
            # Absolute fallback
            try: print(message)
            except: pass

def log(worker_id, message, level="INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    safe_print(f"[{timestamp}] [{worker_id}] [{level}] {message}")

def signal_handler(sig, frame):
    global PAUSE_REQUESTED
    if platform.system() == 'Windows':
        sys.stdout.write('\n[!] Windows Shutdown Initiated...\n')
        SHUTDOWN_EVENT.set()
        try:
            kill_processes()
        except: pass
        sys.exit(0)
    else:
        if not PAUSE_REQUESTED:
            PAUSE_REQUESTED = True
            try:
                sys.stdout.write('\n\n[!] PAUSE REQUESTED (Stopping gracefully...)\n')
                sys.stdout.flush()
            except: pass

def toggle_processes(suspend=True):
    with PROC_LOCK:
        for wid, proc in ACTIVE_PROCS.items():
            if proc.poll() is None:
                try:
                    if platform.system() == 'Windows': pass 
                    else:
                        sig = signal.SIGSTOP if suspend else signal.SIGCONT
                        os.kill(proc.pid, sig)
                except: pass

def kill_processes():
    with PROC_LOCK:
        for wid, proc in ACTIVE_PROCS.items():
            try:
                if proc.poll() is None: proc.kill()
            except: pass

def check_version(manager_url):
    global LAST_UPDATE_CHECK
    with CHECK_LOCK:
        if time.time() - LAST_UPDATE_CHECK < 600: return False
        LAST_UPDATE_CHECK = time.time()
    try:
        url = f"{manager_url}/dl/worker"
        r = requests.get(url, headers=get_auth_headers(), timeout=10)
        if r.status_code == 200:
            match = re.search(r'WORKER_VERSION\s*=\s*"([^"]+)"', r.text)
            if match and match.group(1) != WORKER_VERSION:
                safe_print(f"[!] Update found: {WORKER_VERSION} -> {match.group(1)}")
                return True
    except: pass
    return False

def apply_update(manager_url):
    safe_print("[*] Downloading and applying update...")
    try:
        url = f"{manager_url}/dl/worker"
        r = requests.get(url, headers=get_auth_headers(), timeout=30)
        if r.status_code == 200:
            with open(os.path.abspath(sys.argv[0]), 'w', encoding='utf-8') as f:
                f.write(r.text)
            safe_print("[*] Restarting worker...")
            os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        safe_print(f"[!] Failed to apply update: {e}")

def print_progress(worker_id, current, total, prefix='', suffix=''):
    if total <= 0: return
    percent = 100 * (current / float(total))
    if percent > 100: percent = 100
    width = get_term_width()
    overhead = 12 + len(worker_id) + len(prefix) + 10 + len(suffix)
    bar_length = width - overhead - 5
    if bar_length < 10: bar_length = 10
    filled_length = int(bar_length * current // total)
    
    block_char = '█'
    fill_char = '-'
    
    try:
        bar = block_char * filled_length + fill_char * (bar_length - filled_length)
        line = f'[{datetime.now().strftime("%H:%M:%S")}] [{worker_id}] {prefix} |{bar}| {percent:.1f}% {suffix}'
        
        with CONSOLE_LOCK:
            if len(line) > width - 1:
                line = line[:width - 1]
            
            padded = line.ljust(width - 1)
            sys.stdout.write(f'\r{padded}')
            sys.stdout.flush()
            
    except UnicodeEncodeError:
        block_char = '='
        fill_char = '-'
        try:
            bar = block_char * filled_length + fill_char * (bar_length - filled_length)
            line = f'[{datetime.now().strftime("%H:%M:%S")}] [{worker_id}] {prefix} |{bar}| {percent:.1f}% {suffix}'
            with CONSOLE_LOCK:
                if len(line) > width - 1: line = line[:width - 1]
                padded = line.ljust(width - 1)
                sys.stdout.write(f'\r{padded}')
                sys.stdout.flush()
        except: pass 

    if current >= total: 
        try: sys.stdout.write('\n')
        except: pass

def monitor_status_loop(worker_ids):
    while not SHUTDOWN_EVENT.is_set():
        if PAUSE_REQUESTED or MONITOR_PAUSED.is_set():
             time.sleep(0.5); continue
        parts = []
        with PROGRESS_LOCK:
            for wid in sorted(worker_ids, key=lambda x: x.split('-')[-1]):
                try: short_id = wid.split('-')[-1]
                except: short_id = wid
                state = WORKER_PROGRESS.get(wid, "Idle")
                parts.append(f"[{short_id}: {state}]")
        if parts:
            line = " ".join(parts)
            width = get_term_width()
            if len(line) > width - 1: line = line[:width-4] + "..."
            with CONSOLE_LOCK:
                try:
                    padded = line.ljust(width - 1)
                    sys.stdout.write(f'\r{padded}')
                    sys.stdout.flush()
                except: pass
        time.sleep(0.5)

def get_seconds(t):
    try:
        parts = t.split(':')
        h = int(parts[0]); m = int(parts[1]); s = float(parts[2])
        return h*3600 + m*60 + s
    except: return 0

# ==============================================================================
# FFMPEG MANAGEMENT
# ==============================================================================

def download_ffmpeg_windows():
    print("[*] FFmpeg not found. Attempting download (FULL Version ~128MB)...")
    
    urls = [
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
        "https://vsv.fractumseraph.net/ffmpeg-master-latest-win64-gpl.zip"
    ]
    
    temp_zip = "ffmpeg_temp.zip"
    
    for url in urls:
        print(f"[*] Trying mirror: {url}")
        try:
            with requests.get(url, stream=True, timeout=180) as r:
                r.raise_for_status()
                total_size = int(r.headers.get('content-length', 0))
                downloaded = 0
                
                with open(temp_zip, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            pct = int((downloaded / total_size) * 100)
                            try:
                                msg = f"    Downloading... {pct}%"
                                sys.stdout.write(f"\r{msg}")
                                sys.stdout.flush()
                            except: pass
            print("\n[*] Extracting FFmpeg...")
            
            with zipfile.ZipFile(temp_zip) as z:
                ffmpeg_path = None
                ffprobe_path = None
                for file in z.namelist():
                    if file.endswith("bin/ffmpeg.exe"): ffmpeg_path = file
                    if file.endswith("bin/ffprobe.exe"): ffprobe_path = file
                
                if not ffmpeg_path or not ffprobe_path:
                    print("\n[!] Binaries not found in zip.")
                    continue
                
                with open("ffmpeg.exe", "wb") as f: f.write(z.read(ffmpeg_path))
                with open("ffprobe.exe", "wb") as f: f.write(z.read(ffprobe_path))
                
            os.remove(temp_zip)
            print("[*] FFmpeg installed locally!")
            return True
            
        except Exception as e:
            print(f"\n[!] Mirror failed: {e}")
            if os.path.exists(temp_zip): os.remove(temp_zip)
            continue
            
    return False

def download_ffmpeg_linux():
    print("[*] Downloading static FFmpeg build (BtbN)...")
    arch = platform.machine().lower()
    
    if arch in ['x86_64', 'amd64']:
        url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
    elif arch in ['aarch64', 'arm64']:
        url = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz"
    else:
        print(f"[!] Unsupported architecture for auto-download: {arch}")
        return False

    try:
        r = requests.get(url, stream=True, allow_redirects=True, timeout=180)
        r.raise_for_status()
        
        tar_name = f"ffmpeg_static_{int(time.time())}.tar.xz"
        total_size = int(r.headers.get('content-length', 0))
        downloaded = 0
        
        with open(tar_name, 'wb') as f:
             for chunk in r.iter_content(chunk_size=8192):
                 f.write(chunk)
                 downloaded += len(chunk)
                 if total_size > 0:
                     pct = int((downloaded / total_size) * 100)
                     try:
                         sys.stdout.write(f"\r    Downloading... {pct}%")
                         sys.stdout.flush()
                     except: pass
        
        print("\n[*] Extracting FFmpeg...")
        ext_dir = f"temp_ffmpeg_ext_{int(time.time())}"
        os.makedirs(ext_dir, exist_ok=True)
        
        with tarfile.open(tar_name, "r:xz") as tar:
            tar.extractall(path=ext_dir)
            
        found_ffmpeg = False
        for root, dirs, files in os.walk(ext_dir):
            for file in files:
                if file == "ffmpeg":
                    shutil.move(os.path.join(root, file), "ffmpeg")
                    found_ffmpeg = True
                elif file == "ffprobe":
                    shutil.move(os.path.join(root, file), "ffprobe")

        if os.path.exists(tar_name): os.remove(tar_name)
        if os.path.exists(ext_dir): shutil.rmtree(ext_dir)
        
        if found_ffmpeg:
            os.chmod("ffmpeg", 0o755)
            if os.path.exists("ffprobe"): os.chmod("ffprobe", 0o755)
            print("[*] FFmpeg installed locally!")
            return True
        else:
            print("[!] Could not find 'ffmpeg' binary in extracted archive.")
            return False

    except Exception as e:
        print(f"\n[!] Linux Download failed: {e}")
        return False

def has_svtav1(cmd):
    """Checks if the given ffmpeg command supports libsvtav1"""
    try:
        res = subprocess.run([cmd, "-hide_banner", "-encoders"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding='utf-8', errors='replace')
        return "libsvtav1" in res.stdout
    except:
        return False

def check_ffmpeg():
    global FFMPEG_CMD, FFPROBE_CMD
    
    local_ffmpeg = os.path.abspath("ffmpeg.exe" if platform.system() == "Windows" else "./ffmpeg")
    local_ffprobe = os.path.abspath("ffprobe.exe" if platform.system() == "Windows" else "./ffprobe")
    
    if os.path.exists(local_ffmpeg) and has_svtav1(local_ffmpeg):
        FFMPEG_CMD = local_ffmpeg
        if os.path.exists(local_ffprobe): FFPROBE_CMD = local_ffprobe
        return

    if shutil.which("ffmpeg") and has_svtav1("ffmpeg"):
        FFMPEG_CMD = "ffmpeg"
        FFPROBE_CMD = "ffprobe"
        return

    print("[!] Valid FFmpeg with libsvtav1 not found.")
    
    download_success = False
    if platform.system() == "Windows":
        download_success = download_ffmpeg_windows()
    else:
        download_success = download_ffmpeg_linux()
        
    if download_success:
        if os.path.exists(local_ffmpeg) and has_svtav1(local_ffmpeg):
            FFMPEG_CMD = local_ffmpeg
            if os.path.exists(local_ffprobe): FFPROBE_CMD = local_ffprobe
            return

    print("\n[CRITICAL ERROR] Could not find or download a version of FFmpeg with 'libsvtav1' support.")
    print("Please install FFmpeg with SVT-AV1 support manually.")
    sys.exit(1)

def verify_connection(manager_url):
    try:
        if requests.get(manager_url, timeout=10).status_code < 400: return True
    except: pass
    print(f"[!] Could not connect to {manager_url}"); return False


def worker_task(worker_id, manager_url, temp_dir, quota_tracker, single_mode=False, series_id=None):
    global UPDATE_AVAILABLE
    log(worker_id, "Thread active.")
    os.makedirs(temp_dir, exist_ok=True)
    
    def update_status(msg):
        with PROGRESS_LOCK: WORKER_PROGRESS[worker_id] = msg
        
    def post_status(status, progress=0, duration=0, error_msg=None):
        try:
            payload = {
                "worker_id": worker_id, 
                "job_id": job_id, 
                "status": status, 
                "progress": progress,
                "version": WORKER_VERSION
            }
            if duration > 0: payload["duration"] = duration
            if error_msg: payload["error"] = error_msg
            requests.post(f"{manager_url}/report_status", json=payload, headers=get_auth_headers(), timeout=10)
        except: pass

    while not SHUTDOWN_EVENT.is_set():
        if PAUSE_REQUESTED:
             time.sleep(1); continue

        try:
            if quota_tracker and quota_tracker.check_cap():
                wait_sec = quota_tracker.get_wait_time()
                update_status("Quota Limit")
                log(worker_id, f"Daily Quota Reached. Reset in {wait_sec/3600:.1f} hours.")
                while wait_sec > 0 and not SHUTDOWN_EVENT.is_set():
                    time.sleep(min(60, wait_sec))
                    wait_sec -= 60
                    if not quota_tracker.check_cap(): break
                continue

            update_status("Idle")
            if check_version(manager_url):
                UPDATE_AVAILABLE = True; SHUTDOWN_EVENT.set(); break

            try: 
                params = {'worker_id': worker_id, 'version': WORKER_VERSION}
                if series_id: params['series_id'] = series_id
                r = requests.get(f"{manager_url}/get_job", params=params, headers=get_auth_headers(), timeout=10)
            except: time.sleep(5); continue

            data = r.json() if r.status_code == 200 else None
            
            if r.status_code == 401:
                log(worker_id, "AUTH FAILED: Worker Secret is invalid or missing.", "CRITICAL")
                SHUTDOWN_EVENT.set(); break

            if data and data.get("status") == "ok":
                job = data["job"]; job_id = job['id']; dl_url = job['download_url']
                log(worker_id, f"Job: {job['filename']}")
                
                local_src = os.path.join(temp_dir, "source.tmp")
                local_dst = os.path.join(temp_dir, f"encoded{ENCODING_CONFIG['OUTPUT_EXT']}")
                
                post_status("downloading", 0)
                
                download_success = False
                for attempt in range(3):
                    try:
                        with requests.get(dl_url, stream=True, timeout=60) as r:
                            r.raise_for_status()
                            total_size = int(r.headers.get('content-length', 0))
                            downloaded = 0; last_rep = 0
                            with open(local_src, 'wb') as f:
                                for chunk in r.iter_content(chunk_size=65536):
                                    if PAUSE_REQUESTED: 
                                        while PAUSE_REQUESTED: time.sleep(1)
                                        if SHUTDOWN_EVENT.is_set(): raise Exception("Shutdown")
                                    
                                    if chunk:
                                        if quota_tracker: quota_tracker.add_usage(len(chunk))
                                        f.write(chunk)
                                        downloaded += len(chunk)
                                        pct = int((downloaded/total_size)*100) if total_size > 0 else 0
                                        
                                        if single_mode: print_progress(worker_id, downloaded, total_size, prefix='DL')
                                        else: update_status(f"DL {pct}%")

                                        if time.time() - last_rep > 30:
                                            post_status("downloading", pct)
                                            last_rep = time.time()
                        
                        if quota_tracker: quota_tracker.force_save()
                        if single_mode: print_progress(worker_id, total_size, total_size, prefix='DL', suffix='OK')
                        download_success = True
                        break
                    except Exception as e:
                        log(worker_id, f"Download attempt {attempt+1}/3 failed: {e}", "WARNING")
                        time.sleep(5)

                if not download_success:
                    err_msg = "Download failed after 3 attempts"
                    log(worker_id, err_msg, "ERROR")
                    post_status("failed", error_msg=err_msg)
                    time.sleep(5); continue

                update_status("Probing")
                total_sec = 0; total_min = 0; audio_index = 0; subtitle_indices = []
                try:
                    cmd_probe = [FFPROBE_CMD, '-v', 'quiet', '-print_format', 'json', '-show_streams', '-show_format', local_src]
                    res = subprocess.run(cmd_probe, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8', errors='replace')
                    probe_data = json.loads(res.stdout)
                    dur = probe_data.get('format', {}).get('duration')
                    if dur: total_sec = float(dur); total_min = int(total_sec / 60)
                    
                    audio_streams = [s for s in probe_data.get('streams', []) if s['codec_type'] == 'audio']
                    if audio_streams:
                        audio_index = audio_streams[0]['index']
                        for s in audio_streams:
                            if s.get('tags', {}).get('language', '').lower() in ['eng', 'en', 'english']:
                                audio_index = s['index']; break
                    for s in probe_data.get('streams', []):
                        if s['codec_type'] == 'subtitle':
                            if s.get('codec_name', '').lower() in ['subrip', 'ass', 'webvtt', 'mov_text', 'text', 'srt', 'ssa']:
                                subtitle_indices.append(s['index'])
                except: pass

                log(worker_id, f"Encoding ({total_min}m)...")
                post_status("processing", 0, total_min)

                # Copy font to temp dir for robust relative path usage (Avoids Windows path escaping issues)
                local_font = os.path.join(temp_dir, "arial.ttf")
                try:
                    src_font = os.path.join(_script_dir, "arial.ttf")
                    if os.path.exists(src_font):
                        shutil.copy(src_font, local_font)
                except: pass
                
                # Construct video filter with font
                font_arg = local_font.replace("\\", "/")
                video_filter = f"{ENCODING_CONFIG['VIDEO_SCALE']},drawtext=text='@FractumSeraph':fontfile='{font_arg}':fontcolor=white@0.2:fontsize=12:x=10:y=h-th-10"
                
                # Robust Audio Downmixing (Prevents crashes on corrupt streams claiming 40+ channels)
                audio_channels = 2 # Default assumption
                try:
                    for s in probe_data.get('streams', []):
                        if s['index'] == audio_index:
                            audio_channels = int(s.get('channels', 2))
                            break
                except: pass

                # AUDIO FILTER FIX (MONO + DIALOGUE FOCUS)
                audio_filter = "aresample=async=1" 
                
                if audio_channels > 2:
                    # 5.1 -> Mono: Mix Center (FC) strongly (50%) to ensure dialogue is clear
                    # c0 = 0.5*FC + 0.25*FL + 0.25*FR + 0.1*Surround
                    audio_filter = "pan=mono|c0=0.5*FC+0.25*FL+0.25*FR+0.1*BL+0.1*BR,aresample=async=1"
                elif audio_channels == 2:
                    # Stereo -> Mono: Standard Mix
                    audio_filter = "pan=mono|c0=0.5*c0+0.5*c1,aresample=async=1"
                else:
                    # Mono -> Mono: Passthrough
                    audio_filter = "aresample=async=1"

                cmd = [FFMPEG_CMD, '-y', '-i', local_src, '-map', '0:v:0', '-map', f'0:{audio_index}']
                for idx in subtitle_indices: cmd.extend(['-map', f'0:{idx}'])
                
                cmd.extend([
                    '-fps_mode', 'passthrough',
                    '-avoid_negative_ts', 'make_zero',
                    '-c:v', ENCODING_CONFIG["VIDEO_CODEC"], 
                    '-preset', ENCODING_CONFIG["VIDEO_PRESET"], 
                    '-crf', ENCODING_CONFIG["VIDEO_CRF"], 
                    '-pix_fmt', ENCODING_CONFIG["VIDEO_PIX_FMT"], 
                    '-vf', video_filter, 
                    '-c:a', ENCODING_CONFIG["AUDIO_CODEC"], 
                    '-b:a', ENCODING_CONFIG["AUDIO_BITRATE"], 
                    '-ac', ENCODING_CONFIG["AUDIO_CHANNELS"], # Enforce 1 channel (Mono)
                    '-af', audio_filter, 
                    '-c:s', ENCODING_CONFIG["SUBTITLE_CODEC"], 
                    '-progress', 'pipe:1', 
                    local_dst
                ])
                
                start_enc = time.time(); last_rep = 0
                log_buffer = []
                
                popen_kwargs = {}
                if platform.system() == 'Windows':
                    popen_kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    popen_kwargs['start_new_session'] = True

                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, encoding='utf-8', errors='replace', **popen_kwargs)
                
                with PROC_LOCK: ACTIVE_PROCS[worker_id] = proc
                
                raw_log_path = os.path.join(temp_dir, "encode.log")
                with open(raw_log_path, 'w', encoding='utf-8') as raw_log:
                    while True:
                        if PAUSE_REQUESTED:
                            time.sleep(0.2); continue

                        line = proc.stdout.readline()
                        if line: 
                            log_buffer.append(line); log_buffer = log_buffer[-50:]
                            raw_log.write(line)
                            raw_log.flush()
                            
                        if not line and proc.poll() is not None: break
                        
                        if "out_time=" in line and "N/A" not in line and total_sec > 0:
                            try:
                                time_str = line.split('=')[1].strip()
                                curr_sec = get_seconds(time_str)
                                pct = int((curr_sec/total_sec)*100)
                                
                                if single_mode: print_progress(worker_id, curr_sec, total_sec, prefix='Enc')
                                else: update_status(f"Enc {pct}%")
                                
                                if time.time() - last_rep > 10:
                                    post_status("processing", pct)
                                    last_rep = time.time()
                            except: pass
                
                with PROC_LOCK:
                    if worker_id in ACTIVE_PROCS: del ACTIVE_PROCS[worker_id]

                enc_time = time.time() - start_enc
                
                gz_log_path = os.path.join(temp_dir, "encode.log.gz")
                try:
                    with open(raw_log_path, 'rb') as f_in, gzip.open(gz_log_path, 'wb') as f_out:
                        shutil.copyfileobj(f_in, f_out)
                except Exception as e:
                    log(worker_id, f"Failed to compress log: {e}", "WARN")

                def upload_encode_log():
                    if os.path.exists(gz_log_path):
                        try:
                            with open(gz_log_path, 'rb') as lf:
                                requests.post(f"{manager_url}/upload_log", 
                                    files={'log_file': (f"{job_id.replace('/', '_')}.log.gz", lf)},
                                    data={'job_id': job_id, 'worker_id': worker_id},
                                    headers=get_auth_headers(), timeout=60)
                        except Exception as e:
                            log(worker_id, f"Failed to upload log: {e}", "WARN")

                if single_mode: print_progress(worker_id, total_sec, total_sec, prefix='Enc', suffix='OK')

                if proc.returncode == 0 and os.path.exists(local_dst):
                    final_size = os.path.getsize(local_dst) / 1024 / 1024
                    log(worker_id, f"Encode done ({enc_time:.0f}s, {final_size:.2f}MB). Uploading...")
                    post_status("uploading", 0)
                    
                    class ProgressFileReader:
                        def __init__(self, filename, callback):
                            self._f = open(filename, 'rb'); self._total = os.path.getsize(filename)
                            self._read = 0; self._callback = callback; self._last_time = 0
                        def __enter__(self): return self
                        def __exit__(self, exc_type, exc_val, exc_tb): self._f.close()
                        def read(self, size=-1):
                            if PAUSE_REQUESTED:
                                while PAUSE_REQUESTED: time.sleep(1)
                            data = self._f.read(size); self._read += len(data)
                            pct = int((self._read / self._total) * 100)
                            if single_mode: print_progress(worker_id, self._read, self._total, prefix='Up')
                            else: update_status(f"Up {pct}%")
                            if time.time() - self._last_time > 30:
                                self._callback(pct); self._last_time = time.time()
                            return data
                        def __getattr__(self, attr): return getattr(self._f, attr)

                    def upload_cb(pct): post_status("uploading", pct)

                    # RETRY LOOP for Uploads
                    upload_success = False
                    for up_attempt in range(3):
                        try:
                            with ProgressFileReader(local_dst, upload_cb) as f:
                                # Timeout = 300s (5 minutes) for socket silence
                                r = requests.post(f"{manager_url}/upload_result", 
                                              files={'file': (job_id, f)}, 
                                              data={'job_id': job_id, 'worker_id': worker_id, 'duration': total_min},
                                              headers=get_auth_headers(),
                                              timeout=300)
                                if r.status_code == 200:
                                    upload_success = True
                                    break
                                else:
                                    log(worker_id, f"Upload rejected by server: {r.status_code}", "WARN")
                        except Exception as e:
                            log(worker_id, f"Upload attempt {up_attempt+1} failed/timed out: {e}", "WARN")
                            time.sleep(10) # Wait 10s before retry

                    if upload_success:
                        if single_mode: print_progress(worker_id, 100, 100, prefix='Up', suffix='OK')
                        log(worker_id, "Job complete.")
                        upload_encode_log()
                    else:
                        err_msg = "Upload failed after 3 attempts"
                        log(worker_id, err_msg, "ERROR")
                        post_status("failed", error_msg=err_msg)
                        upload_encode_log()

                else:
                    err_msg = f"FFmpeg exited with code {proc.returncode}"
                    if SHUTDOWN_EVENT.is_set(): err_msg = "Aborted by user/update"
                    
                    log(worker_id, err_msg, "ERROR")
                    log(worker_id, "--- FFmpeg Output Dump ---", "ERROR")
                    for l in log_buffer: safe_print(f"    {l.strip()}")
                    log(worker_id, "--------------------------", "ERROR")
                    post_status("failed", error_msg=err_msg)
                    upload_encode_log()

                if os.path.exists(local_src): os.remove(local_src)
                if os.path.exists(local_dst): os.remove(local_dst)
                if os.path.exists(raw_log_path): os.remove(raw_log_path)
                if os.path.exists(gz_log_path): os.remove(gz_log_path)
            else:
                if single_mode:
                    with CONSOLE_LOCK:
                        sys.stdout.write(f"\033[2K\r[{datetime.now().strftime('%H:%M:%S')}] [{worker_id}] Idle. Waiting...")
                        sys.stdout.flush()
                time.sleep(10)
        except Exception as e:
            err_str = str(e)
            log(worker_id, f"Error: {err_str}", "CRITICAL")
            try: 
                if 'job_id' in locals(): post_status("failed", error_msg=err_str)
            except: pass
            time.sleep(10)

def run_worker(args):
    print("==================================================")
    print(" FRACTUM DISTRIBUTED WORKER")
    print("==================================================")

    config_file = "worker_config.json"
    saved_config = {}
    
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r') as f:
                content = f.read().strip()
                if content:
                    saved_config = json.loads(content)
                else:
                    print("[!] Config file is empty. Resetting.")
                    f.close()
                    os.remove(config_file)
                    
                if args.username == DEFAULT_USERNAME and 'username' in saved_config:
                    args.username = saved_config['username']
                if args.workername == DEFAULT_WORKERNAME and 'workername' in saved_config:
                    args.workername = saved_config['workername']
        except json.JSONDecodeError:
            print("[!] Config file corrupted. Resetting.")
            os.remove(config_file)
        except Exception as e:
            print(f"[!] Warning: Could not read config file: {e}")

    if sys.stdin.isatty():
        config_changed = False
        
        if args.username == DEFAULT_USERNAME:
            print("\n[*] First Time Setup detected.")
            print("    Please enter the USERNAME of the person running the program.")
            print("    (e.g., 'FractumSeraph', 'John Smith')")
            u_input = input(f"    Enter Username (Default: {DEFAULT_USERNAME}): ").strip()
            if u_input:
                args.username = u_input
                config_changed = True
        
        if args.workername == DEFAULT_WORKERNAME:
            w_default = f"Node-{int(time.time())}"
            print("\n    Please enter a name for THIS COMPUTER.")
            print("    (e.g., 'Fractums Laptop', 'Johns Gaming PC')")
            w_input = input(f"    Enter Worker Name (Default: {w_default}): ").strip()
            if w_input:
                args.workername = w_input
            else:
                args.workername = w_default
            config_changed = True

        if config_changed:
            try:
                with open(config_file, 'w') as f:
                    json.dump({"username": args.username, "workername": args.workername}, f, indent=4)
                print(f"[*] Configuration saved to {config_file}")
            except:
                print("[!] Failed to save configuration file.")

    check_ffmpeg()
    
    manager_url = (args.manager or DEFAULT_MANAGER_URL).rstrip('/')
    username = args.username or DEFAULT_USERNAME
    base_workername = args.workername or DEFAULT_WORKERNAME
    
    global WORKER_SECRET
    if args.secret: WORKER_SECRET = args.secret

    if WORKER_SECRET == "DefaultInsecureSecret":
        print("[*] INFO: Using default WORKER_SECRET. Compatible with public manager defaults.")
    
    if not verify_connection(manager_url): sys.exit(1)
    if check_version(manager_url): apply_update(manager_url)
    
    quota_tracker = None
    if args.daily_quota > 0:
        print(f"[*] Daily Quota Active: {args.daily_quota} GB")
        quota_tracker = QuotaTracker(args.daily_quota, base_workername)
        if quota_tracker.check_cap():
            print(f"[!] Quota already exceeded for today. Waiting until tomorrow.")

    num_jobs = args.jobs if args.jobs > 0 else 1
    if num_jobs > 32: num_jobs = 32
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    threads = []
    worker_ids = []
    single_mode = (num_jobs == 1)
    
    if args.series_id:
        print(f"[*] SERIES ID ACTIVE: Processing Series #{args.series_id}")
    
    for i in range(num_jobs):
        worker_id = f"{username}-{base_workername}-{i+1}"
        worker_ids.append(worker_id)
        temp_dir = f"./temp_encode_{base_workername}_{i+1}"

        t = threading.Thread(target=worker_task, args=(worker_id, manager_url, temp_dir, quota_tracker, single_mode, args.series_id))
        t.daemon = True
        t.start()
        threads.append(t)

    if not single_mode:
        monitor_t = threading.Thread(target=monitor_status_loop, args=(worker_ids,))
        monitor_t.daemon = True
        monitor_t.start()
        
    global PAUSE_REQUESTED
    while True:
        if not PAUSE_REQUESTED:
            all_dead = True
            for t in threads:
                if t.is_alive(): all_dead = False; break
            if all_dead: break
            if SHUTDOWN_EVENT.is_set() and not PAUSE_REQUESTED: break 
            time.sleep(0.5)
            continue
        
        MONITOR_PAUSED.set()            # stop monitor from clobbering stdout
        time.sleep(0.6)                 # give it a moment to stop writing
        sys.stdout.write('\n')          # move to a clean line
        sys.stdout.flush()
        toggle_processes(suspend=True)
        print("\n" + "="*40)
        print(" [!] WORKER PAUSED")
        print("="*40)
        print(" [C]ontinue  - Resume encoding")
        print(" [F]inish    - Finish active, then stop")
        print(" [S]top      - Abort immediately")
        
        while PAUSE_REQUESTED:
            try:
                choice = input("Select [c/f/s]: ").strip().lower()
                if choice == 'c':
                    print("[*] Resuming...")
                    MONITOR_PAUSED.clear()
                    PAUSE_REQUESTED = False
                    toggle_processes(suspend=False)
                elif choice == 'f':
                    print("[*] Draining jobs...")
                    PAUSE_REQUESTED = False
                    MONITOR_PAUSED.clear()
                    toggle_processes(suspend=False)
                    SHUTDOWN_EVENT.set()
                elif choice == 's':
                    print("[*] Aborting...")
                    toggle_processes(suspend=False)
                    kill_processes()
                    SHUTDOWN_EVENT.set()
                    PAUSE_REQUESTED = False
                    sys.exit(0)
            except (EOFError, KeyboardInterrupt):
                sys.stdout.write("\n")
                time.sleep(0.5)
                continue
            except Exception: time.sleep(0.5)
            
    if UPDATE_AVAILABLE: apply_update(manager_url)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manager", default=DEFAULT_MANAGER_URL)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--workername", default=DEFAULT_WORKERNAME)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--series-id", default=None, help="Process only specific Series ID")
    parser.add_argument("--secret", default=None, help="Manually set worker secret token")
    parser.add_argument("--daily-quota", type=float, default=0, help="Daily download limit in GB (0 = unlimited)")
    args = parser.parse_args()
    run_worker(args)
