# Development and verification

## Code layout

| File | Purpose |
| --- | --- |
| `osmdroid_to_mbtiles.py` | Source inspection, coordinate decoding, single-file conversion, recursive batching, reports and CLI |
| `Convert folder.cmd` | Windows text wizard launcher, preserving the converter exit code |
| `test_converter.py` | Core conversion and source/output integrity checks |
| `test_batch.py` | Folder mapping, partial failure, reruns, links, cancellation, and Windows launcher tests |
| `.github/workflows/tests.yml` | Windows/Linux test matrix with pinned official Actions revisions |

The tool and tests require only the Python standard library. No installation or build step is needed beyond installing Python. Tests generate synthetic map archives inside temporary directories.

## Run checks

```sh
python -m unittest discover -v
python osmdroid_to_mbtiles.py --help
```

The suite contains 37 tests. On the local macOS Python 3.9.6 run for version 0.2.0, 34 passed and 3 were skipped: two require Windows and one requires a filesystem that can store case-distinct paths. GitHub Actions runs Python 3.11 and 3.13 on Windows and Linux; consult the actual run for platform-specific pass/skip results. A skip is not a successful execution of that test.

Coverage includes the MBTiles specification's known coordinate example; key boundaries and invalid gaps; byte preservation; sparse zooms and bounds; case-sensitive provider separation; attribution; malformed inputs; existing output protection; temporary cleanup; nested folder mapping; source hashes; multiple providers; per-file failures; explicit unvalidated skips; links and reparse points; and return codes. Windows-only tests execute the batch launcher with both a successful conversion and a failing archive.

The converter does not certify device import or validate an actual operational map archive. Image checks use headers, and the synthetic JPEG unit test exercises header parsing rather than full decoding. Add real-format synthetic image fixtures or an optional open-source decoder if extending payload validation.

A separate local Python 3.12.14 integration check ran the version 0.2.0 folder CLI over two synthetic archives containing 11,000 PNG tiles and 11,000 JPEG tiles. Both converted successfully. Every output image BLOB matched its input, source SHA-256 hashes stayed unchanged, coordinate ranges/counts were correct, and SQLite integrity checks passed. Pillow independently decoded output samples and confirmed unchanged pixels. Pillow was used only for this development check and is not required by the converter or the included test suite.

## Change the behavior deliberately

Keep the main interface as `batch INPUT OUTPUT --source-crs EPSG:3857`. Keep the runtime local, deterministic for a given exported archive, and free of network dependencies. Do not fix an unsupported input by silently dropping tiles, swapping providers, ignoring contradictory projection metadata or relabeling vector payloads as raster.

For a new source variant, first document its schema and tiling grid and create a minimal synthetic fixture. Implement the variant only after its coordinate mapping and image encoding are understood. If reprojection or image normalization is added, make those operations explicit because they change image pixels and may change tile coverage.

Tests should target observable conversion behavior: output coordinates, bytes, metadata, file mapping, failure cleanup, and error status. Avoid assuming that one successful SQLite integrity check establishes geographic accuracy.

## Keep generated data local

The default data folders, common map extensions, reports and private handoff names are ignored by Git. Check staged filenames and diffs before committing. Use synthetic fixtures in tests; maps and local reports are not needed to reproduce standard format behavior.

## License and compatibility

Project code is MIT licensed. Python/SQLite provide the runtime. No OSMDroid or Skydio SDK is embedded. The converter implements a documented file format and makes no claim of vendor certification.
