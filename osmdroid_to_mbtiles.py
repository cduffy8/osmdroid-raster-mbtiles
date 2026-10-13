#!/usr/bin/env python3
"""Offline OSMDroid SQLite -> raster MBTiles. MIT license; see LICENSE.

Supports standard Web Mercator OSMDroid keys and 256px PNG/JPEG tiles.
Uses only the Python standard library. No network or subprocess calls.
"""

import argparse
from bisect import bisect_right
from contextlib import closing
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import uuid

VERSION = "0.2.0"
KEY_STARTS = tuple(z << (2 * z) for z in range(30))
MAX_TILE_BYTES = 64 * 1024 * 1024
INPUT_EXTENSIONS = {".sqlite", ".sqlite3", ".sqlitedb", ".db", ".mbtiles"}


class ConversionError(Exception):
    pass


def decode_key(key):
    """Invert key = z * 4**z + x * 2**z + y, with XYZ row direction."""
    if type(key) is not int or key < 0:
        raise ConversionError("A tile key is not a nonnegative integer.")
    z = bisect_right(KEY_STARTS, key) - 1
    if z < 0 or key >= ((z + 1) << (2 * z)):
        raise ConversionError("A tile key is outside standard OSMDroid key ranges.")
    n = 1 << z
    return z, (key >> z) & (n - 1), key & (n - 1)


def image_header(blob):
    """Identify PNG/JPEG dimensions; this is not a full image-decoder check."""
    if not isinstance(blob, bytes) or not blob:
        raise ConversionError("A tile is empty or is not a SQLite BLOB.")
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(blob) < 33 or blob[8:16] != b"\x00\x00\x00\x0dIHDR":
            raise ConversionError("A PNG tile has a malformed header.")
        width, height = struct.unpack(">II", blob[16:24])
        return "png", width, height
    if blob.startswith(b"\xff\xd8"):
        offset = 2
        sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
               0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        while offset < len(blob):
            if blob[offset] != 0xFF:
                break
            while offset < len(blob) and blob[offset] == 0xFF:
                offset += 1
            if offset >= len(blob):
                break
            marker = blob[offset]
            offset += 1
            if marker in (0xDA, 0xD9):
                break
            if marker == 0x01 or 0xD0 <= marker <= 0xD7:
                continue
            if offset + 2 > len(blob):
                break
            length = int.from_bytes(blob[offset:offset + 2], "big")
            if length < 2 or offset + length > len(blob):
                break
            if marker in sof:
                if length < 8:
                    break
                height, width = struct.unpack(">HH", blob[offset + 3:offset + 7])
                return "jpg", width, height
            offset += length
        raise ConversionError("A JPEG tile has no readable dimension header.")
    raise ConversionError("Unsupported tile image. This version accepts PNG or JPEG only.")


def quote_identifier(name):
    return '"' + name.replace('"', '""') + '"'


def open_source(filename):
    path = Path(filename).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ConversionError("The input must be a local SQLite file.")
    # Work with an exported, closed archive, not a phone's changing cache.
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ConversionError(
                "The archive has an active WAL/journal. Export a standalone SQLite "
                "snapshot first; do not delete the sidecar or copy only the main database."
            )
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN")
        return db
    except BaseException:
        db.close()
        raise


def describe(db):
    tables = dict(db.execute(
        "SELECT lower(name), name FROM sqlite_master WHERE type='table'"
    ))
    schema = {}
    for low_name, actual_name in tables.items():
        schema[low_name] = [row[1] for row in db.execute(
            "PRAGMA table_info(" + quote_identifier(actual_name) + ")"
        )]
    if "tiles" not in schema:
        raise ConversionError("No tiles table found; this is not a supported OSMDroid archive.")
    columns = {column.lower() for column in schema["tiles"]}
    if not {"key", "provider", "tile"}.issubset(columns):
        raise ConversionError(
            "Expected tiles(key, provider, tile). This may be MBTiles, OsmAnd, "
            "GeoPackage, or another SQLite layout. Renaming its extension is not a conversion."
        )
    providers = []
    for provider, count in db.execute(
        'SELECT provider, count(*) FROM tiles GROUP BY provider COLLATE BINARY ORDER BY provider COLLATE BINARY'
    ):
        if provider is not None and not isinstance(provider, str):
            raise ConversionError("Unsupported provider value: expected text or NULL.")
        providers.append({"layer": len(providers) + 1, "provider": provider, "tiles": count})
    if not providers:
        raise ConversionError("The archive contains no tiles.")
    # Read only conventional metadata. The explicit CRS argument remains required:
    # arbitrary SQLite schemas do not reliably describe their projection.
    hints = []
    attribution = []
    for table in ("atak_metadata", "metadata"):
        if table not in schema:
            continue
        colmap = {c.lower(): c for c in schema[table]}
        keycol = colmap.get("key", colmap.get("name"))
        if keycol and "value" in colmap:
            sql = "SELECT {}, {} FROM {}".format(
                quote_identifier(keycol), quote_identifier(colmap["value"]),
                quote_identifier(tables[table])
            )
            for key, value in db.execute(sql):
                normalized = str(key).strip().lower()
                if normalized in ("srid", "srs", "crs", "projection"):
                    hints.append({"table": table, "key": str(key), "value": str(value)})
                if normalized == "attribution" and value:
                    attribution.append(str(value))
        if "srid" in colmap:
            for row in db.execute("SELECT DISTINCT {} FROM {}".format(
                quote_identifier(colmap["srid"]), quote_identifier(tables[table])
            )):
                hints.append({"table": table, "key": "srid", "value": str(row[0])})
    return {"schema": schema, "layers": providers, "projection_hints": hints,
            "attribution": list(dict.fromkeys(attribution))}


def select_layer(info, layer):
    if layer is None:
        if len(info["layers"]) != 1:
            raise ConversionError("Multiple providers found. Inspect the archive and select --layer N.")
        return info["layers"][0]
    if not 1 <= layer <= len(info["layers"]):
        raise ConversionError("Layer number is outside the listed range.")
    return info["layers"][layer - 1]


def check_projection(info, source_crs):
    if source_crs != "EPSG:3857":
        raise ConversionError(
            "Confirm the input uses the standard Web Mercator XYZ tile grid, then "
            "pass --source-crs EPSG:3857. This declares the source; it does not reproject it."
        )
    accepted = {"3857", "EPSG:3857", "900913", "EPSG:900913", "102100", "EPSG:102100"}
    for hint in info["projection_hints"]:
        if hint["value"].strip().upper() not in accepted:
            raise ConversionError(
                "Source metadata has an unsupported or unrecognized projection: "
                + json.dumps(hint) + ". Reprojection needs a separate GIS workflow."
            )


def latitude(y, z):
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / (1 << z)))))


def convert(source, destination, source_crs=None, layer=None, name=None,
            attribution=None, progress=None):
    source = Path(source).expanduser().resolve(strict=True)
    destination = Path(destination).expanduser().absolute()
    if os.path.lexists(destination):
        raise ConversionError("Output already exists or matches the input. Choose a new output filename.")
    destination = destination.resolve()
    if source == destination:
        raise ConversionError("Output matches the input. Choose a new output filename.")
    if not destination.parent.is_dir():
        raise ConversionError("The output folder does not exist.")
    if destination.suffix.lower() != ".mbtiles":
        raise ConversionError("The output filename must end in .mbtiles.")
    temporary = None
    try:
        with closing(open_source(source)) as db:
            info = describe(db)
            selected = select_layer(info, layer)
            check_projection(info, source_crs)
            if progress:
                progress("Checking SQLite integrity...")
            if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ConversionError("The input failed SQLite quick_check.")
            provider = selected["provider"]
            invalid = db.execute(
                "SELECT 1 FROM tiles WHERE provider COLLATE BINARY IS ? AND "
                "(typeof(tile) != 'blob' OR length(tile)=0 OR length(tile)>?) LIMIT 1",
                (provider, MAX_TILE_BYTES)
            ).fetchone()
            if invalid:
                raise ConversionError("A tile is empty, not a BLOB, or larger than the supported 64 MiB limit.")
            fd, temporary = tempfile.mkstemp(prefix=destination.stem + ".", suffix=".partial", dir=destination.parent)
            os.close(fd)
            zooms = {}
            tile_format = None
            count = 0
            with closing(sqlite3.connect(temporary)) as out:
                out.executescript(
                    "PRAGMA journal_mode=DELETE;"
                    "CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT);"
                    "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
                    "tile_row INTEGER, tile_data BLOB);"
                    "CREATE UNIQUE INDEX tile_index ON tiles (zoom_level,tile_column,tile_row);"
                )
                out.execute("BEGIN")
                if progress:
                    progress("Copying {} tiles without changing image bytes...".format(selected["tiles"]))
                for key, blob in db.execute("SELECT key, tile FROM tiles WHERE provider COLLATE BINARY IS ?", (provider,)):
                    z, x, y = decode_key(key)
                    fmt, width, height = image_header(blob)
                    if (width, height) != (256, 256):
                        raise ConversionError("This version supports 256 x 256 tiles only; found {} x {}.".format(width, height))
                    if tile_format is not None and fmt != tile_format:
                        raise ConversionError("Mixed PNG/JPEG tiles need normalization before conversion; this version preserves images exactly.")
                    tile_format = fmt
                    try:
                        out.execute("INSERT INTO tiles VALUES (?,?,?,?)", (z, x, (1 << z) - 1 - y, blob))
                    except sqlite3.IntegrityError as error:
                        raise ConversionError("Duplicate tile coordinates found within the selected provider.") from error
                    stats = zooms.setdefault(z, {"count": 0, "min_x": x, "max_x": x, "min_y": y, "max_y": y})
                    stats["count"] += 1
                    stats["min_x"], stats["max_x"] = min(stats["min_x"], x), max(stats["max_x"], x)
                    stats["min_y"], stats["max_y"] = min(stats["min_y"], y), max(stats["max_y"], y)
                    count += 1
                    if count % 10000 == 0:
                        out.commit()
                        out.execute("BEGIN")
                        if progress:
                            progress("Copied {:,} / {:,} tiles".format(count, selected["tiles"]))
                if count != selected["tiles"] or count == 0:
                    raise ConversionError("Tile count did not match the source snapshot.")
                minzoom, maxzoom = min(zooms), max(zooms)
                # MBTiles bounds describe an area common to the stored zooms.
                # Keep every tile, but omit optional bounds if their envelopes
                # do not intersect. Rectangular bounds cannot describe holes.
                bounds = [max(s["min_x"] / (1 << z) * 360 - 180 for z, s in zooms.items()),
                          max(latitude(s["max_y"] + 1, z) for z, s in zooms.items()),
                          min((s["max_x"] + 1) / (1 << z) * 360 - 180 for z, s in zooms.items()),
                          min(latitude(s["min_y"], z) for z, s in zooms.items())]
                if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
                    bounds = None
                metadata = {"name": name or destination.stem, "format": tile_format,
                            "type": "baselayer", "version": "1.0",
                            "description": "Offline raster tiles converted from an OSMDroid archive",
                            "minzoom": str(minzoom), "maxzoom": str(maxzoom)}
                if bounds is not None:
                    metadata["bounds"] = ",".join(format(value, ".14g") for value in bounds)
                attr = attribution if attribution is not None else " | ".join(info["attribution"])
                if attr:
                    metadata["attribution"] = attr
                out.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
                out.commit()
                if out.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise ConversionError("The output failed SQLite quick_check.")
                if out.execute("SELECT count(*) FROM tiles").fetchone()[0] != count:
                    raise ConversionError("The output tile count did not match.")
        # Publish without overwriting an existing destination, even if it appeared
        # during conversion. Windows rename refuses overwrite; POSIX link does too.
        if os.name == "nt":
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
            os.unlink(temporary)
        temporary = None
        return {"output": str(destination), "provider": provider, "tiles": count,
                "format": tile_format, "tile_size": 256, "zooms": zooms,
                "bounds": bounds,
                "note": "Images copied unchanged. Headers checked, not fully decoded. Verify placement and coverage in QGIS and on the controller."}
    finally:
        if temporary is not None:
            for suffix in ("", "-journal", "-wal", "-shm"):
                try:
                    Path(temporary + suffix).unlink()
                except FileNotFoundError:
                    pass


def inspect_archive(source):
    with closing(open_source(source)) as db:
        return describe(db)


def is_link_or_reparse(path):
    """Also reject Windows junctions on Python versions without is_junction()."""
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def contained_in(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def discover_inputs(root):
    archives, ignored, failures = [], [], []

    def walk_error(error):
        failures.append({"source": str(error.filename), "status": "failed", "error": str(error)})

    for current, directories, files in os.walk(root, topdown=True, followlinks=False, onerror=walk_error):
        base = Path(current)
        for name in sorted(directories):
            path = base / name
            if name == ".git":
                directories.remove(name)
                continue
            try:
                if is_link_or_reparse(path):
                    raise ConversionError("Linked/reparse directories are not followed.")
            except (OSError, ConversionError) as error:
                directories.remove(name)
                failures.append({"source": path.relative_to(root).as_posix(), "status": "failed", "error": str(error)})
        directories.sort()
        for name in sorted(files):
            path = base / name
            relative = path.relative_to(root).as_posix()
            try:
                if is_link_or_reparse(path) or not path.is_file():
                    raise ConversionError("Linked/reparse/nonregular files are not converted.")
                if path.suffix.lower() in INPUT_EXTENSIONS:
                    archives.append(path)
                else:
                    ignored.append(relative)
            except (OSError, ConversionError) as error:
                failures.append({"source": relative, "status": "failed", "error": str(error)})
    archives.sort(key=lambda p: (p.relative_to(root).as_posix().casefold(), p.relative_to(root).as_posix()))
    return archives, ignored, failures


def portable_relative_path(relative):
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved.update("{}{}".format(prefix, n) for prefix in ("COM", "LPT") for n in range(1, 10))
    for part in relative.parts:
        if (part in ("", ".", "..") or part.endswith((" ", "."))
                or any(ord(c) < 32 or c in '<>:"|?*' for c in part)
                or part.split(".")[0].upper() in reserved):
            raise ConversionError("Input path cannot map to a portable Windows output filename: " + str(relative))


def output_path(root, relative):
    """Create parent directories without following an existing linked component."""
    portable_relative_path(relative)
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if os.path.lexists(current):
            if is_link_or_reparse(current) or not current.is_dir():
                raise ConversionError("Output parent is not a normal directory: " + str(current))
        else:
            current.mkdir()
    target = root / relative
    if not contained_in(target.resolve(), root):
        raise ConversionError("Output path escapes the output folder.")
    if os.path.lexists(target) and (is_link_or_reparse(target) or not target.is_file()):
        raise ConversionError("Existing output is not a normal file: " + str(target))
    return target


def write_batch_report(root, report):
    filename = "conversion-report-{}-{}.json".format(
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), uuid.uuid4().hex[:12]
    )
    target = root / filename
    fd, temporary = tempfile.mkstemp(prefix=".report-", suffix=".partial", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, ensure_ascii=True)
            stream.write("\n")
        if os.name == "nt":
            os.rename(temporary, target)
        else:
            os.link(temporary, target)
            os.unlink(temporary)
        return str(target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def batch_convert(input_folder, output_folder, source_crs=None, existing="error", progress=None):
    if source_crs != "EPSG:3857":
        raise ConversionError("Batch conversion requires --source-crs EPSG:3857 after confirming the source grid.")
    if existing not in ("error", "skip"):
        raise ConversionError("Existing-file policy must be error or skip.")
    source_root = Path(input_folder).expanduser().resolve(strict=True)
    destination_root = Path(output_folder).expanduser().resolve()
    if not source_root.is_dir():
        raise ConversionError("Input folder must be a directory.")
    if contained_in(source_root, destination_root) or contained_in(destination_root, source_root):
        raise ConversionError("Input and output folders must be separate and must not contain one another.")
    destination_root.mkdir(parents=True, exist_ok=True)
    archives, ignored, results = discover_inputs(source_root)
    report = {"tool_version": VERSION, "started_utc": datetime.now(timezone.utc).isoformat(),
              "input_folder": str(source_root), "output_folder": str(destination_root),
              "source_crs_declared": source_crs, "existing_policy": existing,
              "ignored_files": ignored, "results": results, "cancelled": False}
    jobs = []
    for archive in archives:
        relative = archive.relative_to(source_root)
        try:
            if progress:
                progress("Inspecting " + relative.as_posix())
            portable_relative_path(relative)
            info = inspect_archive(archive)
            check_projection(info, source_crs)
            for layer in info["layers"]:
                suffix = ".mbtiles" if len(info["layers"]) == 1 else ".layer-{:03d}.mbtiles".format(layer["layer"])
                jobs.append({"source": relative.as_posix(), "layer": layer["layer"],
                             "provider": layer["provider"], "output": relative.as_posix() + suffix})
        except (ConversionError, sqlite3.Error, OSError, ValueError) as error:
            results.append({"source": relative.as_posix(), "status": "failed", "error": str(error)})
    if not archives and not results:
        results.append({"source": ".", "status": "failed", "error": "No supported archive filenames found in input folder."})
    # Detect collisions before converting either conflicting job, including
    # case-insensitive collisions which would otherwise behave differently on Windows.
    groups = {}
    for job in jobs:
        groups.setdefault(job["output"].casefold(), []).append(job)
    for index, job in enumerate(jobs):
        item = dict(job)
        item["output"] = Path(job["output"]).as_posix()
        if len(groups[job["output"].casefold()]) > 1:
            results.append(dict(item, status="failed", error="Multiple source paths collide at this Windows output path."))
            continue
        try:
            target = output_path(destination_root, Path(job["output"]))
            if target.exists() and existing == "skip":
                results.append(dict(item, status="skipped_existing", note="Existing file was not validated or modified."))
                continue
            if progress:
                progress("{} / layer {}".format(job["source"], job["layer"]))
            converted = convert(source_root / job["source"], target, source_crs=source_crs,
                                layer=job["layer"], progress=progress)
            converted.pop("output", None)
            item.update(converted)
            results.append(dict(item, status="converted"))
        except KeyboardInterrupt:
            report["cancelled"] = True
            results.append(dict(item, status="cancelled", error="Cancelled during this output."))
            for pending in jobs[index + 1:]:
                results.append(dict(pending, status="not_run", error="Run was cancelled."))
            break
        except (ConversionError, sqlite3.Error, OSError, ValueError) as error:
            results.append(dict(item, status="failed", error=str(error)))
    report["finished_utc"] = datetime.now(timezone.utc).isoformat()
    report["summary"] = {status: sum(item["status"] == status for item in results)
                         for status in ("converted", "failed", "skipped_existing", "cancelled", "not_run")}
    report["summary"].update({"input_archives": len(archives), "ignored_files": len(ignored)})
    report["report_file"] = write_batch_report(destination_root, report)
    return report


def folder_wizard():
    base = Path(__file__).resolve().parent
    print("OSMDroid raster MBTiles {} - folder conversion".format(VERSION))
    print("Place exported archives in the in folder. Results go to out.")
    source = input("Input folder [{}]: ".format(base / "in")).strip().strip('"') or str(base / "in")
    destination = input("Output folder [{}]: ".format(base / "out")).strip().strip('"') or str(base / "out")
    print("This accepts standard Web Mercator (EPSG:3857) XYZ source grids.")
    print("Confirm from the map source configuration; this does not reproject maps.")
    if input("Are the source grids confirmed? [yes/NO]: ").strip().lower() != "yes":
        print("Stopped without converting. Confirm the source grid before running again.")
        return 1
    report = batch_convert(source, destination, source_crs="EPSG:3857", progress=print)
    print(json.dumps({"summary": report["summary"], "report_file": report["report_file"]}, indent=2))
    return batch_exit_code(report)


def batch_exit_code(report):
    if report["cancelled"]:
        return 130
    return 1 if report["summary"]["failed"] else 0


def wizard():
    print("OSMDroid to MBTiles {} - fully local conversion".format(VERSION))
    print("Use a closed/exported archive. Your original file is opened read-only.")
    source = input("Input SQLite file (you can paste its path): ").strip().strip('"')
    info = inspect_archive(source)
    print(json.dumps(info, indent=2))
    layer = None
    if len(info["layers"]) > 1:
        layer = int(input("Layer number to convert: ").strip())
    print("This version accepts standard Web Mercator (EPSG:3857) XYZ tiles.")
    print("Confirm this from the source map configuration. The converter cannot infer it from imagery.")
    if input("Is that the source tile grid? [yes/NO]: ").strip().lower() != "yes":
        print("Stopped. Confirm the map's source projection before converting.")
        return 0
    default = str(Path(source).with_suffix(".mbtiles"))
    destination = input("Output MBTiles path [{}]: ".format(default)).strip().strip('"') or default
    result = convert(source, destination, source_crs="EPSG:3857", layer=layer, progress=print)
    print(json.dumps(result, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("wizard", help="Interactive local prompts (Windows launcher uses this)")
    commands.add_parser("folder-wizard", help="Interactive input-folder to output-folder conversion")
    batch_cmd = commands.add_parser("batch", help="Recursively convert a folder, splitting providers into separate raster MBTiles")
    batch_cmd.add_argument("input_folder")
    batch_cmd.add_argument("output_folder")
    batch_cmd.add_argument("--source-crs", required=True, choices=["EPSG:3857"], help="Declare the source grid; does not reproject")
    batch_cmd.add_argument("--existing", choices=["error", "skip"], default="error", help="Default error; skip leaves existing files unvalidated")
    inspect_cmd = commands.add_parser("inspect", help="Read schema, provider counts and projection hints")
    inspect_cmd.add_argument("source")
    convert_cmd = commands.add_parser("convert", help="Copy one standard Web Mercator raster provider to MBTiles")
    convert_cmd.add_argument("source")
    convert_cmd.add_argument("destination")
    convert_cmd.add_argument("--source-crs", required=True, choices=["EPSG:3857"], help="Declare the SOURCE tile grid; does not reproject")
    convert_cmd.add_argument("--layer", type=int, help="1-based layer number from inspect; required for multiple providers")
    convert_cmd.add_argument("--name")
    convert_cmd.add_argument("--attribution", help="Map attribution; otherwise recognized input attribution is retained")
    args = parser.parse_args(argv)
    try:
        if args.command == "wizard":
            return wizard()
        if args.command == "folder-wizard":
            return folder_wizard()
        if args.command == "batch":
            report = batch_convert(args.input_folder, args.output_folder, args.source_crs, args.existing,
                                   progress=lambda s: print(s, file=sys.stderr))
            print(json.dumps({"summary": report["summary"], "report_file": report["report_file"]}, indent=2))
            return batch_exit_code(report)
        if args.command == "inspect":
            print(json.dumps(inspect_archive(args.source), indent=2))
        elif args.command == "convert":
            result = convert(args.source, args.destination, args.source_crs, args.layer,
                             args.name, args.attribution, progress=lambda s: print(s, file=sys.stderr))
            print(json.dumps(result, indent=2))
        else:
            parser.print_help()
        return 0
    except (ConversionError, sqlite3.Error, OSError, ValueError, EOFError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled. Any unfinished temporary output was removed.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
