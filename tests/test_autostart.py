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

    def test_no_marketplace_credential_never_spawns_a_doomed_child(self):
        with tempfile.TemporaryDirectory() as logs, mock.patch.object(
            autostart.subprocess, "Popen", return_value=mock.Mock()
        ) as popen:
            result = autostart.ensure_proxy_running(
                PLUGIN_DIR, port=_free_port(), wait_seconds=0,
                log_dir=logs, env={"LLMUX_AUTOSTART": "1"},
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
                env={"LLMUX_AUTOSTART": "1", "PYTHONPATH": "/existing", "SURPLUS_API_KEY": "test-key"},
            )
        self.assertIs(result, proc)
        args, kwargs = popen.call_args
        argv = args[0]
        self.assertEqual(argv[-4:], ["-m", "llmux_core", "--port", str(port)])
        self.assertEqual(kwargs["cwd"], str(PLUGIN_DIR))
        self.assertTrue(kwargs["start_new_session"], "child must outlive its spawner")
        python_path = kwargs["env"]["PYTHONPATH"]
        self.assertIn(str(PLUGIN_DIR), python_path)
        self.assertIn("/existing", python_path)

    def test_child_environment_is_scrubbed_and_uses_scoped_keys(self):
        from agent.secret_scope import reset_multiplex_context, reset_secret_scope, set_multiplex_context, set_secret_scope
        token = set_secret_scope({"SURPLUS_API_KEY": "profile-b-key"})
        multiplex = set_multiplex_context(True)
        try:
            with tempfile.TemporaryDirectory() as logs, mock.patch.dict(autostart.os.environ, {
                "SURPLUS_API_KEY": "profile-a-key", "UNRELATED_PRIVATE_TOKEN": "never-in-child", "LLMUX_AUTOSTART": "1"
            }), mock.patch.object(autostart.subprocess, "Popen", return_value=mock.Mock()) as popen:
                autostart.ensure_proxy_running(PLUGIN_DIR, port=_free_port(), wait_seconds=0, log_dir=logs)
                child_env = popen.call_args.kwargs["env"]
            self.assertEqual(child_env["SURPLUS_API_KEY"], "profile-b-key")
            self.assertNotIn("UNRELATED_PRIVATE_TOKEN", child_env)
            self.assertNotIn("profile-a-key", child_env.values())
        finally:
            reset_secret_scope(token)
            reset_multiplex_context(multiplex)

    def test_real_detached_child_authenticates_without_inherited_pythonpath(self):
        from agent.secret_scope import reset_secret_scope, set_secret_scope
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from llmux_core.config import load_settings
        from llmux_core.credentials import verify_proxy_identity
        port = _free_port()
        values = {
            "LLMUX_AUTOSTART": "1", "SURPLUS_API_KEY": "test-local-only-key", "LLMUX_PORT": str(port),
            "LLMUX_SURPLUS_BASE_URL": "http://127.0.0.1:9/v1",
            "LLMUX_ANTSEED_BASE_URL": "http://127.0.0.1:9/v1",
        }
        with tempfile.TemporaryDirectory() as home:
            home_token = set_hermes_home_override(Path(home))
            secret_token = set_secret_scope({"SURPLUS_API_KEY": values["SURPLUS_API_KEY"]})
            proc = None
            try:
                with mock.patch.dict(autostart.os.environ, values, clear=True):
                    proc = autostart.ensure_proxy_running(PLUGIN_DIR, port=port, env=values)
                    settings = load_settings(values)
                self.assertIsNotNone(proc)
                try:
                    verify_proxy_identity(port, settings)
                except Exception:
                    self.fail((Path(home) / "logs/llmux-proxy.log").read_text())
            finally:
                if proc is not None:
                    proc.terminate()
                    proc.wait(timeout=5)
                reset_secret_scope(secret_token)
                reset_hermes_home_override(home_token)

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
                env={"LLMUX_AUTOSTART": "1", "SURPLUS_API_KEY": "test-key"},
            )
        self.assertIs(result, proc)


if __name__ == "__main__":
    unittest.main()
