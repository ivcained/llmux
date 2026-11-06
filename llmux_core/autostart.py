"""Self-healing start for the local LLMUX routing proxy.

The desktop seller card and the dashboard backend both depend on the proxy at
127.0.0.1:LLMUX_PORT. A gateway restart or a crashed proxy must not leave the
user with a manual "start python -m llmux_core" instruction: a cheap TCP probe
decides whether a child process is spawned, and the caller waits briefly for
the port so the very next request can succeed.

The spawned child is tracked and terminated at interpreter exit so a plugin
reload does not leave orphan proxies holding the port.
"""

from __future__ import annotations

import atexit
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_PORT = 8378
DISABLED_VALUES = ("0", "false", "no", "off")
_CHILDREN: list[subprocess.Popen] = []


def port_listening(host: str = "127.0.0.1", port: int = DEFAULT_PORT, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def autostart_enabled(env: dict | None = None) -> bool:
    values = os.environ if env is None else env
    return values.get("LLMUX_AUTOSTART", "1").strip().lower() not in DISABLED_VALUES


def ensure_proxy_running(
    plugin_dir: Path | str,
    port: int = DEFAULT_PORT,
    *,
    wait_seconds: float = 3.0,
    python: str | None = None,
    log_dir: Path | str | None = None,
    env: dict | None = None,
) -> subprocess.Popen | None:
    """Spawn the proxy child when nothing serves `port`.

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
    try:
        logs.mkdir(parents=True, exist_ok=True)
        out = open(logs / "llmux-proxy.log", "ab")  # noqa: SIM115 - the child owns this fd
    except OSError:
        out = subprocess.DEVNULL

    proc = subprocess.Popen(
        [python or sys.executable, "-m", "llmux_core", "--port", str(port)],
        cwd=str(plugin_dir),
        env=base_env,
        stdout=out,
        stderr=subprocess.STDOUT,
    )
    if not _CHILDREN:
        atexit.register(_terminate_children)
    _CHILDREN.append(proc)

    deadline = time.monotonic() + max(0.0, wait_seconds)
    while time.monotonic() < deadline:
        if port_listening(port=port) or proc.poll() is not None:
            break
        time.sleep(0.1)
    return proc


def _terminate_children() -> None:
    for proc in list(_CHILDREN):
        try:
            if proc.poll() is None:
                proc.terminate()
        except OSError:
            pass
    _CHILDREN.clear()
