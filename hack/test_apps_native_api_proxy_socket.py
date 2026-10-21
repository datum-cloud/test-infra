"""Offline regression for UID-safe API proxy sockets under long journals."""

from __future__ import annotations

import os
from pathlib import Path
import socket
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from apps_native_support import APIProxy


class APIProxySocketTests(unittest.TestCase):
    def test_long_private_journal_still_binds_short_private_socket(self):
        with tempfile.TemporaryDirectory(prefix="apps-native-long-journal-") as root:
            private = Path(root) / ("catalog-installer-" * 6)
            private.mkdir(mode=0o700)
            old_path = private / "catalog-installer-host-cleanup-api.sock"
            self.assertGreater(len(os.fsencode(old_path)), 103)
            proxy = APIProxy(SimpleNamespace(), private, "catalog-installer-host-cleanup")
            try:
                self.assertLess(len(os.fsencode(proxy.path)), 100)
                self.assertEqual(stat.S_IMODE(proxy.socket_dir.stat().st_mode), 0o700)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                    server.bind(str(proxy.path))
                    self.assertTrue(proxy.path.is_socket())
            finally:
                directory = proxy.socket_dir
                proxy.close()
            self.assertFalse(directory.exists())

    def test_failed_proxy_start_removes_only_its_ephemeral_directory(self):
        with tempfile.TemporaryDirectory(prefix="apps-native-long-journal-") as root:
            private = Path(root) / "journal"
            private.mkdir(mode=0o700)
            kube = SimpleNamespace(config=private / "host.kubeconfig", context="owned")
            proxy = APIProxy(kube, private, "host")
            directory = proxy.socket_dir
            with patch("apps_native_support.subprocess.Popen", side_effect=OSError("fixture")):
                with self.assertRaisesRegex(OSError, "fixture"):
                    proxy.start()
            self.assertTrue(private.exists())
            self.assertFalse(directory.exists())

    def test_foreign_or_world_readable_journal_refused(self):
        with tempfile.TemporaryDirectory(prefix="apps-native-journal-") as root:
            private = Path(root) / "journal"
            private.mkdir(mode=0o755)
            with self.assertRaisesRegex(RuntimeError, "private API proxy journal"):
                APIProxy(SimpleNamespace(), private, "host")


if __name__ == "__main__":
    unittest.main()
