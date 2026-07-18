#!/usr/bin/env python3
"""
Find (and optionally re-queue) completed jobs whose encoded output is shorter
than its source — i.e. truncated encodes that were accepted as "completed"
before the upload length-check existed.

Run from the same directory as config.py (like the other utility scripts):

    python3 find_truncated.py                 # detect + report only (safe)
    python3 find_truncated.py --requeue       # also reset the bad ones to 'queued'
    python3 find_truncated.py --local-only    # skip probing remote sources (faster)

Detection: compares the completed .mp4's duration against the source duration
(taken from the job's stored source_duration_sec when present, otherwise probed
with ffprobe). A job is flagged when the output is shorter than the source by
more than the tolerance (default 5% + 10s), or when its completed file is
missing entirely. Minor rounding differences are ignored.

Nothing is changed unless you pass --requeue.
"""
import argparse
import os
import sys
import subprocess
from urllib.parse import quote, urljoin

sys.path.append(os.getcwd())
try:
    import config
except ImportError:
    print("[!] config.py not found. Run this from the same folder as the manager.")
    sys.exit(1)

DB_FILE             = getattr(config, 'DB_FILE', 'encoding_jobs.db')
SOURCE_DIRECTORY    = getattr(config, 'SOURCE_DIRECTORY', './source_media')
COMPLETED_DIRECTORY = getattr(config, 'COMPLETED_DIRECTORY', './completed_media')
REMOTE_SOURCE_URL   = getattr(config, 'REMOTE_SOURCE_URL', None)
WORKER_SECRET       = getattr(config, 'WORKER_SECRET', '')
DB_MODE             = getattr(config, 'DB_MODE', 'disk')

import sqlite3


def probe_duration(target, is_remote=False):
    """Return duration in seconds via ffprobe, or 0.0 on any failure."""
    cmd = ['ffprobe', '-v', 'error']
    if is_remote and WORKER_SECRET:
        cmd += ['-headers', f'X-Worker-Token: {WORKER_SECRET}\r\n']
    cmd += ['-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', target]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return float((res.stdout or '0').strip() or 0)
    except Exception:
        return 0.0


def completed_path_for(job_id):
    return os.path.abspath(os.path.join(COMPLETED_DIRECTORY,
                                        os.path.splitext(job_id)[0] + ".mp4"))


def source_target(job_id, source_type, source_url):
    if source_type == 'remote':
        base = source_url or REMOTE_SOURCE_URL
        return (urljoin(base, quote(job_id)), True) if base else (None, True)
    return (os.path.join(SOURCE_DIRECTORY, job_id.replace('/', os.sep)), False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--requeue', action='store_true',
                    help="Reset flagged jobs to 'queued' so they re-encode.")
    ap.add_argument('--local-only', action='store_true',
                    help="Skip jobs whose source is remote (don't probe over HTTP).")
    ap.add_argument('--tolerance-pct', type=float, default=5.0,
                    help="Allowed shortfall as %% of source duration (default 5).")
    ap.add_argument('--min-tolerance', type=float, default=10.0,
                    help="Minimum allowed shortfall in seconds (default 10).")
    args = ap.parse_args()

    if not os.path.exists(DB_FILE):
        print(f"[!] Database '{DB_FILE}' not found.")
        sys.exit(1)

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, source_type, source_url, source_duration_sec "
        "FROM jobs WHERE status='completed' AND id NOT LIKE 'HISTORY_%' ORDER BY id"
    ).fetchall()

    print(f"[*] Checking {len(rows)} completed job(s)...\n")

    truncated, missing_output, missing_source, undetermined, skipped_remote = [], [], [], [], 0

    for r in rows:
        job_id = r['id']
        out_path = completed_path_for(job_id)
        if not os.path.isfile(out_path):
            missing_output.append(job_id)
            continue

        if r['source_type'] == 'remote' and args.local_only:
            skipped_remote += 1
            continue

        # Source duration: stored value if present, else probe the source.
        src_dur = 0.0
        try:
            src_dur = float(r['source_duration_sec'] or 0)
        except (TypeError, ValueError):
            src_dur = 0.0
        if src_dur <= 0:
            tgt, is_remote = source_target(job_id, r['source_type'], r['source_url'])
            if not tgt or (not is_remote and not os.path.isfile(tgt)):
                missing_source.append(job_id)
                continue
            src_dur = probe_duration(tgt, is_remote)
        if src_dur <= 0:
            undetermined.append(job_id)
            continue

        out_dur = probe_duration(out_path)
        tolerance = max(args.min_tolerance, src_dur * args.tolerance_pct / 100.0)
        if out_dur < src_dur - tolerance:
            truncated.append((job_id, out_dur, src_dur))

    # ---- Report ----
    if truncated:
        print(f"[!] {len(truncated)} TRUNCATED encode(s) (output much shorter than source):")
        for jid, o, s in truncated:
            print(f"      {o/60:6.1f}m / {s/60:6.1f}m   {jid}")
    if missing_output:
        print(f"\n[!] {len(missing_output)} completed job(s) with NO output file on disk:")
        for jid in missing_output:
            print(f"      {jid}")
    if missing_source:
        print(f"\n[-] {len(missing_source)} job(s) whose source is gone (can't verify/re-encode): {len(missing_source)}")
    if undetermined:
        print(f"\n[-] {len(undetermined)} job(s) whose source duration couldn't be determined (skipped).")
    if skipped_remote:
        print(f"\n[-] {skipped_remote} remote job(s) skipped (--local-only).")

    to_fix = [t[0] for t in truncated] + missing_output
    print(f"\n[*] {len(to_fix)} job(s) need re-encoding.")

    if not to_fix:
        conn.close()
        return

    if not args.requeue:
        print("[*] Dry run — re-run with --requeue to reset these to 'queued'.")
        conn.close()
        return

    # RAM mode: the live DB is in /dev/shm and the manager overwrites the disk
    # file every ~60s, so an external write here would be lost. The manager
    # must be stopped first; on restart it loads the (now newer) disk copy.
    if DB_MODE == 'ram':
        print("\n[!] DB_MODE='ram' detected.")
        print("    A write here will be CLOBBERED by the running manager's RAM->disk sync.")
        print("    STOP the manager before using --requeue, then start it again after.")
        print("    (Alternatively, leave the manager running and re-queue each job above")
        print("     from the admin panel's RETRY button — that goes through the live DB.)")
        resp = input("    Proceed with the disk write anyway? [y/N]: ").strip().lower()
        if resp != 'y':
            print("[*] Aborted. No changes made.")
            conn.close()
            return

    from datetime import datetime
    c = conn.cursor()
    for jid in to_fix:
        # Clear any chunk state so it re-encodes cleanly (chunked or whole-file).
        try:
            c.execute("DELETE FROM chunks WHERE job_id=?", (jid,))
        except sqlite3.OperationalError:
            pass  # pre-chunking DB
        c.execute("UPDATE jobs SET status='queued', progress=0, worker_id=NULL, "
                  "fail_count=0, chunked=0, chunkable=NULL, started_at=NULL, "
                  "last_updated=? WHERE id=?", (datetime.now(), jid))
    conn.commit()
    conn.close()
    print(f"[+] Re-queued {len(to_fix)} job(s). They will re-encode on the next worker poll.")
    print("[*] If the manager runs with DB_MODE='ram', restart it so it reloads the DB,")
    print("    or trigger a rescan from the admin panel.")


if __name__ == "__main__":
    main()
