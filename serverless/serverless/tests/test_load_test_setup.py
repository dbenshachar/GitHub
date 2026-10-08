"""Verify orchestration without building images or changing the live cluster."""
import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import Mock, patch

from tools import load_test_setup as setup


class SetupTests(unittest.TestCase):
    def test_fingerprint_detects_contents_additions_and_deletions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'worker.py'
            source.write_text('first')
            first = setup.fingerprint([('app', root)])
            source.write_text('other')
            self.assertNotEqual(first, setup.fingerprint([('app', root)]))
            source.write_text('first')
            self.assertEqual(first, setup.fingerprint([('app', root)]))
            added = root / 'new.py'
            added.write_text('new')
            self.assertNotEqual(first, setup.fingerprint([('app', root)]))
            added.unlink()
            self.assertEqual(first, setup.fingerprint([('app', root)]))

    def test_build_artifacts_do_not_invalidate_image(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = setup.fingerprint([('mini', root)])
            (root / 'out').mkdir()
            (root / 'out' / 'object.o').write_bytes(b'build')
            (root / 'kernel.elf').write_bytes(b'build')
            (root / 'disk.img').write_bytes(b'build')
            self.assertEqual(first, setup.fingerprint([('mini', root)]))

    def test_cached_image_skips_all_remote_commands(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'STATE_DIR': folder}):
            app = setup.Setup()
            mini = Path(folder) / 'mini_os'
            mini.mkdir()
            (mini / 'Makefile').write_text('all:')
            source = '1234567890abcdef' * 4
            image = 'docker.io/library/mini-functions:pool-' + source[:16]
            (app.state / 'image.json').write_text(json.dumps({'source': source, 'image': image,
                                                            'nodes': app.nodes, 'control': app.control}))
            with patch.object(app, 'run') as run, patch.object(setup, 'fingerprint', return_value=source), \
                    patch.dict(os.environ, {'MINI_OS': str(mini)}):
                self.assertEqual(app.image(), image)
                run.assert_not_called()

    def test_full_run_order_and_cleanup(self):
        app = Mock(started=['router', 'server'])
        with patch.object(setup, 'Setup', return_value=app), patch('sys.argv', ['setup', 'test']):
            self.assertEqual(setup.main(), 0)
        self.assertEqual([call[0] for call in app.method_calls], ['server', 'load', 'stop', 'stop'])
        self.assertEqual(app.stop.call_args_list[0].args, ('server',))
        self.assertEqual(app.stop.call_args_list[1].args, ('router',))

    def test_retry_reuses_archive_and_skips_successful_nodes(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'STATE_DIR': folder, 'NODES': 'one two'}):
            mini = Path(folder) / 'mini'
            mini.mkdir()
            (mini / 'Makefile').write_text('all:')
            app = setup.Setup()
            source = 'a' * 64
            archive = app.state / ('image-' + source[:16] + '.tar')
            archive.write_bytes(b'completed archive')

            def import_failure(args, **kwargs):
                if args[1:3] == ['shell', 'two']:
                    raise subprocess.CalledProcessError(255, args)

            with patch.dict(os.environ, {'MINI_OS': str(mini)}), \
                    patch.object(setup, 'fingerprint', return_value=source), \
                    patch.object(app, 'build_archive') as build:
                with patch.object(app, 'run', side_effect=import_failure):
                    with self.assertRaises(subprocess.CalledProcessError):
                        app.image()
                self.assertFalse((app.state / 'image.json').exists())
                with patch.object(app, 'run') as run:
                    app.image()
                    imports = [call.args[0] for call in run.call_args_list if call.args[0][1] == 'shell']
                    self.assertEqual(len(imports), 1)
                    self.assertEqual(imports[0][2], 'two')
                    self.assertIn('--local', imports[0])
                    self.assertTrue(imports[0][-1].startswith('/var/tmp/'))
                build.assert_not_called()

    def test_build_and_export_use_disk_backed_paths(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'STATE_DIR': folder}):
            app = setup.Setup()
            archive = Path(folder) / 'finished.tar'

            def fake_run(args, **kwargs):
                if args[1] == 'tools/image_context.py':
                    Path(args[-1]).mkdir()
                elif args[:2] == ['limactl', 'copy'] and str(args[2]).startswith('mini-control:'):
                    Path(args[3]).write_bytes(b'completed image')

            with patch.object(app, 'run', side_effect=fake_run) as run:
                app.build_archive(Path(folder), 'abc', 'mini-functions:test', '/var/tmp/mini-pool-abc', archive)
            self.assertEqual(archive.read_bytes(), b'completed image')
            commands = '\n'.join(str(call.args[0]) for call in run.call_args_list)
            self.assertIn('TMPDIR=/var/tmp', commands)
            self.assertIn('docker-archive:/var/tmp/', commands)
            self.assertNotIn('docker-archive:/tmp/', commands)

    def test_partial_start_failure_cleans_up_only_its_process(self):
        app = Mock(started=['router'])
        app.server.side_effect = RuntimeError('port occupied')
        with patch.object(setup, 'Setup', return_value=app), patch('sys.argv', ['setup', 'server']):
            self.assertEqual(setup.main(), 1)
        app.stop.assert_called_once_with('router')

    def test_individual_start_leaves_services_running(self):
        app = Mock(started=['router', 'server'])
        with patch.object(setup, 'Setup', return_value=app), patch('sys.argv', ['setup', 'server']):
            self.assertEqual(setup.main(), 0)
        app.stop.assert_not_called()


if __name__ == '__main__':
    unittest.main()
