# OSMDroid raster MBTiles converter

Convert a folder of existing OSMDroid SQLite map archives into a folder of **raster MBTiles** files. The converter copies PNG or JPEG map tiles unchanged, decodes their OSMDroid coordinates, and writes the MBTiles schema and row numbering.

The runtime is Python plus its built-in SQLite library. It works locally without network access, accounts, API keys, extra Python packages, map downloads, or commercial tools. The source code is MIT licensed.

## Quick start on Windows

1. Install a supported Python 3 release. For a disconnected machine, transfer the full offline installer in advance. See [Windows setup](docs/windows.md).
2. Download or clone this repository and extract it to a local folder.
3. Place exported, closed OSMDroid archives in `in`. Subfolders are supported.
4. Double-click **Convert folder.cmd**, accept the `in` and `out` folder defaults, and confirm the source uses a standard **Web Mercator EPSG:3857 XYZ tile grid**.
5. Collect the `.mbtiles` files from `out`. Read the generated `conversion-report-*.json` for any failures or ignored files.

The projection confirmation declares what the input already is. It does not reproject a different grid. Confirm it from the original map-source configuration; SQLite keys and image bytes cannot establish that by themselves.

For a terminal workflow, from this repository folder:

```powershell
$env:PYTHON_MANAGER_AUTOMATIC_INSTALL = "false"
py -3 osmdroid_to_mbtiles.py batch ".\in" ".\out" --source-crs EPSG:3857
```

Use `python` in place of `py -3` if that is how your installed runtime is invoked. Linux and macOS use `python3` with the same arguments. QGIS is optional for viewing the output; it is not a conversion dependency.

## File mapping

The input extension is retained in the output name to avoid collisions between archives with the same stem. Provider numbers come from the archive's sorted provider list; names never become filesystem paths.

| Input | Output |
| --- | --- |
| `in/area.sqlite` with one provider | `out/area.sqlite.mbtiles` |
| `in/region/area.db` with one provider | `out/region/area.db.mbtiles` |
| `in/layers.sqlite` with two providers | `out/layers.sqlite.layer-001.mbtiles` and `out/layers.sqlite.layer-002.mbtiles` |

Each output holds one provider. Every stored tile and zoom level for that provider is retained. A failed output leaves successful outputs from the same run available. Missing tiles stay missing; the program does not download or generate additional coverage.

## Supported inputs and output

| Property | Supported |
| --- | --- |
| Input schema | SQLite `tiles(key, provider, tile)` |
| Tile grid | Standard OSMDroid Web Mercator XYZ |
| Images | 256 by 256 PNG, or 256 by 256 JPEG, consistent within each provider |
| Input filenames scanned recursively | `.sqlite`, `.sqlite3`, `.sqlitedb`, `.db`, `.mbtiles`, case insensitive |
| Output | Raster MBTiles with `format=png` or `format=jpg` and unchanged image BLOBs |
| Coordinate conversion | Decode OSMDroid key, then `tile_row = 2^zoom - 1 - y` |

A filename extension does not establish its database format. Existing standard MBTiles, including vector MBTiles, are rejected by this OSMDroid reader. Vector data must be rendered to images in a different workflow; changing its metadata to `png` does not rasterize it.

This version rejects mixed image encodings within one provider, non-256px tiles, WebP, alternate projections, alternate key layouts, duplicate coordinates, unsupported schemas, and recognizable invalid images. It checks image signatures and dimension headers, **not full image decoding**. See [format details](docs/format.md).

## Reports and reruns

Each run writes a separate JSON report in `out`, recording conversion results per provider, errors, ignored filenames, counts, and output metadata. Reports remain local and may include local paths, provider names and map bounds.

Existing outputs are errors by default. They are never replaced. To intentionally skip existing regular files:

```powershell
py -3 osmdroid_to_mbtiles.py batch ".\in" ".\out" --source-crs EPSG:3857 --existing skip
```

Skipped files are **not validated**. This option is convenient for continuing a partially completed run, but it does not check whether an earlier output matches a changed input. For a fresh conversion, choose a new output folder.

| Exit code | Meaning |
| --- | --- |
| `0` | All planned outputs converted or explicitly skipped; no reported failures |
| `1` | Conversion/setup failure, or no candidate archives |
| `2` | Invalid command-line usage |
| `130` | Interrupted by the user |

Input and output folders must be separate and cannot contain each other. Internal symlinks and Windows reparse points are rejected. Files with unsupported extensions are listed as ignored. A corrupt archive does not prevent other valid archives from being converted.

## Inspect or convert a single archive

```powershell
py -3 osmdroid_to_mbtiles.py inspect "C:\Maps\area.sqlite"
py -3 osmdroid_to_mbtiles.py convert "C:\Maps\area.sqlite" "C:\Maps\area.mbtiles" --source-crs EPSG:3857
```

For a multi-provider archive, use the layer number printed by `inspect`, for example `--layer 2`. Recognized source attribution is retained. Single-file conversion also accepts `--name` and `--attribution`.

## Verification and limits

Run the synthetic tests locally:

```powershell
py -3 -m unittest discover -v
```

GitHub Actions runs the same suite on Windows and Linux with Python 3.11 and 3.13. Windows-specific tests execute the `.cmd` launcher. See [development and verification](docs/development.md) and the repository's Actions tab for run results.

The program checks SQLite integrity, coordinate validity, image headers, output counts and duplicate coordinates. It preserves source files and image bytes. It does not establish that the source map is correctly georeferenced, that every image is decodable, or that a particular device accepts the result. This is a map-file converter; it does not operate drones or transfer files to controllers.

Maps and generated reports are not included in this repository. The default `in` and `out` contents and common map extensions are ignored by Git. The software license does not change a source map's data license.

## References

- [OSMDroid archive schema](https://github.com/osmdroid/osmdroid/blob/master/osmdroid-android/src/main/java/org/osmdroid/tileprovider/modules/DatabaseFileArchive.java)
- [OSMDroid key encoding](https://github.com/osmdroid/osmdroid/blob/master/osmdroid-android/src/main/java/org/osmdroid/tileprovider/modules/SqlTileWriter.java)
- [MBTiles specification](https://github.com/mapbox/mbtiles-spec/blob/master/1.3/spec.md)
- [Python SQLite documentation](https://docs.python.org/3/library/sqlite3.html)

## License

[MIT](LICENSE).
