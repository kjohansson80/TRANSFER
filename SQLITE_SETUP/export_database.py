"""Export an entire SQLite database to a single, structured JSON file.

Python standard library only; this file is independent of sqlite_setup.py.

    conda activate test
    python export_database.py --db /path/company.db
    python export_database.py --db /path/company.db --output /path/company.json
    python export_database.py --db /path/company.db --output /path/company.json --overwrite
    python export_database.py --self-test

DEFAULTS: reads work.db next to this script; writes <database-name>.json.
Edit the constants below or supply command-line arguments.

JSON STRUCTURE
    format_version, exported_at_utc, database, schema, tables
    schema: original CREATE SQL for tables, indexes, views and triggers.
    tables: list of {name, columns, foreign_keys, row_count, rows} objects.
    rows: objects mapping column names to values; empty tables are retained.
    Internal tables (e.g. sqlite_sequence) are included if present. Views retain
    their definitions, not duplicate query results. Table order is alphabetical;
    row order is SQLite's scan order, not guaranteed to be a primary-key sort.

VALUE ENCODING
    NULL -> null; text -> JSON string; ordinary numbers -> JSON numbers.
    BLOB -> {"$sqlite_type": "blob", "encoding": "base64", "data": "..."}.
    Integers outside JavaScript's exact range (+/- (2**53 - 1)) ->
        {"$sqlite_type": "integer", "value": "..."}.
    Infinite REALs -> {"$sqlite_type": "real", "value": "Infinity" or "-Infinity"}.
    This avoids invalid JSON and loss of integer precision in common consumers.

The source is opened read-only; a read transaction gives one consistent snapshot
of schema and rows, even if the application writes concurrently. Rows are streamed
to disk instead of loading the whole database into memory. A long export may
delay writers in rollback-journal mode or retain WAL pages in WAL mode.

The output is published only after the export completes successfully. Existing
output is refused unless --overwrite is passed. No records are changed in the
database. Other applications must still manage their own database connections.

This is a logical export, not a byte-for-byte backup or an automatic restore tool.
It does not include other attached databases, external files, encryption keys,
or custom SQLite extensions/collations. Virtual tables require their modules to
be available; otherwise the export fails rather than silently omitting data.
Use SQLite's backup API when you need a directly restorable .db backup.
"""

from datetime import datetime, timezone
from pathlib import Path
import argparse
import base64
import json
import math
import os
import sqlite3
import tempfile


# CONFIGURATION
DEFAULT_DATABASE = Path(__file__).resolve().with_name("work.db")
DEFAULT_TIMEOUT_SECONDS = 30
JSON_INDENT = 2
MAX_EXACT_JSON_INTEGER = 2**53 - 1


def quote_identifier(name):
    """Quote identifiers from the source schema, including spaces and quotes."""
    return '"' + name.replace('"', '""') + '"'


def json_value(value):
    if isinstance(value, bytes):
        return {"$sqlite_type": "blob", "encoding": "base64",
                "data": base64.b64encode(value).decode("ascii")}
    if isinstance(value, int) and abs(value) > MAX_EXACT_JSON_INTEGER:
        return {"$sqlite_type": "integer", "value": str(value)}
    if isinstance(value, float) and not math.isfinite(value):
        return {"$sqlite_type": "real", "value": str(value).replace("inf", "Infinity")}
    return value


def dump(value):
    return json.dumps(value, ensure_ascii=False, indent=JSON_INDENT, allow_nan=False)


def export_database(database=DEFAULT_DATABASE, output=None, overwrite=False):
    """Export schema and all main-database tables; return the destination Path."""
    source = Path(database).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Database does not exist: {source}")
    target = Path(output).expanduser().resolve() if output else source.with_suffix(".json")
    protected = {source, Path(str(source) + "-wal"), Path(str(source) + "-shm"),
                 Path(str(source) + "-journal")}
    if target in protected or (target.exists() and target.samefile(source)):
        raise ValueError("The JSON destination must not overwrite the source database or its journal")
    if target.exists() and not overwrite:
        raise FileExistsError(f"Output already exists: {target}; use --overwrite to replace it")
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True,
                           timeout=DEFAULT_TIMEOUT_SECONDS)
    temporary = None
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        # The first read fixes the snapshot for subsequent schema and table reads.
        schema = [dict(zip(("type", "name", "table_name", "sql"), row)) for row in
                  conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema "
                               "WHERE type IN ('table', 'index', 'view', 'trigger') "
                               "ORDER BY type, name")]
        metadata = {
            "format_version": 1,
            "exported_at_utc": datetime.now(timezone.utc).isoformat(),
            "database": {
                "filename": source.name,
                "sqlite_version": sqlite3.sqlite_version,
                "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
                "application_id": conn.execute("PRAGMA application_id").fetchone()[0],
                "encoding": conn.execute("PRAGMA encoding").fetchone()[0],
            },
            "schema": schema,
        }
        # Temporary file stays in the destination directory for atomic publication.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=target.parent, prefix=".sqlite_export_",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(dump(metadata)[:-1] + ',\n  "tables": [\n')
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")]
            for index, name in enumerate(tables):
                quoted = quote_identifier(name)
                columns = [dict(zip(("cid", "name", "declared_type", "not_null", "default_sql",
                                     "primary_key_order", "hidden"), row))
                           for row in conn.execute(f"PRAGMA table_xinfo({quoted})")]
                foreign_keys = [dict(zip(("id", "sequence", "parent_table", "from_column",
                                          "to_column", "on_update", "on_delete", "match"), row))
                                for row in conn.execute(f"PRAGMA foreign_key_list({quoted})")]
                cursor = conn.execute(f"SELECT * FROM {quoted}")
                names = [c[0] for c in cursor.description]
                # Hidden virtual-table columns are described in metadata; SELECT *
                # exports their normal public rows, including generated columns.
                header = {"name": name, "columns": columns, "foreign_keys": foreign_keys}
                stream.write(dump(header)[:-1] + ',\n  "rows": [')
                count = 0
                for row in cursor:
                    stream.write(",\n" if count else "\n")
                    stream.write(dump({key: json_value(value) for key, value in zip(names, row)}))
                    count += 1
                stream.write(f'\n  ],\n  "row_count": {count}\n}}')
                stream.write(",\n" if index < len(tables) - 1 else "\n")
            stream.write("  ]\n}\n")
            stream.flush()
            os.fsync(stream.fileno())
        conn.rollback()  # end the read snapshot before publishing
        if overwrite:
            os.replace(temporary, target)
        else:
            # Atomic create-without-overwrite, including if another export raced us.
            os.link(temporary, target)
            temporary.unlink()
        temporary = None
    finally:
        conn.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def self_test():
    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        db = directory / "sample #1.db"
        conn = sqlite3.connect(db)
        try:
            conn.executescript('''
                PRAGMA foreign_keys=ON;
                PRAGMA user_version=7;
                CREATE TABLE parent(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE);
                CREATE TABLE "child table"(id INTEGER PRIMARY KEY, parent_id INTEGER
                    REFERENCES parent(id), payload BLOB, optional TEXT, amount REAL, big INTEGER);
                CREATE TABLE empty_table(id INTEGER);
                CREATE INDEX child_parent_idx ON "child table"(parent_id);
                CREATE VIEW names AS SELECT name FROM parent;
                CREATE TRIGGER parent_name AFTER INSERT ON parent BEGIN
                    UPDATE parent SET name=upper(name) WHERE id=new.id;
                END;
            ''')
            conn.execute("INSERT INTO parent(name) VALUES (?)", ("héllo",))
            conn.execute('INSERT INTO "child table" VALUES (?, ?, ?, ?, ?, ?)',
                         (1, 1, b"\x00\xff", None, float("inf"), 2**60))
            conn.commit()
        finally:
            conn.close()
        output = export_database(db)
        data = json.loads(output.read_text(encoding="utf-8"))
        tables = {t["name"]: t for t in data["tables"]}
        assert tables["empty_table"]["rows"] == []
        assert tables["parent"]["rows"][0]["name"] == "HéLLO"
        row = tables["child table"]["rows"][0]
        assert base64.b64decode(row["payload"]["data"]) == b"\x00\xff"
        assert row["optional"] is None
        assert row["amount"]["value"] == "Infinity"
        assert row["big"]["value"] == str(2**60)
        assert tables["child table"]["foreign_keys"][0]["parent_table"] == "parent"
        assert "sqlite_sequence" in tables
        assert data["database"]["user_version"] == 7
        assert {s["type"] for s in data["schema"]} == {"table", "index", "view", "trigger"}
        before = output.read_bytes()
        try:
            export_database(db)
        except FileExistsError:
            assert output.read_bytes() == before
        else:
            raise AssertionError("Existing export was overwritten")
        export_database(db, overwrite=True)
        try:
            export_database(db, db, overwrite=True)
        except ValueError:
            pass
        else:
            raise AssertionError("Source overwrite was allowed")
        invalid = directory / "invalid.db"
        invalid.write_text("not an SQLite database")
        try:
            export_database(invalid, output, overwrite=True)
        except sqlite3.DatabaseError:
            assert json.loads(output.read_text())["format_version"] == 1
        else:
            raise AssertionError("Invalid source accepted")
        assert not list(directory.glob(".sqlite_export_*.tmp"))
    print("Self-test passed: schema, all rows, empty/internal tables, references, "
          "BLOBs, large integers, Unicode, overwrite protection and invalid inputs.")


def main():
    parser = argparse.ArgumentParser(description="Export SQLite schema and data to JSON.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE, help="Source SQLite database")
    parser.add_argument("--output", type=Path, help="Destination JSON; default: database name with .json")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing JSON export")
    parser.add_argument("--self-test", action="store_true", help="Run checks with a temporary database")
    args = parser.parse_args()
    if args.self_test:
        self_test()
    else:
        print(f"JSON export: {export_database(args.db, args.output, args.overwrite)}")


if __name__ == "__main__":
    main()
