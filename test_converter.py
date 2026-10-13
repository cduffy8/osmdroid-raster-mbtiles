"""Offline tests using only the standard library. Run: py -3 -m unittest -v"""
import hashlib
import io
import json
from pathlib import Path
import random
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

import osmdroid_to_mbtiles as converter


def png(width=256, height=256, rgb=(64, 128, 192)):
    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data +
                struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF))
    rows = (b'\0' + bytes(rgb) * width) * height
    return (b'\x89PNG\r\n\x1a\n' +
            chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


def encoded(z, x, y):
    return z * 4**z + x * 2**z + y


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.source = Path(self.folder.name) / 'map with spaces #1.sqlite'
        self.output = Path(self.folder.name) / 'result.mbtiles'
        self.blue = png()

    def archive(self, rows, projection=None, attribution=None):
        with sqlite3.connect(self.source) as db:
            db.execute('CREATE TABLE tiles (key INTEGER, provider TEXT, tile BLOB)')
            db.executemany('INSERT INTO tiles VALUES (?,?,?)', rows)
            if projection is not None or attribution is not None:
                db.execute('CREATE TABLE atak_metadata (key TEXT, value TEXT)')
            if projection is not None:
                db.execute('INSERT INTO atak_metadata VALUES (?,?)', ('srid', projection))
            if attribution is not None:
                db.execute('INSERT INTO atak_metadata VALUES (?,?)', ('attribution', attribution))

    def run_conversion(self, **kwargs):
        return converter.convert(self.source, self.output, source_crs='EPSG:3857', **kwargs)

    def test_known_mbtiles_spec_coordinate_and_byte_preservation(self):
        # MBTiles specification's independent example: XYZ 11/327/791 -> TMS 1256.
        self.archive([(46807831, 'imagery', self.blue)])
        before = hashlib.sha256(self.source.read_bytes()).digest()
        result = self.run_conversion()
        with sqlite3.connect(self.output) as db:
            self.assertEqual(db.execute('SELECT * FROM tiles').fetchall(), [(11, 327, 1256, self.blue)])
            meta = dict(db.execute('SELECT name, value FROM metadata'))
            self.assertEqual((meta['minzoom'], meta['maxzoom'], meta['format']), ('11', '11', 'png'))
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
        self.assertEqual(result['tiles'], 1)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).digest(), before)

    def test_key_boundaries_and_random_roundtrips(self):
        rng = random.Random(73)
        for z in range(30):
            n = 2**z
            for x, y in [(0, 0), (n - 1, n - 1), (rng.randrange(n), rng.randrange(n))]:
                self.assertEqual(converter.decode_key(encoded(z, x, y)), (z, x, y))
        for invalid in [-1, 1, 2, 3, 8, 31, 48, 2**63 - 1, '4', 4.0, None]:
            with self.subTest(invalid=invalid), self.assertRaises(converter.ConversionError):
                converter.decode_key(invalid)

    def test_all_stored_zooms_preserved_and_bounds_shared_across_zooms(self):
        self.archive([(0, 'base', self.blue), (encoded(3, 3, 2), 'base', self.blue)])
        result = self.run_conversion()
        self.assertEqual(sorted(result['zooms']), [0, 3])
        west, south, east, north = result['bounds']
        self.assertEqual((west, east), (-45, 0))
        self.assertAlmostEqual(north, 66.51326044311186)
        self.assertAlmostEqual(south, 40.97989806962013)

    def test_disjoint_zoom_coverage_omits_bounds_without_dropping_tiles(self):
        self.archive([(encoded(1, 0, 0), 'base', self.blue), (encoded(2, 3, 3), 'base', self.blue)])
        result = self.run_conversion()
        self.assertIsNone(result['bounds'])
        with sqlite3.connect(self.output) as db:
            self.assertNotIn('bounds', dict(db.execute('SELECT * FROM metadata')))
            self.assertEqual(db.execute('SELECT count(*) FROM tiles').fetchone()[0], 2)

    def test_exact_one_tile_geography(self):
        self.archive([(4, 'base', self.blue)])  # z=1 x=0 y=0 (northwest quadrant)
        result = self.run_conversion()
        west, south, east, north = result['bounds']
        self.assertEqual((west, south, east), (-180, 0, 0))
        self.assertAlmostEqual(north, 85.0511287798066)

    def test_multiple_providers_require_selection_and_do_not_merge(self):
        red = png(rgb=(255, 0, 0))
        self.archive([(4, 'a', self.blue), (4, 'b', red)])
        with self.assertRaisesRegex(converter.ConversionError, 'Multiple providers'):
            self.run_conversion()
        self.assertFalse(self.output.exists())
        result = self.run_conversion(layer=2)
        self.assertEqual(result['provider'], 'b')
        with sqlite3.connect(self.output) as db:
            self.assertEqual(db.execute('SELECT tile_data FROM tiles').fetchall(), [(red,)])

    def test_null_provider_supported(self):
        self.archive([(4, None, self.blue)])
        self.assertEqual(self.run_conversion()['tiles'], 1)

    def test_metadata_crs_conflict_is_not_overridden(self):
        self.archive([(4, 'base', self.blue)], projection='4326')
        with self.assertRaisesRegex(converter.ConversionError, 'projection'):
            self.run_conversion()
        self.assertFalse(self.output.exists())

    def test_source_crs_must_be_declared(self):
        self.archive([(4, 'base', self.blue)])
        with self.assertRaisesRegex(converter.ConversionError, 'source-crs'):
            converter.convert(self.source, self.output)

    def test_supported_crs_and_attribution_retained(self):
        self.archive([(4, 'base', self.blue)], projection='3857', attribution='Synthetic test map')
        self.run_conversion()
        with sqlite3.connect(self.output) as db:
            self.assertEqual(dict(db.execute('SELECT * FROM metadata'))['attribution'], 'Synthetic test map')

    def test_duplicates_fail_without_partial_output(self):
        self.archive([(4, 'base', self.blue), (4, 'base', self.blue)])
        with self.assertRaisesRegex(converter.ConversionError, 'Duplicate'):
            self.run_conversion()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(Path(self.folder.name).glob('*.partial*')), [])

    def test_invalid_image_and_key_do_not_publish_output(self):
        for key, data in [(4, b'not an image'), (1, self.blue), (4, b''), (4, png(512, 512)), (4, None)]:
            if self.source.exists():
                self.source.unlink()
            self.archive([(key, 'base', data)])
            before = self.source.read_bytes()
            with self.subTest(key=key, size=len(data or b'')), self.assertRaises(converter.ConversionError):
                self.run_conversion()
            self.assertFalse(self.output.exists())
            self.assertEqual(self.source.read_bytes(), before)
            self.assertEqual(list(Path(self.folder.name).glob('*.partial*')), [])

    def test_existing_output_and_input_never_overwritten(self):
        self.archive([(4, 'base', self.blue)])
        self.output.write_bytes(b'keep me')
        with self.assertRaises(converter.ConversionError):
            self.run_conversion()
        self.assertEqual(self.output.read_bytes(), b'keep me')
        with self.assertRaises(converter.ConversionError):
            converter.convert(self.source, self.source, source_crs='EPSG:3857')

    def test_journal_rejected(self):
        self.archive([(4, 'base', self.blue)])
        Path(str(self.source) + '-wal').write_bytes(b'pending write data')
        with self.assertRaisesRegex(converter.ConversionError, 'WAL/journal'):
            self.run_conversion()

    def test_wrong_schema_rejected(self):
        with sqlite3.connect(self.source) as db:
            db.execute('CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)')
        with self.assertRaisesRegex(converter.ConversionError, 'Expected tiles'):
            self.run_conversion()

    def test_inspect_cli_does_not_modify_source(self):
        self.archive([(4, 'base', self.blue)])
        before = self.source.read_bytes()
        output = io.StringIO()
        with patch('sys.stdout', output):
            self.assertEqual(converter.main(['inspect', str(self.source)]), 0)
        self.assertEqual(json.loads(output.getvalue())['layers'][0]['tiles'], 1)
        self.assertEqual(self.source.read_bytes(), before)

    def test_keyboard_interrupt_cleans_partial(self):
        self.archive([(4, 'base', self.blue)])
        with patch.object(converter, 'image_header', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_conversion()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(Path(self.folder.name).glob('*.partial*')), [])

    def test_output_appearing_during_conversion_preserved(self):
        self.archive([(4, 'base', self.blue)])
        def progress(message):
            if message.startswith('Copying'):
                self.output.write_bytes(b'created by another process')
        with self.assertRaises(OSError):
            self.run_conversion(progress=progress)
        self.assertEqual(self.output.read_bytes(), b'created by another process')
        self.assertEqual(list(Path(self.folder.name).glob('*.partial*')), [])

    def test_jpeg_header_and_mixed_format_rejection(self):
        # Minimal structural JPEG header exercises identification, not decoding.
        header = b'\xff\xd8\xff\xc0\x00\x11\x08\x01\x00\x01\x00\x03' + b'\x01\x11\x00\x02\x11\x01\x03\x11\x01' + b'\xff\xd9'
        self.assertEqual(converter.image_header(header), ('jpg', 256, 256))
        self.archive([(4, 'base', self.blue), (5, 'base', header)])
        with self.assertRaisesRegex(converter.ConversionError, 'Mixed PNG/JPEG'):
            self.run_conversion()
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
