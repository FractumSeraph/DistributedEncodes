import sqlite3
import sys
import os
from datetime import datetime

# Ensure we can find config.py in the current directory
sys.path.append(os.getcwd())

try:
    import config
except ImportError:
    print("[!] Error: 'config.py' not found.")
    print("    Please run this script from the same folder as your manager and config.py.")
    sys.exit(1)

# Pull settings dynamically from config.py
DB_FILE = getattr(config, 'DB_FILE', 'fractum.db')

def reset_series(search_term):
    if not os.path.exists(DB_FILE):
        print(f"[!] Error: Database file '{DB_FILE}' not found.")
        return

    print(f"[*] Connecting to database: {DB_FILE}")
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()

        # The '%' allows it to match anything starting with or containing that name
        # We assume 'id' in the DB is the relative path (e.g., "MySeries/Episode1.mkv")
        pattern = f"%{search_term}%"
        
        print(f"[*] Searching for jobs matching: '{pattern}'")

        # Check how many jobs match first
        c.execute("SELECT COUNT(*) FROM jobs WHERE id LIKE ?", (pattern,))
        count = c.fetchone()[0]
        
        if count == 0:
            print("[-] No jobs found matching that name.")
            conn.close()
            return

        print(f"[*] Found {count} jobs. Resetting them to 'queued'...")

        # Update status to 'queued' so workers pick them up again.
        # We also clear worker_id, progress, duration, and timestamps to ensure a clean start.
        # Chunk state (chunked encoding) is cleared too, mirroring the admin
        # 'retry' action — otherwise stale chunk rows could be assembled into
        # a re-queued job or leave it invisible to the stale-job timeout.
        try:
            c.execute("DELETE FROM chunks WHERE job_id LIKE ?", (pattern,))
        except sqlite3.OperationalError:
            pass  # pre-chunking database: no chunks table yet
        try:
            c.execute("""
                UPDATE jobs
                SET status='queued', progress=0, worker_id=NULL,
                    last_updated=?, duration=0, started_at=NULL,
                    chunked=0, chunkable=NULL
                WHERE id LIKE ?
            """, (datetime.now(), pattern))
        except sqlite3.OperationalError:
            c.execute("""
                UPDATE jobs
                SET status='queued', progress=0, worker_id=NULL,
                    last_updated=?, duration=0, started_at=NULL
                WHERE id LIKE ?
            """, (datetime.now(), pattern))

        conn.commit()
        conn.close()
        print(f"[+] Success! {count} jobs have been reset and added back to the queue.")
        
    except Exception as e:
        print(f"[!] Database Error: {e}")

if __name__ == "__main__":
    print("==================================================")
    print(" FRACTUM SERIES RESET TOOL")
    print("==================================================")
    if len(sys.argv) < 2:
        print("Usage: python reset_series.py <SeriesNameOrFolder>")
        print("Example: python reset_series.py \"Breaking Bad\"")
    else:
        reset_series(sys.argv[1])
