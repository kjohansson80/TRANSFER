"""Create and extend SQLite databases from one editable Python file.

QUICK START (Python standard library only)
    conda activate test
    python sqlite_setup.py                         # creates work.db next to this file
    python sqlite_setup.py --db /path/company.db   # create or extend another database
    python sqlite_setup.py --show-sql              # inspect configuration without writes
    python sqlite_setup.py --self-test             # tests in a temporary directory

EDIT THE CONFIGURATION BELOW
    1. Set DEFAULT_DATABASE and CONNECTION_TIMEOUT_SECONDS.
    2. Define tables and columns in SCHEMA. Column definitions use ordinary SQL.
    3. Use REFERENCES for relationships; put parent tables first for readability.
    4. Add new columns to SCHEMA and rerun: missing columns are added automatically.
       Or import add_missing_columns() for a targeted addition (example below).

EXISTING DATABASES
    Existing data and extra tables/columns are retained. No DROP, DELETE or table
    rebuild is performed. Existing column definitions, constraints and indexes are
    NOT modified by changing the configuration. This is an additive setup helper,
    not a general migration engine. Inspect the database before reusing a schema.

    SQLite limits ALTER TABLE ADD COLUMN: a new column cannot be PRIMARY KEY or
    UNIQUE; NOT NULL needs a non-NULL constant default; a REFERENCES column needs
    a NULL default when foreign keys are enabled. Expression/CURRENT_TIMESTAMP
    defaults cannot be added this way. Use a separate explicit migration for
    changes requiring a table rebuild. An unsupported addition raises an error
    and the entire setup transaction rolls back.

    Foreign keys must be enabled on EVERY application's connection. Use
    connect_database() below, or execute PRAGMA foreign_keys = ON after connecting.
    Enabling enforcement does not repair old data: setup checks foreign_key_check.

SQL definitions and table constraints are trusted developer configuration, not
untrusted user input. For INSERT/UPDATE values, use placeholders (?, ...) instead
of formatting values into SQL. Example definitions below demonstrate common
options; replace the example tables with your own before using this at work.

SQLite documentation:
    https://www.sqlite.org/lang_createtable.html
    https://www.sqlite.org/lang_altertable.html
    https://www.sqlite.org/foreignkeys.html
"""

from contextlib import contextmanager
from pathlib import Path
import argparse
import re
import sqlite3
import tempfile


# ---------------------------------------------------------------------------
# CONFIGURATION: edit this section for your database.
# Column order follows dictionary order. Use ISO dates/timestamps in TEXT fields.
# Types: INTEGER, REAL, TEXT, BLOB, NUMERIC. For exact money, prefer INTEGER minor
# units (e.g. cents); REAL is floating point, not an exact decimal money type.
# No sample records are inserted by the normal build.
# ---------------------------------------------------------------------------
DEFAULT_DATABASE = Path(__file__).resolve().with_name("work.db")
CONNECTION_TIMEOUT_SECONDS = 30

SCHEMA = {
    "departments": {
        "columns": {
            "id": "INTEGER PRIMARY KEY",  # SQLite generates IDs on INSERT if omitted
            "code": "TEXT NOT NULL UNIQUE",
            "name": "TEXT NOT NULL",
            "active": "INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))",
        },
    },
    "employees": {
        "columns": {
            "id": "INTEGER PRIMARY KEY",
            "department_id": (
                "INTEGER NOT NULL REFERENCES departments(id) "
                "ON UPDATE CASCADE ON DELETE RESTRICT"
            ),
            "employee_code": "TEXT NOT NULL UNIQUE",
            "name": "TEXT NOT NULL",
            "email": "TEXT",                   # nullable
            "start_date": "TEXT",              # YYYY-MM-DD
            "active": "INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))",
            "created_at": "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
            # To extend this table later, uncomment and rerun:
            # "notes": "TEXT",
            # "reviewed": "INTEGER NOT NULL DEFAULT 0",
        },
        # Optional table constraints (applied only when creating a new table).
        # Examples: 'UNIQUE (department_id, name)', 'CHECK (length(name) > 0)'.
        # Composite relationships: 'FOREIGN KEY (a, b) REFERENCES parent(a, b)'.
        "constraints": [],
        # Indexes can be added on later runs. Existing indexes are not changed.
        "indexes": {
            "idx_employees_department": {"columns": ["department_id"]},
            # "idx_employees_email": {"columns": ["email"], "unique": True},
        },
    },
}


def identifier(name):
    """Validate and quote simple SQL identifiers; SQL keywords are allowed."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"Invalid identifier {name!r}; use letters, digits and underscores")
    return '"' + name + '"'


def connect_database(path=DEFAULT_DATABASE):
    """Open a database with foreign-key enforcement enabled. Caller closes it."""
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=CONNECTION_TIMEOUT_SECONDS)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("Could not enable SQLite foreign keys")
    except Exception:
        conn.close()
        raise
    return conn


@contextmanager
def atomic(conn):
    """Savepoint makes schema changes atomic, including inside a transaction."""
    conn.execute("SAVEPOINT sqlite_setup_changes")
    try:
        yield
        conn.execute("RELEASE SAVEPOINT sqlite_setup_changes")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT sqlite_setup_changes")
        conn.execute("RELEASE SAVEPOINT sqlite_setup_changes")
        raise


def column_names(conn, table):
    # table_xinfo includes generated/hidden columns as well as ordinary columns.
    rows = conn.execute(f"PRAGMA table_xinfo({identifier(table)})").fetchall()
    if not rows:
        raise ValueError(f"Table {table!r} does not exist or has no columns")
    return {r[1].casefold() for r in rows}


def add_missing_columns(conn, table, columns):
    """Add missing columns; return their names. Existing columns stay untouched.

    Example (inside your own script):
        conn = connect_database("company.db")
        try:
            add_missing_columns(conn, "employees", {
                "notes": "TEXT",
                "reviewed": "INTEGER NOT NULL DEFAULT 0",
            })
        finally:
            conn.close()

    Savepoints commit when no outer transaction exists. If the caller already
    has a transaction, additions join it and the caller controls final commit.
    """
    if not isinstance(columns, dict) or not columns:
        raise ValueError("columns must be a non-empty dictionary")
    added = []
    with atomic(conn):
        existing = column_names(conn, table)
        for name, definition in columns.items():
            identifier(name)
            if name.casefold() in existing:
                continue
            if not isinstance(definition, str) or not definition.strip():
                raise ValueError(f"Empty definition for {table}.{name}")
            try:
                conn.execute(f"ALTER TABLE {identifier(table)} ADD COLUMN "
                             f"{identifier(name)} {definition}")
            except sqlite3.Error as exc:
                raise ValueError(f"Cannot add {table}.{name}: {exc}. "
                                 "See ADD COLUMN restrictions in this file.") from exc
            existing.add(name.casefold())
            added.append(name)
    return added


def create_table_sql(table, config):
    columns = config.get("columns", {})
    if not columns:
        raise ValueError(f"No columns configured for {table}")
    definitions = []
    seen = set()
    for name, definition in columns.items():
        quoted = identifier(name)
        if name.casefold() in seen:
            raise ValueError(f"Duplicate column name in {table}: {name}")
        seen.add(name.casefold())
        if not isinstance(definition, str) or not definition.strip():
            raise ValueError(f"Empty definition for {table}.{name}")
        definitions.append(f"{quoted} {definition}")
    definitions.extend(config.get("constraints", []))
    return f"CREATE TABLE {identifier(table)} (\n    " + ",\n    ".join(definitions) + "\n)"


def index_sql(table, name, config):
    columns = config.get("columns", [])
    if not columns:
        raise ValueError(f"No columns configured for index {name}")
    unique = "UNIQUE " if config.get("unique", False) else ""
    return (f"CREATE {unique}INDEX IF NOT EXISTS {identifier(name)} "
            f"ON {identifier(table)} ({', '.join(identifier(c) for c in columns)})")


def apply_schema(conn, schema=SCHEMA):
    """Create missing tables/columns/indexes; return a list of changes.

    All changes roll back together on failure. Existing definitions/constraints
    are not compared or migrated. Table constraints apply only to new tables.
    """
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ValueError("Enable foreign_keys before starting a transaction")
    changes = []
    with atomic(conn):
        for table, config in schema.items():
            sql = create_table_sql(table, config)  # validates even on repeat runs
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? COLLATE NOCASE",
                (table,),
            ).fetchone()
            if not exists:
                conn.execute(sql)
                changes.append(f"Created table: {table}")
            else:
                for name in add_missing_columns(conn, table, config["columns"]):
                    changes.append(f"Added column: {table}.{name}")
        # Build indexes after every table/column is available.
        for table, config in schema.items():
            for name, index in config.get("indexes", {}).items():
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' AND name=? COLLATE NOCASE",
                    (name,),
                ).fetchone()
                conn.execute(index_sql(table, name, index))
                if not exists:
                    changes.append(f"Created index: {name}")
        violations = conn.execute("PRAGMA foreign_key_check").fetchmany(5)
        if violations:
            raise ValueError(f"Foreign-key violations (up to five shown): {violations}")
    return changes


def self_test():
    """Verify relationships, data retention, repeat runs and failed-add rollback."""
    with tempfile.TemporaryDirectory() as tmp:
        conn = connect_database(Path(tmp) / "test.db")
        try:
            assert apply_schema(conn)
            assert apply_schema(conn) == []
            with conn:
                conn.execute("INSERT INTO departments(id, code, name) VALUES (?, ?, ?)",
                             (1, "IT", "Technology"))
                conn.execute("INSERT INTO employees(department_id, employee_code, name) "
                             "VALUES (?, ?, ?)", (1, "E001", "Example employee"))
            try:
                with conn:
                    conn.execute("INSERT INTO employees(department_id, employee_code, name) "
                                 "VALUES (?, ?, ?)", (999, "E002", "Invalid reference"))
            except sqlite3.IntegrityError:
                pass
            else:
                raise AssertionError("Invalid foreign key was accepted")
            assert add_missing_columns(conn, "employees", {"notes": "TEXT",
                "reviewed": "INTEGER NOT NULL DEFAULT 0"}) == ["notes", "reviewed"]
            assert conn.execute("SELECT employee_code, reviewed FROM employees").fetchall() == [("E001", 0)]
            assert add_missing_columns(conn, "employees", {"notes": "TEXT"}) == []
            try:
                add_missing_columns(conn, "employees", {"temporary_column": "TEXT",
                                                        "invalid_column": "TEXT UNIQUE"})
            except ValueError:
                assert "temporary_column" not in column_names(conn, "employees")
            else:
                raise AssertionError("Unsupported UNIQUE addition was accepted")
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    print("Self-test passed: creation, repeat runs, foreign keys, new columns, "
          "data retention and rollback.")


def main():
    parser = argparse.ArgumentParser(description="Create or extend an SQLite database.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE, help="Database file path")
    parser.add_argument("--show-sql", action="store_true", help="Print configured SQL; do not open a database")
    parser.add_argument("--self-test", action="store_true", help="Run checks using a temporary database")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if args.show_sql:
        for table, config in SCHEMA.items():
            print(create_table_sql(table, config) + ";\n")
            for name, index in config.get("indexes", {}).items():
                print(index_sql(table, name, index) + ";\n")
        return
    conn = connect_database(args.db)
    try:
        changes = apply_schema(conn)
    finally:
        conn.close()
    print(f"Database: {args.db.expanduser().resolve()}")
    print("\n".join(changes) if changes else "Schema already present; no changes needed.")
    print("Existing definitions and table constraints are not altered on repeat runs.")


if __name__ == "__main__":
    main()
