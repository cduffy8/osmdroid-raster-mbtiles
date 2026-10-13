# Supported map format and conversion

## Raster contents

The input is an OSMDroid-style SQLite archive with a real table exposing `key`, `provider` and `tile`. `tile` contains the encoded PNG or JPEG image file bytes. Extra columns are permitted. Provider strings and NULL are supported; provider identity is compared with binary collation to avoid merging values such as `Map` and `map`.

This version accepts one consistent image encoding per provider and 256 by 256 pixel images. It rejects empty/non-BLOB tiles and tiles larger than 64 MiB. PNG IHDR and JPEG dimension headers identify format and dimensions. It does not fully decode image payloads or verify every PNG chunk/JPEG scan.

Output tiles are copied byte for byte. The metadata `format` is derived from the observed encoding and is only `png` or `jpg`. No raster-to-vector conversion, vector renderer, image resampling or image recompression is involved.

## Keys and coordinates

The documented OSMDroid key is equivalent to:

```text
key = z * 4^z + x * 2^z + y
```

The implementation searches the supported zoom ranges, checks the key is within a valid range, then extracts the XYZ column and row. Zoom levels 0 through 29 fit the supported signed SQLite integer encoding; this is a parser limit, not a statement about any device's zoom support.

The output schema is:

```sql
CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT);
CREATE TABLE tiles (
    zoom_level INTEGER,
    tile_column INTEGER,
    tile_row INTEGER,
    tile_data BLOB
);
CREATE UNIQUE INDEX tile_index
    ON tiles (zoom_level, tile_column, tile_row);
```

MBTiles uses the TMS row convention, so:

```text
zoom_level = z
tile_column = x
tile_row = (2^z - 1) - y
tile_data = original tile bytes
```

The row number changes; image pixels are not flipped. An independent example from the MBTiles specification is XYZ `11/327/791`, stored as zoom `11`, column `327`, row `1256`.

## Projection and bounds

The caller must declare `--source-crs EPSG:3857` after confirming a standard Web Mercator XYZ grid. Conventional CRS hints in `metadata` and `atak_metadata` are checked. Recognized Web Mercator identifiers are accepted; unknown or conflicting hints are rejected. These checks are deliberately limited, not a universal interpretation of arbitrary ATAK metadata.

All stored tile coordinates are retained. Metadata includes the actual minimum/maximum stored zoom, provider-specific image encoding, and recognized source attribution. Bounds are the intersection of the geographic bounding envelopes of stored zooms. If those envelopes do not intersect, optional `bounds` is omitted. Bounds cannot describe holes, disconnected areas or detailed antimeridian coverage, and no completeness claim is made from them.

## Files and failures

Inputs are opened using SQLite `mode=ro`, `query_only=ON` and a read transaction. Nonempty `-wal` and `-journal` sidecars are rejected before reading. The caller supplies exported, closed archives; each output uses its own read transaction. Batch conversion is not a single snapshot transaction spanning every archive/provider.

For each output, records are streamed one tile at a time into a temporary database. A transaction is committed every 10,000 tiles. The output has a unique coordinate index, and final counts and SQLite `quick_check` are verified before publishing the finished filename. Windows uses a rename that refuses an existing destination; POSIX uses a no-overwrite hard-link publication in the same directory.

Ordinary exceptions or Ctrl+C during conversion remove temporary files. Power loss can leave a `.partial` file. A batch is not all-or-nothing: completed maps remain if another provider/archive fails. Existing maps are never overwritten, and explicit skips do not validate earlier contents.

The batch report names every planned output and its result. A run interrupted before planning finishes, an unwritable output folder, or failure to write the report itself may prevent report creation. Error text is printed and a nonzero exit code is returned in those cases.

## References

- [OSMDroid schema and packed key](https://github.com/osmdroid/osmdroid/blob/master/osmdroid-android/src/main/java/org/osmdroid/tileprovider/modules/DatabaseFileArchive.java)
- [OSMDroid writer implementation](https://github.com/osmdroid/osmdroid/blob/master/osmdroid-android/src/main/java/org/osmdroid/tileprovider/modules/SqlTileWriter.java)
- [MBTiles 1.3 specification](https://github.com/mapbox/mbtiles-spec/blob/master/1.3/spec.md)
