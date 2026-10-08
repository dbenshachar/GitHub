"""Workflow checks never change a VM or start a service."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tools.standalone_setup import Standalone


class StandaloneSetupTests(unittest.TestCase):
    def app(self, folder):
        override = patch('tools.standalone_setup.ROOT', Path(folder))
        override.start()
        self.addCleanup(override.stop)
        return Standalone()

    def test_cached_runtime_verifies_remote_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.app(folder)
            with patch('tools.standalone_setup.fingerprint', return_value='a' * 64), patch.object(app, 'shell', return_value='ready\n') as shell:
                path = app.image()
            self.assertTrue(path.startswith('/var/tmp/'))
            self.assertIn('job-runtime-v2', shell.call_args.args[0])
            self.assertTrue((app.state / 'build.json').exists())
            self.assertEqual(app.token_file.stat().st_mode & 0o777, 0o600)

    def test_direct_network_is_required(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.app(folder)
            with patch.object(app, 'shell', return_value='[]'):
                with self.assertRaisesRegex(RuntimeError, 'vzNAT'):
                    app.url()
            with patch.object(app, 'shell', return_value='[{"addr_info":[{"family":"inet","local":"192.168.64.2"}]}]'):
                self.assertEqual(app.url(), 'http://192.168.64.2:8080')

    def test_bootstrap_touches_only_selected_vm(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'VM': 'fc'}):
            app = self.app(folder)
            with patch.object(app, 'run', return_value='fc\nmini-control\n') as run, patch.object(app, 'shell'), patch.object(app, 'url'):
                app.bootstrap()
            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn(['limactl', 'stop', 'fc'], commands)
            self.assertFalse(any('mini-control' in cmd for cmd in commands))
            self.assertTrue(any('--network' in cmd and 'vzNAT' in cmd for cmd in commands))
