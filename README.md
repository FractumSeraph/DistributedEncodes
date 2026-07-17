# Fractum Distributed Encoder

A self-hosted distributed video encoding system. A central **manager** server holds a queue of source video files. Any number of **worker** machines connect over HTTP, stream a source file directly into FFmpeg, encode it to AV1, and upload the result back. No shared drives or VPNs required — just an internet connection.

Encoded output is intended for [https://vsv.fractumseraph.net/](https://vsv.fractumseraph.net/).

---

## How It Works

```
[ Source Files on Manager ]  →  Worker streams via HTTP  →  FFmpeg (AV1 / SVT-AV1 preset 2)
                                                         →  Upload result to Manager
                                                         →  Manager saves to completed_media/
```

- Workers **stream** the source directly into FFmpeg — no waiting for a full multi-GB download before encoding starts.
- Workers self-update automatically when the manager has a newer version of `worker_template.py`.
- Workers send structured error reports to the manager if something goes wrong.
- All communication is authenticated with a shared `WORKER_SECRET` token.

---

## Running a Worker

### Worker Flags

| Flag | Default | Description |
|---|---|---|
| `--manager URL` | configured default | URL of the manager server |
| `--username NAME` | `Anonymous` | Display name for the scoreboard |
| `--workername NAME` | `Node-<timestamp>` | Identifier for this machine |
| `--jobs N` | `1` | Number of parallel encode threads |
| `--series-id N` | *(all)* | Lock this worker to a specific series |
| `--secret TOKEN` | `$WORKER_SECRET` env | Override the auth token |
| `--daily-quota GB` | `0` (unlimited) | Cap total data downloaded per day |
| `--watermark` | off | Burn `@FractumSeraph` text into the video |
| `--no-tui` | off | Plain terminal output instead of the TUI |
| `--force-tui` | off | Force the TUI on even when auto-detection disables it |
| `--max-size-mb N` | `0` (no limit) | Skip source files larger than N MB |
| `--local-source DIR` | *(none)* | Read source files directly from disk instead of HTTP — useful when the worker runs on the same machine as the manager |
| `--no-chunks` | off | Opt out of chunked encoding — always take whole files |
| `--wallet ADDR` | *(none)* | FractumCoin wallet address — verified uploads are credited to it for later payout (saved to `worker_config.json`) |

The `WORKER_SECRET` environment variable is the preferred way to pass the auth token.

---

### Option 1: Docker (Recommended)

The easiest method. Handles all dependencies (Python, FFmpeg, etc.) automatically.

**From the repository:**
```bash
cd docker
# Edit docker-compose.yml to set your username/workername, then:
docker-compose up -d
```

**Without the repository** — create `docker-compose.yml`:
```yaml
version: '3.8'
services:
  fractum-worker:
    image: python:3.11-slim-bookworm
    container_name: fractum_worker_node
    restart: unless-stopped
    stop_grace_period: 30s
    entrypoint: ["/bin/sh", "-c",
      "apt-get update && apt-get install -y ffmpeg curl && pip install requests textual &&
       curl -fsSL -o worker.py https://encode.fractumseraph.net/dl/worker &&
       exec python worker.py \"$@\"", "--"]
    command: >
      --manager "https://encode.fractumseraph.net/"
      --username "DockerUser"
      --workername "DockerNode"
      --jobs 1
      --no-tui
```

> **Tip:** Add `--series-id X` to the `command` block to lock the container to one series. Uncomment the `tmpfs` section to write temp files to RAM (~3 GB per job) and reduce SSD wear.

---

### Option 2: Linux / WSL (One-liner)

Installs FFmpeg and Python automatically, then starts the worker:

```bash
curl -s "https://encode.fractumseraph.net/install?username=YourName&workername=LinuxNode&jobs=1" | bash
```

Append `&series_id=X` to focus on a specific series.

---

### Option 3: Windows (Manual)

1. **Install Python 3.11+** from [python.org](https://www.python.org/).  
   During installation, check **"Add Python to PATH"**.

2. **Install dependencies:**
   ```powershell
   pip install requests textual
   ```
   > `textual` (the TUI library) is installed automatically on first run if it is not already present. You can pre-install it with `pip install textual` if you prefer.

3. **Download the worker:**  
   [https://encode.fractumseraph.net/dl/worker](https://encode.fractumseraph.net/dl/worker)

4. **Run it:**
   ```powershell
   python worker.py --manager "https://encode.fractumseraph.net/" --username "MyName" --workername "MyPC" --jobs 1
   ```

   > FFmpeg is downloaded automatically (~40 MB portable build) if it is not found on the system.

---

### Option 4: Linux Systemd Service

Use `fractum-worker.service` to run the worker as a persistent background service that restarts on crash and on reboot.

1. Edit `fractum-worker.service` — set `User`, `WorkingDirectory`, and the flags in `ExecStart`.
2. Copy and enable:
   ```bash
   sudo cp fractum-worker.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now fractum-worker
   ```

---

### TUI Controls

When running with the Textual TUI (default — installed automatically on first run if not already present):

| Key | Action |
|---|---|
| `P` or `Ctrl+C` | Pause — suspends all FFmpeg processes and shows a menu in the log |
| `Q` | Quit immediately (kills active encodes) |

> The TUI works over SSH and inside tmux. Key input is read directly from `/dev/tty` as a fallback when the terminal environment does not pass key events through normally.

**While paused**, the log panel shows the available choices:

| Key | Behaviour |
|---|---|
| `C` | Resume encoding from where it was paused |
| `F` | Finish the current job, then stop |
| `S` | Kill all encodes and exit immediately |

Pressing `P` or `Ctrl+C` a second time while already paused acts as an immediate force-stop.

FFmpeg is frozen at the OS level during a pause (`NtSuspendProcess` on Windows, `SIGSTOP` on Linux), so CPU usage drops to zero.

**Table columns:** Worker · Current File · Phase · Progress · Elapsed · Done · ETA

**Stats bar** (below the table): shows session totals — jobs completed, gigabytes uploaded, uptime, and quota remaining (if `--daily-quota` is set).

---

## Hosting the Manager

### Prerequisites

- **OS:** Ubuntu 22.04 / 24.04 LTS (recommended)
- **Python:** 3.11+
- **pip packages:** see `requirements.txt`
- **FFmpeg:** required on the server for upload verification
- **Network:** A public IP or domain, or local LAN

---

### 1. Install

```bash
git clone https://github.com/FractumSeraph/DistributedEncodes.git
cd DistributedEncodes
pip3 install -r requirements.txt
```

---

### 2. Configure

Copy the example config and edit it:

```bash
cp config.py.example config.py
nano config.py
```

Key settings:

```python
# Public URL that workers and browsers use to reach this server
SERVER_URL_DISPLAY = "https://encode.yourdomain.com/"

# Folder the manager scans for source video files
SOURCE_DIRECTORY = "./source_media"

# Folder where completed encodes are saved
COMPLETED_DIRECTORY = "./completed_media"

# Admin panel credentials
ADMIN_USER = "admin"
ADMIN_PASS = "ChangeMeToSomethingSecure"

# Shared token all workers must present — generate a long random string
WORKER_SECRET = "ChangeThisToALongRandomString"

# Optional: scan an HTTP directory for remote source files
# REMOTE_SOURCE_URL = "http://192.168.1.100:8080/"

# Database mode: 'disk' (default) or 'ram' (Linux only, fastest)
DB_MODE = 'disk'
```

All available options are documented in `config.py.example`.

---

### 3. Run the Server

**Development (single machine, testing only):**
```bash
python3 manager.py
```

**Production (Gunicorn):**
```bash
gunicorn --workers 1 --threads 8 --bind 0.0.0.0:5000 manager:app
```

> **Important:** Always use `--workers 1`. The job queue lives in memory; multiple Gunicorn workers would each have separate queues and cause duplicate job assignments.

**Systemd service:**

The included `distributed-encodes.service` file can be used for automatic startup:

```bash
# Edit WorkingDirectory to match your clone path, then:
systemctl --user enable --now distributed-encodes.service
loginctl enable-linger $USER
```

Or copy to `/etc/systemd/system/` for a system-wide service.

---

### 4. Add Source Files

Drop video files (`.mkv`, `.mp4`, `.avi`, `.mov`) into the `source_media/` folder.  
The manager scans on startup and can be rescanned manually from the admin panel.

**Organizing by series:**  
Put episodes inside a subfolder: `source_media/ShowName/ep01.mkv`.  
Workers can be locked to a series with `--series-id N` (series IDs are shown in the dashboard).

**Remote sources:**  
Set `REMOTE_SOURCE_URL` in `config.py` to an HTTP directory listing URL. The manager will scan it and workers will stream from that URL directly — the manager server never downloads the file itself.

---

### 5. Admin Panel

Navigate to `https://your-domain.com/admin` (HTTP Basic Auth — credentials from `config.py`).

| Feature | Description |
|---|---|
| Job Registry | Filter by status (failed / active / queued / done), search by worker or filename |
| LOG button | Download the raw FFmpeg `.log.gz` for any job |
| Error Reports | Structured error log sent by workers — includes FFmpeg output or Python traceback |
| System Logs | Real-time event log with ERROR / WARN / INFO filter |
| Config | Set or change the Remote Source URL without restarting |
| Prune Dead Workers | Reset jobs stalled for more than 10 minutes |
| Reset Failed | Bulk-requeue all failed jobs |
| Scan Files | Manually trigger a source directory scan |
| Archive History | Rename completed jobs so they can be re-encoded |
| Purge Queue | Delete all queued jobs (files are re-queued on next scan) |
| Clear Errors | Remove all error reports |

---

## Encoding Settings

All encoding is done by the worker. Settings are baked into `worker_template.py`:

| Parameter | Value |
|---|---|
| Video codec | `libsvtav1` (SVT-AV1) |
| Preset | `2` (high quality, slow) |
| CRF | `63` standard / `57` live-action |
| Resolution | 480p (scale to width, keep aspect) |
| Audio codec | Opus, mono, 24kbps |
| Container | `.mp4` |

The manager detects a `live_action` content profile and tells the worker to reduce CRF by 6 (allocating ~2x bitrate).

---

## Chunked Encoding (many workers, one video)

By default the manager splits long videos into ~5-minute **chunks** so that multiple workers — or multiple cores on one machine via `--jobs N` — encode a *single* video in parallel, instead of each worker grabbing a different video. The swarm finishes one file at a time.

**How it works:**

1. When a worker asks for work (`/get_chunk`), the manager probes the next queued video and splits it into time ranges. Each range becomes a video-only chunk; the audio track (plus subtitles) becomes one extra chunk encoded by a single worker.
2. Workers encode their slice with the exact same settings (SVT-AV1, preset 2, same CRF) using an accurate `-ss` seek on the streamed source — no full download needed.
3. Uploaded chunks are verified with `ffprobe` (codec, resolution, expected duration) and cheat-checked from the FFmpeg log, same as whole files.
4. When the last chunk arrives, the manager **concatenates the chunks with a lossless stream copy** (`-c copy`) and muxes in the audio, then runs the normal upload verification.

**Quality / size impact:** effectively none. Encoding is CRF-based (constant quality, not bitrate-targeted), so per-chunk encoding produces the same quality as a single pass. The only overhead is one extra keyframe at each chunk boundary — SVT-AV1 already places keyframes every few seconds, so the size difference is negligible. The final concat is a bit-exact stream copy.

**Fallbacks & safety:**

- Videos shorter than ~1.5× the chunk length, VFR sources, and sources whose audio/video streams start at different offsets are encoded whole via the classic path.
- If a chunk fails 3 times, or assembly fails, the job automatically falls back to whole-file encoding.
- Chunks with no heartbeat for 30 minutes are handed to another worker; completed chunks are never lost when a worker dies (only the in-flight chunk is redone). If a split job sees no chunk activity at all for 6 hours (e.g. every chunk-capable worker left), it is returned to the normal queue without penalty.
- Old workers and the browser-based web worker keep using `/get_job` untouched.
- Watermarks (`--watermark`) are skipped on chunks so the final video is consistent.

**Config** (`config.py`, both optional):

```python
CHUNKED_ENCODING = True     # set False to disable splitting entirely
CHUNK_DURATION_SEC = 300    # target chunk length in seconds
```

> **Note:** the manager temporarily stores uploaded chunks in `chunk_store/` until assembly — keep roughly one encoded video's worth of free disk per active chunked job.

---

## FractumCoin Rewards

Workers can attach a FractumCoin wallet address with `--wallet ADDR` (or `&wallet=ADDR` on the `/install` one-liner). The manager keeps an append-only **earnings ledger**: one row per *verified* upload, credited in **minutes of source video encoded**.

- A whole-file upload earns the encode's duration, measured by the manager's own `ffprobe` of the delivered file (not the worker's claim).
- A video chunk earns its slice's minutes; the audio helper chunk earns 0, so a chunked video pays out exactly its real length across contributors.
- Ledger rows are never rewritten by retries, archives, or chunk cleanup, and re-uploads to an already-completed job earn nothing — the payout record is stable.
- The public scoreboard is a view of the same ledger, so score = minutes of successful encodes uploaded, identical between chunked and whole-file work. Existing history is backfilled into the ledger on first startup (with no wallet attached).

**Payouts:** `GET /api/earnings` (admin auth) returns per-wallet totals (`total_minutes`, `unpaid_minutes`, upload counts, first/last activity) plus recent ledger rows. Work uploaded without a wallet appears under `(no wallet)`. A `paid` flag exists on every row for future payout tooling.

---

## Utility Scripts

### `maintenance_tool.py`
Interactive CLI for admin actions. Run from the same directory as `config.py`:
```bash
python3 maintenance_tool.py
```
Options: Archive History, Purge Queue.

### `reset_series.py`
Resets all jobs matching a series name back to `queued` so they re-encode:
```bash
python3 reset_series.py "ShowName"
```

### `update.sh` / `update.ps1`
Pulls the latest code from git and restarts the service. Run on the manager host.

---

## Architecture Overview

```
                ┌─────────────────────────────────┐
                │        Manager (Flask)          │
                │  – Job queue (SQLite)           │
                │  – /get_job  /get_chunk         │
                │  – /download_source/<file>      │
                │  – /upload_result /upload_chunk │
                │  – /report_status /report_chunk │
                │  – /report_error                │
                │  – /admin  (dashboard)          │
                └────────────┬────────────────────┘
                             │  HTTP
          ┌──────────────────┼──────────────────┐
          │                  │                  │
   ┌──────┴──────┐   ┌───────┴─────┐   ┌───────┴─────┐
   │  Worker A   │   │  Worker B   │   │  Worker C   │
   │ (Linux)     │   │ (Windows)   │   │ (Docker)    │
   └─────────────┘   └─────────────┘   └─────────────┘
```

- **Job lifecycle:** `queued` → `processing` → `completed` / `failed` / `permanently_failed`
- **Chunked jobs** additionally track per-chunk state in a `chunks` table (`pending` → `processing` → `completed`), and the job completes when the manager assembles the chunks.
- **Upgrades are queue-safe:** all database changes are additive (`CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ADD COLUMN`), so updating the manager never drops or rewrites the existing job queue.
- **Permanent failures:** jobs that fail 5 times are marked `permanently_failed` and excluded from the queue. An admin can re-enable them via *Reset Failed* in the admin panel.
- **Stale jobs** (no heartbeat for 4 hours) are automatically reset to `queued` by the maintenance loop.
- **Upload verification:** The manager runs `ffprobe` on every uploaded file and rejects anything that isn't AV1 at 480p.
- **Cheating detection:** The manager parses the uploaded FFmpeg log to verify the correct codec and preset were used.

---

## Security Notes

- Set a strong `WORKER_SECRET` and `ADMIN_PASS` in `config.py` before exposing the server publicly.
- Put the server behind a reverse proxy (nginx / Caddy) with HTTPS in production.
- `ADMIN_PASS` is used for HTTP Basic Auth on the `/admin` route. Do not reuse a password you use elsewhere.
- Workers are validated against a minimum version (`MIN_CLIENT_VERSION` in `manager.py`). Outdated workers are denied jobs until they auto-update.
