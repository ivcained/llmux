"""Autostart contract: the proxy dependency must be self-healing.

The desktop card and dashboard backend both need 127.0.0.1:LLMUX_PORT. A dead
proxy must respawn on the next request instead of surfacing a manual command.
"""

import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from llmux_core import autostart

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _listening_port() -> tuple[socket.socket, int]:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    return srv, srv.getsockname()[1]


class PortListeningTests(unittest.TestCase):
    def test_listening_socket_detected(self):
        srv, port = _listening_port()
        try:
            self.assertTrue(autostart.port_listening(port=port))
        finally:
            srv.close()

    def test_free_port_not_listening(self):
        self.assertFalse(autostart.port_listening(port=_free_port()))


class EnsureProxyRunningTests(unittest.TestCase):
    def test_skips_spawn_when_port_already_served(self):
        srv, port = _listening_port()
        try:
            with mock.patch.object(autostart.subprocess, "Popen") as popen:
                result = autostart.ensure_proxy_running(
                    PLUGIN_DIR, port=port, env={"LLMUX_AUTOSTART": "1"}
                )
        finally:
            srv.close()
        self.assertIsNone(result)
        popen.assert_not_called()

    def test_disabled_by_env_does_not_spawn(self):
        with mock.patch.object(autostart.subprocess, "Popen") as popen:
            result = autostart.ensure_proxy_running(
                PLUGIN_DIR,
                port=_free_port(),
                wait_seconds=0,
                env={"LLMUX_AUTOSTART": "0"},
            )
        self.assertIsNone(result)
        popen.assert_not_called()

    def test_spawns_child_with_module_argv_and_pythonpath(self):
        port = _free_port()
        proc = mock.Mock()
        proc.poll.return_value = None
        with tempfile.TemporaryDirectory() as log_dir, mock.patch.object(
            autostart.subprocess, "Popen", return_value=proc
        ) as popen:
            result = autostart.ensure_proxy_running(
                PLUGIN_DIR,
                port=port,
                wait_seconds=0,
                log_dir=log_dir,
                env={"LLMUX_AUTOSTART": "1", "PYTHONPATH": "/existing"},
            )
        self.assertIs(result, proc)
        args, kwargs = popen.call_args
        argv = args[0]
        self.assertEqual(argv[-4:], ["-m", "llmux_core", "--port", str(port)])
        self.assertEqual(kwargs["cwd"], str(PLUGIN_DIR))
        python_path = kwargs["env"]["PYTHONPATH"]
        self.assertIn(str(PLUGIN_DIR), python_path)
        self.assertIn("/existing", python_path)
        self.assertIn(proc, autostart._CHILDREN)
        autostart._CHILDREN.remove(proc)

    def test_child_that_exits_immediately_is_not_waited_on_forever(self):
        proc = mock.Mock()
        proc.poll.return_value = 1  # died instantly
        with tempfile.TemporaryDirectory() as log_dir, mock.patch.object(
            autostart.subprocess, "Popen", return_value=proc
        ):
            result = autostart.ensure_proxy_running(
                PLUGIN_DIR,
                port=_free_port(),
                wait_seconds=5.0,
                log_dir=log_dir,
                env={"LLMUX_AUTOSTART": "1"},
            )
        self.assertIs(result, proc)
        if proc in autostart._CHILDREN:
            autostart._CHILDREN.remove(proc)


if __name__ == "__main__":
    unittest.main()
