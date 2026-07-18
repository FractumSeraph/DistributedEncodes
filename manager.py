import os
import time
import threading
import sqlite3
import subprocess
import json
import re
import shutil
import traceback
import uuid
import secrets
import platform
import gzip
import hashlib
import html as html_lib
from functools import wraps
from datetime import datetime, timedelta
from urllib.parse import quote, urljoin

# [ADDED] Requests for remote scanning
try:
    import requests
except ImportError:
    print("[!] Error: 'requests' module not found. Please run: pip install requests")
    exit(1)

# Flask & Extensions
from flask import Flask, request, jsonify, render_template, send_from_directory, send_file, Response, abort, after_this_request
from werkzeug.exceptions import HTTPException
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# ==============================================================================
# CONFIGURATION
# ==============================================================================

# [SECURITY] Minimum Worker Version to accept. 
# Workers older than this will be denied jobs until they auto-update.
MIN_CLIENT_VERSION = "2.5.0"

try:
    from config import (
        SERVER_HOST, SERVER_PORT, SERVER_URL_DISPLAY,
        SOURCE_DIRECTORY, COMPLETED_DIRECTORY, WORKER_TEMPLATE_FILE,
        DB_FILE, VIDEO_EXTENSIONS, 
        ADMIN_USER, ADMIN_PASS
    )
    
    try:
        from config import WORKER_SECRET
    except ImportError:
        print("[!] WARNING: WORKER_SECRET not found. Using unsafe default.")
        WORKER_SECRET = "DefaultInsecureSecret"

    try:
        from config import SECRET_KEY
    except ImportError:
        SECRET_KEY = secrets.token_hex(32)

    try:
        from config import USE_WAL_MODE
    except ImportError:
        USE_WAL_MODE = True
    
    try:
        from config import REMOTE_SOURCE_URL
    except ImportError:
        REMOTE_SOURCE_URL = None

    try:
        from config import DB_MODE
    except ImportError:
        DB_MODE = 'disk'

    # [ADDED] Chunked encoding: split long videos into time-range chunks so
    # multiple workers can encode a single video in parallel.
    try:
        from config import CHUNKED_ENCODING
    except ImportError:
        CHUNKED_ENCODING = True

    try:
        from config import CHUNK_DURATION_SEC
    except ImportError:
        CHUNK_DURATION_SEC = 300

    # [SECURITY] When True, worker endpoints require a valid X-Worker-Token
    # (or ?token=). Requests with NO token are rejected, not waved through.
    # Regular workers already send the token, so they are unaffected.
    try:
        from config import REQUIRE_WORKER_TOKEN
    except ImportError:
        REQUIRE_WORKER_TOKEN = True

except ImportError:
    print("[!] Critical Error: config.py not found.")
    exit(1)

# Sanity clamp: chunks shorter than 60s waste more overhead than they parallelize
try:
    CHUNK_DURATION_SEC = max(60, int(CHUNK_DURATION_SEC))
except (TypeError, ValueError):
    CHUNK_DURATION_SEC = 300

app = Flask(__name__)
app.secret_key = SECRET_KEY

# Security Config
app.config['SESSION_COOKIE_SECURE'] = True    
app.config['SESSION_COOKIE_HTTPONLY'] = True  
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax' 
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 * 1024 

limiter = Limiter(
    get_remote_address,
    app=app,
    storage_uri="memory://"
)

db_lock = threading.RLock()

# --- Chunked encoding state ---
CHUNK_STORE_DIR = os.path.abspath("chunk_store")   # uploaded chunk files live here until assembly
CHUNK_STALE_SECONDS = 1800                          # processing chunk with no heartbeat -> pending
CHUNK_MAX_FAILS = 3                                 # chunk failures before whole-job fallback
ASSEMBLING_JOBS = set()                             # job_ids with an assembly thread running
ASSEMBLY_LOCK = threading.Lock()
SPLIT_LOCK = threading.Lock()                       # at most one probe/split at a time

# Cache for outdated worker logs to prevent spamming DB
OUTDATED_LOG_CACHE = {} 

# In-memory cache for banned_workers.txt (refreshed every 60s)
_BANNED_CACHE: set = set()
_BANNED_CACHE_TIME: float = 0.0
_BANNED_CACHE_LOCK = threading.Lock()

# ==============================================================================
# ADVANCED DATABASE HANDLER (RAM/DISK)
# ==============================================================================

class DatabaseHandler:
    def __init__(self, disk_path, mode):
        self.disk_path = os.path.abspath(disk_path)
        self.mode = mode
        self.active_db_path = self.disk_path
        
        # Determine RAM path based on OS
        if self.mode == 'ram':
            if platform.system() == 'Linux' and os.path.exists('/dev/shm'):
                self.ram_path = os.path.join('/dev/shm', os.path.basename(self.disk_path))
                print(f"[*] DB Mode: RAM (Shared Memory at {self.ram_path})")
                self._load_to_ram()
                self.active_db_path = self.ram_path
                # Start Sync Thread
                threading.Thread(target=self._background_sync_loop, daemon=True).start()
            else:
                print("[!] DB Mode: RAM requested but not supported on this OS/Config. Falling back to DISK.")
                self.mode = 'disk'

    def _load_to_ram(self):
        """Load the DB into RAM at startup.

        A plain restart (systemd) does NOT clear /dev/shm, so a RAM copy from
        the previous run can survive and be NEWER than the disk file (the disk
        sync only runs every 60s). Blindly copying disk->ram would roll back up
        to a minute of completed jobs and earnings. So: if the surviving RAM
        file is newer, recover it to disk first instead of overwriting it.
        """
        with db_lock:
            # Ensure disk file exists to copy
            if not os.path.exists(self.disk_path):
                open(self.disk_path, 'a').close()

            if (os.path.exists(self.ram_path)
                    and os.path.getmtime(self.ram_path) > os.path.getmtime(self.disk_path) + 1):
                print("[!] DB Mode: surviving RAM copy is newer than disk — recovering it to disk.")
                try:
                    src = sqlite3.connect(self.ram_path)
                    dst = sqlite3.connect(self.disk_path)
                    with dst:
                        src.backup(dst)
                    dst.close(); src.close()
                except Exception as e:
                    print(f"[!] RAM->disk recovery failed ({e}); keeping RAM copy as-is.")
                # RAM copy is already the freshest; leave it in place.
                try:
                    os.chmod(self.ram_path, 0o666)
                except Exception:
                    pass
                return

            # Copy to RAM
            shutil.copy2(self.disk_path, self.ram_path)

            # Set permissions
            try:
                os.chmod(self.ram_path, 0o666)
            except:
                pass

    def sync_to_disk(self):
        """Safely backups RAM DB to Disk using SQLite Backup API."""
        if self.mode != 'ram': return
        
        # Don't grab the main lock; backup API handles concurrency well enough
        try:
            source_conn = sqlite3.connect(self.active_db_path)
            dest_conn = sqlite3.connect(self.disk_path)
            
            with source_conn:
                source_conn.backup(dest_conn)
            
            dest_conn.close()
            source_conn.close()
        except Exception as e:
            print(f"[!] DB Sync Failed: {e}")

    def _background_sync_loop(self):
        while True:
            time.sleep(60) # Sync every 60 seconds
            self.sync_to_disk()

    def get_connection(self):
        """Returns a connection to the currently active DB (RAM or Disk)."""
        conn = sqlite3.connect(self.active_db_path, timeout=60)
        
        # Enable WAL for concurrency
        if USE_WAL_MODE:
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
            except:
                pass
        else:
            try:
                conn.execute("PRAGMA journal_mode=DELETE;")
            except:
                pass
            
        return conn

# Initialize the Handler
db_handler = DatabaseHandler(DB_FILE, DB_MODE)

# ==============================================================================
# LOGGING
# ==============================================================================

def log_event(level, message, related_id=None):
    try:
        clean_msg = str(message).replace('<', '&lt;').replace('>', '&gt;')
        clean_id = str(related_id) if related_id else None
        if clean_id:
            clean_id = re.sub(r'[^a-zA-Z0-9_.-]', '', clean_id)
        
        # NOTE: never call log_event while another connection in the same
        # thread has an uncommitted write transaction — SQLite allows one
        # writer at a time, so this INSERT would block for the full busy
        # timeout while db_lock is held, freezing every endpoint.
        with db_lock:
            conn = db_handler.get_connection()
            try:
                conn.execute("INSERT INTO system_logs (timestamp, level, message, related_id) VALUES (?, ?, ?, ?)",
                             (datetime.now(), level, clean_msg, clean_id))
                conn.commit()
            finally:
                conn.close()
        print(f"[{level}] {message}")
    except Exception as e:
        print(f"[!] Logging failed: {e}")

# ==============================================================================
# HELPERS
# ==============================================================================

def sanitize_input(val):
    if not val: return None
    return re.sub(r'[^a-zA-Z0-9_.-]', '', str(val))

def sanitize_wallet(val):
    """Normalize a FractumCoin wallet address for storage (charset kept a bit
    wider than sanitize_input since address formats may include ':')."""
    if not val: return None
    cleaned = re.sub(r'[^a-zA-Z0-9:_.-]', '', str(val))[:128]
    return cleaned or None

def _record_earning(conn, wallet, worker_id, job_id, kind, chunk_index, minutes):
    """Append one verified upload to the FractumCoin earnings ledger.
    Caller holds db_lock and commits."""
    conn.execute(
        "INSERT INTO earnings (timestamp, wallet, worker_id, job_id, kind, chunk_index, minutes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (datetime.now(), wallet, worker_id, job_id, kind, chunk_index, round(float(minutes), 3)))

def is_version_sufficient(client_ver, min_ver):
    if not client_ver: return False
    try:
        c_parts = [int(x) for x in client_ver.split('.') if x.isdigit()]
        m_parts = [int(x) for x in min_ver.split('.') if x.isdigit()]
        return c_parts >= m_parts
    except:
        return False

def is_worker_banned(worker_id):
    global _BANNED_CACHE, _BANNED_CACHE_TIME
    if not worker_id: return False
    with _BANNED_CACHE_LOCK:
        now = time.time()
        if now - _BANNED_CACHE_TIME > 60:
            try:
                with open("banned_workers.txt", "r") as f:
                    _BANNED_CACHE = {line.strip().lower() for line in f if line.strip()}
            except FileNotFoundError:
                _BANNED_CACHE = set()
            _BANNED_CACHE_TIME = now
        return worker_id.strip().lower() in _BANNED_CACHE

@app.after_request
def add_security_headers(response):
    response.headers['Cross-Origin-Opener-Policy'] = 'same-origin'
    response.headers['Cross-Origin-Embedder-Policy'] = 'require-corp'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

@app.before_request
def csrf_protect():
    if request.method == "POST" and request.path.startswith('/api/admin_action'):
        origin = request.headers.get('Origin')
        referer = request.headers.get('Referer')
        target = origin or referer or ""
        # Compare the actual host component, not a substring: a naive
        # `request.host not in target` passes for evil-<host>.attacker.com
        # because it merely contains the host as a substring.
        from urllib.parse import urlparse
        target_host = urlparse(target).netloc
        if not target_host or target_host != request.host:
             return jsonify({"status": "error", "message": "CSRF Blocked: Origin Mismatch"}), 403

def check_auth(u, p):
    return u == ADMIN_USER and p == ADMIN_PASS

def authenticate():
    return Response(
        'Could not verify your access level for that URL.\n'
        'You have to login with proper credentials', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'})

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

def requires_worker_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('X-Worker-Token') or request.args.get('token')
        if token is None:
            # Previously a missing token was waved through entirely, so the
            # shared secret gated nothing. When REQUIRE_WORKER_TOKEN is on we
            # now reject tokenless requests too (regular workers always send
            # one). Left off => the old open behavior for a fully public setup.
            if REQUIRE_WORKER_TOKEN:
                return jsonify({"status": "error", "message": "Worker token required"}), 401
            return f(*args, **kwargs)
        if not secrets.compare_digest(str(token), str(WORKER_SECRET)):
            return jsonify({"status": "error", "message": "Unauthorized Worker"}), 401
        return f(*args, **kwargs)
    return decorated

def verify_upload(filepath):
    """Validate a finished encode. Returns (ok, reason, duration_sec) — the
    duration comes from ffprobe, so earnings credit is based on what was
    actually delivered, not on what the worker claims."""
    try:
        cmd = ['ffprobe', '-v', 'error', '-print_format', 'json', '-show_streams', '-show_format', filepath]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return False, "FFprobe Error", 0.0

        data = json.loads(result.stdout)
        duration = float(data.get('format', {}).get('duration') or 0)
        has_video = False
        for stream in data.get('streams', []):
            if stream['codec_type'] == 'video':
                if stream.get('codec_name') != 'av1': return False, "Invalid Codec (Not AV1)", 0.0
                if int(stream.get('height', 0)) != 480: return False, "Invalid Height", 0.0
                has_video = True
            elif stream['codec_type'] == 'audio':
                if stream.get('codec_name') != 'opus': return False, "Invalid Audio Codec", 0.0

        if not has_video: return False, "No Video Stream", 0.0
        return True, "Verified", duration
    except Exception as e:
        return False, str(e), 0.0

# ==============================================================================
# CHUNKED ENCODING (split one video across many workers)
# ==============================================================================

def build_download_url(job_id, source_type, source_url):
    """Build the URL a worker should stream the source from."""
    if source_type == 'remote':
        base_url = source_url if source_url else REMOTE_SOURCE_URL
        return urljoin(base_url, quote(job_id)) if base_url else ""
    return f"{SERVER_URL_DISPLAY.rstrip('/')}/download_source/{quote(job_id, safe='/')}"

def _chunk_dir(job_id):
    """Directory where uploaded chunks for a job are stored (short + unique)."""
    safe = re.sub(r'[^a-zA-Z0-9_.-]', '_', job_id)[:60]
    tag = hashlib.md5(job_id.encode('utf-8', 'replace')).hexdigest()[:10]
    return os.path.join(CHUNK_STORE_DIR, f"{safe}_{tag}")

def _parse_rate(rate_str):
    """Parse an ffprobe frame-rate fraction like '24000/1001' into a float."""
    try:
        num, _, den = str(rate_str).partition('/')
        num = float(num); den = float(den) if den else 1.0
        return num / den if den else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0

def _probe_media(src, timeout=60):
    """ffprobe a local path or URL. Returns a dict with duration/stream layout,
    or None on failure."""
    try:
        cmd = ['ffprobe', '-v', 'error', '-print_format', 'json',
               '-show_streams', '-show_format', src]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0:
            return None
        data = json.loads(res.stdout)
        dur = float(data.get('format', {}).get('duration') or 0)
        if dur <= 0:
            return None
        try:
            format_start = float(data.get('format', {}).get('start_time') or 0)
        except (TypeError, ValueError):
            format_start = 0.0
        streams = data.get('streams', [])
        has_audio = any(s.get('codec_type') == 'audio' for s in streams)
        sub_codecs = ['subrip', 'ass', 'webvtt', 'mov_text', 'text', 'srt', 'ssa']
        has_subs = any(s.get('codec_type') == 'subtitle'
                       and s.get('codec_name', '').lower() in sub_codecs
                       for s in streams)
        audio_start = None
        first_audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
        if first_audio is not None:
            try: audio_start = float(first_audio.get('start_time') or 0)
            except (TypeError, ValueError): audio_start = None

        # Frame-rate info of the first video stream — chunk boundaries must be
        # snapped to the frame grid, which is only well-defined for CFR sources.
        fps = 0.0; start_time = 0.0; is_cfr = False
        video = next((s for s in streams if s.get('codec_type') == 'video'), None)
        if video:
            r_rate = _parse_rate(video.get('r_frame_rate', '0/1'))
            avg_rate = _parse_rate(video.get('avg_frame_rate', '0/1'))
            try: start_time = float(video.get('start_time') or 0)
            except (TypeError, ValueError): start_time = 0.0
            fps = r_rate
            # CFR when the container's average rate agrees with the nominal rate
            is_cfr = (r_rate > 0 and avg_rate > 0 and abs(r_rate - avg_rate) / r_rate < 0.005)
        return {
            "duration": dur, "has_audio": has_audio, "has_subs": has_subs,
            "fps": fps, "start_time": start_time, "cfr": is_cfr,
            "format_start": format_start, "audio_start": audio_start,
        }
    except Exception:
        return None

def _plan_chunks(duration_sec, fps, start_time=0.0):
    """Return a list of (start, dur) video chunk ranges, or None if not worth splitting.

    Boundaries are snapped to half a frame BEFORE a source frame time, so every
    cut falls cleanly between two frames: chunk N's `-t` window ends exactly
    where chunk N+1's accurate `-ss` seek begins — no duplicated or dropped
    boundary frames, which keeps A/V sync exact after concatenation."""
    if not fps or fps <= 0:
        return None
    if duration_sec < CHUNK_DURATION_SEC * 1.5:
        return None
    n = max(2, int(round(duration_sec / CHUNK_DURATION_SEC)))
    n = min(n, 400)
    target = duration_sec / n
    bounds = [0.0]
    for i in range(1, n):
        frame_no = round(i * target * fps)
        b = start_time + (frame_no - 0.5) / fps
        bounds.append(round(max(b, 0.0), 5))
    ranges = []
    for i in range(n):
        start = bounds[i]
        end = duration_sec if i == n - 1 else bounds[i + 1]
        if end - start <= 0:
            return None  # degenerate plan (absurd fps/duration metadata)
        ranges.append((start, round(end - start, 5)))
    return ranges

def _resolve_folder_filter(series_id):
    """Map a numeric series_id to its folder prefix, or None."""
    if not series_id or not str(series_id).isdigit():
        return None
    for s in get_series_list()[0]:
        if s['id'] == int(series_id):
            return s['folder']
    return None

def _remove_chunk_dir_async(job_id):
    """Delete a job's chunk files off-thread — chunk dirs can hold gigabytes
    and callers usually hold db_lock."""
    threading.Thread(target=shutil.rmtree, args=(_chunk_dir(job_id),),
                     kwargs={'ignore_errors': True}, daemon=True).start()

def _requeue_job_whole(conn, job_id, reason, penalize=True, disable_chunking=True):
    """Chunking gave up on this job: drop chunk state and put it back in the queue.

    penalize:        count this as a failure (encode/verify problems). Off for
                     no-fault paths like 'no workers were online'.
    disable_chunking: set chunkable=0 so the job is encoded whole next time.
                     Off when chunking itself wasn't at fault."""
    c = conn.cursor()
    c.execute("SELECT COALESCE(fail_count, 0) FROM jobs WHERE id=?", (job_id,))
    row = c.fetchone()
    if row is None:
        return
    new_fc = row[0] + (1 if penalize else 0)
    new_status = 'permanently_failed' if new_fc >= 5 else 'queued'
    c.execute("DELETE FROM chunks WHERE job_id=?", (job_id,))
    c.execute("UPDATE jobs SET status=?, chunked=0, chunkable=?, progress=0, worker_id=NULL, "
              "started_at=NULL, fail_count=?, last_updated=? WHERE id=?",
              (new_status, 0 if disable_chunking else None, new_fc, datetime.now(), job_id))
    # Commit before log_event opens its own write connection, otherwise the
    # pending transaction on `conn` would block it (SQLite single-writer).
    conn.commit()
    _remove_chunk_dir_async(job_id)
    log_event("WARN", f"Chunked encode abandoned ({reason}). Job requeued (status={new_status}).", job_id)

def _update_job_chunk_progress(conn, job_id):
    """Recompute aggregate job progress from its chunks (call with db_lock held)."""
    c = conn.cursor()
    c.execute("SELECT COUNT(*), SUM(CASE WHEN status='completed' THEN 100 ELSE COALESCE(progress, 0) END) "
              "FROM chunks WHERE job_id=?", (job_id,))
    total, score = c.fetchone()
    if not total:
        return
    pct = min(99, int((score or 0) / total))  # 100 is reserved for the assembled result
    c.execute("UPDATE jobs SET progress=?, last_updated=? WHERE id=? AND status='processing'",
              (pct, datetime.now(), job_id))

def _register_chunk_failure(job_id, kind, chunk_index, worker_id, reason):
    """A worker reported a chunk failure (or its upload failed verification)."""
    with db_lock:
        conn = db_handler.get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT COALESCE(fail_count, 0), status, worker_id FROM chunks WHERE job_id=? AND kind=? AND chunk_index=?",
                      (job_id, kind, chunk_index))
            row = c.fetchone()
            if row is None:
                conn.commit()
                return
            # Only the worker currently assigned to an in-flight chunk may fail it —
            # ignores stray/duplicate reports that could otherwise kill a healthy job.
            if row[1] != 'processing' or (row[2] and worker_id and row[2] != worker_id):
                conn.commit()
                return
            new_fc = row[0] + 1
            if new_fc >= CHUNK_MAX_FAILS:
                _requeue_job_whole(conn, job_id, f"chunk {kind}#{chunk_index} failed {new_fc}x: {reason}")
            else:
                c.execute("UPDATE chunks SET status='pending', worker_id=NULL, progress=0, "
                          "fail_count=?, last_updated=? WHERE job_id=? AND kind=? AND chunk_index=?",
                          (new_fc, datetime.now(), job_id, kind, chunk_index))
            conn.commit()
        finally:
            conn.close()
    log_event("WARN", f"Chunk {kind}#{chunk_index} failed on {worker_id}: {reason}", job_id)

def _claim_job_for_split(folder_filter, max_size_mb):
    """Pick the next queued, chunk-eligible job and mark it as being split.
    Returns the job row dict or None. Runs entirely under db_lock."""
    with db_lock:
        conn = db_handler.get_connection()
        conn.row_factory = sqlite3.Row
        try:
            params = []
            query_parts = ["status='queued'", "COALESCE(fail_count,0) < 5", "COALESCE(chunkable,1)=1"]
            if max_size_mb and str(max_size_mb).isdigit():
                query_parts.append("file_size <= ?")
                params.append(int(max_size_mb) * 1024 * 1024)
            if folder_filter:
                query_parts.append("id LIKE ?")
                params.append(f"{folder_filter}%")
            # Prefer remote sources first, mirroring /get_job's search order
            sql = ("SELECT id, filename, file_size, source_type, source_url, content_profile "
                   f"FROM jobs WHERE {' AND '.join(query_parts)} "
                   "ORDER BY CASE WHEN source_type='remote' THEN 0 ELSE 1 END, id ASC LIMIT 1")
            c = conn.cursor()
            c.execute(sql, tuple(params))
            row = c.fetchone()
            if row is None:
                return None
            job = dict(row)
            # '(chunking)' marks the short-lived probe/split window; the
            # maintenance loop clears it if the split crashes mid-way.
            c.execute("UPDATE jobs SET status='processing', worker_id='(chunking)', "
                      "last_updated=?, started_at=? WHERE id=?",
                      (datetime.now(), datetime.now(), job['id']))
            conn.commit()
            return job
        finally:
            conn.close()

def _split_claimed_job(job):
    """Probe a claimed job and create its chunk rows.
    Returns True when the job was split, False when it was reverted to the queue."""
    job_id = job['id']
    if job['source_type'] == 'remote':
        src = build_download_url(job_id, 'remote', job.get('source_url'))
    else:
        src = os.path.join(SOURCE_DIRECTORY, job_id.replace('/', os.sep))

    # Probe with a short timeout: this runs inside a /get_chunk request and
    # workers give up waiting after ~30s.
    probe = _probe_media(src, timeout=20) if src else None
    ranges = None
    if probe and probe['cfr']:
        # Streams that don't share a common start offset (e.g. broadcast
        # captures where audio leads video) can't be sliced on the video
        # frame grid without shifting A/V sync — encode those whole.
        skew = abs(probe['start_time'] - probe['format_start'])
        if probe['audio_start'] is not None:
            skew = max(skew, abs(probe['audio_start'] - probe['start_time']))
        if skew > 0.15:
            log_event("INFO", f"Stream start offsets differ by {skew:.2f}s — encoding whole.", job_id)
        else:
            ranges = _plan_chunks(probe['duration'], probe['fps'], probe['start_time'])
    elif probe:
        log_event("INFO", "Source is VFR / has no reliable frame grid — encoding whole.", job_id)

    with db_lock:
        conn = db_handler.get_connection()
        try:
            c = conn.cursor()
            if not ranges:
                # Too short / VFR / unprobeable: hand it back for whole-file
                # encoding. Guarded so we don't clobber the job if an admin
                # reset it (and a worker re-claimed it) while we were probing.
                c.execute("UPDATE jobs SET status='queued', worker_id=NULL, started_at=NULL, "
                          "chunkable=0, source_duration_sec=?, last_updated=? "
                          "WHERE id=? AND status='processing' AND worker_id='(chunking)'",
                          (probe['duration'] if probe else None, datetime.now(), job_id))
                conn.commit()
                return False

            duration, has_audio, has_subs = probe['duration'], probe['has_audio'], probe['has_subs']
            now = datetime.now()
            total = len(ranges) + (1 if (has_audio or has_subs) else 0)
            # Re-assert our claim before creating chunks: an admin reset (or a
            # whole-file worker via /get_job after such a reset) may have taken
            # the job while the probe ran. If the claim is gone, walk away.
            c.execute("UPDATE jobs SET chunked=1, chunkable=1, source_duration_sec=?, total_chunks=?, "
                      "progress=0, worker_id='(chunked)', last_updated=? "
                      "WHERE id=? AND status='processing' AND worker_id='(chunking)'",
                      (duration, total, now, job_id))
            if c.rowcount != 1:
                conn.commit()
                log_event("WARN", "Split abandoned: job was taken by someone else during the probe.", job_id)
                return False
            # Stale rows can exist if this job was reset earlier — start clean
            c.execute("DELETE FROM chunks WHERE job_id=?", (job_id,))
            for i, (start, dur) in enumerate(ranges):
                c.execute("INSERT INTO chunks (job_id, kind, chunk_index, start_sec, duration_sec, "
                          "status, last_updated) VALUES (?, 'video', ?, ?, ?, 'pending', ?)",
                          (job_id, i, start, dur, now))
            if has_audio or has_subs:
                c.execute("INSERT INTO chunks (job_id, kind, chunk_index, start_sec, duration_sec, "
                          "status, last_updated) VALUES (?, 'audio', -1, 0, ?, 'pending', ?)",
                          (job_id, duration, now))
            conn.commit()
            log_event("INFO", f"Split into {total} chunk(s) ({duration/60:.1f} min source).", job_id)
            return True
        finally:
            conn.close()

def _assign_pending_chunk(worker_id, folder_filter, max_size_mb, video_only=False, max_chunk_sec=None):
    """Hand the oldest pending chunk to a worker. Prioritizes the earliest-started
    job so the swarm finishes one video before spilling into the next.
    video_only=True (browser workers) skips the whole-file audio chunk, which
    they can't segment; a native worker encodes the audio track.
    max_chunk_sec caps the chunk LENGTH a worker will accept — browser (wasm)
    workers set this so they are never handed a chunk too long to hold in the
    32-bit heap (which traps as 'unreachable executed'); native workers leave it
    unset and take any length."""
    with db_lock:
        conn = db_handler.get_connection()
        conn.row_factory = sqlite3.Row
        try:
            params = []
            query_parts = ["c.status='pending'", "j.status='processing'", "COALESCE(j.chunked,0)=1"]
            if video_only:
                query_parts.append("c.kind='video'")
            if max_chunk_sec:
                try:
                    query_parts.append("c.duration_sec <= ?")
                    params.append(float(max_chunk_sec))
                except (TypeError, ValueError):
                    pass
            if folder_filter:
                query_parts.append("j.id LIKE ?")
                params.append(f"{folder_filter}%")
            if max_size_mb and str(max_size_mb).isdigit():
                # Estimate the bytes this chunk will stream (audio reads the whole file)
                query_parts.append("(CASE WHEN c.kind='audio' THEN j.file_size "
                                   "ELSE j.file_size * c.duration_sec / MAX(COALESCE(j.source_duration_sec, 1), 1) END) <= ?")
                params.append(int(max_size_mb) * 1024 * 1024)
            sql = ("SELECT c.rowid AS chunk_rowid, c.job_id, c.kind, c.chunk_index, c.start_sec, c.duration_sec, "
                   "j.filename, j.file_size, j.source_type, j.source_url, j.content_profile, "
                   "j.source_duration_sec, j.total_chunks "
                   "FROM chunks c JOIN jobs j ON j.id = c.job_id "
                   f"WHERE {' AND '.join(query_parts)} "
                   "ORDER BY j.started_at ASC, CASE WHEN c.kind='audio' THEN 0 ELSE 1 END, c.chunk_index ASC "
                   "LIMIT 1")
            c = conn.cursor()
            c.execute(sql, tuple(params))
            row = c.fetchone()
            if row is None:
                return None
            chunk = dict(row)
            # The final video chunk encodes to EOF instead of using -t.
            # Computed as a separate single-row query so the assignment scan
            # doesn't pay a correlated subquery per candidate row.
            chunk['is_last'] = False
            if chunk['kind'] == 'video':
                c.execute("SELECT COALESCE(MAX(chunk_index), -1) FROM chunks WHERE job_id=? AND kind='video'",
                          (chunk['job_id'],))
                chunk['is_last'] = (chunk['chunk_index'] == c.fetchone()[0])
            now = datetime.now()
            c.execute("UPDATE chunks SET status='processing', worker_id=?, progress=0, last_updated=? "
                      "WHERE rowid=?", (worker_id, now, chunk.pop('chunk_rowid')))
            c.execute("UPDATE jobs SET last_updated=? WHERE id=?", (now, chunk['job_id']))
            conn.commit()
            chunk['download_url'] = build_download_url(chunk['job_id'], chunk['source_type'], chunk.pop('source_url'))
            # Browser workers can't range-stream a multi-GB source, so they fetch
            # a small pre-cut segment for this chunk instead (see /download_segment).
            if chunk['kind'] == 'video':
                chunk['segment_url'] = (f"{SERVER_URL_DISPLAY.rstrip('/')}/download_segment"
                                        f"?job_id={quote(chunk['job_id'], safe='')}&chunk_index={chunk['chunk_index']}")
            return chunk
        finally:
            conn.close()

def _verify_chunk(filepath, kind, expected_dur, is_last):
    """Validate an uploaded chunk with ffprobe."""
    try:
        cmd = ['ffprobe', '-v', 'error', '-print_format', 'json', '-show_streams', '-show_format', filepath]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return False, "FFprobe Error"
        data = json.loads(result.stdout)
        streams = data.get('streams', [])
        dur = float(data.get('format', {}).get('duration') or 0)

        if kind == 'video':
            video = [s for s in streams if s.get('codec_type') == 'video']
            if not video:
                return False, "No Video Stream"
            if video[0].get('codec_name') != 'av1':
                return False, "Invalid Codec (Not AV1)"
            if int(video[0].get('height', 0)) != 480:
                return False, "Invalid Height"
            tolerance = max(10.0, expected_dur * 0.10) if is_last else max(2.0, expected_dur * 0.05)
            if expected_dur > 0 and abs(dur - expected_dur) > tolerance:
                return False, f"Duration mismatch ({dur:.1f}s vs expected {expected_dur:.1f}s)"
        else:
            audio = [s for s in streams if s.get('codec_type') == 'audio']
            subs = [s for s in streams if s.get('codec_type') == 'subtitle']
            if not audio and not subs:
                return False, "No Audio/Subtitle Streams"
            for s in audio:
                if s.get('codec_name') != 'opus':
                    return False, "Invalid Audio Codec"
            # A truncated audio track must not slip into the final mux.
            # (Duration is only meaningful when an audio stream exists —
            # subtitle-only files end at the last cue.)
            if audio and expected_dur > 0:
                tolerance = max(10.0, expected_dur * 0.05)
                if abs(dur - expected_dur) > tolerance:
                    return False, f"Audio duration mismatch ({dur:.1f}s vs expected {expected_dur:.1f}s)"
        return True, "Verified"
    except Exception as e:
        return False, str(e)

def _maybe_start_assembly(job_id):
    """Spawn an assembly thread if all chunks of the job are completed."""
    with ASSEMBLY_LOCK:
        if job_id in ASSEMBLING_JOBS:
            return
        ASSEMBLING_JOBS.add(job_id)
    threading.Thread(target=_assemble_job, args=(job_id,), daemon=True).start()

def _assemble_job(job_id):
    """Concatenate all uploaded video chunks (stream copy — zero quality loss)
    and mux in the audio/subtitle track, then verify and finalize the job."""
    try:
        with db_lock:
            conn = db_handler.get_connection()
            conn.row_factory = sqlite3.Row
            try:
                c = conn.cursor()
                c.execute("SELECT status, source_duration_sec, total_chunks FROM jobs WHERE id=? AND COALESCE(chunked,0)=1", (job_id,))
                jrow = c.fetchone()
                if jrow is None or jrow['status'] != 'processing':
                    return
                c.execute("SELECT kind, chunk_index, duration_sec, uploaded_path, worker_id FROM chunks "
                          "WHERE job_id=? ORDER BY chunk_index ASC", (job_id,))
                rows = [dict(r) for r in c.fetchall()]
                c.execute("SELECT COUNT(*) FROM chunks WHERE job_id=? AND status != 'completed'", (job_id,))
                if c.fetchone()[0] != 0:
                    return  # not everything is in yet
            finally:
                conn.close()

        video_chunks = sorted([r for r in rows if r['kind'] == 'video'], key=lambda r: r['chunk_index'])
        audio_chunk = next((r for r in rows if r['kind'] == 'audio'), None)
        if not video_chunks:
            raise RuntimeError("no video chunks recorded")
        for r in video_chunks:
            if not r['uploaded_path'] or not os.path.exists(r['uploaded_path']):
                raise RuntimeError(f"chunk file missing: video#{r['chunk_index']}")
        if audio_chunk and (not audio_chunk['uploaded_path'] or not os.path.exists(audio_chunk['uploaded_path'])):
            raise RuntimeError("chunk file missing: audio")

        cdir = _chunk_dir(job_id)
        list_path = os.path.join(cdir, "concat_list.txt")
        out_tmp = os.path.join(cdir, "assembled.mp4")
        with open(list_path, 'w', encoding='utf-8') as f:
            for r in video_chunks:
                # Paths are manager-generated (safe charset), so no quote escaping needed
                f.write(f"file '{os.path.abspath(r['uploaded_path']).replace(os.sep, '/')}'\n")

        log_event("INFO", f"All chunks received. Assembling final file...", job_id)
        cmd = ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', list_path]
        if audio_chunk:
            cmd += ['-i', audio_chunk['uploaded_path'], '-map', '0:v:0', '-map', '1:a?', '-map', '1:s?']
        else:
            cmd += ['-map', '0:v:0']
        cmd += ['-c', 'copy', '-movflags', '+faststart', out_tmp]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if res.returncode != 0 or not os.path.exists(out_tmp):
            raise RuntimeError(f"concat failed (rc={res.returncode}): {res.stderr[-500:] if res.stderr else ''}")

        is_valid, reason, _assembled_dur = verify_upload(out_tmp)
        if not is_valid:
            raise RuntimeError(f"assembled file failed verification: {reason}")

        # Sanity: assembled duration should match the source (warn-only)
        warn = None
        src_dur = jrow['source_duration_sec'] or 0
        final_probe = _probe_media(out_tmp)
        if src_dur and final_probe and abs(final_probe['duration'] - src_dur) > 2.0:
            warn = f"CHUNK ASSEMBLY DRIFT ({final_probe['duration']:.1f}s vs source {src_dur:.1f}s)"

        new_filename = os.path.splitext(job_id)[0] + ".mp4"
        save_path = os.path.abspath(os.path.join(COMPLETED_DIRECTORY, new_filename))
        if not save_path.startswith(os.path.abspath(COMPLETED_DIRECTORY) + os.sep):
            raise RuntimeError("path traversal blocked")
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        shutil.move(out_tmp, save_path)

        workers = sorted({r['worker_id'] for r in rows if r['worker_id']})
        with db_lock:
            conn = db_handler.get_connection()
            try:
                c = conn.cursor()
                # Guard: assembly can take up to an hour (ffmpeg concat). If an
                # admin reset/deleted the job meanwhile (status left 'processing'
                # + chunked=1), don't resurrect it or move the file over a job
                # someone re-claimed whole.
                c.execute("UPDATE jobs SET status='completed', progress=100, duration=?, worker_id=?, last_updated=? "
                          "WHERE id=? AND status='processing' AND COALESCE(chunked,0)=1",
                          (int(src_dur / 60), f"(chunked x{len(workers)})", datetime.now(), job_id))
                if c.rowcount != 1:
                    conn.commit()
                    log_event("WARN", "Assembled file discarded: job was reset/taken during assembly.", job_id)
                    try:
                        if os.path.exists(save_path): os.remove(save_path)
                    except Exception: pass
                    return
                if warn:
                    conn.execute("UPDATE jobs SET warnings = COALESCE(warnings || ' | ', '') || ? WHERE id=?",
                                 (warn, job_id))
                # Worker credit lives in the earnings ledger (recorded at chunk
                # upload), so the work-queue rows can go — keeps the chunks
                # table holding only active work.
                conn.execute("DELETE FROM chunks WHERE job_id=?", (job_id,))
                conn.commit()
            finally:
                conn.close()
        db_handler.sync_to_disk()

        try:
            shutil.rmtree(cdir, ignore_errors=True)
        except Exception:
            pass
        if warn:
            log_event("WARN", warn, job_id)
        log_event("INFO", f"Chunked job assembled and completed ({len(video_chunks)} chunks by {len(workers)} worker(s)).", job_id)

    except Exception as e:
        log_event("ERROR", f"Chunk assembly failed: {e}", job_id)
        with db_lock:
            conn = db_handler.get_connection()
            try:
                _requeue_job_whole(conn, job_id, f"assembly failed: {e}")
                conn.commit()
            finally:
                conn.close()
    finally:
        with ASSEMBLY_LOCK:
            ASSEMBLING_JOBS.discard(job_id)

# ==============================================================================
# HYBRID SCANNER (STREAMING GENERATOR)
# ==============================================================================

# Only one scan may run at a time. Startup, the admin rescan button, and the
# purge/archive actions all spawn scan threads — without this guard they used
# to stack up and crawl the HTTP source several times concurrently.
SCAN_LOCK = threading.Lock()

def _legacy_encoded_id(job_id):
    """The pre-fix scanner left subdirectory components percent-encoded.
    Return that legacy form of a decoded job id (identical when no subdirs)."""
    parts = job_id.split('/')
    if len(parts) <= 1:
        return job_id
    return '/'.join(quote(p, safe='') for p in parts[:-1]) + '/' + parts[-1]

def _broken_id_variants(clean_id):
    """All historical broken forms this file's id may be stored under:
      1. legacy: directory components percent-encoded (pre-double-encoding fix)
      2. mojibake: UTF-8 listing decoded as Latin-1 (e.g. 'BURN·E' → 'BURNÂ·E')
         — happened when the remote server omitted the charset header
      3. legacy encoding of the mojibake form (both eras combined)
    Download URLs built from any of these 404 on the real source, so rows
    stored under them can never encode successfully until migrated."""
    variants = []
    legacy = _legacy_encoded_id(clean_id)
    if legacy != clean_id:
        variants.append(legacy)
    try:
        moji = clean_id.encode('utf-8').decode('latin-1')
        if moji != clean_id:
            variants.append(moji)
            moji_legacy = _legacy_encoded_id(moji)
            if moji_legacy != moji:
                variants.append(moji_legacy)
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return variants

def scan_remote_http(url, prefix="", depth=0, known_ids=None):
    """
    Recursively scans an HTTP directory listing for video files.
    YIELDS results as they are found (Generator) to allow real-time queuing.
    Files whose job id is already in known_ids are skipped entirely, so a
    rescan only fetches directory listings instead of re-HEADing every file.
    """
    if depth > 10: return # Prevent infinite recursion

    headers = {'User-Agent': 'FractumManager/1.0'}
    
    # Retry Loop & Increased Timeout
    r = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code == 200:
                break 
            else:
                return # Stop if 404/403
        except requests.exceptions.RequestException as e:
            if attempt == 2:
                print(f"[!] HTTP Scan Error on {url} after 3 attempts: {e}")
                return
            time.sleep(3)

    if not r or r.status_code != 200: return

    # Directory listings are almost always UTF-8, but when the server omits
    # the charset, `requests` falls back to Latin-1 (an HTTP/1.1 relic).
    # That turns e.g. "BURN·E.2008" into the mojibake "BURNÂ·E.2008", which
    # then percent-encodes to the wrong bytes and 404s on every download.
    if 'charset' not in (r.headers.get('Content-Type') or '').lower():
        r.encoding = 'utf-8'

    try:
        links = re.findall(r'href=["\']([^"\'<>]+)["\']', r.text)

        for link in links:
            # hrefs are HTML-escaped attributes: "&amp;" in a listing means a
            # literal "&" in the real filename.
            link = html_lib.unescape(link)
            if link.startswith('?') or link.startswith('/') or link in ['../', './']: continue
            if "parent directory" in link.lower(): continue

            full_url = urljoin(url, link)

            if link.endswith('/'):
                time.sleep(0.2) # Tiny delay to prevent hammering
                # Recursively yield results from subdirectories.
                # Always unquote the link before appending to prefix so that
                # clean_id is consistently decoded text — quote() in the
                # download URL builder will then encode it exactly once.
                # Without this, a percent-encoded subdir name (e.g. %C2%B7 for ·)
                # gets double-encoded to %25C2%25B7, producing a 404.
                from urllib.parse import unquote as _unquote
                yield from scan_remote_http(full_url, prefix=f"{prefix}{_unquote(link)}", depth=depth+1, known_ids=known_ids)
            elif any(link.lower().endswith(ext) for ext in VIDEO_EXTENSIONS):
                from urllib.parse import unquote
                clean_name = unquote(link)
                clean_id = f"{prefix}{clean_name}"

                # Already tracked under the correct id? Skip the per-file HEAD
                # request — the dominant cost of a rescan. Files tracked only
                # under a BROKEN historical id are deliberately not skipped:
                # process_batch migrates those rows to the working id.
                if known_ids is not None and clean_id in known_ids:
                    continue

                size = 0
                try:
                    h = requests.head(full_url, headers=headers, timeout=15)
                    size = int(h.headers.get('content-length', 0))
                except: pass

                # Yield the found file immediately
                yield (clean_id, clean_name, size)
                
    except Exception as e:
        print(f"[!] HTTP Parsing Error on {url}: {e}")

def scan_and_queue():
    """
    Scans local and remote sources and updates the database/queue in batches.
    Only one scan runs at a time; extra requests are dropped.
    """
    if not SCAN_LOCK.acquire(blocking=False):
        print("[*] Scan already in progress — ignoring duplicate scan request.")
        return
    try:
        _scan_and_queue_inner()
    finally:
        SCAN_LOCK.release()

def _scan_and_queue_inner():
    # --- Helper: Process a batch of files ---
    def process_batch(file_batch):
        if not file_batch: return
        
        count_new = 0
        migrations = []
        # 1. Update Database
        with db_lock:
            conn = db_handler.get_connection()
            try:
                cursor = conn.cursor()
                for item in file_batch:
                    # Unpack
                    if len(item) == 5:
                        job_id, fname, fsize, src_type, src_url = item
                    else:
                        job_id, fname, fsize, src_type = item
                        src_url = None

                    # Profile Logic: Remote is ALWAYS live_action
                    profile = 'standard'
                    if src_type == 'remote':
                        profile = 'live_action'
                    elif 'live_action' in str(job_id).lower():
                        profile = 'live_action'

                    cursor.execute("SELECT id FROM jobs WHERE id=?", (job_id,))
                    if not cursor.fetchone():
                        # The same file may be tracked under a BROKEN historical
                        # id (percent-encoded directories from the old scanner,
                        # or mojibake from charset-less listings — see
                        # _broken_id_variants). Those ids build download URLs
                        # that 404 forever, so repair the row in place instead
                        # of inserting a duplicate.
                        migrated = False
                        for variant in _broken_id_variants(job_id):
                            cursor.execute("SELECT status FROM jobs WHERE id=?", (variant,))
                            vrow = cursor.fetchone()
                            if vrow is None:
                                continue
                            cursor.execute("UPDATE jobs SET id=?, filename=? WHERE id=?",
                                           (job_id, fname, variant))
                            cursor.execute("UPDATE chunks SET job_id=? WHERE job_id=?", (job_id, variant))
                            cursor.execute("UPDATE earnings SET job_id=? WHERE job_id=?", (job_id, variant))
                            if vrow[0] in ('failed', 'permanently_failed'):
                                # Its failures were caused by the broken id
                                # (unreachable download URL) — give it a clean slate.
                                cursor.execute("UPDATE jobs SET status='queued', fail_count=0, progress=0, "
                                               "worker_id=NULL, started_at=NULL, chunked=0, chunkable=NULL, "
                                               "last_updated=? WHERE id=?", (datetime.now(), job_id))
                            migrations.append((variant, job_id, vrow[0]))
                            migrated = True
                            break
                        if migrated:
                            continue
                        cursor.execute(
                            "INSERT INTO jobs (id, filename, status, last_updated, file_size, source_type, source_url, content_profile, fail_count) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, 0)",
                            (job_id, fname, datetime.now(), fsize, src_type, src_url, profile)
                        )
                        count_new += 1
                conn.commit()
            finally:
                conn.close()

        if count_new > 0:
            print(f"[*] Added {count_new} new files to Database...")
        for old_id, new_id, old_status in migrations:
            log_event("INFO", f"Repaired broken job id: '{old_id}' -> '{new_id}' (was {old_status}).", new_id)

    # --- 1. Scan Local ---
    print(f"[*] Scanning LOCAL Source: {SOURCE_DIRECTORY} ...")
    local_batch = []
    if os.path.exists(SOURCE_DIRECTORY):
        try:
            for root, dirs, files in os.walk(SOURCE_DIRECTORY, topdown=True):
                dirs.sort(); files.sort()
                for file in files:
                    if file.lower().endswith(VIDEO_EXTENSIONS):
                        rel_path = os.path.relpath(os.path.join(root, file), SOURCE_DIRECTORY)
                        fsize = os.path.getsize(os.path.join(root, file))
                        local_batch.append((rel_path, file, fsize, 'local'))
        except Exception as e:
            print(f"[!] Local Scanner error: {e}")
    
    if local_batch:
        process_batch(local_batch)

    # --- 2. Scan Remote (Streaming) ---
    if REMOTE_SOURCE_URL:
        print(f"[*] Scanning REMOTE Source: {REMOTE_SOURCE_URL} ...")
        # Snapshot the ids we already track so the crawl can skip the
        # per-file HEAD request for every known file.
        known_ids = set()
        with db_lock:
            conn = db_handler.get_connection()
            try:
                known_ids = {r[0] for r in conn.execute("SELECT id FROM jobs")}
            finally:
                conn.close()
        remote_batch = []
        # Iterate over the generator
        for r_id, r_name, r_size in scan_remote_http(REMOTE_SOURCE_URL, known_ids=known_ids):
            remote_batch.append((r_id, r_name, r_size, 'remote', REMOTE_SOURCE_URL))
            
            # Commit every 10 files so workers don't wait
            if len(remote_batch) >= 10:
                process_batch(remote_batch)
                remote_batch = []
        
        # Commit any remaining files
        if remote_batch:
            process_batch(remote_batch)

    # Folder set may have changed — refresh the cached series list.
    try:
        get_series_list(force_refresh=True)
    except Exception:
        pass
    print("[*] Scan complete.")

# Cache for get_series_list: it hits the filesystem (a directory listing + a
# stat per entry + a JSON read), and it was being called on every /get_job and
# /get_chunk poll that carried a series_id — dozens of listings per second, and
# painful when SOURCE_DIRECTORY is a network mount. 60s TTL, like _BANNED_CACHE.
_SERIES_CACHE = None
_SERIES_CACHE_TIME = 0.0
_SERIES_CACHE_LOCK = threading.Lock()

def get_series_list(force_refresh=False):
    global _SERIES_CACHE, _SERIES_CACHE_TIME
    with _SERIES_CACHE_LOCK:
        now = time.time()
        if not force_refresh and _SERIES_CACHE is not None and now - _SERIES_CACHE_TIME < 60:
            return _SERIES_CACHE
        try:
            # [FIX] Allow series listing even in hybrid mode
            if not os.path.exists(SOURCE_DIRECTORY):
                _SERIES_CACHE = ([], []); _SERIES_CACHE_TIME = now
                return _SERIES_CACHE

            folders = sorted([d for d in os.listdir(SOURCE_DIRECTORY) if os.path.isdir(os.path.join(SOURCE_DIRECTORY, d))])
            folder_set = set(folders)
            mapping = {}
            stale_keys = []

            if os.path.exists('series_names.json'):
                try:
                    mapping = json.load(open('series_names.json', 'r'))
                    stale_keys = [k for k in mapping if k not in folder_set]
                    if stale_keys:
                        print(f"[!] series_names.json has {len(stale_keys)} stale key(s): {', '.join(stale_keys[:5])}")
                except: pass

            result = ([{"id": i+1, "folder": f, "name": mapping.get(f, f)} for i, f in enumerate(folders)], stale_keys)
            _SERIES_CACHE = result; _SERIES_CACHE_TIME = now
            return result
        except:
            return _SERIES_CACHE if _SERIES_CACHE is not None else ([], [])

# ==============================================================================
# DATABASE INIT
# ==============================================================================

def init_db():
    with db_lock:
        conn = db_handler.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, filename TEXT, status TEXT, worker_id TEXT,
                progress INTEGER DEFAULT 0, duration INTEGER DEFAULT 0, last_updated TIMESTAMP,
                started_at TIMESTAMP, file_size INTEGER DEFAULT 0, source_type TEXT DEFAULT 'local'
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS system_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP,
                level TEXT,
                message TEXT,
                related_id TEXT
            )
        ''')
        try: cursor.execute("ALTER TABLE jobs ADD COLUMN file_size INTEGER DEFAULT 0")
        except sqlite3.OperationalError: pass
        try: cursor.execute("ALTER TABLE jobs ADD COLUMN worker_version TEXT")
        except sqlite3.OperationalError: pass
        try: cursor.execute("ALTER TABLE jobs ADD COLUMN source_type TEXT DEFAULT 'local'")
        except sqlite3.OperationalError: pass
        try: cursor.execute("ALTER TABLE jobs ADD COLUMN source_url TEXT")
        except sqlite3.OperationalError: pass
        try: cursor.execute("ALTER TABLE jobs ADD COLUMN warnings TEXT")
        except sqlite3.OperationalError: pass
        # [ADDED] Live Action Profile Column
        try: cursor.execute("ALTER TABLE jobs ADD COLUMN content_profile TEXT DEFAULT 'standard'")
        except sqlite3.OperationalError: pass
        # [ADDED] fail_count column for permanent failure tracking
        try: cursor.execute("ALTER TABLE jobs ADD COLUMN fail_count INTEGER DEFAULT 0")
        except sqlite3.OperationalError: pass
        # [ADDED] source_hash: MD5 of first 4 MB of source file for integrity verification
        try: cursor.execute("ALTER TABLE jobs ADD COLUMN source_hash TEXT")
        except sqlite3.OperationalError: pass
        # [ADDED] Chunked encoding columns (all additive — existing queues survive updates)
        # chunked:  1 = this job is currently split into chunks
        # chunkable: NULL = unknown, 1 = allowed, 0 = never chunk (too short / probe failed / chunking failed before)
        try: cursor.execute("ALTER TABLE jobs ADD COLUMN chunked INTEGER DEFAULT 0")
        except sqlite3.OperationalError: pass
        try: cursor.execute("ALTER TABLE jobs ADD COLUMN chunkable INTEGER")
        except sqlite3.OperationalError: pass
        try: cursor.execute("ALTER TABLE jobs ADD COLUMN source_duration_sec REAL")
        except sqlite3.OperationalError: pass
        try: cursor.execute("ALTER TABLE jobs ADD COLUMN total_chunks INTEGER DEFAULT 0")
        except sqlite3.OperationalError: pass

        # Chunk work-items for split jobs.  kind = 'video' (a time range) or
        # 'audio' (full-length audio + subtitles, encoded once by one worker).
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chunks (
                job_id TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'video',
                chunk_index INTEGER NOT NULL DEFAULT 0,
                start_sec REAL DEFAULT 0,
                duration_sec REAL DEFAULT 0,
                status TEXT DEFAULT 'pending',
                worker_id TEXT,
                progress INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                last_updated TIMESTAMP,
                uploaded_path TEXT,
                PRIMARY KEY (job_id, kind, chunk_index)
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chunks_status ON chunks(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chunks_job ON chunks(job_id)")
        # Composite index for the job-pick query (status + source_type filter,
        # ORDER BY id) so /get_job and _claim_job_for_split do an index seek
        # instead of scanning + sorting the whole queued set every poll.
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_pick ON jobs(status, source_type, id)")
        # last_updated drives the dashboard history + admin job list sort.
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_last_updated ON jobs(last_updated)")
        # id drives the system_logs prune + the LIMIT view.
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_syslogs_id ON system_logs(id)")

        # FractumCoin earnings ledger: one row per VERIFIED upload (whole file
        # or video chunk). Durable payout record — unlike the jobs/chunks
        # tables, rows here are never rewritten by retries, archives, or
        # chunk cleanup. `paid` is reserved for future payout tooling.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS earnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP,
                wallet TEXT,
                worker_id TEXT,
                job_id TEXT,
                kind TEXT DEFAULT 'full',
                chunk_index INTEGER,
                minutes REAL DEFAULT 0,
                paid INTEGER DEFAULT 0
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_earnings_wallet ON earnings(wallet)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_earnings_worker ON earnings(worker_id)")
        # timestamp drives the 24h/30d scoreboard filters.
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_earnings_ts ON earnings(timestamp)")

        # One-time backfill so historical scoreboard credit carries over into
        # the ledger (wallet unknown for past work — recorded as NULL).
        cursor.execute("SELECT COUNT(*) FROM earnings")
        if cursor.fetchone()[0] == 0:
            cursor.execute('''
                INSERT INTO earnings (timestamp, wallet, worker_id, job_id, kind, chunk_index, minutes)
                SELECT last_updated, NULL, worker_id, id, 'full', NULL, COALESCE(duration, 0)
                FROM jobs WHERE status='completed' AND worker_id IS NOT NULL AND COALESCE(chunked, 0)=0
            ''')
            backfilled_full = cursor.rowcount
            cursor.execute('''
                INSERT INTO earnings (timestamp, wallet, worker_id, job_id, kind, chunk_index, minutes)
                SELECT c2.last_updated, NULL, c2.worker_id, c2.job_id, 'video_chunk', c2.chunk_index, c2.duration_sec / 60.0
                FROM chunks c2 JOIN jobs j2 ON j2.id = c2.job_id
                WHERE c2.status='completed' AND c2.kind='video' AND c2.worker_id IS NOT NULL AND j2.status='completed'
            ''')
            backfilled_chunks = cursor.rowcount
            if backfilled_full > 0 or backfilled_chunks > 0:
                print(f"[*] Earnings ledger backfilled from history: {backfilled_full} whole file(s), {backfilled_chunks} chunk(s).")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS error_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP,
                job_id TEXT,
                worker_id TEXT,
                error_type TEXT,
                message TEXT,
                details TEXT
            )
        ''')

        conn.commit()
        conn.close()

# ==============================================================================
# ROUTES
# ==============================================================================

# Number of bytes used for the fast source-file hash (4 MB)
_HASH_BYTES = 4 * 1024 * 1024

def _fast_hash_file(path, nbytes=_HASH_BYTES):
    """Return MD5 hex digest of the first `nbytes` of a local file."""
    h = hashlib.md5()
    try:
        with open(path, 'rb') as f:
            remaining = nbytes
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                h.update(chunk)
                remaining -= len(chunk)
    except Exception:
        return None
    return h.hexdigest()

@app.route('/')
@app.route('/dashboard')
def dashboard(): return render_template('dashboard.html')

@app.route('/web')
@app.route('/node')
def web_worker():
    # In-browser encoder page. The worker token is injected so a volunteer can
    # just open the page and contribute (same exposure as /install, which by
    # policy also embeds the secret). Safely embedded via |tojson in the page.
    return render_template('web_worker.html', worker_token=WORKER_SECRET)

@app.route('/admin')
@limiter.limit("5 per minute") 
@requires_auth
def admin_panel(): return render_template('admin.html')

@app.route('/api/ping')
def api_ping(): return jsonify({"status": "pong"})

@app.route('/dl/worker')
def download_worker_script(): return send_file(WORKER_TEMPLATE_FILE, as_attachment=True, download_name='worker.py')

# [ADDED] Route to serve the watermark font to workers
@app.route('/dl/font')
def download_font():
    font_path = os.path.abspath('arial.ttf')
    if os.path.exists(font_path):
        return send_file(font_path, as_attachment=True, download_name='arial.ttf')
    return abort(404)

@app.route('/api/series')
def api_series_list():
    series, stale = get_series_list()
    return jsonify({"series": series, "stale_series_keys": stale})

@app.route('/install')
def install_script():
    u = sanitize_input(request.args.get('username')) or 'Anonymous'
    w = sanitize_input(request.args.get('workername')) or 'LinuxNode'
    s_id = request.args.get('series_id', '')
    if s_id and not s_id.isdigit(): s_id = ''
    j = request.args.get('jobs', '1')
    if not j.isdigit(): j = '1'
    wallet = sanitize_wallet(request.args.get('wallet'))
    wallet_arg = f' --wallet "{wallet}"' if wallet else ''
    script = f"""#!/bin/bash
if [ -x "$(command -v apt-get)" ]; then sudo apt-get update -qq && sudo apt-get install -y ffmpeg python3 python3-requests; fi
if [ -x "$(command -v dnf)" ]; then sudo dnf install -y ffmpeg python3 python3-requests; fi
curl -s "{SERVER_URL_DISPLAY.rstrip('/')}/dl/worker" -o worker.py
export WORKER_SECRET="{WORKER_SECRET}"
python3 worker.py --username "{u}" --workername "{w}" --jobs {j} --manager "{SERVER_URL_DISPLAY}" --series-id "{s_id}"{wallet_arg}
"""
    return Response(script, mimetype='text/x-shellscript')

@app.route('/download_source/<path:filename>')
def download_source(filename):
    return send_from_directory(SOURCE_DIRECTORY, filename, as_attachment=True)

@app.route('/download_media')
@requires_worker_auth
def download_media():
    """Same-origin source download for the browser worker. The /web page is
    cross-origin isolated (COEP: require-corp), so it CANNOT fetch a remote
    source directly — the browser blocks it with a NetworkError. This proxies
    the source through the manager (which has no such restriction): local files
    are served from disk, remote files are streamed through. Only used for
    whole-file browser jobs (chunk workers use /download_segment)."""
    job_id = (request.args.get('job_id') or '').strip()
    if not job_id:
        return abort(400)
    with db_lock:
        conn = db_handler.get_connection()
        conn.row_factory = sqlite3.Row
        try:
            c = conn.cursor()
            c.execute("SELECT source_type, source_url FROM jobs WHERE id=?", (job_id,))
            row = c.fetchone()
        finally:
            conn.close()
    if row is None:
        return abort(404)

    if row['source_type'] != 'remote':
        # Local file: reuse the safe directory sender (blocks path traversal).
        return send_from_directory(SOURCE_DIRECTORY, job_id, as_attachment=True)

    # Remote: stream it through the manager so the browser sees a same-origin
    # response. The manager fetches server-side (no COEP), forwarding the token.
    remote_url = build_download_url(job_id, 'remote', row['source_url'])
    if not remote_url:
        return abort(404)
    try:
        upstream = requests.get(remote_url,
                                headers={'User-Agent': 'FractumManager/1.0',
                                         'X-Worker-Token': WORKER_SECRET},
                                stream=True, timeout=60)
    except requests.exceptions.RequestException as e:
        log_event("WARN", f"download_media proxy failed: {e}", job_id)
        return abort(502)
    if upstream.status_code != 200:
        upstream.close()
        return abort(upstream.status_code if upstream.status_code >= 400 else 502)

    from flask import stream_with_context
    def _gen():
        try:
            for block in upstream.iter_content(chunk_size=1024 * 256):
                yield block
        finally:
            upstream.close()
    headers = {}
    if upstream.headers.get('Content-Length'):
        headers['Content-Length'] = upstream.headers['Content-Length']
    return Response(stream_with_context(_gen()),
                    content_type=upstream.headers.get('Content-Type', 'application/octet-stream'),
                    headers=headers)

@app.route('/get_job', methods=['GET'])
@requires_worker_auth
def get_job():
    max_size_mb = request.args.get('max_size_mb')
    series_id = request.args.get('series_id')
    worker_id = sanitize_input(request.args.get('worker_id'))
    worker_version = sanitize_input(request.args.get('version'))
    
    if is_worker_banned(worker_id):
        now = time.time()
        last_log = OUTDATED_LOG_CACHE.get(f"banned_{worker_id}", 0)
        if now - last_log > 300: 
            log_event("WARN", f"Banned worker blocked from getting jobs: {worker_id}")
            OUTDATED_LOG_CACHE[f"banned_{worker_id}"] = now
        
        return jsonify({"status": "empty", "message": "Unauthorized"}), 403
    
    # [ENFORCEMENT] Check Minimum Client Version
    if not is_version_sufficient(worker_version, MIN_CLIENT_VERSION):
        # LOGGING ADDED: Log specific workers (throttled to 5 mins) to prove they are alive
        now = time.time()
        last_log = OUTDATED_LOG_CACHE.get(worker_id, 0)
        if now - last_log > 300: # Log only once every 5 minutes per worker
            log_event("WARN", f"Outdated Worker Denied: {worker_id} (v{worker_version}). Waiting for auto-update.")
            OUTDATED_LOG_CACHE[worker_id] = now
            
        return jsonify({"status": "empty", "message": "Update Required"}), 200

    search_phases = []
    # Only offer remote jobs to updated clients (Redundant now due to enforcement above, but kept safe)
    if REMOTE_SOURCE_URL and is_version_sufficient(worker_version, "1.9.0"): 
        search_phases.append('remote')
    
    # Always offer local jobs as fallback
    search_phases.append('local')

    try:
        # Resolve series folders BEFORE taking db_lock: get_series_list reads
        # the filesystem, and a slow/hung source mount must not stall every
        # endpoint behind the lock.
        search_attempts = [series_id] if series_id and series_id.isdigit() else []
        search_attempts.append(None)
        folder_filters = {sid: _resolve_folder_filter(sid) for sid in search_attempts}

        with db_lock:
            conn = db_handler.get_connection(); conn.row_factory = sqlite3.Row
            try:
                c = conn.cursor()
                job = None

                for source_type in search_phases:
                    if job: break

                    for current_search_id in search_attempts:
                        folder_filter = folder_filters.get(current_search_id)

                        params = [source_type]
                        query_parts = ["status='queued'", "source_type=?", "COALESCE(fail_count,0) < 5"]
                        
                        if max_size_mb and max_size_mb.isdigit():
                            query_parts.append("file_size <= ?")
                            params.append(int(max_size_mb) * 1024 * 1024)
                        if folder_filter:
                            query_parts.append("id LIKE ?")
                            params.append(f"{folder_filter}%")
                        
                        # [ADDED] content_profile to SELECT query
                        sql = f"SELECT id, filename, file_size, source_type, source_url, content_profile, source_hash FROM jobs WHERE {' AND '.join(query_parts)} ORDER BY id ASC LIMIT 1"
                        c.execute(sql, tuple(params)); row = c.fetchone()
                        if row: job = dict(row); break
                
                if job:
                    job['download_url'] = build_download_url(job['id'], job['source_type'], job.get('source_url'))
                    conn.execute("UPDATE jobs SET status='processing', worker_id=?, worker_version=?, last_updated=?, started_at=? WHERE id=?",
                        (worker_id, worker_version, datetime.now(), datetime.now(), job['id']))
                    conn.commit()
            finally:
                conn.close()

        if job is None:
            return jsonify({"status": "empty"})

        # Lazily compute the source hash for local files on first pickup —
        # OUTSIDE db_lock, since it reads 4 MB from the source disk and a slow
        # drive/mount would otherwise stall every endpoint. If the worker's
        # hash check races this, /verify_source_hash just answers "pending".
        needs_hash = (job['source_type'] != 'remote' and not job.get('source_hash'))
        # Never reveal the hash to the worker — workers must submit their own
        # computed hash blind so they cannot cheat by echoing the known value.
        job.pop('source_hash', None)
        if needs_hash:
            _src_path = os.path.join(SOURCE_DIRECTORY, job['id'].replace('/', os.sep))
            _computed = _fast_hash_file(_src_path)
            if _computed:
                with db_lock:
                    conn = db_handler.get_connection()
                    try:
                        conn.execute("UPDATE jobs SET source_hash=? WHERE id=? AND source_hash IS NULL",
                                     (_computed, job['id']))
                        conn.commit()
                    finally:
                        conn.close()

        return jsonify({"status": "ok", "job": job})
    except Exception as e:
        log_event("ERROR", f"get_job failed: {e}")
        return jsonify({"status": "error"}), 500

@app.route('/get_chunk', methods=['GET'])
@requires_worker_auth
def get_chunk():
    """Assign one chunk of a split video to a worker. Splits the next queued
    job on demand when no pending chunks exist. Workers fall back to /get_job
    when this returns empty."""
    if not CHUNKED_ENCODING:
        return jsonify({"status": "empty", "message": "Chunking disabled"})

    max_size_mb = request.args.get('max_size_mb')
    series_id = request.args.get('series_id')
    worker_id = sanitize_input(request.args.get('worker_id'))
    worker_version = sanitize_input(request.args.get('version'))
    # Browser workers pass video_only=1: they fetch a pre-cut segment per video
    # chunk and can't handle the whole-file audio chunk. max_chunk_sec caps the
    # chunk length they'll accept so the 32-bit wasm doesn't OOM/trap on a long
    # chunk; native workers omit it.
    video_only = request.args.get('video_only') in ('1', 'true', 'yes')
    max_chunk_sec = request.args.get('max_chunk_sec')

    if is_worker_banned(worker_id):
        return jsonify({"status": "empty", "message": "Unauthorized"}), 403
    if not is_version_sufficient(worker_version, MIN_CLIENT_VERSION):
        return jsonify({"status": "empty", "message": "Update Required"}), 200

    try:
        folder_filter = _resolve_folder_filter(series_id)

        chunk = _assign_pending_chunk(worker_id, folder_filter, max_size_mb, video_only=video_only, max_chunk_sec=max_chunk_sec)
        if chunk is None:
            if SPLIT_LOCK.acquire(blocking=False):
                # No pending chunks anywhere — split the next queued job.
                # One candidate per request and one split at a time: this keeps
                # the request latency bounded (workers time out around 30s) and
                # stops a poll storm from splitting many jobs simultaneously.
                # An unsplittable candidate is requeued with chunkable=0 and gets
                # picked up whole via /get_job instead.
                try:
                    job = _claim_job_for_split(folder_filter, max_size_mb)
                    if job is not None and _split_claimed_job(job):
                        chunk = _assign_pending_chunk(worker_id, folder_filter, max_size_mb, video_only=video_only, max_chunk_sec=max_chunk_sec)
                finally:
                    SPLIT_LOCK.release()
            else:
                # Another request is mid-split: tell the worker to ask again in
                # a moment INSTEAD of falling back to /get_job — otherwise a
                # worker started with --jobs N grabs N different whole videos
                # in the few seconds before the first split lands.
                return jsonify({"status": "retry", "wait": 3})

        if chunk is None:
            return jsonify({"status": "empty"})
        return jsonify({"status": "ok", "chunk": chunk})
    except Exception as e:
        log_event("ERROR", f"get_chunk failed: {e}")
        return jsonify({"status": "error"}), 500

# Small tail past the chunk end so the segment reliably contains the full
# [start, start+dur] window after keyframe/rounding effects. With -copyts + -to
# the end is anchored to an ABSOLUTE timestamp, so this need only cover rounding
# (not the GOP length — unlike a -t-based cut, which measures from the keyframe).
_SEGMENT_GUARD_SEC = 2.0

@app.route('/download_segment', methods=['GET'])
@requires_worker_auth
def download_segment():
    """Stream a small, standalone, keyframe-aligned segment covering one video
    chunk's time range — so a browser worker can encode a chunk of a multi-GB
    source without downloading the whole file.

    The cut is a lossless stream copy with -copyts, so the segment keeps the
    source's absolute timestamps: the worker then does an accurate
    `-ss <start> -t <dur>` seek INSIDE the segment and gets exactly the same
    [start, start+dur] window a native chunk worker produces — so browser and
    desktop chunks tile identically at assembly.
    """
    job_id = (request.args.get('job_id') or '').strip()
    try:
        chunk_index = int(request.args.get('chunk_index'))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "chunk_index required"}), 400
    if not job_id:
        return jsonify({"status": "error", "message": "job_id required"}), 400

    with db_lock:
        conn = db_handler.get_connection()
        conn.row_factory = sqlite3.Row
        try:
            c = conn.cursor()
            c.execute("SELECT start_sec, duration_sec FROM chunks WHERE job_id=? AND kind='video' AND chunk_index=?",
                      (job_id, chunk_index))
            crow = c.fetchone()
            c.execute("SELECT source_type, source_url FROM jobs WHERE id=?", (job_id,))
            jrow = c.fetchone()
        finally:
            conn.close()
    if crow is None or jrow is None:
        return abort(404)

    start = float(crow['start_sec'] or 0)
    dur = float(crow['duration_sec'] or 0)
    if jrow['source_type'] == 'remote':
        src = build_download_url(job_id, 'remote', jrow['source_url'])
        src_input = src
    else:
        src_input = os.path.join(SOURCE_DIRECTORY, job_id.replace('/', os.sep))
        if not os.path.isfile(src_input):
            return abort(404)

    seg_dir = os.path.join("temp_uploads", "segments")
    os.makedirs(seg_dir, exist_ok=True)
    seg_path = os.path.join(seg_dir, f"{uuid.uuid4().hex}.mkv")

    # -copyts keeps absolute timestamps; -ss before -i seeks to the keyframe at
    # or before `start`; -to anchors the end to an ABSOLUTE time so the window is
    # always covered regardless of GOP length. Matroska handles the copied
    # stream + preserved timestamps cleanly and streams progressively.
    input_flags = ['-ss', f"{start:.3f}"]
    if jrow['source_type'] == 'remote':
        input_flags = ['-headers', f"X-Worker-Token: {WORKER_SECRET}\r\n"] + input_flags
    cmd = (['ffmpeg', '-y', '-copyts'] + input_flags +
           ['-i', src_input, '-to', f"{start + dur + _SEGMENT_GUARD_SEC:.3f}",
            '-map', '0:v:0', '-c', 'copy', '-f', 'matroska', seg_path])
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if res.returncode != 0 or not os.path.exists(seg_path) or os.path.getsize(seg_path) == 0:
            try:
                if os.path.exists(seg_path): os.remove(seg_path)
            except OSError: pass
            log_event("WARN", f"Segment extraction failed for chunk {chunk_index} (rc={res.returncode})", job_id)
            return jsonify({"status": "error", "message": "segment extraction failed"}), 500
    except Exception as e:
        try:
            if os.path.exists(seg_path): os.remove(seg_path)
        except OSError: pass
        log_event("WARN", f"Segment extraction error for chunk {chunk_index}: {e}", job_id)
        return jsonify({"status": "error", "message": "segment extraction error"}), 500

    # The copied segment starts at the keyframe at-or-before `start`, and input
    # -ss on it is start_time-relative, so the worker must seek by the LEAD
    # (start - segment_start_time), not by absolute `start`. Probe the header
    # for that start_time and hand the worker the exact lead + duration.
    seg_start = 0.0
    try:
        pr = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=start_time',
                             '-of', 'default=nw=1:nk=1', seg_path],
                            capture_output=True, text=True, timeout=30)
        seg_start = float((pr.stdout or '0').strip() or 0)
    except Exception:
        seg_start = 0.0
    lead = max(0.0, start - seg_start)

    @after_this_request
    def _cleanup(response):
        try: os.remove(seg_path)
        except OSError: pass
        return response

    resp = send_file(seg_path, mimetype='video/x-matroska', as_attachment=True,
                     download_name=f"segment_{chunk_index}.mkv")
    # Worker reads these to run: -ss <lead> -i seg -t <duration>  (accurate seek)
    resp.headers['X-Segment-Lead'] = f"{lead:.3f}"
    resp.headers['X-Segment-Duration'] = f"{dur:.3f}"
    return resp

@app.route('/upload_chunk', methods=['POST'])
@requires_worker_auth
def upload_chunk():
    """Receive one encoded chunk, verify it, and store it until assembly."""
    job_id = request.form.get('job_id')
    worker_id = sanitize_input(request.form.get('worker_id'))
    wallet = sanitize_wallet(request.form.get('wallet'))
    kind = request.form.get('kind', 'video')
    if kind not in ('video', 'audio'):
        return jsonify({"status": "error", "message": "invalid kind"}), 400
    try:
        chunk_index = int(request.form.get('chunk_index'))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "chunk_index required"}), 400
    if 'file' not in request.files or not job_id:
        return jsonify({"status": "error"}), 400

    with db_lock:
        conn = db_handler.get_connection()
        conn.row_factory = sqlite3.Row
        try:
            c = conn.cursor()
            c.execute("SELECT status, duration_sec FROM chunks WHERE job_id=? AND kind=? AND chunk_index=?",
                      (job_id, kind, chunk_index))
            crow = c.fetchone()
            c.execute("SELECT status FROM jobs WHERE id=?", (job_id,))
            jrow = c.fetchone()
            c.execute("SELECT COALESCE(MAX(chunk_index), -1) FROM chunks WHERE job_id=? AND kind='video'", (job_id,))
            max_video_index = c.fetchone()[0]
        finally:
            conn.close()

    if crow is None or jrow is None or jrow['status'] != 'processing':
        return jsonify({"status": "stale", "message": "Chunk no longer expected"}), 409
    if crow['status'] == 'completed':
        return jsonify({"status": "stale", "message": "Chunk already uploaded"}), 409

    quarantine_dir = os.path.join("temp_uploads", "quarantine")
    os.makedirs(quarantine_dir, exist_ok=True)
    temp_path = os.path.join(quarantine_dir, f"{uuid.uuid4().hex}.mp4")
    request.files['file'].save(temp_path)

    # Only the highest-indexed video chunk runs to EOF, so only its duration is fuzzier
    is_last = (kind == 'video' and chunk_index == max_video_index)
    is_valid, reason = _verify_chunk(temp_path, kind, float(crow['duration_sec'] or 0), is_last)
    if not is_valid:
        try: os.remove(temp_path)
        except OSError: pass
        _register_chunk_failure(job_id, kind, chunk_index, worker_id, f"upload rejected: {reason}")
        return jsonify({"status": "error", "message": reason}), 400

    cdir = _chunk_dir(job_id)
    os.makedirs(cdir, exist_ok=True)
    final_path = os.path.join(cdir, f"{kind}_{chunk_index if chunk_index >= 0 else 'track'}.mp4")
    shutil.move(temp_path, final_path)

    all_done = False
    with db_lock:
        conn = db_handler.get_connection()
        try:
            c = conn.cursor()
            c.execute("UPDATE chunks SET status='completed', progress=100, worker_id=?, "
                      "uploaded_path=?, last_updated=? WHERE job_id=? AND kind=? AND chunk_index=? "
                      "AND status != 'completed'",
                      (worker_id, final_path, datetime.now(), job_id, kind, chunk_index))
            newly_completed = (c.rowcount == 1)
            # FractumCoin credit: each verified video chunk earns its slice of
            # the source's minutes (the audio helper pass earns 0, so a chunked
            # job totals exactly its real length). rowcount guard = no double
            # credit if two workers race the same chunk.
            if newly_completed and kind == 'video':
                _record_earning(conn, wallet, worker_id, job_id, 'video_chunk', chunk_index,
                                float(crow['duration_sec'] or 0) / 60.0)
            _update_job_chunk_progress(conn, job_id)
            c.execute("SELECT COUNT(*) FROM chunks WHERE job_id=? AND status != 'completed'", (job_id,))
            all_done = (c.fetchone()[0] == 0)
            conn.commit()
        finally:
            conn.close()

    log_event("INFO", f"Chunk {kind}#{chunk_index} received from {worker_id}", job_id)
    if all_done:
        _maybe_start_assembly(job_id)
    return jsonify({"status": "success"})

@app.route('/report_chunk', methods=['POST'])
@requires_worker_auth
def report_chunk():
    """Heartbeat / failure / abandon notifications for an in-flight chunk."""
    d = request.json or {}
    job_id = str(d.get('job_id', '') or '').strip()
    worker_id = sanitize_input(d.get('worker_id'))
    kind = d.get('kind', 'video')
    status = d.get('status')
    try:
        chunk_index = int(d.get('chunk_index'))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "chunk_index required"}), 400
    if not job_id or kind not in ('video', 'audio'):
        return jsonify({"status": "error"}), 400

    if status == 'failed':
        err = str(d.get('error', 'Unknown Error'))[:512]
        _register_chunk_failure(job_id, kind, chunk_index, worker_id, err)
        return jsonify({"status": "received"})

    with db_lock:
        conn = db_handler.get_connection()
        try:
            c = conn.cursor()
            if status == 'abandoned':
                # Worker is shutting down mid-chunk: return it to the pool without penalty
                c.execute("UPDATE chunks SET status='pending', worker_id=NULL, progress=0, last_updated=? "
                          "WHERE job_id=? AND kind=? AND chunk_index=? AND status='processing' AND worker_id=?",
                          (datetime.now(), job_id, kind, chunk_index, worker_id))
            else:
                try:
                    progress = max(0, min(100, int(d.get('progress', 0))))
                except (TypeError, ValueError):
                    progress = 0
                # Ownership guard: a worker whose chunk timed out and was
                # reassigned must not steal it back via a late heartbeat.
                c.execute("UPDATE chunks SET progress=?, last_updated=? "
                          "WHERE job_id=? AND kind=? AND chunk_index=? AND status='processing' "
                          "AND (worker_id IS NULL OR worker_id=?)",
                          (progress, datetime.now(), job_id, kind, chunk_index, worker_id))
            _update_job_chunk_progress(conn, job_id)
            conn.commit()
        finally:
            conn.close()
    return jsonify({"status": "received"})

@app.route('/upload_result', methods=['POST'])
@requires_worker_auth
def upload_result():
    job_id = request.form.get('job_id')
    worker_id = sanitize_input(request.form.get('worker_id'))
    wallet = sanitize_wallet(request.form.get('wallet'))
    try:
        duration = int(float(request.form.get('duration', 0)))
    except:
        duration = 0
    
    if 'file' in request.files and job_id:
        # Reject stale/misdirected uploads BEFORE touching disk: a job that was
        # since split into chunks (chunked=1) or already completed must not be
        # overwritten by a straggler whole-file worker.
        with db_lock:
            conn = db_handler.get_connection()
            try:
                c = conn.cursor()
                c.execute("SELECT status, COALESCE(chunked, 0) FROM jobs WHERE id=?", (job_id,))
                jrow = c.fetchone()
            finally:
                conn.close()
        if jrow is None:
            return jsonify({"status": "error", "message": "unknown job"}), 404
        if jrow[1] == 1 or jrow[0] == 'completed':
            return jsonify({"status": "stale", "message": "Job no longer accepts a whole-file upload"}), 409

        new_filename = os.path.splitext(job_id)[0] + ".mp4"
        quarantine_dir = os.path.join("temp_uploads", "quarantine")
        os.makedirs(quarantine_dir, exist_ok=True)
        temp_path = os.path.join(quarantine_dir, f"{uuid.uuid4().hex}.mp4")
        request.files['file'].save(temp_path)

        is_valid, reason, verified_dur = verify_upload(temp_path)
        if not is_valid:
            log_event("WARN", f"Security: Upload rejected ({reason})", job_id)
            os.remove(temp_path)
            with db_lock:
                conn = db_handler.get_connection()
                try:
                    # Count the failure so a permanently-broken source doesn't
                    # loop forever through the new auto-requeue path.
                    conn.execute("UPDATE jobs SET status='failed', fail_count=COALESCE(fail_count,0)+1, "
                                 "last_updated=? WHERE id=? AND COALESCE(chunked,0)=0", (datetime.now(), job_id))
                    conn.execute("UPDATE jobs SET status='permanently_failed' WHERE id=? AND COALESCE(fail_count,0) >= 5", (job_id,))
                    conn.commit()
                finally:
                    conn.close()
            return jsonify({"status": "error", "message": reason}), 400

        save_path = os.path.abspath(os.path.join(COMPLETED_DIRECTORY, new_filename))
        if not save_path.startswith(os.path.abspath(COMPLETED_DIRECTORY) + os.sep):
             os.remove(temp_path); return jsonify({"status": "error"}), 403

        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        shutil.move(temp_path, save_path)

        with db_lock:
            conn = db_handler.get_connection()
            try:
                c = conn.cursor()
                # Guard chunked=0 again (race: split may have happened during
                # the encode verify/move) and skip double-credit on re-uploads.
                c.execute("SELECT status FROM jobs WHERE id=?", (job_id,))
                prev = c.fetchone()
                c.execute("UPDATE jobs SET status='completed', progress=100, worker_id=?, last_updated=?, duration=? WHERE id=? AND COALESCE(chunked,0)=0",
                    (worker_id, datetime.now(), duration, job_id))
                # FractumCoin credit: minutes come from ffprobe of the delivered
                # file, not the worker's claim. A re-upload to an already
                # completed job (stale worker) earns nothing twice.
                if prev is not None and prev[0] != 'completed':
                    _record_earning(conn, wallet, worker_id, job_id, 'full', None, verified_dur / 60.0)
                conn.commit()
            finally:
                conn.close()

        # [CRITICAL] Immediate Sync to Disk
        db_handler.sync_to_disk()

        log_event("INFO", f"Job completed by {worker_id}", job_id)
        return jsonify({"status": "success"})
    return jsonify({"status": "error"}), 400

@app.route('/upload_log', methods=['POST'])
@requires_worker_auth
def receive_log():
    MAX_LOG_BYTES = 50 * 1024 * 1024  # 50 MB hard cap
    if request.content_length and request.content_length > MAX_LOG_BYTES:
        return jsonify({"status": "error", "message": "Log file exceeds 50 MB limit"}), 413

    job_id = request.form.get('job_id')
    worker_id = sanitize_input(request.form.get('worker_id'))

    # Chunk logs arrive as "<job_id>::chunk_<kind>_<index>" — warnings are
    # recorded against the parent job, the log file keeps the full name.
    parent_job_id = job_id.split('::chunk', 1)[0] if job_id else job_id
    is_chunk_log = (parent_job_id != job_id)

    if 'log_file' in request.files and job_id:
        log_dir = os.path.join(os.getcwd(), "encode_logs")
        os.makedirs(log_dir, exist_ok=True)
        
        # Save the compressed file
        safe_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', job_id)
        log_path = os.path.join(log_dir, f"{safe_name}.log.gz")
        request.files['log_file'].save(log_path)
        
        warnings = []
        # Audio chunks contain no video encoder — the video cheat checks below
        # would false-positive on them.
        is_audio_chunk_log = is_chunk_log and '::chunk_audio' in job_id
        try:
            if not is_audio_chunk_log:
                with gzip.open(log_path, 'rt', encoding='utf-8', errors='ignore') as f:
                    log_content = f.read().lower()

                    # 1. Check for GPU Encoders (Safely)
                    mapping_block = re.search(r'stream mapping:(.*?)(?:press \[|output #)', log_content, re.DOTALL)

                    if mapping_block:
                        mapping_text = mapping_block.group(1)
                        gpu_encoders = ['nvenc', 'qsv', 'amf', 'videotoolbox', 'hevc_amf']
                        for enc in gpu_encoders:
                            if enc in mapping_text:
                                warnings.append(f"GPU USED ({enc.upper()})")
                                break

                    # 2. Check SVT-AV1 preset
                    preset_match = re.search(r'svt\[info\]:\s*preset\s*:\s*(\d+)', log_content)
                    if preset_match:
                        used_preset = preset_match.group(1)
                        if used_preset != "2":
                            warnings.append(f"PRESET MODIFIED (Used {used_preset}, Expected 2)")
                    elif "libsvtav1" not in log_content:
                        if not warnings:
                            warnings.append("NON-STANDARD ENCODER USED")

        except Exception as e:
            log_event("WARN", f"Could not parse log for {job_id}: {e}")
            
        if warnings:
            warning_str = " | ".join(warnings)
            if is_chunk_log:
                warning_str = f"[{job_id.split('::', 1)[1]}] {warning_str}"
            log_event("WARN", f"CHEATING DETECTED by {worker_id}: {warning_str}", parent_job_id)
            with db_lock:
                conn = db_handler.get_connection()
                if is_chunk_log:
                    # A chunked job produces one log per chunk — append so no
                    # worker's cheat evidence (or the assembly drift warning)
                    # is overwritten by a later log.
                    conn.execute("UPDATE jobs SET warnings = COALESCE(warnings || ' | ', '') || ? WHERE id=?",
                                 (warning_str, parent_job_id))
                else:
                    conn.execute("UPDATE jobs SET warnings=? WHERE id=?", (warning_str, parent_job_id))
                conn.commit()
                conn.close()
                
        return jsonify({"status": "success"})
    return jsonify({"status": "error"}), 400

@app.route('/report_error', methods=['POST'])
@requires_worker_auth
def receive_error_report():
    d = request.json
    if not d:
        return jsonify({"status": "error"}), 400

    job_id   = str(d.get('job_id', '') or 'unknown').strip()[:512]
    worker_id = sanitize_input(d.get('worker_id', '')) or 'unknown'
    error_type = sanitize_input(d.get('error_type', 'unknown')) or 'unknown'
    message  = str(d.get('message', ''))[:2048]
    details  = str(d.get('details', ''))[:32768]

    with db_lock:
        conn = db_handler.get_connection()
        try:
            conn.execute(
                "INSERT INTO error_reports (timestamp, job_id, worker_id, error_type, message, details) VALUES (?, ?, ?, ?, ?, ?)",
                (datetime.now(), job_id, worker_id, error_type, message, details)
            )
            conn.commit()
        finally:
            conn.close()

    log_event("ERROR", f"Error report from {worker_id} [{error_type}]: {message}", job_id)
    return jsonify({"status": "received"})


@app.route('/api/error_reports')
@requires_auth
def api_error_reports():
    try:
        limit = min(int(request.args.get('limit', 200)), 1000)
    except (ValueError, TypeError):
        limit = 200

    with db_lock:
        conn = db_handler.get_connection()
        conn.row_factory = sqlite3.Row
        try:
            c = conn.cursor()
            c.execute("SELECT id, timestamp, job_id, worker_id, error_type, message, details FROM error_reports ORDER BY timestamp DESC LIMIT ?", (limit,))
            reports = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
    return jsonify({"reports": reports})


@app.route('/api/download_log')
@requires_auth
def download_encode_log():
    job_id = request.args.get('job_id', '')
    if not job_id:
        return abort(400)
    safe_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', job_id)
    log_dir = os.path.abspath("encode_logs")
    log_path = os.path.abspath(os.path.join(log_dir, f"{safe_name}.log.gz"))
    # Path traversal guard
    if not log_path.startswith(log_dir + os.sep):
        return abort(403)
    if os.path.exists(log_path):
        return send_file(log_path, as_attachment=True, download_name=f"{safe_name}.log.gz")
    return jsonify({"status": "error", "message": "Log file not found"}), 404


@app.route('/job_status', methods=['GET'])
@requires_worker_auth
def job_status():
    """Worker-facing endpoint: returns the current status and assigned worker for a job.
    Used by recovering workers to detect whether a checkpoint is still viable."""
    job_id = (request.args.get('job_id', '') or '').strip()
    if not job_id:
        return jsonify({"status": "error", "message": "job_id required"}), 400
    with db_lock:
        conn = db_handler.get_connection()
        conn.row_factory = sqlite3.Row
        try:
            c = conn.cursor()
            c.execute("SELECT status, worker_id FROM jobs WHERE id=?", (job_id,))
            row = c.fetchone()
        finally:
            conn.close()
    if row is None:
        return jsonify({"status": "error", "message": "not_found"}), 404
    return jsonify({"job_status": row["status"], "worker_id": row["worker_id"]})


@app.route('/reclaim_job', methods=['POST'])
@requires_worker_auth
def reclaim_job():
    """Worker-facing endpoint: attempt to reclaim a queued (or timed-out) job by ID.
    Only succeeds if the job is currently in a reclaimable state (queued / failed
    with fail_count < 5).  Returns {"status": "ok"} on success so the worker knows
    it is safe to upload the resumed encode."""
    d = request.json or {}
    job_id   = str(d.get('job_id', '') or '').strip()
    worker_id = sanitize_input(d.get('worker_id', ''))
    worker_version = sanitize_input(d.get('version', ''))
    if not job_id or not worker_id:
        return jsonify({"status": "error", "message": "job_id and worker_id required"}), 400
    with db_lock:
        conn = db_handler.get_connection()
        conn.row_factory = sqlite3.Row
        try:
            c = conn.cursor()
            c.execute("SELECT status, COALESCE(fail_count, 0) as fail_count FROM jobs WHERE id=?", (job_id,))
            row = c.fetchone()
            if row is None:
                return jsonify({"status": "error", "message": "not_found"}), 404
            if row["status"] not in ('queued', 'failed') or row["fail_count"] >= 5:
                return jsonify({"status": "conflict",
                                "message": f"Job is not reclaimable (status={row['status']})"}), 409
            conn.execute(
                "UPDATE jobs SET status='processing', worker_id=?, worker_version=?, "
                "last_updated=?, started_at=COALESCE(started_at, ?) WHERE id=?",
                (worker_id, worker_version, datetime.now(), datetime.now(), job_id))
            conn.commit()
        finally:
            conn.close()
    log_event("INFO", f"Job reclaimed by {worker_id} (resume path)", job_id)
    return jsonify({"status": "ok"})


@app.route('/verify_source_hash', methods=['POST'])
@requires_worker_auth
def verify_source_hash():
    """Worker submits the MD5 hash it computed from the source file before encoding.
    Server compares against the stored hash to detect wrong-file or cheating scenarios.
    Returns {"status": "ok"} on match, {"status": "mismatch"} on mismatch,
    or {"status": "pending"} when the server has not yet computed its hash."""
    d = request.json or {}
    job_id    = str(d.get('job_id', '') or '').strip()
    worker_id = sanitize_input(d.get('worker_id', ''))
    worker_hash = str(d.get('source_hash', '')).strip().lower()

    if not job_id or not worker_id or not worker_hash:
        return jsonify({"status": "error", "message": "job_id, worker_id and source_hash required"}), 400

    with db_lock:
        conn = db_handler.get_connection()
        conn.row_factory = sqlite3.Row
        try:
            c = conn.cursor()
            c.execute("SELECT source_hash FROM jobs WHERE id=?", (job_id,))
            row = c.fetchone()
            if row is None:
                return jsonify({"status": "error", "message": "not_found"}), 404
            stored_hash = (row["source_hash"] or "").strip().lower()
            if not stored_hash:
                # Hash not computed yet (remote job or hash still pending)
                return jsonify({"status": "pending"})
            if worker_hash == stored_hash:
                return jsonify({"status": "ok"})
            # Mismatch: flag in warnings, increment fail_count so admin can see it
            warn_msg = f"HASH_MISMATCH (worker={worker_hash[:8]}… expected={stored_hash[:8]}…)"
            conn.execute(
                "UPDATE jobs SET warnings = COALESCE(warnings || ' | ', '') || ?, "
                "fail_count = COALESCE(fail_count, 0) + 1 WHERE id=?",
                (warn_msg, job_id))
            conn.commit()
        finally:
            conn.close()

    log_event("WARN", f"Source hash mismatch from {worker_id}: {warn_msg}", job_id)
    return jsonify({"status": "mismatch"})


@app.route('/report_status', methods=['POST'])
@requires_worker_auth
def report_status():
    d = request.json or {}; status = d.get('status')
    worker_id = sanitize_input(d.get('worker_id'))
    worker_version = sanitize_input(d.get('version'))
    
    if status == 'completed': return jsonify({"status": "ignored"}), 403
    if status == 'failed':
        err_msg = d.get('error', 'Unknown Error')
        log_event("WARN", f"Worker {worker_id} (v{worker_version}) reported failure: {err_msg}", d.get('job_id'))

    with db_lock:
        conn = db_handler.get_connection()
        try:
            job_id_val = d.get('job_id')
            # Guards on every update:
            #  - COALESCE(chunked,0)=0: never touch a job that was split into
            #    chunks (that would stall assignment/assembly).
            #  - worker_id=?: only the CURRENT owner may report. A stale worker
            #    whose job was reassigned/completed can't revert it.
            #  - status NOT IN terminal: never resurrect a completed or
            #    permanently_failed job.
            base_where = ("WHERE id=? AND COALESCE(chunked, 0)=0 AND worker_id=? "
                          "AND status NOT IN ('completed', 'permanently_failed')")
            sql = f"UPDATE jobs SET status=?, progress=?, last_updated=? {base_where}"
            params = [status, d.get('progress', 0), datetime.now(), job_id_val, worker_id]
            if d.get('duration', 0) > 0:
                sql = f"UPDATE jobs SET status=?, progress=?, last_updated=?, duration=? {base_where}"
                params.insert(3, d.get('duration'))
            conn.execute(sql, tuple(params))
            if status == 'failed':
                # Only count the failure if this worker actually owned the job
                # (the UPDATE above changed the row). Re-read to confirm.
                conn.execute("UPDATE jobs SET fail_count = COALESCE(fail_count, 0) + 1 "
                             "WHERE id=? AND COALESCE(chunked, 0)=0 AND worker_id=? AND status='failed'",
                             (job_id_val, worker_id))
                conn.execute("UPDATE jobs SET status='permanently_failed' WHERE id=? AND COALESCE(fail_count, 0) >= 5 AND status='failed'", (job_id_val,))
            conn.commit()
        finally:
            conn.close()
    return jsonify({"status": "received"})

@app.route('/api/stats')
def api_stats():
    filter_val = request.args.get('filter')

    with db_lock:
        conn = db_handler.get_connection(); conn.row_factory = sqlite3.Row
        try:
            c = conn.cursor()
            # Scoreboard = the earnings ledger: minutes of successful,
            # verified encodes uploaded (whole files + video chunk slices;
            # the audio helper pass earns 0 so chunked jobs count exactly
            # once). The ledger is append-only, so scores survive retries,
            # archives, and chunk cleanup.
            # timestamps are stored with Python datetime.now() (LOCAL time), so
            # the window must also be computed in local time — datetime('now')
            # alone is UTC and would skew the window by the server's offset.
            earn_filter = (" AND timestamp > datetime('now', 'localtime', '-1 day')" if filter_val == '24h'
                           else " AND timestamp > datetime('now', 'localtime', '-30 days')" if filter_val == '30d' else "")
            c.execute(f"""
                SELECT CASE WHEN instr(worker_id, '-') > 0 THEN substr(worker_id, 1, instr(worker_id, '-') - 1) ELSE worker_id END as worker_id,
                       CAST(SUM(minutes) AS INTEGER) as total_minutes,
                       COUNT(DISTINCT job_id) as files_count
                FROM earnings WHERE worker_id IS NOT NULL {earn_filter}
                GROUP BY 1 ORDER BY total_minutes DESC""")
            sb = [dict(r) for r in c.fetchall()]

            c.execute("SELECT id, COALESCE(worker_id, 'Pending...') as worker_id, filename, duration, progress, status, COALESCE(chunked, 0) as chunked FROM jobs WHERE status IN ('processing', 'downloading', 'uploading')")
            act = [dict(r) for r in c.fetchall()]

            # For chunked jobs, show swarm status instead of a single worker name.
            chunk_info = {}
            active_chunked_ids = [a['id'] for a in act if a['chunked']]
            if active_chunked_ids:
                ph = ",".join("?" * len(active_chunked_ids))
                c.execute(f"""SELECT job_id, COUNT(*) as total,
                                    SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as done,
                                    COUNT(DISTINCT CASE WHEN status='processing' THEN worker_id END) as active_workers
                             FROM chunks WHERE job_id IN ({ph}) GROUP BY job_id""", active_chunked_ids)
                for r in c.fetchall():
                    chunk_info[r['job_id']] = dict(r)
            for a in act:
                ci = chunk_info.get(a['id'])
                if a['chunked'] and ci:
                    if ci['active_workers'] > 0:
                        a['worker_id'] = f"{ci['active_workers']} worker(s) · {ci['done']}/{ci['total']} chunks"
                    else:
                        a['worker_id'] = f"chunked · {ci['done']}/{ci['total']} chunks"
                a.pop('id', None); a.pop('chunked', None)
            
            c.execute("SELECT id, status, worker_id FROM jobs ORDER BY last_updated DESC LIMIT 20")
            hist = [dict(r) for r in c.fetchall()]
            
            c.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'")
            queue_depth = c.fetchone()[0]
            
            c.execute("SELECT id, filename, file_size FROM jobs WHERE status='queued' ORDER BY id ASC LIMIT 50")
            queue_items = [dict(r) for r in c.fetchall()]

            c.execute("SELECT COUNT(*) FROM jobs")
            total_count = c.fetchone()[0]

            c.execute("SELECT COUNT(*) FROM jobs WHERE status='completed'")
            total_completed = c.fetchone()[0]
        finally:
            conn.close()
    return jsonify({"scoreboard": sb, "active": act, "history": hist, "queue_depth": queue_depth, "queue_items": queue_items, "total_jobs": total_count, "total_completed": total_completed})

@app.route('/api/all_jobs')
@requires_auth
def api_all_jobs():
    # Bounded + optionally status-filtered: returning every one of thousands of
    # rows as JSON on each admin refresh serialized megabytes under db_lock.
    try:
        limit = min(int(request.args.get('limit', 2000)), 10000)
    except (ValueError, TypeError):
        limit = 2000
    status_filter = request.args.get('status')
    with db_lock:
        conn = db_handler.get_connection(); conn.row_factory = sqlite3.Row
        try:
            c = conn.cursor()
            if status_filter:
                c.execute("SELECT id, status, worker_id, worker_version, last_updated, warnings FROM jobs "
                          "WHERE status=? ORDER BY last_updated DESC LIMIT ?", (status_filter, limit))
            else:
                c.execute("SELECT id, status, worker_id, worker_version, last_updated, warnings FROM jobs "
                          "ORDER BY last_updated DESC LIMIT ?", (limit,))
            jobs = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
        return jsonify({"jobs": jobs})

@app.route('/api/earnings')
@requires_auth
def api_earnings():
    """FractumCoin payout view: per-wallet totals plus the most recent ledger
    rows. Work uploaded without --wallet is grouped under '(no wallet)'."""
    try:
        limit = min(int(request.args.get('limit', 100)), 1000)
    except (ValueError, TypeError):
        limit = 100
    with db_lock:
        conn = db_handler.get_connection(); conn.row_factory = sqlite3.Row
        try:
            c = conn.cursor()
            c.execute("""
                SELECT COALESCE(NULLIF(wallet, ''), '(no wallet)') as wallet,
                       ROUND(SUM(minutes), 2) as total_minutes,
                       ROUND(SUM(CASE WHEN paid=0 THEN minutes ELSE 0 END), 2) as unpaid_minutes,
                       COUNT(*) as uploads,
                       COUNT(DISTINCT job_id) as jobs,
                       COUNT(DISTINCT worker_id) as workers,
                       MIN(timestamp) as first_upload,
                       MAX(timestamp) as last_upload
                FROM earnings GROUP BY 1 ORDER BY total_minutes DESC""")
            wallets = [dict(r) for r in c.fetchall()]
            c.execute("SELECT id, timestamp, wallet, worker_id, job_id, kind, chunk_index, minutes, paid "
                      "FROM earnings ORDER BY id DESC LIMIT ?", (limit,))
            recent = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
    return jsonify({"wallets": wallets, "recent": recent})

@app.route('/api/logs')
@requires_auth
def get_logs():
    try:
        limit = min(int(request.args.get('limit', 100)), 1000)
    except (ValueError, TypeError):
        limit = 100
    with db_lock:
        conn = db_handler.get_connection(); conn.row_factory = sqlite3.Row
        try:
            c = conn.cursor()
            c.execute("SELECT * FROM system_logs ORDER BY timestamp DESC LIMIT ?", (limit,))
            logs = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
    return jsonify({"logs": logs})

@app.route('/api/admin_action', methods=['POST'])
@requires_auth
def admin_action():
    data = request.json or {}; job_id = data.get('job_id'); action = data.get('action')
    log_event("WARN", f"Admin performed '{action}' on job", job_id)
    post_commit_logs = []

    with db_lock:
        conn = db_handler.get_connection(); c = conn.cursor()
        try:
            if action == 'delete':
                c.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
                c.execute("DELETE FROM chunks WHERE job_id = ?", (job_id,))
                _remove_chunk_dir_async(job_id)
            elif action == 'retry':
                c.execute("DELETE FROM chunks WHERE job_id = ?", (job_id,))
                _remove_chunk_dir_async(job_id)
                c.execute("UPDATE jobs SET status='queued', progress=0, worker_id=NULL, fail_count=0, chunked=0, chunkable=NULL, started_at=NULL, last_updated=? WHERE id=?", (datetime.now(), job_id))
            elif action == 'retry_all_failed':
                c.execute("SELECT id FROM jobs WHERE status IN ('failed', 'permanently_failed')")
                for (fid,) in c.fetchall():
                    _remove_chunk_dir_async(fid)
                c.execute("DELETE FROM chunks WHERE job_id IN (SELECT id FROM jobs WHERE status IN ('failed', 'permanently_failed'))")
                c.execute("UPDATE jobs SET status='queued', progress=0, worker_id=NULL, fail_count=0, chunked=0, chunkable=NULL, started_at=NULL, last_updated=? WHERE status IN ('failed', 'permanently_failed')", (datetime.now(),))
            elif action == 'clear_stale':
                cutoff = datetime.now() - timedelta(minutes=10)
                c.execute("UPDATE jobs SET status='queued', progress=0, worker_id=NULL, last_updated=?, started_at=NULL WHERE status IN ('processing', 'downloading', 'uploading') AND COALESCE(chunked, 0)=0 AND last_updated < ?", (datetime.now(), cutoff))
                c.execute("UPDATE chunks SET status='pending', worker_id=NULL, progress=0, last_updated=? WHERE status='processing' AND last_updated < ?", (datetime.now(), cutoff))
                # A chunked job whose chunks have ALL gone silent is wedged
                # (e.g. no chunk-capable workers left) — give the admin the
                # same recovery lever whole-file jobs have. No fail penalty.
                c.execute("SELECT id FROM jobs WHERE status='processing' AND COALESCE(chunked, 0)=1 AND last_updated < ?", (cutoff,))
                for (wjid,) in c.fetchall():
                    _requeue_job_whole(conn, wjid, "admin clear_stale",
                                       penalize=False, disable_chunking=False)
            elif action == 'archive_history':
                ts = int(time.time())
                c.execute("SELECT id FROM jobs WHERE status='completed' AND id NOT LIKE 'HISTORY_%'")
                rows = c.fetchall()
                for (jid,) in rows:
                    new_id = f"HISTORY_{ts}_{jid}"
                    c.execute("UPDATE jobs SET id = ? WHERE id = ?", (new_id, jid))
                    # Keep chunk rows pointing at the archived job so scoreboard credit survives
                    c.execute("UPDATE chunks SET job_id = ? WHERE job_id = ?", (new_id, jid))
                post_commit_logs.append(f"Admin archived {len(rows)} jobs.")
            elif action == 'purge_queue':
                c.execute("DELETE FROM jobs WHERE status='queued'")
                post_commit_logs.append("Admin PURGED the queue. Rescan triggered (background).")
            elif action == 'clear_error_reports':
                c.execute("DELETE FROM error_reports")
                post_commit_logs.append("Admin cleared all error reports.")

            conn.commit()
        finally:
            conn.close()

    # log_event opens its own write connection — calling it while the
    # transaction above was still open used to block it for the full SQLite
    # busy timeout (60s) WITH db_lock held, freezing every worker endpoint.
    for _msg in post_commit_logs:
        log_event("WARN", _msg)

    if action == 'purge_queue' or action == 'archive_history':
        # FIXED: Run scan in a background thread to prevent client timeout
        threading.Thread(target=scan_and_queue, daemon=True).start()

    return jsonify({"status": "ok"})

@app.route('/api/get_config')
@requires_auth
def get_config():
    return jsonify({"REMOTE_SOURCE_URL": REMOTE_SOURCE_URL})

@app.route('/api/update_config', methods=['POST'])
@requires_auth
def update_config():
    global REMOTE_SOURCE_URL
    data = request.json
    if 'REMOTE_SOURCE_URL' in data:
        new_url = data['REMOTE_SOURCE_URL'].strip()
        if not new_url: new_url = None
        REMOTE_SOURCE_URL = new_url
        log_event("WARN", f"Admin updated Remote Source URL to: {REMOTE_SOURCE_URL}")
    return jsonify({"status": "ok", "new_value": REMOTE_SOURCE_URL})

@app.route('/api/rescan_db')
@requires_auth
def api_rescan():
    try:
        # Background thread for manual rescans too
        threading.Thread(target=scan_and_queue, daemon=True).start()
        return jsonify({"status": "ok", "message": "Rescan started in background."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, (Response, HTTPException)): return e
    log_event("CRITICAL", f"Unhandled Exception: {str(e)}\n{traceback.format_exc()}")
    return "Internal Server Error", 500

def sweep_quarantine():
    """Remove stale files from the quarantine + segment temp directories."""
    cutoff = time.time() - 3600  # 1 hour
    count = 0
    # segment temp files are normally deleted right after send; sweep covers
    # aborted downloads where after_this_request didn't fire.
    for sub in ("quarantine", "segments"):
        d = os.path.join("temp_uploads", sub)
        if not os.path.exists(d): continue
        for fname in os.listdir(d):
            fpath = os.path.join(d, fname)
            try:
                if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
                    count += 1
            except Exception: pass
    if count > 0:
        print(f"[*] Temp sweep: removed {count} stale file(s).")

def maintenance_loop():
    while True:
        try:
            logs_to_write = []
            assembly_candidates = []
            with db_lock:
                conn = db_handler.get_connection(); cursor = conn.cursor()
                try:
                    now = datetime.now()
                    # Whole-file jobs: 4h heartbeat timeout (chunked jobs are handled below)
                    cursor.execute("SELECT id, filename, last_updated, worker_id FROM jobs WHERE status IN ('processing', 'downloading', 'uploading') AND COALESCE(chunked, 0)=0")
                    for row in cursor.fetchall():
                        jid, fname, last_up, worker_id = row
                        if last_up:
                            try:
                                l_time = datetime.strptime(str(last_up).split('.')[0], "%Y-%m-%d %H:%M:%S")
                                stale_sec = (now - l_time).total_seconds()
                                # '(chunking)' = a split probe that crashed mid-way; recover quickly
                                if worker_id == '(chunking)' and stale_sec > 600:
                                    logs_to_write.append(("WARN", "Chunk split never finished. Returning job to queue.", jid))
                                    cursor.execute("UPDATE jobs SET status='queued', progress=0, worker_id=NULL, last_updated=?, started_at=NULL WHERE id=?", (now, jid))
                                elif stale_sec > 14400: # 4 Hours Timeout
                                    logs_to_write.append(("WARN", f"Worker {worker_id} timed out. Resetting.", jid))
                                    cursor.execute("UPDATE jobs SET status='queued', progress=0, worker_id=NULL, last_updated=?, started_at=NULL WHERE id=?", (now, jid))
                            except: pass

                    # Chunks: return silent ones to the pool so another worker picks them up
                    chunk_cutoff = now - timedelta(seconds=CHUNK_STALE_SECONDS)
                    cursor.execute("UPDATE chunks SET status='pending', worker_id=NULL, progress=0, last_updated=? WHERE status='processing' AND last_updated < ?", (now, chunk_cutoff))
                    if cursor.rowcount > 0:
                        logs_to_write.append(("WARN", f"Reset {cursor.rowcount} stale chunk(s) to pending.", None))

                    # Chunked jobs with every chunk completed but no final file yet
                    # (e.g. the manager restarted mid-assembly) — re-trigger assembly
                    cursor.execute("""SELECT j.id FROM jobs j
                                      WHERE COALESCE(j.chunked, 0)=1 AND j.status='processing'
                                        AND EXISTS (SELECT 1 FROM chunks c WHERE c.job_id = j.id)
                                        AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.job_id = j.id AND c.status != 'completed')""")
                    assembly_candidates = [r[0] for r in cursor.fetchall()]

                    # Backstop: a chunked job with zero chunk activity for 6h gets
                    # requeued (e.g. every chunk-capable worker left). No fail
                    # penalty and chunking stays allowed — nothing actually failed.
                    dead_cutoff = now - timedelta(hours=6)
                    cursor.execute("SELECT id FROM jobs WHERE COALESCE(chunked, 0)=1 AND status='processing' AND last_updated < ?", (dead_cutoff,))
                    for (jid,) in cursor.fetchall():
                        _requeue_job_whole(conn, jid, "no chunk activity for 6h",
                                           penalize=False, disable_chunking=False)

                    # Auto-requeue transiently-failed jobs (fail_count < 5) after
                    # a short cooldown. Previously a worker-reported 'failed' left
                    # the job dead until an admin manually clicked Reset Failed;
                    # /get_job only serves 'queued'. The fail_count<5 retry design
                    # was never actually wired up for this path.
                    failed_cutoff = now - timedelta(minutes=10)
                    cursor.execute(
                        "UPDATE jobs SET status='queued', progress=0, worker_id=NULL, started_at=NULL, last_updated=? "
                        "WHERE status='failed' AND COALESCE(fail_count, 0) < 5 AND COALESCE(chunked, 0)=0 AND last_updated < ?",
                        (now, failed_cutoff))
                    if cursor.rowcount > 0:
                        logs_to_write.append(("INFO", f"Auto-requeued {cursor.rowcount} transiently-failed job(s).", None))

                    # Prune the ever-growing system_logs table (nothing else does).
                    # Keep the most recent ~50k rows; the id index makes this cheap.
                    cursor.execute("SELECT MAX(id) FROM system_logs")
                    _max_log_id = cursor.fetchone()[0]
                    if _max_log_id and _max_log_id > 50000:
                        cursor.execute("DELETE FROM system_logs WHERE id < ?", (_max_log_id - 50000,))
                        if cursor.rowcount > 0:
                            logs_to_write.append(("INFO", f"Pruned {cursor.rowcount} old system_logs row(s).", None))

                    conn.commit()
                finally:
                    conn.close()
            for level, msg, jid in logs_to_write: log_event(level, msg, jid)
            for jid in assembly_candidates:
                _maybe_start_assembly(jid)

            # Prune quarantine (files older than 1 hour)
            sweep_quarantine()

            # Prune encode_logs (files older than 30 days)
            log_dir = os.path.join(os.getcwd(), "encode_logs")
            if os.path.exists(log_dir):
                cutoff_30d = time.time() - 30 * 86400
                for fname in os.listdir(log_dir):
                    fpath = os.path.join(log_dir, fname)
                    try:
                        if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff_30d:
                            os.remove(fpath)
                    except Exception: pass

        except Exception as e:
            print(f"[!] Maintenance error: {e}")
        time.sleep(60)

print("[*] Initializing Database...")
init_db()
# Sweep stale quarantine files from any previous crash
sweep_quarantine()
# FIXED: Run startup scan in thread to allow Gunicorn to bind immediately
threading.Thread(target=scan_and_queue, daemon=True).start() 
threading.Thread(target=maintenance_loop, daemon=True).start()
print(f"[*] Manager initialized and ready. (Service URL: {SERVER_URL_DISPLAY})")

if __name__ == '__main__':
    print(f"[*] Manager running at {SERVER_URL_DISPLAY}")
    print("[!] WARNING: Running in dev mode. Use 'gunicorn manager:app' for production.")
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)
