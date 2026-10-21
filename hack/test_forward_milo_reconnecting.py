"""Deterministic offline checks for the bounded Milo tunnel supervisor."""

import importlib.util
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "milo_forward", Path(__file__).with_name("forward-milo-reconnecting.py"))
forward = importlib.util.module_from_spec(spec)
spec.loader.exec_module(forward)


class Child:
    def __init__(self, exited=False):
        self.exited = exited
        self.terminated = False
        self.killed = False

    def poll(self):
        return 1 if self.exited or self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.private = tempfile.TemporaryDirectory()
        self.addCleanup(self.private.cleanup)
        self.root = Path(self.private.name)
        self.config = self.root / "kubeconfig"
        self.config.write_text("fixture", encoding="utf-8")
        self.config.chmod(0o600)
        self.log = self.root / "forward.log"
        self.argv = ["forward-milo-reconnecting.py", "--host-kubeconfig", str(self.config),
                     "--host-context", "kind-owned", "--local-port", "42443",
                     "--deadline-seconds", "60", "--log-file", str(self.log)]

    def test_restarts_exited_child_then_stops_active_child_at_deadline(self):
        clock = [0.0]
        children = [Child(exited=True), Child()]
        commands = []

        def popen(argv, **_kwargs):
            commands.append(argv)
            return children[len(commands) - 1]

        def sleep(seconds):
            clock[0] += max(seconds, 15)

        with patch.object(sys, "argv", self.argv), \
             patch.object(forward.subprocess, "Popen", side_effect=popen), \
             patch.object(forward.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(forward.time, "sleep", side_effect=sleep), \
             patch.object(forward.signal, "signal"):
            with self.assertRaisesRegex(SystemExit, "deadline expired"):
                forward.main()
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0], commands[1])
        self.assertIn("127.0.0.1", commands[0])
        self.assertTrue(children[1].terminated)
        self.assertFalse(children[1].killed)

    def test_sigterm_terminates_active_child_before_return(self):
        child = Child()
        handlers = {}

        def register(sig, handler):
            handlers[sig] = handler

        def sleep(_seconds):
            handlers[signal.SIGTERM](signal.SIGTERM, None)

        with patch.object(sys, "argv", self.argv), \
             patch.object(forward.subprocess, "Popen", return_value=child) as popen, \
             patch.object(forward.time, "monotonic", return_value=0.0), \
             patch.object(forward.time, "sleep", side_effect=sleep), \
             patch.object(forward.signal, "signal", side_effect=register):
            forward.main()
        popen.assert_called_once()
        self.assertTrue(child.terminated)
        self.assertFalse(child.killed)

    def test_refuses_permissive_kubeconfig(self):
        self.config.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private regular file"):
            forward.private_file(self.config)


if __name__ == "__main__":
    unittest.main()
