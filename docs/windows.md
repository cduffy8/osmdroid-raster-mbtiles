# Windows installation and operation

## Install the runtime once

Use a currently supported Python 3 release from [Python for Windows](https://www.python.org/downloads/windows/). The converter uses only standard-library modules; no `pip install`, GDAL, QGIS, Docker or Java installation is required.

For a disconnected computer, obtain the full offline Windows installer and transfer it along with this repository. A small bootstrap installer or Python Install Manager package alone may not include an actual Python runtime. Complete installation before attempting offline conversion. Select the launcher or add Python to PATH when offered by your installation method.

Check the runtime in a terminal:

```powershell
py -3 -c "import sys, sqlite3; print(sys.version); print(sqlite3.sqlite_version)"
```

If your installation exposes `python` instead, use that command. A full path to the installed `python.exe` works as well.

The supplied launcher sets `PYTHON_MANAGER_AUTOMATIC_INSTALL=false`, so supported Python Install Manager installations do not automatically download a missing runtime for that launch. For terminal commands, set it explicitly in PowerShell:

```powershell
$env:PYTHON_MANAGER_AUTOMATIC_INSTALL = "false"
```

In Command Prompt, use `set PYTHON_MANAGER_AUTOMATIC_INSTALL=false`. See the [official Windows Python documentation](https://docs.python.org/3/using/windows.html) for installer and launcher behavior.

## Use the folder launcher

1. Extract the repository ZIP. Running directly from Windows' compressed-folder view is not supported.
2. Copy source archives into the `in` folder. Keep originals/exported snapshots intact.
3. Double-click `Convert folder.cmd`.
4. Press Enter to accept the adjacent `in` and `out` directories, or paste other local folder paths.
5. Type `yes` only after confirming the inputs use the supported Web Mercator XYZ grid.
6. Let the run finish. Review the summary and JSON report path printed in the terminal.

There is one grid confirmation per folder run. Conflicting recognized metadata still causes that archive to fail. The converter does not infer a projection from the map's appearance or change an unsupported projection.

To convert into a new output folder without prompts:

```powershell
py -3 osmdroid_to_mbtiles.py batch "C:\LocalMaps\in" "C:\LocalMaps\out-new" --source-crs EPSG:3857
```

The output directory is created if necessary. The input directory must already exist. Reserve free space for a second copy of the imagery plus database indexes and temporary journaling; output size is not guaranteed to equal input size.

## Understand failures

| Message or result | Next step |
| --- | --- |
| Python command missing | Install the full runtime or use its executable path |
| No supported archive filenames | Check input directory, extensions, and whether files are still inside a ZIP |
| Expected `tiles(key, provider, tile)` | Inspect the source format; ordinary MBTiles, OsmAnd and GeoPackage differ |
| Unsupported projection | Inspect the original map-source definition; do not relabel the CRS |
| Mixed PNG/JPEG or non-256px image | This version requires a separate normalization step before conversion |
| WAL/journal present | Export a consistent standalone snapshot; do not delete sidecars to force acceptance |
| Output already exists | Use a new output directory, or explicitly skip existing files |
| Duplicate coordinates | Inspect conflicting source entries instead of silently choosing one |
| Access denied or insufficient space | Choose a writable local output directory and check free space |
| Some outputs failed | Read per-output errors; completed outputs remain available |

An MBTiles file that opens in a SQLite viewer is not necessarily a usable map. The converter checks structure and headers; a local GIS viewer can help inspect source placement and image decoding when needed.
