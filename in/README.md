# Input archives

Place exported, closed OSMDroid SQLite map archives here. Subfolders are scanned recursively. Candidate extensions are `.sqlite`, `.sqlite3`, `.sqlitedb`, `.db`, and `.mbtiles`; the database must still have the supported OSMDroid layout.

Use a consistent standalone export. Do not copy only the main database from an active cache with outstanding WAL/journal data. The converter refuses nonempty WAL or journal sidecars.

Map files in this folder are ignored by Git. This README is listed as an ignored non-map file in conversion reports.
