"""Behavioral tests for recursive folder conversion; no external packages."""
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import osmdroid_to_mbtiles as converter
from test_converter import png


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input = self.root / 'in'
        self.output = self.root / 'out'
        self.input.mkdir()
        self.tile = png()

    def archive(self, name, providers=('map',), tile=None):
        path = self.input / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE tiles (key INTEGER, provider TEXT, tile BLOB)')
            db.executemany('INSERT INTO tiles VALUES (4,?,?)',
                           ((provider, self.tile if tile is None else tile) for provider in providers))
            db.commit()
        return path

    def run_batch(self, **kwargs):
        return converter.batch_convert(self.input, self.output, source_crs='EPSG:3857', **kwargs)

    def test_recursive_mapping_keeps_extensions_and_splits_providers(self):
        self.archive('a.sqlite')
        self.archive('nested/a.db')
        self.archive('nested/two.SQLITEDB', providers=('one', 'two'))
        before = {p: hashlib.sha256(p.read_bytes()).digest() for p in self.input.rglob('*') if p.is_file()}
        report = self.run_batch()
        expected = {'a.sqlite.mbtiles', 'nested/a.db.mbtiles',
                    'nested/two.SQLITEDB.layer-001.mbtiles', 'nested/two.SQLITEDB.layer-002.mbtiles'}
        self.assertEqual({p.relative_to(self.output).as_posix() for p in self.output.rglob('*.mbtiles')}, expected)
        self.assertEqual(report['summary']['converted'], 4)
        self.assertEqual(report['summary']['failed'], 0)
        self.assertEqual(converter.batch_exit_code(report), 0)
        self.assertTrue(Path(report['report_file']).is_file())
        saved = json.loads(Path(report['report_file']).read_text())
        self.assertEqual(saved['summary'], report['summary'])
        for path, digest in before.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), digest)
        for path in self.output.rglob('*.mbtiles'):
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(dict(db.execute('SELECT * FROM metadata'))['format'], 'png')
                self.assertEqual(db.execute('SELECT * FROM tiles').fetchall(), [(1, 0, 1, self.tile)])

    def test_bad_archive_does_not_stop_valid_archive(self):
        self.archive('a-good.sqlite')
        (self.input / 'b-bad.db').write_bytes(b'not SQLite')
        self.archive('c-good.sqlite')
        report = self.run_batch()
        self.assertEqual((report['summary']['converted'], report['summary']['failed']), (2, 1))
        self.assertEqual(converter.batch_exit_code(report), 1)
        self.assertFalse(list(self.output.rglob('*.partial*')))

    def test_nonraster_payload_fails_and_produces_no_map(self):
        self.archive('vectors.sqlite', tile=b'\x1f\x8bvector-like payload')
        report = self.run_batch()
        self.assertEqual(report['summary']['failed'], 1)
        self.assertFalse(list(self.output.rglob('*.mbtiles')))

    def test_rerun_default_errors_then_explicit_skip_is_reported_unvalidated(self):
        self.archive('a.sqlite')
        first = self.run_batch()
        target = self.output / 'a.sqlite.mbtiles'
        before = target.read_bytes()
        second = self.run_batch()
        third = self.run_batch(existing='skip')
        self.assertEqual(second['summary']['failed'], 1)
        self.assertEqual(third['summary']['skipped_existing'], 1)
        self.assertIn('not validated', third['results'][0]['note'])
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(len({r['report_file'] for r in (first, second, third)}), 3)

    def test_overlap_rejected_before_outputs_created(self):
        for source, destination in [(self.input, self.input), (self.input, self.input/'out'),
                                    (self.input, self.root)]:
            with self.subTest(destination=destination), self.assertRaisesRegex(converter.ConversionError, 'separate'):
                converter.batch_convert(source, destination, source_crs='EPSG:3857')
        self.assertFalse((self.input/'out').exists())

    def test_empty_or_only_ignored_files_is_failure(self):
        (self.input/'notes.txt').write_text('not a map')
        report = self.run_batch()
        self.assertEqual(report['summary']['failed'], 1)
        self.assertEqual(report['ignored_files'], ['notes.txt'])
        self.assertFalse(list(self.output.rglob('*.mbtiles')))

    def test_same_stem_different_extensions_cannot_collide(self):
        self.archive('area.sqlite')
        self.archive('area.db')
        report = self.run_batch()
        self.assertEqual(report['summary']['converted'], 2)

    def test_case_collision_preflight(self):
        # Inject colliding jobs through a case-distinct archive pair only on a
        # filesystem that can represent them; Windows is already case-insensitive.
        self.archive('case.sqlite')
        second = self.input/'CASE.sqlite'
        if second.exists():
            self.skipTest('Filesystem is case-insensitive')
        self.archive('CASE.sqlite')
        report = self.run_batch()
        self.assertEqual(report['summary']['failed'], 2)
        self.assertFalse(list(self.output.rglob('*.mbtiles')))

    def make_symlink(self, target, link, is_dir=False):
        try:
            link.symlink_to(target, target_is_directory=is_dir)
        except OSError as error:
            self.skipTest('Symlink privilege unavailable: ' + str(error))

    def test_input_symlink_is_not_followed(self):
        path = self.archive('normal.sqlite')
        self.make_symlink(path, self.input/'linked.sqlite')
        report = self.run_batch()
        self.assertEqual((report['summary']['converted'], report['summary']['failed']), (1, 1))

    def test_linked_output_parent_is_not_followed(self):
        self.archive('nested/normal.sqlite')
        self.output.mkdir()
        outside = self.root/'outside'
        outside.mkdir()
        self.make_symlink(outside, self.output/'nested', is_dir=True)
        report = self.run_batch()
        self.assertEqual(report['summary']['failed'], 1)
        self.assertEqual(list(outside.iterdir()), [])

    def test_dangling_output_symlink_not_followed(self):
        self.archive('a.sqlite')
        self.output.mkdir()
        outside = self.root/'must-not-exist.mbtiles'
        self.make_symlink(outside, self.output/'a.sqlite.mbtiles')
        report = self.run_batch(existing='skip')
        self.assertEqual(report['summary']['failed'], 1)
        self.assertFalse(outside.exists())

    def test_cancellation_preserves_completed_output_and_reports_remaining(self):
        self.archive('a.sqlite')
        self.archive('b.sqlite')
        self.archive('c.sqlite')
        real_convert = converter.convert
        def interrupt_second(source, *args, **kwargs):
            if Path(source).name == 'b.sqlite':
                raise KeyboardInterrupt
            return real_convert(source, *args, **kwargs)
        with patch.object(converter, 'convert', side_effect=interrupt_second):
            report = self.run_batch()
        self.assertEqual(converter.batch_exit_code(report), 130)
        self.assertEqual([r['status'] for r in report['results']], ['converted', 'cancelled', 'not_run'])
        self.assertTrue((self.output/'a.sqlite.mbtiles').exists())
        self.assertTrue(Path(report['report_file']).exists())

    def test_batch_cli_exit_status_and_json_summary(self):
        self.archive('a.sqlite')
        with patch('sys.stdout', new_callable=io.StringIO) as output, patch('sys.stderr', new_callable=io.StringIO):
            code = converter.main(['batch', str(self.input), str(self.output), '--source-crs', 'EPSG:3857'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())['summary']['converted'], 1)

    def test_nocase_provider_column_does_not_merge_layers(self):
        path = self.input/'providers.sqlite'
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE tiles (key INTEGER, provider TEXT COLLATE NOCASE, tile BLOB)')
            db.executemany('INSERT INTO tiles VALUES (4,?,?)', [('Map', self.tile), ('map', png(rgb=(255,0,0)))])
            db.commit()
        report = self.run_batch()
        self.assertEqual(report['summary']['converted'], 2)
        self.assertEqual({item['provider'] for item in report['results']}, {'Map', 'map'})

    def test_portable_names_reject_reserved_or_illegal_paths(self):
        for name in ('CON.sqlite', 'bad?.sqlite', 'bad:name.db', 'trailing. /x.sqlite'):
            with self.subTest(name=name), self.assertRaises(converter.ConversionError):
                converter.portable_relative_path(Path(name))

    def test_declared_grid_and_existing_policy_checked(self):
        self.archive('a.sqlite')
        with self.assertRaises(converter.ConversionError):
            converter.batch_convert(self.input, self.output)
        with self.assertRaises(converter.ConversionError):
            self.run_batch(existing='overwrite')

    @unittest.skipUnless(os.name == 'nt', 'Windows launcher test')
    def test_windows_launcher_runs_folder_conversion(self):
        self.archive('a.sqlite')
        launcher = Path(__file__).resolve().parent / 'Convert folder.cmd'
        result = subprocess.run(['cmd.exe', '/d', '/c', str(launcher)],
                                input='{}\n{}\nyes\n\n'.format(self.input, self.output),
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.output/'a.sqlite.mbtiles').is_file())

    @unittest.skipUnless(os.name == 'nt', 'Windows launcher test')
    def test_windows_launcher_propagates_conversion_failure(self):
        (self.input/'bad.sqlite').write_bytes(b'not SQLite')
        launcher = Path(__file__).resolve().parent / 'Convert folder.cmd'
        result = subprocess.run(['cmd.exe', '/d', '/c', str(launcher)],
                                input='{}\n{}\nyes\n\n'.format(self.input, self.output),
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(list(self.output.rglob('*.mbtiles')))


if __name__ == '__main__':
    unittest.main()
