
"""Safely recover missing user accounts from a private SQLite seed database.

Dry-run is the default. Account recovery must be explicitly enabled by the
caller, and schema validation, conflict detection, and integrity checks run
before any changes are committed.
"""

import re
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote


class AccountSeedError(Exception):
    """Raised when account recovery inputs or database state are unsafe."""


UNIQUE_IDENTITY_COLUMNS = ("username", "email", "student_number")


def _database_uri(path: Path, *, read_only: bool) -> str:
    """Build a SQLite URI from a resolved local path."""
    uri_path = quote(path.as_posix(), safe="/:")
    return f"file:{uri_path}?mode={'ro' if read_only else 'rw'}"


def _open_database(path: Path, *, read_only: bool):
    connection = sqlite3.connect(
        _database_uri(path, read_only=read_only),
        uri=True,
        timeout=10,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def _schema_signature(connection):
    """Validate essential constraints and return structural schema details."""
    table = connection.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'table' AND name = 'users'"
    ).fetchone()

    if table is None or not table["sql"]:
        raise AccountSeedError("A database is missing the users table.")

    table_sql = re.sub(r"\s+", " ", table["sql"]).casefold()
    columns = connection.execute("PRAGMA table_info(users)").fetchall()
    foreign_keys = connection.execute(
        "PRAGMA foreign_key_list(users)"
    ).fetchall()

    column_names = {row["name"].casefold() for row in columns}
    required_columns = {
        "id",
        "username",
        "full_name",
        "email",
        "password_hash",
        "role",
        "approval_status",
        "student_number",
    }

    if not required_columns.issubset(column_names):
        raise AccountSeedError(
            "The users table is missing required account columns."
        )

    id_column = next(row for row in columns if row["name"].casefold() == "id")
    if (
        not id_column["pk"]
        or "int" not in (id_column["type"] or "").casefold()
    ):
        raise AccountSeedError("The users table has an invalid ID primary key.")

    username_column = next(
        row for row in columns if row["name"].casefold() == "username"
    )
    if not username_column["notnull"]:
        raise AccountSeedError("The username column must be NOT NULL.")

    if not re.search(r"\busername\b[^,]*\bcollate\s+nocase\b", table_sql):
        raise AccountSeedError(
            "The users table is missing case-insensitive username rules."
        )

    if not re.search(
        r"\bcheck\s*\(\s*approval_status\s+in\s*\(",
        table_sql,
    ):
        raise AccountSeedError(
            "The users table is missing approval-status validation."
        )

    column_signature = tuple(
        (
            row["name"].casefold(),
            (row["type"] or "").casefold(),
            row["notnull"],
            row["dflt_value"],
            row["pk"],
        )
        for row in columns
    )

    foreign_key_signature = tuple(
        sorted(
            (
                row["table"].casefold(),
                row["from"].casefold(),
                (row["to"] or "").casefold(),
                row["on_update"].upper(),
                row["on_delete"].upper(),
            )
            for row in foreign_keys
        )
    )

    return column_signature, foreign_key_signature


def _check_database(connection, label: str) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise AccountSeedError(f"{label} database integrity check failed.")

    foreign_key_errors = connection.execute(
        "PRAGMA foreign_key_check"
    ).fetchall()

    if foreign_key_errors:
        raise AccountSeedError(
            f"{label} database has foreign-key integrity violations."
        )


def _read_users(connection):
    columns = [
        row["name"]
        for row in connection.execute("PRAGMA table_info(users)").fetchall()
    ]
    rows = connection.execute(
        "SELECT * FROM users ORDER BY id"
    ).fetchall()
    return columns, rows


def _identity_value(row, column):
    value = row[column]
    if value is None:
        return None
    return str(value).casefold()


def _conflict_summary(source_rows, target_rows, columns):
    """Return existing matches, missing source rows, and conflicting IDs."""
    target_by_id = {row["id"]: row for row in target_rows}
    target_unique = {name: {} for name in UNIQUE_IDENTITY_COLUMNS}

    for row in target_rows:
        for column in UNIQUE_IDENTITY_COLUMNS:
            if column not in row.keys():
                continue

            value = _identity_value(row, column)
            if value is not None:
                target_unique[column].setdefault(value, row["id"])

    source_unique = {name: {} for name in UNIQUE_IDENTITY_COLUMNS}
    already_present = 0
    missing_rows = []
    conflicts = set()

    for source_row in source_rows:
        source_id = source_row["id"]
        existing = target_by_id.get(source_id)

        if existing is not None:
            if all(
                existing[column] == source_row[column]
                for column in columns
            ):
                already_present += 1
            else:
                conflicts.add(source_id)
            continue

        for column in UNIQUE_IDENTITY_COLUMNS:
            if column not in source_row.keys():
                continue

            value = _identity_value(source_row, column)
            if value is None:
                continue

            target_id = target_unique[column].get(value)
            if target_id is not None:
                conflicts.add(source_id)

            previous_id = source_unique[column].get(value)
            if previous_id is not None and previous_id != source_id:
                conflicts.add(source_id)
                conflicts.add(previous_id)
            else:
                source_unique[column][value] = source_id

        missing_rows.append(source_row)

    return already_present, missing_rows, conflicts


def recover_missing_accounts(seed_path, database_path, apply=False):
    """Report recovery candidates; insert them only when apply=True.

    Returns counts only. Never returns or logs account data or password hashes.
    """
    seed_path = Path(seed_path)
    database_path = Path(database_path)

    if seed_path.is_symlink() or database_path.is_symlink():
        raise AccountSeedError("Database paths must not be symbolic links.")

    try:
        seed_path = seed_path.resolve(strict=True)
        database_path = database_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AccountSeedError(
            "A required database path is unavailable."
        ) from exc

    if not seed_path.is_file() or not database_path.is_file():
        raise AccountSeedError(
            "Both database paths must refer to existing files."
        )

    if seed_path == database_path:
        raise AccountSeedError(
            "Seed and application databases must be different."
        )

    with closing(_open_database(seed_path, read_only=True)) as seed:
        with closing(
            _open_database(database_path, read_only=not apply)
        ) as target:
            seed_tables = {
                row["name"]
                for row in seed.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }

            if seed_tables != {"users"}:
                raise AccountSeedError(
                    "The recovery seed must contain only the users table."
                )

            seed_signature = _schema_signature(seed)
            target_signature = _schema_signature(target)

            if seed_signature != target_signature:
                raise AccountSeedError(
                    "Account seed schema mismatch: users column or "
                    "foreign-key definitions differ."
                )

            columns, source_rows = _read_users(seed)
            _check_database(seed, "Seed")

            if apply:
                target.execute("PRAGMA foreign_keys = ON")
                target.execute("BEGIN IMMEDIATE")

            try:
                _check_database(target, "Application")
                target_columns, target_rows = _read_users(target)

                if columns != target_columns:
                    raise AccountSeedError(
                        "Account seed column order differs from the application."
                    )

                already_present, missing_rows, conflicts = _conflict_summary(
                    source_rows, target_rows, columns
                )

                summary = {
                    "source_accounts": len(source_rows),
                    "already_present": already_present,
                    "missing_accounts": len(missing_rows),
                    "conflicts": len(conflicts),
                    "added_accounts": 0,
                }

                if conflicts:
                    if apply:
                        target.rollback()
                    return summary

                if not apply:
                    return summary

                placeholders = ", ".join("?" for _ in columns)
                quoted_columns = ", ".join(
                    '"' + column.replace('"', '""') + '"'
                    for column in columns
                )
                insert_sql = (
                    f"INSERT INTO users ({quoted_columns}) "
                    f"VALUES ({placeholders})"
                )

                target.execute("PRAGMA defer_foreign_keys = ON")
                target.executemany(
                    insert_sql,
                    [
                        tuple(row[column] for column in columns)
                        for row in missing_rows
                    ],
                )

                _check_database(target, "Application")

                actual_count = target.execute(
                    "SELECT COUNT(*) FROM users"
                ).fetchone()[0]
                expected_count = len(target_rows) + len(missing_rows)

                if actual_count != expected_count:
                    raise AccountSeedError(
                        "Post-recovery account count validation failed."
                    )

                target.commit()
                summary["added_accounts"] = len(missing_rows)
                return summary

            except Exception:
                if apply:
                    target.rollback()
                raise
