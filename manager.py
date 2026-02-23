import os
import time
import queue
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
from flask import Flask, request, jsonify, render_template, send_from_directory, send_file, Response, abort
from werkzeug.exceptions import HTTPException
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# ==============================================================================
# CONFIGURATION
# ==============================================================================

# [SECURITY] Minimum Worker Version to accept. 
# Workers older than this will be denied jobs until they auto-update.
MIN_CLIENT_VERSION = "2.8.0"

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

except ImportError:
    print("[!] Critical Error: config.py not found.")
    exit(1)

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

job_queue = queue.Queue()
queued_job_ids = set()
db_lock = threading.RLock()

# Cache for outdated worker logs to prevent spamming DB
OUTDATED_LOG_CACHE = {} 

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
        """Copies Disk DB to RAM at startup."""
        with db_lock:
            # Ensure disk file exists to copy
            if not os.path.exists(self.disk_path):
                open(self.disk_path, 'a').close()
            
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
        
        with db_lock:
            conn = db_handler.get_connection()
            conn.execute("INSERT INTO system_logs (timestamp, level, message, related_id) VALUES (?, ?, ?, ?)",
                         (datetime.now(), level, clean_msg, clean_id))
            conn.commit()
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

def is_version_sufficient(client_ver, min_ver):
    if not client_ver: return False
    try:
        c_parts = [int(x) for x in client_ver.split('.') if x.isdigit()]
        m_parts = [int(x) for x in min_ver.split('.') if x.isdigit()]
        return c_parts >= m_parts
    except:
        return False

def is_worker_banned(worker_id):
    if not worker_id: return False
    try:
        with open("banned_workers.txt", "r") as f:
            banned_list = [line.strip().lower() for line in f.readlines() if line.strip()]
            return worker_id.strip().lower() in banned_list
    except FileNotFoundError:
        return False

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
        if request.host not in target:
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
        if token is None: return f(*args, **kwargs)
        if token != WORKER_SECRET:
            return jsonify({"status": "error", "message": "Unauthorized Worker"}), 401
        return f(*args, **kwargs)
    return decorated

def verify_upload(filepath):
    try:
        cmd = ['ffprobe', '-v', 'error', '-print_format', 'json', '-show_streams', '-show_format', filepath]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return False, "FFprobe Error"
        
        data = json.loads(result.stdout)
        has_video = False
        for stream in data.get('streams', []):
            if stream['codec_type'] == 'video':
                if stream.get('codec_name') != 'av1': return False, "Invalid Codec (Not AV1)"
                if int(stream.get('height', 0)) != 480: return False, "Invalid Height"
                has_video = True
            elif stream['codec_type'] == 'audio':
                if stream.get('codec_name') != 'opus': return False, "Invalid Audio Codec"
                
        if not has_video: return False, "No Video Stream"
        return True, "Verified"
    except Exception as e:
        return False, str(e)

# ==============================================================================
# HYBRID SCANNER (STREAMING GENERATOR)
# ==============================================================================

def scan_remote_http(url, prefix="", depth=0):
    """
    Recursively scans an HTTP directory listing for video files.
    YIELDS results as they are found (Generator) to allow real-time queuing.
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

    try:
        links = re.findall(r'href=["\']([^"\'<>]+)["\']', r.text)
        
        for link in links:
            if link.startswith('?') or link.startswith('/') or link in ['../', './']: continue
            if "parent directory" in link.lower(): continue
            
            full_url = urljoin(url, link)
            
            if link.endswith('/'):
                time.sleep(0.2) # Tiny delay to prevent hammering
                # Recursively yield results from subdirectories
                yield from scan_remote_http(full_url, prefix=f"{prefix}{link}", depth=depth+1)
            elif any(link.lower().endswith(ext) for ext in VIDEO_EXTENSIONS):
                from urllib.parse import unquote
                clean_name = unquote(link)
                clean_id = f"{prefix}{clean_name}"
                
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
    """
    
    # --- Helper: Process a batch of files ---
    def process_batch(file_batch):
        if not file_batch: return
        
        count_new = 0
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
                        cursor.execute(
                            "INSERT INTO jobs (id, filename, status, last_updated, file_size, source_type, source_url, content_profile) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?)", 
                            (job_id, fname, datetime.now(), fsize, src_type, src_url, profile)
                        )
                        count_new += 1
                conn.commit()
            finally:
                conn.close()
        
        if count_new > 0:
            print(f"[*] Added {count_new} new files to Database...")

        # 2. Update Active Queue (Push to Workers)
        with db_lock:
            conn = db_handler.get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT id, filename, file_size, source_type, source_url FROM jobs WHERE status = 'queued'")
                for row in cursor.fetchall():
                    jid, fname, fsize, stype, surl = row
                    if jid not in queued_job_ids:
                        dl_link = ""
                        if stype == 'remote':
                            base_url = surl if surl else REMOTE_SOURCE_URL
                            if base_url:
                                dl_link = urljoin(base_url, quote(jid))
                        else:
                            dl_link = f"{SERVER_URL_DISPLAY.rstrip('/')}/download_source/{quote(jid, safe='/')}"
                        
                        if dl_link:
                            job_queue.put({ 
                                "id": jid, 
                                "filename": fname, 
                                "file_size": fsize, 
                                "download_url": dl_link 
                            })
                            queued_job_ids.add(jid)
            finally:
                conn.close()

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
        remote_batch = []
        # Iterate over the generator
        for r_id, r_name, r_size in scan_remote_http(REMOTE_SOURCE_URL):
            remote_batch.append((r_id, r_name, r_size, 'remote', REMOTE_SOURCE_URL))
            
            # Commit every 10 files so workers don't wait
            if len(remote_batch) >= 10:
                process_batch(remote_batch)
                remote_batch = []
        
        # Commit any remaining files
        if remote_batch:
            process_batch(remote_batch)
            
    print("[*] Scan complete.")

def get_series_list():
    try:
        # [FIX] Allow series listing even in hybrid mode
        if not os.path.exists(SOURCE_DIRECTORY): return []
        
        folders = sorted([d for d in os.listdir(SOURCE_DIRECTORY) if os.path.isdir(os.path.join(SOURCE_DIRECTORY, d))])
        mapping = {}
        
        if os.path.exists('series_names.json'):
            try:
                mapping = json.load(open('series_names.json', 'r'))
            except: pass
            
        return [{"id": i+1, "folder": f, "name": mapping.get(f, f)} for i, f in enumerate(folders)]
    except:
        return []

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
        
        conn.commit()
        conn.close()

# ==============================================================================
# ROUTES
# ==============================================================================

@app.route('/')
def dashboard(): return render_template('dashboard.html')

@app.route('/admin')
@limiter.limit("5 per minute") 
@requires_auth
def admin_panel(): return render_template('admin.html')

@app.route('/api/ping')
def api_ping(): return jsonify({"status": "pong"})

@app.route('/dl/worker')
def download_worker_script(): return send_file(WORKER_TEMPLATE_FILE, as_attachment=True, download_name='worker.py')

@app.route('/api/series')
def api_series_list(): return jsonify({"series": get_series_list()})

@app.route('/install')
def install_script():
    u = sanitize_input(request.args.get('username')) or 'Anonymous'
    w = sanitize_input(request.args.get('workername')) or 'LinuxNode'
    s_id = request.args.get('series_id', '') 
    if s_id and not s_id.isdigit(): s_id = ''
    j = request.args.get('jobs', '1')
    if not j.isdigit(): j = '1'
    script = f"""#!/bin/bash
if [ -x "$(command -v apt-get)" ]; then sudo apt-get update -qq && sudo apt-get install -y ffmpeg python3 python3-requests; fi
if [ -x "$(command -v dnf)" ]; then sudo dnf install -y ffmpeg python3 python3-requests; fi
curl -s "{SERVER_URL_DISPLAY.rstrip('/')}/dl/worker" -o worker.py
export WORKER_SECRET="{WORKER_SECRET}"
python3 worker.py --username "{u}" --workername "{w}" --jobs {j} --manager "{SERVER_URL_DISPLAY}" --series-id "{s_id}"
"""
    return Response(script, mimetype='text/x-shellscript')

@app.route('/download_source/<path:filename>')
def download_source(filename): 
    return send_from_directory(SOURCE_DIRECTORY, filename, as_attachment=True)

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
        with db_lock:
            conn = db_handler.get_connection(); conn.row_factory = sqlite3.Row
            try:
                c = conn.cursor()
                job = None
                
                for source_type in search_phases:
                    if job: break
                    
                    search_attempts = [series_id] if series_id and series_id.isdigit() else []
                    search_attempts.append(None) 
                    
                    for current_search_id in search_attempts:
                        folder_filter = None
                        if current_search_id:
                            for s in get_series_list():
                                if s['id'] == int(current_search_id):
                                    folder_filter = s['folder']; break
                        
                        params = [source_type]
                        query_parts = ["status='queued'", "source_type=?"]
                        
                        if max_size_mb and max_size_mb.isdigit():
                            query_parts.append("file_size <= ?")
                            params.append(int(max_size_mb) * 1024 * 1024)
                        if folder_filter:
                            query_parts.append("id LIKE ?")
                            params.append(f"{folder_filter}%")
                        
                        # [ADDED] content_profile to SELECT query
                        sql = f"SELECT id, filename, file_size, source_type, source_url, content_profile FROM jobs WHERE {' AND '.join(query_parts)} ORDER BY id ASC LIMIT 1"
                        c.execute(sql, tuple(params)); row = c.fetchone()
                        if row: job = dict(row); break
                
                if job:
                    if job['source_type'] == 'remote':
                        # Use stored URL if available, else fallback
                        base_url = job.get('source_url') if job.get('source_url') else REMOTE_SOURCE_URL
                        if base_url:
                            job['download_url'] = urljoin(base_url, quote(job['id']))
                        else:
                             # Fallback for old remote jobs if config is gone (might fail, but best effort)
                             job['download_url'] = "" 
                    else:
                         job['download_url'] = f"{SERVER_URL_DISPLAY.rstrip('/')}/download_source/{quote(job['id'], safe='/')}"

                    conn.execute("UPDATE jobs SET status='processing', worker_id=?, worker_version=?, last_updated=?, started_at=? WHERE id=?", 
                        (worker_id, worker_version, datetime.now(), datetime.now(), job['id']))
                    conn.commit()
                    return jsonify({"status": "ok", "job": job})
            finally:
                conn.close()
            return jsonify({"status": "empty"})
    except Exception as e:
        log_event("ERROR", f"get_job failed: {e}")
        return jsonify({"status": "error"}), 500

@app.route('/upload_result', methods=['POST'])
@requires_worker_auth
def upload_result():
    job_id = request.form.get('job_id')
    worker_id = sanitize_input(request.form.get('worker_id'))
    try:
        duration = int(float(request.form.get('duration', 0)))
    except:
        duration = 0
    
    if 'file' in request.files and job_id:
        new_filename = os.path.splitext(job_id)[0] + ".mp4"
        quarantine_dir = os.path.join("temp_uploads", "quarantine")
        os.makedirs(quarantine_dir, exist_ok=True)
        temp_path = os.path.join(quarantine_dir, f"{uuid.uuid4().hex}.mp4")
        request.files['file'].save(temp_path)
        
        is_valid, reason = verify_upload(temp_path)
        if not is_valid:
            log_event("WARN", f"Security: Upload rejected ({reason})", job_id)
            os.remove(temp_path)
            with db_lock:
                conn = db_handler.get_connection()
                conn.execute("UPDATE jobs SET status='failed', last_updated=? WHERE id=?", (datetime.now(), job_id))
                conn.commit(); conn.close()
            return jsonify({"status": "error", "message": reason}), 400

        save_path = os.path.abspath(os.path.join(COMPLETED_DIRECTORY, new_filename))
        if not save_path.startswith(os.path.abspath(COMPLETED_DIRECTORY)):
             os.remove(temp_path); return jsonify({"status": "error"}), 403

        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        shutil.move(temp_path, save_path) 

        with db_lock:
            conn = db_handler.get_connection()
            conn.execute("UPDATE jobs SET status='completed', progress=100, worker_id=?, last_updated=?, duration=? WHERE id=?", 
                (worker_id, datetime.now(), duration, job_id))
            conn.commit(); conn.close()
        
        # [CRITICAL] Immediate Sync to Disk
        db_handler.sync_to_disk()
        
        log_event("INFO", f"Job completed by {worker_id}", job_id)
        return jsonify({"status": "success"})
    return jsonify({"status": "error"}), 400

@app.route('/upload_log', methods=['POST'])
@requires_worker_auth
def receive_log():
    job_id = request.form.get('job_id')
    worker_id = sanitize_input(request.form.get('worker_id'))
    
    if 'log_file' in request.files and job_id:
        log_dir = os.path.join(os.getcwd(), "encode_logs")
        os.makedirs(log_dir, exist_ok=True)
        
        # Save the compressed file
        safe_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', job_id)
        log_path = os.path.join(log_dir, f"{safe_name}.log.gz")
        request.files['log_file'].save(log_path)
        
        warnings = []
        try:
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
            log_event("WARN", f"CHEATING DETECTED by {worker_id}: {warning_str}", job_id)
            with db_lock:
                conn = db_handler.get_connection()
                conn.execute("UPDATE jobs SET warnings=? WHERE id=?", (warning_str, job_id))
                conn.commit()
                conn.close()
                
        return jsonify({"status": "success"})
    return jsonify({"status": "error"}), 400

@app.route('/report_status', methods=['POST'])
@requires_worker_auth
def report_status():
    d = request.json; status = d.get('status')
    worker_id = sanitize_input(d.get('worker_id'))
    worker_version = sanitize_input(d.get('version'))
    
    if status == 'completed': return jsonify({"status": "ignored"}), 403
    if status == 'failed':
        err_msg = d.get('error', 'Unknown Error')
        log_event("WARN", f"Worker {worker_id} (v{worker_version}) reported failure: {err_msg}", d.get('job_id'))

    with db_lock:
        conn = db_handler.get_connection()
        try:
            sql = "UPDATE jobs SET status=?, worker_id=?, progress=?, last_updated=? WHERE id=?"
            params = [status, worker_id, d.get('progress',0), datetime.now(), d.get('job_id')]
            if d.get('duration', 0) > 0: 
                sql = "UPDATE jobs SET status=?, worker_id=?, progress=?, last_updated=?, duration=? WHERE id=?"
                params.insert(4, d.get('duration'))
            conn.execute(sql, tuple(params)); conn.commit()
        finally:
            conn.close()
    return jsonify({"status": "received"})

@app.route('/api/stats')
def api_stats():
    filter_val = request.args.get('filter')
    time_filter = " AND last_updated > datetime('now', '-1 day')" if filter_val == '24h' else " AND last_updated > datetime('now', '-30 days')" if filter_val == '30d' else ""

    with db_lock:
        conn = db_handler.get_connection(); conn.row_factory = sqlite3.Row
        try:
            c = conn.cursor()
            c.execute(f"SELECT CASE WHEN instr(worker_id, '-') > 0 THEN substr(worker_id, 1, instr(worker_id, '-') - 1) ELSE worker_id END as worker_id, SUM(duration) as total_minutes, COUNT(*) as files_count FROM jobs WHERE status='completed' AND worker_id IS NOT NULL {time_filter} GROUP BY 1 ORDER BY total_minutes DESC")
            sb = [dict(r) for r in c.fetchall()]
            
            c.execute("SELECT COALESCE(worker_id, 'Pending...') as worker_id, filename, duration, progress, status FROM jobs WHERE status IN ('processing', 'downloading', 'uploading')")
            act = [dict(r) for r in c.fetchall()]
            
            c.execute("SELECT id, status, worker_id FROM jobs ORDER BY last_updated DESC LIMIT 20")
            hist = [dict(r) for r in c.fetchall()]
            
            c.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'")
            queue_depth = c.fetchone()[0]
            
            c.execute("SELECT id, filename, file_size FROM jobs WHERE status='queued' ORDER BY id ASC LIMIT 50")
            queue_items = [dict(r) for r in c.fetchall()]

            c.execute("SELECT COUNT(*) FROM jobs")
            total_count = c.fetchone()[0]
        finally:
            conn.close()
    return jsonify({"scoreboard": sb, "active": act, "history": hist, "queue_depth": queue_depth, "queue_items": queue_items, "total_jobs": total_count})

@app.route('/api/all_jobs')
@requires_auth
def api_all_jobs():
    with db_lock:
        conn = db_handler.get_connection(); conn.row_factory = sqlite3.Row
        try:
            c = conn.cursor()
            c.execute("SELECT id, status, worker_id, worker_version, last_updated, warnings FROM jobs ORDER BY last_updated DESC")
            jobs = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
        return jsonify({"jobs": jobs})

@app.route('/api/logs')
@requires_auth
def get_logs():
    limit = request.args.get('limit', 100)
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
    data = request.json; job_id = data.get('job_id'); action = data.get('action')
    log_event("WARN", f"Admin performed '{action}' on job", job_id)
    
    with db_lock:
        conn = db_handler.get_connection(); c = conn.cursor()
        try:
            if action == 'delete':
                c.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            elif action == 'retry':
                c.execute("UPDATE jobs SET status='queued', progress=0, worker_id=NULL, last_updated=? WHERE id=?", (datetime.now(), job_id))
            elif action == 'retry_all_failed':
                c.execute("UPDATE jobs SET status='queued', progress=0, worker_id=NULL, last_updated=? WHERE status='failed'", (datetime.now(),))
            elif action == 'clear_stale':
                cutoff = datetime.now() - timedelta(minutes=10)
                c.execute("UPDATE jobs SET status='queued', progress=0, worker_id=NULL, last_updated=?, started_at=NULL WHERE status IN ('processing', 'downloading', 'uploading') AND last_updated < ?", (datetime.now(), cutoff))
            elif action == 'archive_history':
                ts = int(time.time())
                c.execute("SELECT id FROM jobs WHERE status='completed' AND id NOT LIKE 'HISTORY_%'")
                rows = c.fetchall()
                for (jid,) in rows:
                    new_id = f"HISTORY_{ts}_{jid}"
                    c.execute("UPDATE jobs SET id = ? WHERE id = ?", (new_id, jid))
                log_event("WARN", f"Admin archived {len(rows)} jobs.")
            elif action == 'purge_queue':
                c.execute("DELETE FROM jobs WHERE status='queued'")
                log_event("WARN", "Admin PURGED the queue. Rescan triggered (background).")
                
            conn.commit()
        finally:
            conn.close()
            
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

def maintenance_loop():
    while True:
        try:
            logs_to_write = [] 
            with db_lock:
                conn = db_handler.get_connection(); cursor = conn.cursor()
                try:
                    now = datetime.now()
                    cursor.execute("SELECT id, filename, last_updated, worker_id FROM jobs WHERE status IN ('processing', 'downloading', 'uploading')")
                    for row in cursor.fetchall():
                        jid, fname, last_up, worker_id = row
                        if last_up:
                            try:
                                l_time = datetime.strptime(str(last_up).split('.')[0], "%Y-%m-%d %H:%M:%S")
                                if (now - l_time).total_seconds() > 14400: # 4 Hours Timeout (Increased from 2hr)
                                    logs_to_write.append(("WARN", f"Worker {worker_id} timed out. Resetting.", jid))
                                    cursor.execute("UPDATE jobs SET status='queued', progress=0, worker_id=NULL, last_updated=?, started_at=NULL WHERE id=?", (now, jid))
                            except: pass 
                    conn.commit()
                finally:
                    conn.close()
            for level, msg, jid in logs_to_write: log_event(level, msg, jid)
        except Exception as e:
            print(f"[!] Maintenance error: {e}")
        time.sleep(60)

print("[*] Initializing Database...")
init_db()
# FIXED: Run startup scan in thread to allow Gunicorn to bind immediately
threading.Thread(target=scan_and_queue, daemon=True).start() 
threading.Thread(target=maintenance_loop, daemon=True).start()
print(f"[*] Manager initialized and ready. (Service URL: {SERVER_URL_DISPLAY})")

if __name__ == '__main__':
    print(f"[*] Manager running at {SERVER_URL_DISPLAY}")
    print("[!] WARNING: Running in dev mode. Use 'gunicorn manager:app' for production.")
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)
