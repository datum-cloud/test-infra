from pathlib import Path
import importlib.util
import shutil
import subprocess
import tempfile
import unittest
import uuid
from types import SimpleNamespace

SCRIPT = Path(__file__).with_name('bootstrap-apps-native-vz-host.sh')
AUDIT_HELPER_PATH = Path(__file__).with_name('apps_native_vz_audit.py')
spec = importlib.util.spec_from_file_location('apps_native_vz_audit', AUDIT_HELPER_PATH)
audit_helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_helper)


class VZHostGuards(unittest.TestCase):
    def invoke(self, args, expected):
        result = subprocess.run(['bash', str(SCRIPT), *args], capture_output=True,
                                text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(expected, result.stderr)

    def args(self, target):
        return ['--profile', 'apps-native-vz-offline', '--cluster',
                'apps-a1t-ci-vz-offline', '--private-dir', str(target)]

    def test_no_implicit_profile_or_unknown_options(self):
        self.invoke([], 'Explicit dedicated Apps VZ')
        self.invoke(['--unknown'], 'Usage:')

    def test_unrelated_profile_and_path_traversal_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for profile in ('default', 'other-team', 'apps-native-vz-../../other'):
                args = self.args(Path(directory) / 'absent')
                args[1] = profile
                self.invoke(args, 'Explicit dedicated Apps VZ')

    def test_existing_directory_file_and_symlink_are_never_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / 'file'
            file.write_text('preserve')
            link = root / 'link'
            link.symlink_to(root / 'absent')
            for target in (root, file, link):
                self.invoke(self.args(target) + ['--execute'], 'absent absolute path')
            self.assertEqual(file.read_text(), 'preserve')
            self.assertTrue(link.is_symlink())
            self.assertFalse((root / 'absent').exists())

    def test_relative_or_root_evidence_path_is_rejected(self):
        for target in ('relative', '/'):
            self.invoke(self.args(target), 'absent absolute path')

    def test_ports_cannot_overflow_or_inject_shell_arithmetic(self):
        for port in ('0', '1023', '65093', '65535', '1+2', '32000;id', '-1'):
            self.invoke(self.args('/tmp/unused-vz-evidence') + ['--port-base', port],
                        'Port base must be')

    def test_audit_directory_is_random_private_and_preserves_legacy_symlink(self):
        profile = 'apps-native-vz-offline'
        cluster = f'apps-a1t-ci-vz-audit-test-{uuid.uuid4().hex[:8]}'
        legacy = Path('/tmp') / f'{cluster}-audit-policy.yaml'
        marker = Path(tempfile.mkdtemp(prefix='apps-vz-audit-marker-')) / 'original'
        marker.write_text('preserve')
        private_dir = None
        observed = {}
        try:
            legacy.symlink_to(marker)

            def stub_runner(argv, **kwargs):
                observed['argv'] = argv
                observed['kwargs'] = kwargs
                self.assertEqual(argv[:6], ['colima', 'ssh', '--profile', profile, '--', 'sh'])
                self.assertIn(f'mktemp -d /tmp/{cluster}-audit.XXXXXXXX', argv[-1])
                # Run the exact remote shell payload locally, replacing only
                # the SSH transport. This checks real mktemp/umask behavior.
                result = subprocess.run(argv[5:], **kwargs)
                if result.returncode == 0:
                    observed['created'] = Path(result.stdout.strip())
                return result

            private_dir = Path(audit_helper.create_private_audit_directory(
                profile, cluster, runner=stub_runner))
            self.assertEqual(private_dir, observed['created'])
            self.assertEqual(private_dir.stat().st_mode & 0o777, 0o700)
            self.assertNotEqual(private_dir, legacy)
            self.assertTrue(legacy.is_symlink())
            self.assertEqual(legacy.readlink(), marker)
            self.assertEqual(legacy.read_text(), 'preserve')
            self.assertNotIn(str(legacy), observed['argv'])
        finally:
            created = private_dir or observed.get('created')
            if created is not None and created.is_dir():
                shutil.rmtree(created)
            if legacy.is_symlink():
                legacy.unlink()
            shutil.rmtree(marker.parent)

    def test_audit_directory_rejects_untrusted_remote_path(self):
        result = SimpleNamespace(returncode=0, stdout='/tmp/other-cluster-audit.ABC12345\n', stderr='')
        with self.assertRaisesRegex(ValueError, 'unexpected audit directory'):
            audit_helper.create_private_audit_directory(
                'apps-native-vz-offline', 'apps-a1t-ci-vz-offline',
                runner=lambda *_args, **_kwargs: result)


if __name__ == '__main__':
    unittest.main()
