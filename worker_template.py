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
import ctypes
import hashlib
from datetime import datetime, timedelta

# Textual TUI (optional — install with: pip install textual)
# Minimum required version: 0.20 (RichLog, ModalScreen, Binding, DataTable etc.)
_TEXTUAL_MIN_VERSION = (0, 20)

_VENV_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.worker_venv')

def _bootstrap_textual():
    """Auto-install/upgrade textual if missing or too old, then re-exec.

    Strategy:
      1. Try plain pip install into the current interpreter (works on most systems).
      2. If pip rejects it (externally-managed-environment / PEP 668), create a
         local venv at _VENV_DIR and install there, then re-exec with that Python.
    """
    try:
        import textual as _t
        if tuple(int(x) for x in _t.__version__.split('.')[:2]) >= _TEXTUAL_MIN_VERSION:
            return  # already fine
        reason = f"textual {_t.__version__} is too old (>= {'.'.join(str(x) for x in _TEXTUAL_MIN_VERSION)} required)"
    except ImportError:
        reason = "textual not found"
    print(f"[*] {reason} — attempting automatic install...")

    # Attempt 1: plain pip upgrade into current interpreter
    result = subprocess.run(
        [sys.executable, '-m', 'pip', 'install', '--upgrade', 'textual'],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print("[*] textual installed — restarting...")
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # Attempt 2: create/reuse a local venv and install there
    print("[*] pip install failed (system-managed env?) — trying local venv...")
    try:
        import venv as _venv
        if not os.path.isdir(_VENV_DIR):
            print(f"[*] Creating venv at {_VENV_DIR} ...")
            _venv.create(_VENV_DIR, with_pip=True)
        venv_python = (
            os.path.join(_VENV_DIR, 'Scripts', 'python.exe')  # Windows
            if sys.platform == 'win32' else
            os.path.join(_VENV_DIR, 'bin', 'python')
        )
        result2 = subprocess.run(
            [venv_python, '-m', 'pip', 'install', '--upgrade', 'textual'],
            capture_output=True, text=True,
        )
        if result2.returncode == 0:
            print(f"[*] textual installed in venv — restarting under {venv_python} ...")
            os.execv(venv_python, [venv_python] + sys.argv)
        else:
            print(f"[!] venv pip install failed:\n{result2.stderr.strip()}")
    except Exception as _ve:
        print(f"[!] venv setup failed: {_ve}")

    print("[!] Could not install textual automatically — continuing without TUI.")

_bootstrap_textual()

try:
    import textual as _textual_mod
    _tv = tuple(int(x) for x in _textual_mod.__version__.split('.')[:2])
    if _tv < _TEXTUAL_MIN_VERSION:
        raise ImportError(
            f"textual {_textual_mod.__version__} is too old; "
            f">= {'.'.join(str(x) for x in _TEXTUAL_MIN_VERSION)} required"
        )
    from textual.app import App, ComposeResult
    from textual.widgets import Header, Footer, RichLog, DataTable, Button, Label
    from textual.screen import ModalScreen
    from textual.containers import Horizontal, Vertical
    from textual.binding import Binding
    HAS_TEXTUAL = True
    _TEXTUAL_UNAVAIL_REASON = ""
except ImportError as _e:
    HAS_TEXTUAL = False
    _TEXTUAL_UNAVAIL_REASON = str(_e)

# ==============================================================================
# CONFIGURATION
# ==============================================================================
DEFAULT_MANAGER_URL = "https://encode.fractumseraph.net/"
DEFAULT_USERNAME = "Anonymous"
DEFAULT_WORKERNAME = f"Node-{int(time.time())}"
WORKER_VERSION = "3.0.4" # Incremented for fixing regex

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

        BINDINGS = [  # type: ignore[assignment]
            Binding("c", "choose_continue", show=False),
            Binding("f", "choose_finish", show=False),
            Binding("s", "choose_stop", show=False),
            Binding("escape", "choose_continue", show=False),
        ]

        def compose(self) -> ComposeResult:
            on_windows = platform.system() == 'Windows'
            subtitle = "FFmpeg has been suspended." if not on_windows else "FFmpeg has been suspended (Windows)."
            with Vertical(id="pause-dialog"):
                yield Label("\u23f8  WORKER PAUSED", id="pause-title")
                yield Label(subtitle, id="pause-subtitle")
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

        BINDINGS = [  # type: ignore[assignment]
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
            self._col_elapsed = self._col_done = self._col_eta = None

        def compose(self) -> ComposeResult:
            yield Header()
            yield DataTable(id="workers-table", show_cursor=False)
            yield Label("", id="stats-bar")
            yield RichLog(id="log-panel", highlight=True, markup=False, max_lines=1000)
            yield Footer()

        def on_mount(self) -> None:
            table = self.query_one("#workers-table", DataTable)
            cols = table.add_columns("Worker", "Current File", "Phase", "Progress", "Elapsed", "Done", "ETA")
            _cw, self._col_file, self._col_phase, self._col_bar, self._col_elapsed, self._col_done, self._col_eta = cols
            for wid in self._worker_ids:
                table.add_row(wid, "-", "Starting", _make_bar(0), "-", "0", "-", key=wid)
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

                # ETA: estimate remaining time from elapsed and progress
                if pct > 5 and elapsed > 0 and phase == "Encoding":
                    eta_sec = elapsed * (100 - pct) / pct
                else:
                    eta_sec = 0.0

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
                    table.update_cell(wid, self._col_eta,     _fmt_elapsed(eta_sec),   update_width=False)
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
            # Push the modal immediately so it always appears, regardless of how long
            # OS-level process suspension takes.  toggle_processes acquires PROC_LOCK
            # and makes blocking kernel calls — running it on the Textual event loop
            # (main thread) can block long enough that push_screen never executes, or
            # call_from_thread inside safe_print can raise when called from the main
            # thread.  Offloading to a daemon thread fixes both issues.
            self.push_screen(PauseModal(), self._handle_pause_result)
            threading.Thread(target=lambda: toggle_processes(suspend=True),
                             daemon=True).start()

        async def _handle_pause_result(self, choice: str | None) -> None:
            global PAUSE_REQUESTED
            if choice == "continue":
                PAUSE_REQUESTED = False
                threading.Thread(target=lambda: toggle_processes(suspend=False),
                                 daemon=True).start()
                self.write_log("[*] Encoding resumed.")
            elif choice == "finish":
                PAUSE_REQUESTED = False
                threading.Thread(target=lambda: toggle_processes(suspend=False),
                                 daemon=True).start()
                SHUTDOWN_EVENT.set()
                self.write_log("[*] Finishing active jobs, then stopping...")
            elif choice == "stop":
                threading.Thread(target=lambda: toggle_processes(suspend=False),
                                 daemon=True).start()
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
        # Windows has no SIGSTOP/SIGCONT, but NtSuspendProcess / NtResumeProcess
        # (ntdll.dll) achieve the identical effect: all threads in the target
        # process are frozen atomically.  ctypes is stdlib — no extra deps.
        try:
            ntdll    = ctypes.windll.ntdll
            kernel32 = ctypes.windll.kernel32
            PROCESS_ALL_ACCESS = 0x1F0FFF
            with PROC_LOCK:
                for wid, proc in ACTIVE_PROCS.items():
                    if proc.poll() is None:
                        try:
                            h = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, proc.pid)
                            if h:
                                if suspend:
                                    ntdll.NtSuspendProcess(h)
                                else:
                                    ntdll.NtResumeProcess(h)
                                kernel32.CloseHandle(h)
                        except Exception as _e:
                            safe_print(f"[!] Could not {'suspend' if suspend else 'resume'} PID {proc.pid}: {_e}")
        except Exception as _e:
            if suspend:
                safe_print(f"[!] WARNING: Windows process suspension unavailable: {_e}")
        return
    with PROC_LOCK:
        for wid, proc in ACTIVE_PROCS.items():
            if proc.poll() is None:
                try:
                    sig = signal.SIGSTOP if suspend else signal.SIGCONT  # type: ignore[attr-defined]
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
    tmp_path = None
    try:
        url = f"{manager_url}/dl/worker"
        r = requests.get(url, headers=get_auth_headers(), timeout=30)
        if r.status_code == 200:
            target_path = os.path.abspath(sys.argv[0])
            tmp_path = target_path + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                f.write(r.text)
            os.replace(tmp_path, target_path)
            safe_print("[*] Restarting worker...")
            os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        safe_print(f"[!] Failed to apply update: {e}")
        if tmp_path:
            try:
                if os.path.exists(tmp_path): os.remove(tmp_path)
            except: pass

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
# RESUME / PARTIAL ENCODE HELPERS
# ==============================================================================

_RESUME_CHK_PREFIX = "resume_chk_"

def _probe_local_duration(file_path):
    """Return duration in seconds of a local media file, or 0.0 on failure."""
    try:
        res = subprocess.run(
            [FFPROBE_CMD, '-v', 'quiet', '-print_format', 'json', '-show_format', file_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding='utf-8', errors='replace', timeout=60)
        dur = json.loads(res.stdout).get('format', {}).get('duration')
        return float(dur) if dur else 0.0
    except Exception:
        return 0.0

def _save_resume_checkpoint(path, payload):
    """Atomically write a resume checkpoint JSON."""
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp): os.remove(tmp)
        except Exception:
            pass

def _load_resume_checkpoints(temp_dir):
    """Return list of (path, data) for all resume checkpoints found in temp_dir."""
    results = []
    try:
        for fname in os.listdir(temp_dir):
            if fname.startswith(_RESUME_CHK_PREFIX) and fname.endswith('.json'):
                fpath = os.path.join(temp_dir, fname)
                try:
                    with open(fpath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    results.append((fpath, data))
                except Exception:
                    pass
    except Exception:
        pass
    return results

# Number of bytes hashed for source-integrity check (must match server constant)
_HASH_BYTES = 4 * 1024 * 1024

def _compute_source_hash(dl_url, is_local):
    """Return MD5 hex digest of the first _HASH_BYTES of the source file.
    For local files the path is read directly; for remote URLs an HTTP Range
    request is used so we never need to download the whole file first.
    Returns None on any error so callers can skip the check gracefully."""
    h = hashlib.md5()
    try:
        if is_local:
            with open(dl_url, 'rb') as f:
                remaining = _HASH_BYTES
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    h.update(chunk)
                    remaining -= len(chunk)
        else:
            headers = dict(get_auth_headers())
            headers['Range'] = f'bytes=0-{_HASH_BYTES - 1}'
            r = requests.get(dl_url, headers=headers, stream=True, timeout=30)
            # Accept both 206 Partial Content and 200 OK (some servers ignore Range)
            if r.status_code not in (200, 206):
                return None
            read = 0
            for chunk in r.iter_content(65536):
                if read >= _HASH_BYTES:
                    break
                if read + len(chunk) > _HASH_BYTES:
                    chunk = chunk[:_HASH_BYTES - read]
                h.update(chunk)
                read += len(chunk)
    except Exception:
        return None
    return h.hexdigest()

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


def worker_task(worker_id, manager_url, temp_dir, quota_tracker, single_mode=False, series_id=None, watermark=False, max_size_mb=0, local_source_dir=None):
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

            # =========================================================
            # RESUME: Finish any partial encodes from a previous run
            # =========================================================
            for _r_chk_path, _r_chk in _load_resume_checkpoints(temp_dir):
                if SHUTDOWN_EVENT.is_set(): break
                _r_partial = _r_chk.get('partial_path', '')
                _r_job_id  = _r_chk.get('job_id', '')
                if not _r_job_id or not os.path.exists(_r_partial):
                    log(worker_id, f"Stale resume checkpoint ({_r_job_id}). Removing.")
                    try: os.remove(_r_chk_path)
                    except: pass
                    continue

                # ----------------------------------------------------------
                # Check with the server: is this job still ours to resume?
                # ----------------------------------------------------------
                _r_server_status = None
                _r_server_owner  = None
                _r_check_failed  = False
                try:
                    _r_chk_resp = requests.get(
                        f"{manager_url}/job_status",
                        params={"job_id": _r_job_id},
                        headers=get_auth_headers(), timeout=10)
                    if _r_chk_resp.status_code == 200:
                        _r_chk_data    = _r_chk_resp.json()
                        _r_server_status = _r_chk_data.get("job_status")
                        _r_server_owner  = _r_chk_data.get("worker_id")
                    elif _r_chk_resp.status_code == 404:
                        # Job no longer exists on the server; discard checkpoint
                        log(worker_id, f"Checkpoint job {_r_job_id} not found on server. Discarding.")
                        for _fp in [_r_chk_path, _r_partial]:
                            try:
                                if os.path.exists(_fp): os.remove(_fp)
                            except: pass
                        continue
                    else:
                        _r_check_failed = True
                except Exception as _r_ce:
                    log(worker_id, f"Could not reach server to validate checkpoint ({_r_job_id}): {_r_ce}. Skipping resume this cycle.", "WARN")
                    _r_check_failed = True

                if _r_check_failed:
                    # Can't confirm ownership — skip this iteration, try again next cycle
                    continue

                if _r_server_status == "completed":
                    log(worker_id, f"Job {_r_job_id} already completed on server. Discarding checkpoint.")
                    for _fp in [_r_chk_path, _r_partial]:
                        try:
                            if os.path.exists(_fp): os.remove(_fp)
                        except: pass
                    continue

                if _r_server_status == "permanently_failed":
                    log(worker_id, f"Job {_r_job_id} is permanently failed. Discarding checkpoint.")
                    for _fp in [_r_chk_path, _r_partial]:
                        try:
                            if os.path.exists(_fp): os.remove(_fp)
                        except: pass
                    continue

                if _r_server_status == "processing" and _r_server_owner and _r_server_owner != worker_id:
                    # A different worker has been assigned this job — leave it alone
                    log(worker_id, f"Job {_r_job_id} is now owned by {_r_server_owner}. Abandoning checkpoint.")
                    for _fp in [_r_chk_path, _r_partial]:
                        try:
                            if os.path.exists(_fp): os.remove(_fp)
                        except: pass
                    continue

                # Job is queued (timed out) or still ours — reclaim it before resuming
                if _r_server_status in ("queued", "failed"):
                    try:
                        _r_reclaim = requests.post(
                            f"{manager_url}/reclaim_job",
                            json={"job_id": _r_job_id, "worker_id": worker_id,
                                  "version": WORKER_VERSION},
                            headers=get_auth_headers(), timeout=10)
                        if _r_reclaim.status_code == 409:
                            # Race: another worker claimed it first
                            log(worker_id, f"Job {_r_job_id} reclaim failed (already taken). Abandoning checkpoint.")
                            for _fp in [_r_chk_path, _r_partial]:
                                try:
                                    if os.path.exists(_fp): os.remove(_fp)
                                except: pass
                            continue
                        if _r_reclaim.status_code != 200:
                            log(worker_id, f"Job {_r_job_id} reclaim returned {_r_reclaim.status_code}. Skipping.", "WARN")
                            continue
                        log(worker_id, f"Reclaimed job {_r_job_id} from the queue.")
                    except Exception as _r_re:
                        log(worker_id, f"Reclaim request failed for {_r_job_id}: {_r_re}. Skipping.", "WARN")
                        continue
                # ----------------------------------------------------------

                _r_partial_dur = _probe_local_duration(_r_partial)
                if _r_partial_dur < 5.0:
                    log(worker_id, f"Partial encode for {_r_job_id} unreadable/too short ({_r_partial_dur:.1f}s). Discarding.")
                    for _fp in [_r_chk_path, _r_partial]:
                        try:
                            if os.path.exists(_fp): os.remove(_fp)
                        except: pass
                    continue
                _r_total_sec = _r_chk.get('total_sec', 0)
                _r_total_min = _r_chk.get('total_min', 0)
                _r_pct = int(_r_partial_dur / _r_total_sec * 100) if _r_total_sec > 0 else 0
                log(worker_id, f"Resuming job {_r_job_id}: was {_r_pct}% done ({_r_partial_dur:.1f}s/{_r_total_sec:.1f}s).")
                job_id = _r_job_id
                d = WORKER_DETAILS.get(worker_id)
                if d is not None:
                    d.update({"file": _r_job_id, "phase": "Encoding", "pct": _r_pct, "job_start": time.time()})
                post_status("processing", _r_pct, _r_total_min)

                _r_p1     = _r_partial + '.p1.mp4'
                _r_p2     = _r_partial + '.p2.mp4'
                _r_clist  = _r_partial + '.concat.txt'
                _r_merged = _r_partial + '.merged.mp4'
                _r_tmps   = [_r_p1, _r_p2, _r_clist, _r_merged]
                _resume_ok = False
                try:
                    # Trim part1 to a clean keyframe boundary using stream-copy (no re-encode)
                    _r_trim_pt = max(0.0, _r_partial_dur - 5.0)
                    _r_trim_rc = subprocess.run(
                        [FFMPEG_CMD, '-y', '-i', _r_partial,
                         '-t', str(_r_trim_pt), '-c', 'copy', _r_p1],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        encoding='utf-8', errors='replace', timeout=300)
                    if _r_trim_rc.returncode != 0 or not os.path.exists(_r_p1):
                        raise RuntimeError(f"Part-1 trim failed (rc={_r_trim_rc.returncode})")
                    _r_p1_dur = _probe_local_duration(_r_p1)
                    if _r_p1_dur <= 0:
                        raise RuntimeError("Trimmed part-1 not readable")

                    # Encode the remainder starting from _r_p1_dur
                    _r_hdrs  = f"X-Worker-Token: {WORKER_SECRET}\r\n"
                    _r_dl    = _r_chk.get('dl_url', '')
                    _r_ai    = _r_chk.get('audio_index', 0)
                    _r_si    = _r_chk.get('subtitle_indices', [])
                    _r_vf    = _r_chk.get('video_filter', ENCODING_CONFIG['VIDEO_SCALE'])
                    _r_af    = _r_chk.get('audio_filter', 'aresample=async=1')
                    _r_crf   = _r_chk.get('target_crf', int(ENCODING_CONFIG['VIDEO_CRF']))
                    log(worker_id, f"Encoding remainder from {_r_p1_dur:.1f}s...")
                    _r_rem_cmd = (
                        [FFMPEG_CMD,
                         '-headers', _r_hdrs,
                         '-reconnect', '1', '-reconnect_streamed', '1',
                         '-reconnect_delay_max', '60', '-reconnect_on_network_error', '1',
                         '-ss', str(_r_p1_dur),
                         '-y', '-i', _r_dl,
                         '-map', '0:v:0', '-map', f'0:{_r_ai}']
                        + [x for idx in _r_si for x in ['-map', f'0:{idx}']]
                        + ['-fps_mode', 'passthrough', '-avoid_negative_ts', 'make_zero',
                           '-c:v', ENCODING_CONFIG["VIDEO_CODEC"],
                           '-preset', ENCODING_CONFIG["VIDEO_PRESET"],
                           '-crf', str(_r_crf),
                           '-pix_fmt', ENCODING_CONFIG["VIDEO_PIX_FMT"],
                           '-vf', _r_vf,
                           '-c:a', ENCODING_CONFIG["AUDIO_CODEC"],
                           '-b:a', ENCODING_CONFIG["AUDIO_BITRATE"],
                           '-ac', ENCODING_CONFIG["AUDIO_CHANNELS"],
                           '-af', _r_af,
                           '-c:s', ENCODING_CONFIG["SUBTITLE_CODEC"],
                           '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
                           '-progress', 'pipe:1', _r_p2])

                    _r_popen_kw = {}
                    if platform.system() == 'Windows':
                        _r_popen_kw['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
                    else:
                        _r_popen_kw['start_new_session'] = True
                    _r_rem = subprocess.Popen(
                        _r_rem_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL, encoding='utf-8', errors='replace', **_r_popen_kw)
                    assert _r_rem.stdout is not None
                    with PROC_LOCK: ACTIVE_PROCS[worker_id] = _r_rem
                    _r_last_rep = 0
                    while True:
                        if SHUTDOWN_EVENT.is_set(): _r_rem.terminate(); break
                        _r_ln = _r_rem.stdout.readline()
                        if not _r_ln and _r_rem.poll() is not None: break
                        if "out_time=" in _r_ln and "N/A" not in _r_ln and _r_total_sec > 0:
                            try:
                                _r_cur = get_seconds(_r_ln.split('=')[1].strip())
                                _r_ov  = min(100, int((_r_p1_dur + _r_cur) / _r_total_sec * 100))
                                update_status(f"Enc {_r_ov}%")
                                if time.time() - _r_last_rep > 10:
                                    post_status("processing", _r_ov, _r_total_min)
                                    _r_last_rep = time.time()
                            except: pass
                    with PROC_LOCK:
                        if worker_id in ACTIVE_PROCS: del ACTIVE_PROCS[worker_id]
                    if SHUTDOWN_EVENT.is_set():
                        break  # keep partial + checkpoint for the next run
                    if _r_rem.returncode != 0 or not os.path.exists(_r_p2):
                        raise RuntimeError(f"Remainder encode failed (rc={_r_rem.returncode})")

                    # Concatenate part1 + part2 into the final merged file
                    log(worker_id, "Concatenating resume parts...")
                    with open(_r_clist, 'w', encoding='utf-8') as _cf:
                        _cf.write(f"file '{_r_p1.replace(os.sep, '/')}'\n")
                        _cf.write(f"file '{_r_p2.replace(os.sep, '/')}'\n")
                    _r_cat_rc = subprocess.run(
                        [FFMPEG_CMD, '-y', '-f', 'concat', '-safe', '0',
                         '-i', _r_clist, '-c', 'copy', '-movflags', '+faststart', _r_merged],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        encoding='utf-8', errors='replace', timeout=600)
                    if _r_cat_rc.returncode != 0 or not os.path.exists(_r_merged):
                        raise RuntimeError(f"Concat failed (rc={_r_cat_rc.returncode})")
                    os.replace(_r_merged, _r_partial)
                    _r_tmps.remove(_r_merged)  # already moved, skip final-cleanup
                    _resume_ok = True

                except Exception as _r_err:
                    log(worker_id, f"Resume failed for {_r_job_id}: {_r_err}. Discarding.", "ERROR")
                finally:
                    for _fp in _r_tmps:
                        try:
                            if os.path.exists(_fp): os.remove(_fp)
                        except: pass

                if not _resume_ok:
                    for _fp in [_r_chk_path, _r_partial]:
                        try:
                            if os.path.exists(_fp): os.remove(_fp)
                        except: pass
                    continue

                # Upload the resumed, fully merged file
                _r_file_bytes = os.path.getsize(_r_partial)
                log(worker_id, f"Resume complete ({_r_file_bytes/1024/1024:.2f} MB). Uploading...")
                post_status("uploading", 0)
                update_status("Up 0%")
                _r_up_ok = False
                for _r_up_att in range(3):
                    if SHUTDOWN_EVENT.is_set(): break
                    try:
                        with open(_r_partial, 'rb') as _r_f:
                            _r_resp = requests.post(
                                f"{manager_url}/upload_result",
                                files={'file': (_r_job_id, _r_f)},
                                data={'job_id': _r_job_id, 'worker_id': worker_id,
                                      'duration': _r_total_min},
                                headers=get_auth_headers(), timeout=300)
                            if _r_resp.status_code == 200:
                                _r_up_ok = True; break
                            else:
                                log(worker_id, f"Resume upload rejected: {_r_resp.status_code}", "WARN")
                    except Exception as _r_ue:
                        log(worker_id, f"Resume upload attempt {_r_up_att+1} failed: {_r_ue}", "WARN")
                        time.sleep(10)

                if _r_up_ok:
                    log(worker_id, f"Resumed job {_r_job_id} complete.")
                    d = WORKER_DETAILS.get(worker_id)
                    if d is not None:
                        d.update({"phase": "Done", "pct": 100})
                        d["jobs_done"] += 1
                    with STATS_LOCK:
                        SESSION_STATS["jobs_done"] += 1
                        SESSION_STATS["bytes_uploaded"] += _r_file_bytes
                else:
                    log(worker_id, f"Resume upload failed for {_r_job_id}.", "ERROR")
                    post_status("failed", error_msg="Resume upload failed after 3 attempts")

                # Always clean up the checkpoint and partial file after an upload attempt
                for _fp in [_r_chk_path, _r_partial]:
                    try:
                        if os.path.exists(_fp): os.remove(_fp)
                    except: pass
            # ========================================================

            if check_version(manager_url):
                UPDATE_AVAILABLE = True; SHUTDOWN_EVENT.set(); break

            try: 
                params = {'worker_id': worker_id, 'version': WORKER_VERSION}
                if series_id: params['series_id'] = series_id
                if max_size_mb and max_size_mb > 0: params['max_size_mb'] = max_size_mb
                r = requests.get(f"{manager_url}/get_job", params=params, headers=get_auth_headers(), timeout=10)
            except: time.sleep(5); continue

            data = r.json() if r.status_code == 200 else None
            
            if r.status_code == 401:
                log(worker_id, "AUTH FAILED: Worker Secret is invalid or missing.", "CRITICAL")
                SHUTDOWN_EVENT.set(); break

            if data and data.get("status") == "ok":
                job = data["job"]; job_id = job['id']; dl_url = job['download_url']
                # If this worker has direct access to the source directory, prefer the
                # local file path — avoids streaming the file through the HTTP server.
                _local_src = None
                if local_source_dir:
                    _candidate = os.path.join(local_source_dir, job['id'].replace('/', os.sep))
                    if os.path.isfile(_candidate):
                        _local_src = os.path.abspath(_candidate)
                        dl_url = _local_src
                        log(worker_id, f"Job (local): {job['filename']}")
                    else:
                        log(worker_id, f"Job (local path not found, falling back to HTTP): {job['filename']}", "WARN")
                if _local_src is None:
                    log(worker_id, f"Job: {job['filename']}")
                d = WORKER_DETAILS.get(worker_id)
                if d is not None:
                    d.update({"file": job['filename'], "phase": "Probe", "pct": 0, "job_start": time.time()})

                local_dst = os.path.join(temp_dir, f"encoded{ENCODING_CONFIG['OUTPUT_EXT']}")

                # FFmpeg auth header string (format required by libavformat HTTP demuxer)
                ffmpeg_http_headers = f"X-Worker-Token: {WORKER_SECRET}\r\n"
                # When the source is a local file the auth header and reconnect flags
                # are unnecessary; flag this so the probe/encode commands omit them.
                _is_local = (_local_src is not None)

                # Quota accounting: a single HEAD request gives us the source size instantly,
                # letting encoding start immediately rather than waiting for a full download.
                if quota_tracker:
                    try:
                        if _is_local:
                            src_size = os.path.getsize(dl_url)
                        else:
                            head_r = requests.head(dl_url, headers=get_auth_headers(), timeout=10)
                            src_size = int(head_r.headers.get('content-length', 0))
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
                        log(worker_id, f"Quota size check failed: {e}", "WARN")

                post_status("downloading", 0)

                # -------------------------------------------------------
                # SOURCE INTEGRITY CHECK
                # Compute a hash of the source file and submit it to the
                # server for verification.  The server compares it against
                # its own stored hash.  The expected hash is never sent to
                # the worker, so a cheating worker cannot simply echo it back.
                # -------------------------------------------------------
                log(worker_id, "Verifying source file integrity...")
                update_status("Verifying")
                _our_hash = _compute_source_hash(dl_url, _is_local)
                if _our_hash is None:
                    log(worker_id, "Could not compute source hash. Skipping integrity check.", "WARN")
                else:
                    try:
                        _vr = requests.post(
                            f"{manager_url}/verify_source_hash",
                            json={"job_id": job_id, "worker_id": worker_id,
                                  "source_hash": _our_hash},
                            headers=get_auth_headers(), timeout=10)
                        _vstatus = _vr.json().get("status") if _vr.status_code == 200 else "error"
                    except Exception as _ve:
                        log(worker_id, f"Hash verify request failed: {_ve}. Skipping check.", "WARN")
                        _vstatus = "error"

                    if _vstatus == "mismatch":
                        log(worker_id,
                            f"SOURCE HASH MISMATCH for {job_id}! "
                            "The file on this worker does not match the server's source. "
                            "Aborting encode.", "ERROR")
                        post_status("failed", error_msg="Source hash mismatch")
                        report_error(job_id, "hash_mismatch",
                                     f"Worker hash: {_our_hash}")
                        continue
                    elif _vstatus == "ok":
                        log(worker_id, "Source integrity verified.")
                    # "pending" or "error" → server has no hash yet, proceed normally
                # -------------------------------------------------------

                update_status("Probing")
                total_sec = 0; total_min = 0; audio_index = 0; subtitle_indices = []
                for _probe_attempt in range(3):
                    try:
                        if _is_local:
                            cmd_probe = [FFPROBE_CMD,
                                '-v', 'quiet', '-print_format', 'json', '-show_streams', '-show_format', dl_url]
                        else:
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

                # Disk space pre-flight: need at least 500 MB free for the encoded output
                try:
                    _disk_free = shutil.disk_usage(temp_dir).free
                    if _disk_free < 500 * 1024 * 1024:
                        log(worker_id, f"Low disk space: {_disk_free/1024**2:.0f} MB free, need 500 MB. Skipping.", "ERROR")
                        post_status("failed", error_msg="Insufficient disk space")
                        report_error(job_id, "disk_space", f"Only {_disk_free/1024**2:.0f} MB free, need 500 MB")
                        continue
                except Exception as _disk_e:
                    log(worker_id, f"Disk space check error: {_disk_e}", "WARN")

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

                if _is_local:
                    cmd = [FFMPEG_CMD, '-y', '-i', dl_url,
                           '-map', '0:v:0', '-map', f'0:{audio_index}']
                else:
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
                    # frag_keyframe writes each GOP as a self-contained fragment so the file
                    # remains readable even if the process is killed mid-encode.
                    '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
                    '-progress', 'pipe:1', 
                    local_dst
                ])

                # Save a resume checkpoint so we can recover this encode if interrupted.
                _chk_safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', str(job_id))
                _chk_path = os.path.join(temp_dir, f"{_RESUME_CHK_PREFIX}{_chk_safe_id}.json")
                _save_resume_checkpoint(_chk_path, {
                    "job_id":           job_id,
                    "dl_url":           dl_url,
                    "partial_path":     local_dst,
                    "total_sec":        total_sec,
                    "total_min":        total_min,
                    "audio_index":      audio_index,
                    "subtitle_indices": subtitle_indices,
                    "target_crf":       target_crf,
                    "video_filter":     video_filter,
                    "audio_filter":     audio_filter,
                    "created_at":       time.time(),
                })

                proc = None; enc_time = 0
                for _enc_attempt in range(3):
                    if SHUTDOWN_EVENT.is_set(): break
                    if _enc_attempt > 0:
                        _retry_delay = 30
                        log(worker_id, f"Encode failed (attempt {_enc_attempt}/3, rc={proc.returncode if proc else '?'}). Retrying in {_retry_delay}s...", "WARN")
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
                    assert proc.stdout is not None

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
                        # Remove the resume checkpoint now that the job is finished
                        try:
                            _done_chk = os.path.join(temp_dir, f"{_RESUME_CHK_PREFIX}{re.sub(r'[^a-zA-Z0-9_-]', '_', str(job_id))}.json")
                            if os.path.exists(_done_chk): os.remove(_done_chk)
                        except: pass
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
    use_tui = HAS_TEXTUAL and not getattr(args, 'no_tui', False)
    single_mode = (num_jobs == 1) and not use_tui

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

        t = threading.Thread(target=worker_task, args=(worker_id, manager_url, temp_dir, quota_tracker, single_mode, args.series_id, args.watermark, args.max_size_mb, getattr(args, 'local_source', None)))
        t.daemon = True
        t.start()
        threads.append(t)

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
        if HAS_TEXTUAL and getattr(args, 'no_tui', False):
            _tui_reason = "--no-tui flag passed"
        else:
            _tui_reason = _TEXTUAL_UNAVAIL_REASON or "textual not installed (pip install textual)"
        print(f"Fractum Worker v{WORKER_VERSION} — TUI disabled ({_tui_reason})")
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
    parser.add_argument("--max-size-mb", type=int, default=0, help="Skip source files larger than this size in MB (0 = no limit)")
    parser.add_argument("--local-source", default=None, metavar="DIR",
                        help="Path to the source media directory on this machine. When set and the "
                             "source file exists locally, it is read directly instead of streaming "
                             "over HTTP (useful when this worker runs on the server itself).")
    args = parser.parse_args()
    run_worker(args)
