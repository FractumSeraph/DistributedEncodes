"""Runs the Fractum worker (the same worker.py every desktop worker runs)
inside the Android app, and gives the app a small control surface:

    start(config_json)   load worker.py and run it in a background thread
    pause() / resume()   freeze / continue (charging, Wi-Fi, temperature rules)
    stop(abort)          finish the work in hand, or stop now and hand it back
    get_state()          JSON: running, paused, version, status line, error
    get_log(since)       new output lines since a sequence number

The worker updates itself from the manager like any other worker. Here it
cannot restart the process (os.execv), so in EMBEDDED mode it raises
RestartForUpdate after writing the new script, and the loop below loads it.
FFmpeg is the app's own build, passed with --ffmpeg/--ffprobe.
"""
import collections
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import threading
import traceback

_LOG = collections.deque(maxlen=1000)
_LOG_LOCK = threading.Lock()
_seq = 0
_state = {"running": False, "paused": False, "version": "", "status": "", "job": "",
          "error": "", "stopping": False}
_INFO = re.compile(r'^\[[0-9:]+\] \[[^\]]+\] \[[A-Z]+\] (.*)$')
_mod = None
_thread = None
_ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')


def _add_line(line):
    global _seq
    line = _ANSI.sub('', line).rstrip()
    if not line:
        return
    # "[12:00:00] [user-device-1] [INFO] Encoding 40%": the latest such line
    # is the status; a "... job: <name>" line names the work in hand.
    m = _INFO.match(line)
    if m:
        msg = m.group(1).strip()
        if "job:" in msg.lower():
            _state["job"] = msg.split(":", 1)[1].strip()
        _state["status"] = msg
    with _LOG_LOCK:
        _seq += 1
        _LOG.append((_seq, line))


class _Capture(io.TextIOBase):
    """stdout/stderr: whole lines go to the log; a line rewritten in place with
    \\r (the worker's progress line) becomes the status line instead."""

    def __init__(self, passthrough):
        self._buf = ""
        self._pass = passthrough

    def write(self, s):
        try:
            self._pass.write(s)       # still visible in logcat
        except Exception:
            pass
        self._buf += s
        while True:
            nl = self._buf.find("\n")
            if nl < 0:
                break
            line, self._buf = self._buf[:nl], self._buf[nl + 1:]
            if "\r" in line:
                line = line.rsplit("\r", 1)[-1]
            _add_line(line)
        if "\r" in self._buf:
            cur = _ANSI.sub('', self._buf.rsplit("\r", 1)[-1]).strip()
            if cur:
                _state["status"] = cur
            self._buf = self._buf.rsplit("\r", 1)[-1]
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


def _script_version(path):
    try:
        with open(path, "rb") as f:
            m = re.search(rb'^WORKER_VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
        return tuple(int(x) for x in m.group(1).decode().split(".")) if m else ()
    except Exception:
        return ()


def _install_bundled(bundled, script):
    """Use the copy shipped in the app unless the worker already updated
    itself to something newer."""
    if os.path.exists(bundled) and _script_version(bundled) > _script_version(script):
        shutil.copyfile(bundled, script)


def _install_ca_bundle(files_dir):
    """The app's FFmpeg checks HTTPS certificates against the file named by
    SSL_CERT_FILE (see android/ffmpeg/build.sh). Give it the Mozilla CA
    bundle that certifi ships (the one requests already uses)."""
    import importlib.resources
    data = importlib.resources.files("certifi").joinpath("cacert.pem").read_bytes()
    path = os.path.join(files_dir, "cacert.pem")
    try:
        with open(path, "rb") as f:
            same = f.read() == data
    except OSError:
        same = False
    if not same:
        with open(path, "wb") as f:
            f.write(data)
    os.environ["SSL_CERT_FILE"] = path


def _run(cfg):
    global _mod
    _state.update(running=True, paused=False, error="", status="Starting...", job="", stopping=False)
    try:
        workdir = os.path.join(cfg["files_dir"], "worker")
        os.makedirs(workdir, exist_ok=True)
        os.chdir(workdir)
        _install_ca_bundle(cfg["files_dir"])
        script = os.path.join(workdir, "worker.py")
        _install_bundled(cfg["bundled"], script)
        n = 0
        while True:
            n += 1
            # The worker reads sys.argv at import time (TUI choice).
            sys.argv = [script, "--no-tui"]
            spec = importlib.util.spec_from_file_location(f"fractum_worker_{n}", script)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            if not all(hasattr(m, a) for a in ("EMBEDDED", "build_arg_parser", "embedded_pause", "RestartForUpdate")):
                # A worker older than 3.18.0 cannot run inside the app (it would
                # restart the whole process to update, and has no app pause).
                # Go back to the copy shipped in the app, and never take that
                # version again in this run.
                bad = getattr(m, "WORKER_VERSION", "?")
                _add_line(f"[!] Worker {bad} cannot run in the app (it needs 3.18.0 or newer); "
                          f"using the app's own copy instead.")
                os.environ["FRACTUM_UPDATE_FAILED"] = str(bad)
                if n > 3 or not os.path.exists(cfg["bundled"]):
                    raise RuntimeError(f"no usable worker script (got {bad})")
                shutil.copyfile(cfg["bundled"], script)
                continue
            m.EMBEDDED = True
            m.FFPROBE_OVERRIDE = cfg["ffprobe"]
            argv = ["--no-tui", "--manager", cfg["manager"], "--username", cfg["username"],
                    "--workername", cfg["workername"], "--jobs", str(cfg.get("jobs", 1)),
                    "--ffmpeg", cfg["ffmpeg"], "--ffprobe", cfg["ffprobe"]]
            if int(cfg.get("max_size_mb") or 0) > 0:
                argv += ["--max-size-mb", str(int(cfg["max_size_mb"]))]
            if cfg.get("wallet"):
                argv += ["--wallet", cfg["wallet"]]
            if cfg.get("secret"):
                argv += ["--secret", cfg["secret"]]
            args = m.build_arg_parser().parse_args(argv)
            _mod = m
            _state["version"] = m.WORKER_VERSION
            if _state["stopping"]:
                break
            try:
                m.run_worker(args)
                break
            except m.RestartForUpdate as e:
                _add_line(f"[*] Loading worker {e} ...")
                continue
    except SystemExit as e:
        if e.code not in (0, None):
            _state["error"] = f"The worker stopped (exit code {e.code}); see the log."
    except BaseException as e:
        _state["error"] = f"{type(e).__name__}: {e}"
        for line in traceback.format_exc().splitlines():
            _add_line(line)
    finally:
        _state.update(running=False, paused=False, status="Stopped", job="", stopping=False)


def start(config_json):
    """Start the worker unless it is already running. Returns False if it was."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return False
    if not isinstance(sys.stdout, _Capture):
        sys.stdout = _Capture(sys.stdout)
        sys.stderr = _Capture(sys.stderr)
    cfg = json.loads(config_json)
    _thread = threading.Thread(target=_run, args=(cfg,), name="fractum-worker", daemon=True)
    _thread.start()
    return True


def pause():
    if _mod is not None and _state["running"] and not _state["paused"]:
        _mod.embedded_pause()
        _state["paused"] = True


def resume():
    if _mod is not None and _state["paused"]:
        _mod.embedded_resume()
        _state["paused"] = False


def stop(abort=False):
    _state["stopping"] = True
    if _mod is not None:
        _mod.embedded_stop(bool(abort))
        _state["paused"] = False


def is_running():
    return _thread is not None and _thread.is_alive()


def get_state():
    return json.dumps(dict(_state, running=is_running()))


def get_log(since=0):
    """JSON {"seq": last sequence number, "lines": [...]} for lines after since."""
    with _LOG_LOCK:
        lines = [l for s, l in _LOG if s > since]
        return json.dumps({"seq": _seq, "lines": lines})
