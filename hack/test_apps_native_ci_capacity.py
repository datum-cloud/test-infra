"""Offline controls for the native CI capacity gate."""

from fractions import Fraction
from pathlib import Path
import tempfile
import unittest

import importlib.util


spec = importlib.util.spec_from_file_location(
    "apps_native_ci_capacity", Path(__file__).with_name("check-apps-native-ci-capacity.py")
)
capacity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capacity)


class CapacityTest(unittest.TestCase):
    def test_standard_private_runner_two_cpus_fails(self):
        with self.assertRaisesRegex(ValueError, "APPS_NATIVE_RUNNER"):
            capacity.check("Linux", "x86_64", 2, None)

    def test_four_cpus_pass_trial_floor(self):
        self.assertEqual(capacity.check("Linux", "x86_64", 4, None), 4)

    def test_cgroup_quota_bounds_affinity(self):
        with self.assertRaisesRegex(ValueError, "APPS_NATIVE_RUNNER"):
            capacity.check("Linux", "x86_64", 8, Fraction(5, 2))

    def test_non_linux_amd64_fails(self):
        with self.assertRaisesRegex(ValueError, "Linux amd64"):
            capacity.check("Darwin", "arm64", 8, None)

    def test_cpu_max_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cpu.max"
            path.write_text("250000 100000\n")
            self.assertEqual(capacity.cpu_quota(path), Fraction(5, 2))
            path.write_text("max 100000\n")
            self.assertIsNone(capacity.cpu_quota(path))
            path.write_text("broken\n")
            with self.assertRaisesRegex(ValueError, "invalid cgroup"):
                capacity.cpu_quota(path)


if __name__ == "__main__":
    unittest.main()
