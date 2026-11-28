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
| `--no-auto-update` | off | Never replace the worker script with a newer version from the manager; the worker only says when one is available (download `<manager>/dl/worker` over the script yourself). The manager still turns away workers older than its `MIN_CLIENT_VERSION`. Updates that are installed are always checked against the project's signing key (see Security Notes) |
| `--ffmpeg PATH` | *(worker's own build)* | Use this FFmpeg instead of the worker's own build (see below); `ffprobe` is taken from the same folder |
| `--wallet ADDR` | *(none)* | FractumCoin wallet address — verified uploads are credited to it for later payout (saved to `worker_config.json`) |

The `WORKER_SECRET` environment variable is the preferred way to pass the auth token.

**Which FFmpeg the worker uses.** On Windows and Linux, on x64 and ARM64 (including native Windows-on-ARM builds), the worker keeps its own copy of the newest FFmpeg *release* build from BtbN, unless an FFmpeg already on the machine is just as good (see below). New FFmpeg releases often encode faster or better, and builds found on a machine vary (development snapshots, old distro packages, different HTTPS libraries; one such build hung on every source it opened).

- **Newest first, but only if it works here.** Before a build is used, the worker runs a one-second test encode with SVT-AV1 and Opus on that machine and reads it back. A build that fails (missing encoder, CPU or OS it cannot run on) is discarded and the next newest release is tried; the build already in use is kept until a replacement passes.
- **Found even when GitHub's list is unavailable.** New release branches are discovered from BtbN's asset list, or, when that cannot be read, by checking whether the next version numbers exist.
- **An FFmpeg already on the machine is used if it is just as good** (worker 3.16.0+), so nothing is downloaded. The worker finds one in its own folder or on the `PATH` when it has no build of its own yet, and only trusts it if it:
  1. is at least as new as the newest release (a development snapshot is placed by its libavformat version);
  2. passes the same test encode as the worker's own builds;
  3. reads a real source file from the manager the way an encode does, over HTTPS and seeking into the file, within 2 minutes for ffprobe and 3 for the read (this is the test that catches builds that hang on every source; a test encode alone does not).

  If the read fails while the worker itself downloads the same file fine, the build is remembered as bad (in `.ffmpeg_system.json`, until that file changes) and not tested again. If the test cannot run (the manager is older than 3.16.0's endpoint, or the network is down), the worker installs its own build as before. A trusted build is replaced by the worker's own when a newer release comes out (checked daily), or when it gets stuck on a source the worker can download.
- **Kept current while running.** The worker checks at start and then once a day. A newer release branch is installed as soon as it is found; a rebuild of the same branch (point releases, fixes) at most once a week. Each build lives in its own folder under `ffmpeg_builds/`, and switching only changes which build *new* encodes use, so encodes already running finish undisturbed.
- **Your choice wins.** `--ffmpeg PATH` uses a specific build instead; it must have `libsvtav1` and `libopus` and be FFmpeg 7.1 or newer. If no managed build can be installed, the worker falls back to an FFmpeg found on the machine, says why, and keeps trying to install its own while it runs: every 6 hours, and within the hour after that fallback build gets stuck on a source the worker itself can download (worker 3.15.0+). Source-read error reports name which FFmpeg was in use and why (its own build, a tested one found on the machine, `--ffmpeg`, or a fallback plus the install error).
- **macOS** has no BtbN build, so the worker uses the installed FFmpeg (`brew install ffmpeg`; keep it current with `brew upgrade ffmpeg`).

Encoding settings are the same whichever build is used.

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

Installs FFmpeg and Python (the only step that uses `sudo`), then starts the worker as your own user in `~/FractumWorker`. Re-running the same line later picks up that folder again, including the saved worker name and any partial encode. Set `FRACTUM_DIR` to use a different folder.

```bash
curl -s "https://encode.fractumseraph.net/install?username=YourName&workername=LinuxNode&jobs=1" | bash
```

Append `&series_id=X` to focus on a specific series.

---

### Option 3: Windows (One-liner)

Open **PowerShell** (no admin needed) and paste:

```powershell
irm https://encode.fractumseraph.net/install/windows | iex
```

The script asks for a username and a worker name (press Enter to use the computer's name), installs Python 3.11 per-user if it is missing, downloads the worker into `%USERPROFILE%\FractumWorker`, and starts it. The worker's own FFmpeg build (~130 MB) and the TUI library are fetched automatically on first run.

- Re-run the same line any time to update and restart the worker — the identity saved in `worker_config.json` is reused, so it only asks once.
- Append `?jobs=2` or `?series_id=X` (combine with `&`) to the URL to run more parallel jobs or focus on one series.

<details>
<summary><b>Manual setup</b> (if you'd rather not pipe a script into your shell)</summary>

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

   > The worker downloads its own FFmpeg build (~130 MB) on first run, even if another FFmpeg is installed (see "Which FFmpeg the worker uses" above).

</details>

---

### Option 4: Linux Systemd Service

Use `fractum-worker.service` to run the worker as a persistent background service that restarts on crash and on reboot.

1. Run the Linux one-liner from the dashboard once. It creates `~/FractumWorker` with `worker.py` in it. Stop it (Ctrl+C, then `s`).
2. Edit `fractum-worker.service` — set `User`, `WorkingDirectory` (that folder), and the flags in `ExecStart`.
3. Copy and enable:
   ```bash
   sudo cp fractum-worker.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now fractum-worker
   ```

---

### Option 5: Kaggle Notebook (free cloud CPU)

[`kaggle_worker.ipynb`](kaggle_worker.ipynb) runs the worker on a free Kaggle session for up to ~12 hours per run.

1. On Kaggle: **Create → New Notebook → File → Import Notebook** and upload `kaggle_worker.ipynb`.
2. In the notebook sidebar: *Internet* → **On**, *Accelerator* → **None** (the encoder is CPU-only — a GPU adds nothing and burns your weekly GPU quota).
3. Edit the **CONFIG** cell — username, worker name, jobs, optional extra flags.
4. **Save Version → Save & Run All (Commit)** — the worker runs headless and stops itself gracefully before Kaggle's 12 h session limit. You can close the browser.
5. Re-run the notebook — or schedule it (*Notebook → Schedule a notebook run*) — to keep contributing.

The notebook runs the worker with `--no-tui` and samples progress lines once a minute so the saved log stays small, keeps temp files out of the size-capped `/kaggle/working`, and skips manual FFmpeg installs entirely (the worker downloads its own `libsvtav1`-capable static build). An optional `WORKER_SECRET` can be supplied via Kaggle's *Add-ons → Secrets* instead of being hardcoded.

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
| Source Read Health | Per machine: source-read failures vs finished work (24h), the worker's and the manager's download tests, and a verdict. **Test source from manager** downloads a little of a source right now |
| System Logs | Real-time event log with ERROR / WARN / INFO filter |
| Config | Set or change the Remote Source URL without restarting |
| Prune Dead Workers | Reset jobs stalled for more than 10 minutes |
| Reset Failed | Bulk-requeue all failed jobs |
| Scan Files | Manually trigger a source directory scan |
| Archive History | Rename completed jobs so they can be re-encoded |
| Purge Queue | Delete all queued jobs (files are re-queued on next scan) |
| Clear Errors | Remove all error reports |

#### Diagnosing "Could not read the source"

When FFmpeg cannot read a source, a worker (3.12.0 or newer) adds to its error report:

- **where FFmpeg stopped**: still opening the file, or partway through the encode (and how far);
- **its FFmpeg build** and the TLS library it uses for HTTPS;
- **a download test from the worker's own machine**: the same file, from the point where the encode needed it, with the HTTP status, time to first byte and speed (or the error). This test also runs at the first failed attempt, so its result shows in the worker's window long before the final report.

When the report arrives, the manager downloads the same part of the same file itself (a few MB). Comparing the two gives the verdict shown in **Source Read Health** and in the report's details:

| Worker test | Manager test | Verdict |
|---|---|---|
| fails | works | the worker's network, firewall, DNS or ISP route |
| fails | fails | the source host was down or refusing requests |
| works, but FFmpeg never opened the file | — | the worker's FFmpeg build: the verdict says whether it was the worker's own build, one chosen with `--ffmpeg`, or a fallback (and why the worker's own could not be installed) |
| works, but slowly | — | a slow or congested link on the worker's side |
| works | — | the connection dropped mid-encode and did not recover |

Reports from older workers carry only the manager's test, which still shows whether the source host was up at the time.

A failed source read is not held against the job (so an outage cannot push jobs to permanently failed). Instead, when the **same machine** fails to read the same job's source twice, that machine is not given the job again for 12 hours, so another worker takes it. This does not apply while the source host is marked as down. A worker running with `--local-source` reports a failure of its own copy as "Could not read the local source file" (worker 3.13.1+); those failures do not count toward the source-outage pause, since the source host is not involved.

---

## Encoding Settings

All encoding is done by the worker. Settings are baked into `worker_template.py`:

| Parameter | Value |
|---|---|
| Video codec | `libsvtav1` (SVT-AV1) |
| Preset | `2` (high quality, slow) |
| CRF | `57` (every video) |
| Resolution | 480p (scale to width, keep aspect) |
| Audio codec | Opus, mono, 24kbps |
| Container | `.mp4` |

Every video uses the same CRF. (Older workers chose 63 or 57 from a per-job content profile; the manager now marks every job so they use 57 too.)

---

## Chunked Encoding (many workers, one video)

By default the manager splits long videos into **chunks** of at most `CHUNK_DURATION_SEC` seconds (the shipped config uses 120s) so that multiple workers — or multiple cores on one machine via `--jobs N` — encode a *single* video in parallel, instead of each worker grabbing a different video. The swarm finishes one file at a time.

**How it works:**

1. When a worker asks for work (`/get_chunk`), the manager probes the next queued video and splits it into time ranges. Each range becomes a video-only chunk; the audio track (plus subtitles) becomes one extra chunk encoded by a single worker.
2. Workers encode their slice with the exact same settings (SVT-AV1, preset 2, same CRF) using an accurate `-ss` seek on the streamed source — no full download needed.
3. Uploaded chunks are verified with `ffprobe` (codec, resolution, expected duration) and cheat-checked from the FFmpeg log, same as whole files.
4. When the last chunk arrives, the manager **concatenates the chunks with a lossless stream copy** (`-c copy`) and muxes in the audio, then runs the normal upload verification.

**Quality / size impact:** effectively none. Encoding is CRF-based (constant quality, not bitrate-targeted), so per-chunk encoding produces the same quality as a single pass. The only overhead is one extra keyframe at each chunk boundary — SVT-AV1 already places keyframes every few seconds, so the size difference is negligible. The final concat is a bit-exact stream copy.

**Probe once, reuse everywhere:** when the manager probes a source (to plan the split), it records the stream layout — the chosen audio track, its channel count, and any subtitle tracks — on the job. Workers are then handed that layout with their chunk/job and **skip their own `ffprobe`**. This matters most on constrained nodes: a Raspberry Pi range-streaming a large MKV over HTTP could otherwise sit on "Probe" for minutes (and, for MKVs whose index sits at the end of the file, occasionally time out and retry). Workers still fall back to probing themselves if the manager hasn't recorded a layout yet (older manager, or a job that never went through a split attempt). Fully backward compatible — the field is additive and ignored by old workers.

**Fallbacks & safety:**

- Videos shorter than ~1.5× the chunk length, VFR sources, and sources whose audio/video streams start at different offsets are encoded whole via the classic path.
- If a chunk fails 3 times, or assembly fails, the job automatically falls back to whole-file encoding.
- Chunks with no heartbeat for 30 minutes are handed to another worker; completed chunks are never lost when a worker dies (only the in-flight chunk is redone). If a split job sees no chunk activity at all for 6 hours (e.g. every chunk-capable worker left) but already has completed chunks banked, it is **held** — in-flight chunks return to pending and the job waits for chunk workers — for up to 3 such cycles before being returned to the normal queue.
- **Completed chunk uploads survive requeues.** Whenever a chunked job is sent back to the queue (stall backstop, 3-strikes fallback, admin Retry), the verified chunk files stay on disk alongside a plan manifest. The chunk plan is deterministic, so if the job is ever split again, those chunks are restored as completed instantly and only the missing ones re-encode — hours of volunteer work are never thrown away. Files are cleaned up when the job finally completes (assembled or whole), is deleted, or permanently fails; a manifest that no longer matches (e.g. `CHUNK_DURATION_SEC` changed) is discarded rather than reused.
- Old workers keep using `/get_job` untouched. The browser worker takes video chunks too (via pre-cut segments — see below), and falls back to `/get_job` for small whole files.
- Watermarks (`--watermark`) are skipped on chunks so the final video is consistent.

### Audio from chunk slices (read each source once instead of twice)

By default a chunked job reads its source **twice**: the video chunks cover the file once between them, and a separate audio pass downloads the whole file again, because audio is interleaved through it. When the source host's upload speed is what limits the farm, that second download is half the traffic.

With `AUDIO_FROM_CHUNKS = 'on'` (config, or the **AUDIO FROM CHUNKS** selector on the admin page, which needs no restart):

- Each video-chunk worker (worker 3.10.0+) adds a second output to the same FFmpeg run: a **stream copy** of the source's audio and text-subtitle packets for its time range, running one second past the end of its chunk. Those bytes are being downloaded anyway. The slice is uploaded just before the chunk.
- When the last video chunk is in, the manager rejoins the slices into the complete original packet stream and encodes the audio/subtitle track itself, with the same codec, bitrate, downmix and filters a worker uses. No worker is handed the audio chunk.
- **Nothing is taken on trust.** At every join the one-second overlap is compared packet by packet (content hash and timestamp); the joined stream is read back and compared with what the slices say it must contain; the last slice must reach the end of the source; and the finished track goes through the same verifier as a worker's upload. Any doubt, at any step, and that job simply reverts to the separate audio pass.
- The video chunks are not affected: their encode command is unchanged apart from the extra output.

A job falls back to the separate pass (logged as *"Audio from chunk slices not used for this job (reason)"*) when: one of its chunks is taken by an older or browser worker (such workers are steered to other jobs first); a slice is missing, too large (96 MB) or fails a check; the source's audio is not AC-3, E-AC-3, AAC, DTS or Opus (the codecs this was tested with), or it has text subtitles other than SRT/ASS; or the audio ends more than 3 s before the video. A worker whose encode fails with the slice output retries without it, so a slice can never be the reason a chunk fails.

`AUDIO_FROM_CHUNKS = 'verify'` is for checking this against your own sources before relying on it: the separate pass still runs and supplies the final file, the slices are built as well, and the two are compared. Look for **"Audio-from-chunks check: MATCH / MISMATCH"** in the log. It saves no traffic.

The built track is exactly what the manager's own FFmpeg would produce from the whole source (byte-identical in testing on FFmpeg 6.1, 8.1 and 9.0). FFmpeg versions differ slightly from each other, though: on sources whose audio carries an encoder delay, FFmpeg 6.1 places the audio a few milliseconds later (5 ms for AC-3, 23 ms for AAC) than 8.1 and 9.0 do; versions in between were not tested. If the manager's FFmpeg is older than the workers', the check reports this as a constant offset. To make the track identical to a worker's, point `AUDIO_FROM_CHUNKS_FFMPEG_DIR` at a folder containing the same `ffmpeg` and `ffprobe` build the workers use.

The cost is upload from workers to the manager: each chunk brings its share of the source audio (about 17 MB per five minutes for 448 kb/s AC-3).

### Server-side segmentation (browser workers on big files)

Browser workers can't range-stream a multi-GB source (32-bit WebAssembly, and the wasm has no network), so they'd otherwise be limited to small whole files. Segmentation fixes that:

- A browser node calls `/get_chunk?video_only=1` and is assigned one of the job's existing video chunks (same plan the desktop workers use — nothing about the boundaries changes).
- It fetches that chunk's bytes from **`/download_segment`**, which stream-copies (`-c copy`, no re-encode) just a small, keyframe-aligned segment covering the chunk's time range and returns it with `X-Segment-Lead` / `X-Segment-Duration` headers.
- The browser seeks by the lead and encodes exactly `[start, start+dur]` — identical to what a native chunk worker produces — then uploads via `/upload_chunk`.
- The whole-file **audio** chunk is skipped by browser nodes (`video_only`); a native worker encodes it. The manager assembles as usual.
- **Memory limit:** a browser node advertises a **MAX CHUNK SEC** (default 600, i.e. 10 minutes) and a **MAX SOURCE MB** (default 600, capped at 1000), and is only handed chunks within both (the size is estimated per chunk). A 10-minute chunk of a typical 1080p film (H.264 at 6.5 Mbps) is a 451 MB download and peaked at about 1 GB of browser memory in testing (web worker 3.6.0, which stores the download without a second copy). On low-memory devices lower MAX CHUNK SEC if the tab crashes. Native workers take any length. For browser volunteers to get work, keep `CHUNK_DURATION_SEC` at or below their MAX CHUNK SEC.
- **Speed:** a browser encodes on one thread, so a 10-minute chunk takes hours (about 3.5 h on a server core; longer on a tablet). Keep the tab in the foreground and the screen on: phones and tablets pause background tabs.

So a browser volunteer only ever downloads ~one chunk's worth of data (e.g. ~30 MB for a 2-min slice of a 2 GB file), never the whole source. Boundaries come from the single per-job chunk plan, so browser and desktop chunks tile identically.

**Config** (`config.py`, both optional):

```python
CHUNKED_ENCODING = True     # set False to disable splitting entirely
CHUNK_DURATION_SEC = 120    # maximum chunk length in seconds (no chunk exceeds this)
```

> **Note:** the manager temporarily stores uploaded chunks in `chunk_store/` until assembly — keep roughly one encoded video's worth of free disk per active chunked job.

---

## FractumCoin Rewards

Workers can attach a FractumCoin wallet address with `--wallet ADDR` (or `&wallet=ADDR` on the `/install` one-liner). The manager keeps an append-only **earnings ledger**: one row per *verified* upload, credited in **minutes of source video encoded**.

- A whole-file upload earns the encode's duration, measured by the manager's own `ffprobe` of the delivered file (not the worker's claim).
- A video chunk earns its slice's minutes; the audio helper chunk earns 0, so a chunked video pays out exactly its real length across contributors.
- Ledger rows are never rewritten by retries, archives, or chunk cleanup, and re-uploads to an already-completed job earn nothing — the payout record is stable.
- The public scoreboard is a view of the same ledger, so score = minutes of successful encodes uploaded, identical between chunked and whole-file work. Existing history is backfilled into the ledger on first startup (with no wallet attached).

**Payouts:** `GET /api/earnings` (admin auth) returns per-wallet totals (`total_minutes`, `unpaid_minutes`, upload counts, first/last activity) plus recent ledger rows. Work uploaded without a wallet appears under `(no wallet)` and cannot be paid.

Actual coin settlement is done by the **payout daemon** (FractumCoin repo, `encode-rewards/`), which runs next to the coin wallet — the manager never talks to the coin daemon, so wallet RPC stays localhost-only on that machine. The flow:

1. The daemon polls `GET /api/payouts/pending` (authenticated with `PAYOUT_TOKEN` via the `X-Payout-Token` header) for unpaid ledger rows grouped by wallet.
2. It validates each address, converts minutes → FRCT at its configured rate, sends coins, then settles the rows via `POST /api/payouts/mark_paid` (idempotent — retries after a lost response can't double-record; rows already paid update nothing).
3. **Hybrid approval:** wallets owed at most the auto cap are paid automatically. Larger balances need an admin click in the **FRACTUM_PAYOUTS** panel on `/admin` (`POST /api/payouts/approve`). Approval is a snapshot of the wallet's unpaid rows at click time — work uploaded afterwards accumulates unapproved.
4. The daemon reports heartbeat + invalid addresses via `POST /api/payouts/report`; the admin panel shows daemon health (dry-run/paused state, treasury balance, rate), per-wallet owed FRCT with AUTO / NEEDS APPROVAL / INVALID ADDRESS badges, and paid history with txids.

Config: `PAYOUT_TOKEN` (shared secret for the daemon; `None` disables the token path), plus `PAYOUT_RATE_FRCT_PER_MIN` / `PAYOUT_AUTO_CAP_FRCT` — **display hints for the admin UI only**; the daemon's own config is authoritative for money movement, and the UI warns when they drift apart.

---

## Android App

Tablets and phones (64-bit ARM, Android 7+) can run the worker as an app: the same `worker.py`, run inside the app with its own FFmpeg build (SVT-AV1, Opus, HTTPS). It is much faster than the browser worker, keeps working in the background, and by default only works while charging, on Wi-Fi and below 40 °C. The dashboard offers it when `FractumWorker.apk` (or `ANDROID_APK_FILE`) sits next to `manager.py`, served at `/dl/android`. Building and details: [`android/README.md`](android/README.md).

## Browser Worker (no install)

Volunteers can encode small files directly in their browser — no Python, no FFmpeg install. The manager serves an in-browser node at **`/web`** (linked from the dashboard) that runs a purpose-built FFmpeg WebAssembly bundle with **SVT-AV1 + Opus** compiled in, using the same targets as the native worker (preset 2, CRF 57, 480p, mono Opus).

- The page authenticates automatically (the worker token is injected server-side).
- **Big files, too:** browser nodes take chunks of large videos via *server-side segmentation* — the manager stream-copies just the piece for one chunk into a small standalone file and sends that, so the browser never downloads a multi-GB source. See "Server-side segmentation" below.
- Whole-file browser jobs accept only sources up to **MAX SOURCE MB** (default 600, at most 1000). Browser encoding is memory-bound (32-bit WebAssembly), so a multi-GB *whole* film cannot work; big films reach browsers as chunks instead.
- **Rebuilding the WASM:** the FFmpeg→WebAssembly bundle (SVT-AV1 with WebAssembly SIMD + Opus) is built by [`wasm-build/build.sh`](wasm-build/README.md); read that README before changing component or compiler versions.
- Threads require cross-origin isolation; the manager already sends the needed `Cross-Origin-Opener-Policy` / `Cross-Origin-Embedder-Policy` headers, and the `.wasm` is served with the correct `application/wasm` type.
- Subtitles are dropped in the browser (no `ffprobe` in the wasm build to filter subtitle types safely); output is video + Opus audio.
- A `WALLET` field on the page credits FractumCoin earnings just like the native `--wallet` flag.

> The dashboard's "Run worker app" dialog links to `/web` last, labelled as the slower option, and the `/web` page points back to the app instructions (`/#run-worker`). Native workers are preferred: the browser build is at least four times slower than a native worker (a quarter of native speed on a single thread, and it only uses one), and only works while the tab is open.

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

### `find_truncated.py`
Finds completed jobs whose encoded output is much shorter than its source —
truncated encodes that were accepted before the upload length-check existed:
```bash
python3 find_truncated.py                # detect + report only (safe)
python3 find_truncated.py --requeue      # also reset the truncated ones to 'queued'
python3 find_truncated.py --local-only   # skip probing remote sources
python3 find_truncated.py --include-missing  # also re-encode jobs with no output file
```
> **Missing outputs are informational by default.** Because finished encodes are
> routinely moved off-server to archive/storage, a completed job with no file on
> disk is *expected*, not a defect — those are listed separately and **not**
> counted as needing re-encode. Only add `--include-missing` if you know those
> outputs are truly gone and must be regenerated.
> **RAM mode:** detection is always safe to run, but `--requeue` writes to the
> disk DB, which a running RAM-mode manager overwrites on its next sync — stop
> the manager before `--requeue` and start it again after (it reloads the newer
> disk copy), or re-queue the listed jobs from the admin panel instead. The
> script warns and asks for confirmation when it detects RAM mode.

### `update.sh` / `update.ps1`
Pulls the latest code from git and restarts the service. Run on the manager host.

### Built-in auto-update (no scripts needed)
Set `MANAGER_AUTO_UPDATE = True` in `config.py` and the manager keeps itself
current: every `MANAGER_UPDATE_INTERVAL_HOURS` it fetches
`origin/MANAGER_UPDATE_BRANCH`, fast-forwards to it, refreshes
`requirements.txt` dependencies, syncs/backs up the database, and restarts
into the new code — via `MANAGER_RESTART_CMD` if you set one, otherwise a
zero-downtime gunicorn reload (SIGHUP), otherwise a clean exit under systemd
(`Restart=always` relaunches it). A repo with local modifications is left
alone, and diverged history is reported instead of force-reset. Security
note: whoever can push to that branch controls the server — protect it.

**Worker FFmpeg requirements:** workers require FFmpeg **7.1+** with
`libsvtav1` at startup. Older or unsuitable system installs are ignored and a
pinned stable 7.1 build (BtbN release branch) is downloaded instead; git dev
snapshots print a warning (unreleased builds have produced broken uploads).

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
- `REQUIRE_WORKER_TOKEN` (default **True**) makes `WORKER_SECRET` an actual gate: worker endpoints now reject requests that present **no** token, not just wrong ones. Regular workers always send the token, so they are unaffected. Set it to `False` only if you intentionally run a fully open, anonymous-worker manager.
- Put the server behind a reverse proxy (nginx / Caddy) with HTTPS in production.
- `ADMIN_PASS` is used for HTTP Basic Auth on the `/admin` route. Do not reuse a password you use elsewhere.
- Workers are validated against a minimum version (`MIN_CLIENT_VERSION` in `manager.py`). Outdated workers are denied jobs until they auto-update.

### Signed worker updates (worker 3.17.0+)

> Workers never update themselves to an **older** version than they run (3.18.1+): a manager that still serves an old script no longer downgrades them.
>
> **Not enforced yet (worker 3.17.1+).** Workers check a signature when the manager has one and say what they found, but still install unsigned updates. Enforcement is switched on later with `ENFORCE_UPDATE_SIGNATURES = True` in `worker_template.py`, once every release is being signed. Workers that already run **3.17.0** do enforce: they accept 3.17.1 only once it is signed (one `ssh-keygen` run, below), or after `worker.py` is downloaded again by hand.

When enforced, workers install a new `worker.py` from the manager only when it is **signed by the project owner's key** and is **newer** than the one running. The signing key never lives on the manager server, so someone who breaks into the server, or into the git checkout it serves from, cannot push code onto volunteers' machines: workers refuse the update, keep running their current version, and say why.

- The trusted public key(s) are built into the worker (`UPDATE_SIGNING_KEYS` in `worker_template.py`, Ed25519 only, checked in pure Python with no extra package). Add a second, backup key there while you still can sign: if the only private key is lost, workers can only be updated by reinstalling them.
- The signature is an ordinary OpenSSH signature, kept next to the script as `worker_template.py.sig` and served at `/dl/worker.sig`. If the manager fills a custom `WORKER_SECRET` into the script it serves, workers undo that one line before checking, so one signature works for every manager.
- `launch_worker.ps1` hands each fresh download to the worker already installed (`worker.py --install-update worker.py.new`), which applies the same check. The `/install` one-liners and the Docker image download the worker from the manager every time they start, and the one-liners are themselves served by the manager, so those cannot protect a first install from a compromised server; once installed, every update is checked.
- Running your own manager with your own changes to the worker? Put your own public key in `UPDATE_SIGNING_KEYS` and sign with it, or start workers with `--no-auto-update`.

**Releasing a worker update** (after the change is merged into `main`), on the machine that holds the private key:

```
git pull
ssh-keygen -Y sign -f FractumEncodes -n fractum-worker worker_template.py
git add worker_template.py.sig
git commit -m "Sign worker <version>"
git push
```

(Remove the old `worker_template.py.sig` first, or answer `y` when `ssh-keygen` asks to overwrite it.) Then pull on the manager. Until the matching signature is there, workers refuse the new version and retry every ten minutes; nothing breaks. To check a signature yourself:

```
echo "fractum ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHH/vpS1Q+KoSPBObtdZYEztRqC8Nppg/mkkTsBQGG7c" > allowed_signers
ssh-keygen -Y verify -f allowed_signers -I fractum -n fractum-worker -s worker_template.py.sig < worker_template.py
```
