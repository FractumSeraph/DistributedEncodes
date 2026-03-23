import argparse
import time
import requests
import subprocess
import os
import re
import shutil
import threading
import sys
import platform
import json
import signal
import zipfile
import tarfile
import gzip
import traceback
from datetime import datetime, timedelta

# Textual TUI (optional — install with: pip install textual)
try:
    from textual.app import App, ComposeResult
    from textual.widgets import Header, Footer, RichLog, DataTable, Button, Label
    from textual.screen import ModalScreen
    from textual.containers import Horizontal, Vertical
    from textual.binding import Binding
    HAS_TEXTUAL = True
except ImportError:
    HAS_TEXTUAL = False

# ==============================================================================
# CONFIGURATION
# ==============================================================================
DEFAULT_MANAGER_URL = "https://encode.fractumseraph.net/"
DEFAULT_USERNAME = "Anonymous"
DEFAULT_WORKERNAME = f"Node-{int(time.time())}"
WORKER_VERSION = "2.9.0" # Incremented for font fix

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
TUI_APP = None  # Set to WorkerApp instance when running in TUI mode

# Per-worker rich state, polled by the TUI every 0.5s
# {"file": str, "phase": str, "pct": int, "job_start": float, "jobs_done": int}
WORKER_DETAILS = {}
SESSION_STATS = {"jobs_done": 0, "bytes_uploaded": 0, "start": 0.0}
STATS_LOCK = threading.Lock()

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
        with self.lock:
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


# ==============================================================================
# TEXTUAL TUI
# ==============================================================================

if HAS_TEXTUAL:
    from rich.text import Text

    # ---------------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------------

    def _phase_style(phase: str) -> str:
        return {
            "Idle":      "dim",
            "Starting":  "dim",
            "Probe":     "cyan",
            "DL":        "bold cyan",
            "Encoding":  "bold yellow",
            "Uploading": "bold green",
            "Done":      "bright_green",
            "Failed":    "bold red",
            "Paused":    "bold orange3",
            "Quota":     "orange_red1",
            "Retrying":  "bold orange3",
        }.get(phase, "white")

    def _make_bar(pct: int, width: int = 16) -> str:
        pct = max(0, min(100, pct))
        filled = int(width * pct / 100)
        return "▓" * filled + "░" * (width - filled) + f" {pct:3d}%"

    def _fmt_elapsed(seconds: float) -> str:
        if seconds <= 0:
            return "-"
        s = int(seconds)
        h, r = divmod(s, 3600)
        m, sec = divmod(r, 60)
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

    # ---------------------------------------------------------------------------
    # Pause modal
    # ---------------------------------------------------------------------------

    class PauseModal(ModalScreen):
        """Modal dialog shown when the worker is paused."""

        BINDINGS = [
            Binding("c", "choose_continue", show=False),
            Binding("f", "choose_finish", show=False),
            Binding("s", "choose_stop", show=False),
            Binding("escape", "choose_continue", show=False),
        ]

        def compose(self) -> ComposeResult:
            with Vertical(id="pause-dialog"):
                yield Label("\u23f8  WORKER PAUSED", id="pause-title")
                yield Label("FFmpeg has been suspended.", id="pause-subtitle")
                with Horizontal(id="pause-buttons"):
                    yield Button("Continue  [C]", id="btn-continue", variant="success")
                    yield Button("Finish  [F]", id="btn-finish", variant="warning")
                    yield Button("Stop  [S]", id="btn-stop", variant="error")

        def action_choose_continue(self) -> None: self.dismiss("continue")
        def action_choose_finish(self) -> None: self.dismiss("finish")
        def action_choose_stop(self) -> None: self.dismiss("stop")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            choices = {"btn-continue": "continue", "btn-finish": "finish", "btn-stop": "stop"}
            if event.button.id in choices:
                self.dismiss(choices[event.button.id])

    # ---------------------------------------------------------------------------
    # Main application
    # ---------------------------------------------------------------------------

    class WorkerApp(App):
        """Fractum Distributed Worker \u2014 Textual TUI."""

        CSS = """
        Screen {
            background: $background;
        }
        #workers-table {
            height: auto;
            max-height: 16;
            margin: 1 1 0 1;
            border: solid $primary;
        }
        #stats-bar {
            height: 1;
            margin: 0 2;
            background: $surface-darken-1;
            color: $text-muted;
            padding: 0 1;
        }
        #log-panel {
            height: 1fr;
            margin: 0 1 1 1;
            border: solid $primary-darken-2;
        }
        PauseModal {
            align: center middle;
        }
        #pause-dialog {
            background: $surface;
            border: thick $warning;
            padding: 1 4;
            width: 56;
            height: auto;
        }
        #pause-title {
            text-align: center;
            color: $warning;
            text-style: bold;
            padding-bottom: 1;
        }
        #pause-subtitle {
            text-align: center;
            color: $text-disabled;
            padding-bottom: 1;
        }
        #pause-buttons {
            height: auto;
            align: center middle;
            padding-top: 1;
        }
        #btn-continue { margin: 0 1; }
        #btn-finish   { margin: 0 1; }
        #btn-stop     { margin: 0 1; }
        """

        BINDINGS = [
            Binding("p", "request_pause", "Pause"),
            Binding("q", "request_quit", "Quit"),
        ]

        def __init__(self, worker_ids: list, threads: list,
                     quota_tracker=None, manager_url: str = "", **kwargs):
            super().__init__(**kwargs)
            self._worker_ids = worker_ids
            self._threads = threads
            self._quota_tracker = quota_tracker
            self._manager_url = manager_url
            # Column keys assigned in on_mount
            self._col_file = self._col_phase = self._col_bar = None
            self._col_elapsed = self._col_done = None

        def compose(self) -> ComposeResult:
            yield Header()
            yield DataTable(id="workers-table", show_cursor=False)
            yield Label("", id="stats-bar")
            yield RichLog(id="log-panel", highlight=True, markup=False, max_lines=1000)
            yield Footer()

        def on_mount(self) -> None:
            table = self.query_one("#workers-table", DataTable)
            cols = table.add_columns("Worker", "Current File", "Phase", "Progress", "Elapsed", "Done")
            _cw, self._col_file, self._col_phase, self._col_bar, self._col_elapsed, self._col_done = cols
            for wid in self._worker_ids:
                table.add_row(wid, "-", "Starting", _make_bar(0), "-", "0", key=wid)
            self.title = "Fractum Distributed Worker"
            url_display = self._manager_url or "no manager"
            self.sub_title = f"v{WORKER_VERSION}  \u2502  {len(self._worker_ids)} worker(s)  \u2502  {url_display}"
            self.set_interval(0.5, self._tick)
            self.set_interval(1.5, self._check_all_done)

        # ------------------------------------------------------------------
        # Periodic refresh
        # ------------------------------------------------------------------

        def _tick(self) -> None:
            """Refresh the workers table and stats bar from shared state."""
            table = self.query_one("#workers-table", DataTable)
            now = time.time()

            for wid in self._worker_ids:
                d = WORKER_DETAILS.get(wid, {})
                phase     = d.get("phase", "Starting")
                pct       = d.get("pct", 0)
                filename  = d.get("file", "-")
                job_start = d.get("job_start", 0.0)
                jobs_done = d.get("jobs_done", 0)

                idle_phases = {"Idle", "Starting", "Quota", "Done", "Failed"}
                elapsed = (now - job_start) if job_start > 0 and phase not in idle_phases else 0.0

                style = _phase_style(phase)
                phase_text = Text(phase, style=style)
                bar_text   = Text(_make_bar(pct) if phase not in {"Idle", "Starting"} else " " * 20, style=style)

                max_fn = 32
                fn_display = filename if len(filename) <= max_fn else "\u2026" + filename[-(max_fn - 1):]

                try:
                    table.update_cell(wid, self._col_file,    fn_display,              update_width=False)
                    table.update_cell(wid, self._col_phase,   phase_text,              update_width=False)
                    table.update_cell(wid, self._col_bar,     bar_text,                update_width=False)
                    table.update_cell(wid, self._col_elapsed, _fmt_elapsed(elapsed),   update_width=False)
                    table.update_cell(wid, self._col_done,    str(jobs_done),          update_width=False)
                except Exception:
                    pass

            # Stats bar
            with STATS_LOCK:
                jd = SESSION_STATS.get("jobs_done", 0)
                bu = SESSION_STATS.get("bytes_uploaded", 0)
                st = SESSION_STATS.get("start", now)
            uptime_str = _fmt_elapsed(now - st)
            gb_str = f"{bu / 1024 ** 3:.2f} GB"
            quota_str = ""
            if self._quota_tracker:
                quota_str = f"  \u2502  Quota Remaining: {self._quota_tracker.get_remaining_str()}"
            stats = (
                f"  Jobs Completed: {jd}"
                f"  \u2502  Uploaded: {gb_str}"
                f"  \u2502  Uptime: {uptime_str}"
                f"{quota_str}"
            )
            try:
                self.query_one("#stats-bar", Label).update(stats)
            except Exception:
                pass

        def _check_all_done(self) -> None:
            if SHUTDOWN_EVENT.is_set() and all(not t.is_alive() for t in self._threads):
                self.exit()

        # ------------------------------------------------------------------
        # Thread-safe log write (called via call_from_thread)
        # ------------------------------------------------------------------

        def write_log(self, message: str) -> None:
            try:
                self.query_one("#log-panel", RichLog).write(message)
            except Exception:
                pass

        def update_worker_status(self, worker_id: str, status: str) -> None:
            # Status is now driven by WORKER_DETAILS polling in _tick; this is a no-op.
            pass

        # ------------------------------------------------------------------
        # Pause / quit actions
        # ------------------------------------------------------------------

        def action_request_pause(self) -> None:
            global PAUSE_REQUESTED
            if PAUSE_REQUESTED:
                return
            PAUSE_REQUESTED = True
            toggle_processes(suspend=True)
            self.push_screen(PauseModal(), self._handle_pause_result)

        def _handle_pause_result(self, choice: str) -> None:
            global PAUSE_REQUESTED
            if choice == "continue":
                PAUSE_REQUESTED = False
                toggle_processes(suspend=False)
                self.write_log("[*] Encoding resumed.")
            elif choice == "finish":
                PAUSE_REQUESTED = False
                toggle_processes(suspend=False)
                SHUTDOWN_EVENT.set()
                self.write_log("[*] Finishing active jobs, then stopping...")
            elif choice == "stop":
                toggle_processes(suspend=False)
                kill_processes()
                SHUTDOWN_EVENT.set()
                PAUSE_REQUESTED = False
                self.write_log("[*] Stopping immediately...")
                self.set_timer(1.5, self.exit)

        def action_request_quit(self) -> None:
            SHUTDOWN_EVENT.set()
            kill_processes()
            self.exit()


def get_auth_headers():
    headers = {'User-Agent': f'FractumWorker/{WORKER_VERSION}'}
    if WORKER_SECRET:
        headers['X-Worker-Token'] = WORKER_SECRET
    return headers

def get_term_width():
    try: return shutil.get_terminal_size((80, 20)).columns
    except: return 80

def safe_print(message):
    if TUI_APP is not None:
        try:
            TUI_APP.call_from_thread(TUI_APP.write_log, message)
            return
        except Exception:
            pass
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
        SHUTDOWN_EVENT.set()
        try: kill_processes()
        except: pass
        if TUI_APP is not None:
            try: TUI_APP.call_from_thread(TUI_APP.exit)
            except: pass
        else:
            sys.stdout.write('\n[!] Windows Shutdown Initiated...\n')
        sys.exit(0)
    else:
        if TUI_APP is not None:
            # In TUI mode, Ctrl+C triggers a clean shutdown
            SHUTDOWN_EVENT.set()
            try: kill_processes()
            except: pass
            try: TUI_APP.call_from_thread(TUI_APP.exit)
            except: pass
        elif not PAUSE_REQUESTED:
            PAUSE_REQUESTED = True
            try:
                sys.stdout.write('\n\n[!] PAUSE REQUESTED (Stopping gracefully...)\n')
                sys.stdout.flush()
            except: pass

def toggle_processes(suspend=True):
    if platform.system() == 'Windows':
        if suspend:
            safe_print("[!] WARNING: Process suspension is not supported on Windows. FFmpeg will continue running until the current job finishes.")
        return
    with PROC_LOCK:
        for wid, proc in ACTIVE_PROCS.items():
            if proc.poll() is None:
                try:
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
    percent = min(100, int(100 * current / float(total)))
    if TUI_APP is not None:
        # Update WORKER_DETAILS so the polling _tick() picks it up
        d = WORKER_DETAILS.get(worker_id)
        if d is not None:
            phase_map = {'DL': 'DL', 'Enc': 'Encoding', 'Up': 'Uploading'}
            d["phase"] = phase_map.get(prefix, prefix)
            d["pct"]   = percent
        return
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
        if TUI_APP is not None:
            time.sleep(1); continue  # TUI handles all status display
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


def worker_task(worker_id, manager_url, temp_dir, quota_tracker, single_mode=False, series_id=None, watermark=False):
    global UPDATE_AVAILABLE
    log(worker_id, "Thread active.")
    os.makedirs(temp_dir, exist_ok=True)
    
    def update_status(msg):
        with PROGRESS_LOCK:
            WORKER_PROGRESS[worker_id] = msg
        d = WORKER_DETAILS.get(worker_id)
        if d is not None:
            if msg.startswith("DL "):
                d["phase"] = "DL"
                try: d["pct"] = int(msg[3:].rstrip('%'))
                except: pass
            elif msg.startswith("Enc "):
                d["phase"] = "Encoding"
                try: d["pct"] = int(msg[4:].rstrip('%'))
                except: pass
            elif msg.startswith("Up "):
                d["phase"] = "Uploading"
                try: d["pct"] = int(msg[3:].rstrip('%'))
                except: pass
            elif msg == "Idle":
                d.update({"phase": "Idle", "pct": 0, "file": "-", "job_start": 0.0})
            elif msg == "Probing":
                d["phase"] = "Probe"
            elif msg == "Quota Limit":
                d.update({"phase": "Quota", "pct": 0, "file": "-", "job_start": 0.0})
            else:
                d["phase"] = msg
        
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

    def report_error(rpt_job_id, error_type, message, details=""):
        """Send a structured error report to the manager for admin visibility."""
        try:
            requests.post(
                f"{manager_url}/report_error",
                json={
                    "job_id": rpt_job_id,
                    "worker_id": worker_id,
                    "error_type": error_type,
                    "message": str(message)[:2048],
                    "details": str(details)[:32768],
                },
                headers=get_auth_headers(),
                timeout=10
            )
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
                d = WORKER_DETAILS.get(worker_id)
                if d is not None:
                    d.update({"file": job['filename'], "phase": "Probe", "pct": 0, "job_start": time.time()})

                local_dst = os.path.join(temp_dir, f"encoded{ENCODING_CONFIG['OUTPUT_EXT']}")

                # FFmpeg auth header string (format required by libavformat HTTP demuxer)
                ffmpeg_http_headers = f"X-Worker-Token: {WORKER_SECRET}\r\n"

                # Quota accounting: a single HEAD request gives us the source size instantly,
                # letting encoding start immediately rather than waiting for a full download.
                if quota_tracker:
                    try:
                        head_r = requests.head(dl_url, headers=get_auth_headers(), timeout=10)
                        src_size = int(head_r.headers.get('content-length', 0))
                        if quota_tracker.check_cap():
                            wait_sec = quota_tracker.get_wait_time()
                            update_status("Quota Limit")
                            log(worker_id, f"Daily Quota Reached. Reset in {wait_sec/3600:.1f} hours.")
                            while wait_sec > 0 and not SHUTDOWN_EVENT.is_set():
                                time.sleep(min(60, wait_sec))
                                wait_sec -= 60
                                if not quota_tracker.check_cap(): break
                            continue
                        if src_size > 0:
                            quota_tracker.add_usage(src_size)
                            quota_tracker.force_save()
                    except Exception as e:
                        log(worker_id, f"HEAD request for quota failed: {e}", "WARN")

                post_status("downloading", 0)

                update_status("Probing")
                total_sec = 0; total_min = 0; audio_index = 0; subtitle_indices = []
                for _probe_attempt in range(3):
                    try:
                        cmd_probe = [FFPROBE_CMD,
                            '-headers', ffmpeg_http_headers,
                            '-v', 'quiet', '-print_format', 'json', '-show_streams', '-show_format', dl_url]
                        res = subprocess.run(cmd_probe, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8', errors='replace', timeout=60)
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
                        break  # probe succeeded
                    except Exception as _probe_err:
                        if _probe_attempt < 2:
                            log(worker_id, f"Probe failed (attempt {_probe_attempt+1}/3): {_probe_err}. Retrying in {5*(_probe_attempt+1)}s...", "WARN")
                            time.sleep(5 * (_probe_attempt + 1))

                log(worker_id, f"Encoding ({total_min}m)...")
                post_status("processing", 0, total_min)

                # Font is only needed for the watermark
                local_font = os.path.join(temp_dir, "arial.ttf")
                if watermark:
                    src_font = os.path.join(_script_dir, "arial.ttf")
                    if not os.path.exists(src_font):
                        log(worker_id, "arial.ttf missing locally. Downloading from manager...")
                        try:
                            font_req = requests.get(f"{manager_url}/dl/font", headers=get_auth_headers(), timeout=30)
                            if font_req.status_code == 200:
                                with open(src_font, 'wb') as f:
                                    f.write(font_req.content)
                                log(worker_id, "Font downloaded successfully.")
                        except Exception as e:
                            log(worker_id, f"Font download failed: {e}", "WARN")
                    try:
                        if os.path.exists(src_font):
                            shutil.copy(src_font, local_font)
                    except: pass

                # Construct video filter conditionally based on watermark flag and font availability
                if watermark and os.path.exists(local_font):
                    font_arg = local_font.replace("\\", "/")
                    video_filter = f"{ENCODING_CONFIG['VIDEO_SCALE']},drawtext=text='@FractumSeraph':fontfile='{font_arg}':fontcolor=white@0.2:fontsize=12:x=10:y=h-th-10"
                else:
                    video_filter = ENCODING_CONFIG['VIDEO_SCALE']
                    if watermark:
                        log(worker_id, "Warning: arial.ttf could not be sourced. Skipping watermark.", "WARN")
                
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

                # [ADDED] Dynamic CRF Adjustment for Live Action Profile
                base_crf = int(ENCODING_CONFIG["VIDEO_CRF"])
                profile = job.get('content_profile', 'standard')
                
                if profile == 'live_action':
                    target_crf = base_crf - 6
                    log(worker_id, f"Live Action profile detected! Allocating 2x bitrate (CRF: {target_crf})")
                else:
                    target_crf = base_crf

                cmd = [FFMPEG_CMD,
                       '-headers', ffmpeg_http_headers,
                       '-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '60',
                       '-reconnect_on_network_error', '1',
                       '-y', '-i', dl_url, '-map', '0:v:0', '-map', f'0:{audio_index}']
                for idx in subtitle_indices: cmd.extend(['-map', f'0:{idx}'])
                
                cmd.extend([
                    '-fps_mode', 'passthrough',
                    '-avoid_negative_ts', 'make_zero',
                    '-c:v', ENCODING_CONFIG["VIDEO_CODEC"], 
                    '-preset', ENCODING_CONFIG["VIDEO_PRESET"], 
                    '-crf', str(target_crf),   # [CHANGED] Uses dynamically calculated CRF
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
                
                proc = None; enc_time = 0
                for _enc_attempt in range(3):
                    if SHUTDOWN_EVENT.is_set(): break
                    if _enc_attempt > 0:
                        _retry_delay = 30
                        log(worker_id, f"Encode failed (attempt {_enc_attempt}/3, rc={proc.returncode}). Retrying in {_retry_delay}s...", "WARN")
                        update_status("Retrying")
                        for _ in range(_retry_delay):
                            if SHUTDOWN_EVENT.is_set(): break
                            time.sleep(1)
                        if SHUTDOWN_EVENT.is_set(): break
                        log(worker_id, f"Re-encoding ({total_min}m)...")
                        post_status("processing", 0, total_min)

                    start_enc = time.time(); last_rep = 0; last_enc_pct = 0; last_hb = 0
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
                                _now = time.time()
                                if _now - last_hb > 30:
                                    post_status("paused", last_enc_pct)
                                    last_hb = _now
                                time.sleep(0.2)
                                continue

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
                                    pct = min(100, int((curr_sec/total_sec)*100))
                                    last_enc_pct = pct

                                    if single_mode: print_progress(worker_id, curr_sec, total_sec, prefix='Enc')
                                    else: update_status(f"Enc {pct}%")

                                    if time.time() - last_rep > 10:
                                        post_status("processing", pct)
                                        last_rep = time.time()
                                        last_hb = last_rep
                                except: pass

                    with PROC_LOCK:
                        if worker_id in ACTIVE_PROCS: del ACTIVE_PROCS[worker_id]

                    enc_time = time.time() - start_enc
                    if proc.returncode == 0 or SHUTDOWN_EVENT.is_set(): break
                
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

                if proc and proc.returncode == 0 and os.path.exists(local_dst):
                    final_size_bytes = os.path.getsize(local_dst)
                    final_size = final_size_bytes / 1024 / 1024
                    log(worker_id, f"Encode done ({enc_time:.0f}s, {final_size:.2f}MB). Uploading...")
                    post_status("uploading", 0)
                    
                    class ProgressFileReader:
                        def __init__(self, filename, callback):
                            self._f = open(filename, 'rb'); self._total = os.path.getsize(filename)
                            self._read = 0; self._callback = callback; self._last_time = 0
                            self._last_pct = 0
                        def __enter__(self): return self
                        def __exit__(self, exc_type, exc_val, exc_tb): self._f.close()
                        def read(self, size=-1):
                            if PAUSE_REQUESTED:
                                _up_hb = time.time()
                                while PAUSE_REQUESTED and not SHUTDOWN_EVENT.is_set():
                                    if time.time() - _up_hb > 30:
                                        self._callback(self._last_pct)
                                        _up_hb = time.time()
                                    time.sleep(1)
                            data = self._f.read(size); self._read += len(data)
                            pct = int((self._read / self._total) * 100)
                            self._last_pct = pct
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
                        d = WORKER_DETAILS.get(worker_id)
                        if d is not None:
                            d.update({"phase": "Done", "pct": 100})
                            d["jobs_done"] += 1
                        with STATS_LOCK:
                            SESSION_STATS["jobs_done"] += 1
                            SESSION_STATS["bytes_uploaded"] += final_size_bytes
                        upload_encode_log()
                    else:
                        d = WORKER_DETAILS.get(worker_id)
                        if d is not None: d["phase"] = "Failed"
                        err_msg = "Upload failed after 3 attempts"
                        log(worker_id, err_msg, "ERROR")
                        post_status("failed", error_msg=err_msg)
                        upload_encode_log()

                else:
                    rc = proc.returncode if proc else -1
                    err_msg = f"FFmpeg exited with code {rc}"
                    if SHUTDOWN_EVENT.is_set(): err_msg = "Aborted by user/update"

                    log(worker_id, err_msg, "ERROR")
                    log(worker_id, "--- FFmpeg Output Dump ---", "ERROR")
                    for l in log_buffer: safe_print(f"    {l.strip()}")
                    log(worker_id, "--------------------------", "ERROR")
                    post_status("failed", error_msg=err_msg)
                    if not SHUTDOWN_EVENT.is_set():
                        report_error(job_id, "encode_failure", err_msg,
                                     "\n".join(l.rstrip() for l in log_buffer))
                    upload_encode_log()

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
            err_tb = traceback.format_exc()
            log(worker_id, f"Error: {err_str}", "CRITICAL")
            try:
                if 'job_id' in locals():
                    post_status("failed", error_msg=err_str)
                    report_error(job_id, "exception", err_str, err_tb)
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

    # Initialize per-worker detail state and session stats
    global WORKER_DETAILS, SESSION_STATS
    WORKER_DETAILS = {}
    with STATS_LOCK:
        SESSION_STATS["jobs_done"] = 0
        SESSION_STATS["bytes_uploaded"] = 0
        SESSION_STATS["start"] = time.time()

    if args.series_id:
        print(f"[*] SERIES ID ACTIVE: Processing Series #{args.series_id}")

    for i in range(num_jobs):
        worker_id = f"{username}-{base_workername}-{i+1}"
        worker_ids.append(worker_id)
        WORKER_DETAILS[worker_id] = {"file": "-", "phase": "Starting", "pct": 0, "job_start": 0.0, "jobs_done": 0}
        temp_dir = f"./temp_encode_{base_workername}_{i+1}"

        t = threading.Thread(target=worker_task, args=(worker_id, manager_url, temp_dir, quota_tracker, single_mode, args.series_id, args.watermark))
        t.daemon = True
        t.start()
        threads.append(t)

    use_tui = HAS_TEXTUAL and not getattr(args, 'no_tui', False)

    if not single_mode and not use_tui:
        monitor_t = threading.Thread(target=monitor_status_loop, args=(worker_ids,))
        monitor_t.daemon = True
        monitor_t.start()

    global PAUSE_REQUESTED, TUI_APP
    if use_tui:
        app = WorkerApp(
            worker_ids=worker_ids,
            threads=threads,
            quota_tracker=quota_tracker,
            manager_url=manager_url,
        )
        TUI_APP = app
        app.run()
        TUI_APP = None
    else:
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
    parser.add_argument("--watermark", action="store_true", default=False, help="Burn the @FractumSeraph watermark into encoded video")
    parser.add_argument("--no-tui", action="store_true", default=False, help="Disable Textual TUI and use plain terminal output")
    args = parser.parse_args()
    run_worker(args)
