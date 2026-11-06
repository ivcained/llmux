"""Self-healing start for the local LLMUX routing proxy.

The desktop seller card, the dashboard backend and every inference request
depend on the proxy at 127.0.0.1:LLMUX_PORT. A dead proxy must respawn on the
next use instead of surfacing a manual "start python -m llmux_core"
instruction.

The child is detached into its own session so it survives whatever spawned it:
short-lived spawners (a CLI plugin load, a gateway reload, a test run) must not
take the proxy down with them, which previously caused restart churn. Lifetime
is therefore "until the user or the machine stops it", concurrent spawners are
serialized with a lock file so only one child binds the port, and every later
caller adopts the running proxy through the TCP probe.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_PORT = 8378
DISABLED_VALUES = ("0", "false", "no", "off")


def port_listening(host: str = "127.0.0.1", port: int = DEFAULT_PORT, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def autostart_enabled(env: dict | None = None) -> bool:
    values = os.environ if env is None else env
    return values.get("LLMUX_AUTOSTART", "1").strip().lower() not in DISABLED_VALUES


@contextlib.contextmanager
def _spawn_lock(logs: Path | None, port: int):
    """Serialize probe-and-spawn across processes.

    Degrades to an unlocked no-op when the lock file cannot be created, so a
    read-only log directory still allows best-effort spawning.
    """

    if logs is None:
        yield
        return
    try:
        handle = open(logs / f"llmux-proxy-{port}.lock", "a+b")
    except OSError:
        yield
        return
    try:
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Another process holds the lock and is spawning: wait for the port
            # instead of racing a second child that cannot bind.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not port_listening(port=port):
                time.sleep(0.1)
        yield
    finally:
        handle.close()


def ensure_proxy_running(
    plugin_dir: Path | str,
    port: int = DEFAULT_PORT,
    *,
    wait_seconds: float = 5.0,
    python: str | None = None,
    log_dir: Path | str | None = None,
    env: dict | None = None,
) -> subprocess.Popen | None:
    """Spawn a detached proxy child when nothing serves `port`.

    Returns the Popen handle when a child was started, None when the port was
    already served or autostart is disabled. Never raises for expected failure
    modes: a child that dies immediately is still returned so callers can
    surface their own offline state.
    """

    if port_listening(port=port):
        return None
    if not autostart_enabled(env):
        return None

    plugin_dir = Path(plugin_dir)
    base_env = dict(os.environ if env is None else env)
    existing_python_path = base_env.get("PYTHONPATH")
    base_env["PYTHONPATH"] = (
        f"{plugin_dir}{os.pathsep}{existing_python_path}" if existing_python_path else str(plugin_dir)
    )

    logs = Path(log_dir) if log_dir is not None else Path.home() / ".hermes" / "logs"
    out: object = subprocess.DEVNULL
    log_handle = None
    try:
        logs.mkdir(parents=True, exist_ok=True)
        log_handle = open(logs / "llmux-proxy.log", "ab")
        out = log_handle
    except OSError:
        logs = None
        out = subprocess.DEVNULL

    try:
        with _spawn_lock(logs, port):
            if port_listening(port=port):
                return None
            proc = subprocess.Popen(
                [python or sys.executable, "-m", "llmux_core", "--port", str(port)],
                cwd=str(plugin_dir),
                env=base_env,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    finally:
        # The child inherited the descriptor; the parent must not leak it.
        if log_handle is not None:
            log_handle.close()

    deadline = time.monotonic() + max(0.0, wait_seconds)
    while time.monotonic() < deadline:
        if port_listening(port=port) or proc.poll() is not None:
            break
        time.sleep(0.1)
    return proc
