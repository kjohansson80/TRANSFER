# TRANSFER

## SQLite utilities

Two independent Python files using only the standard library:

- `SQLITE_SETUP/sqlite_setup.py`: configure tables, columns, references and
  indexes; create a database or add missing tables/columns.
- `SQLITE_SETUP/export_database.py`: export the complete main database schema
  and table rows to JSON, including references and base64-encoded binary data.

```sh
conda activate test
python SQLITE_SETUP/sqlite_setup.py --db /path/company.db
python SQLITE_SETUP/export_database.py --db /path/company.db --output /path/company.json
```

The export opens the database read-only and streams a consistent snapshot.
Existing JSON files require `--overwrite` to replace. This is a logical export;
use SQLite's backup API for a directly restorable database backup. Each file
contains configuration, usage notes and a `--self-test` command.
