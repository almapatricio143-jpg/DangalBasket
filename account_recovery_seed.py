"""Safely seed missing accounts from a private SQLite seed database."""

import re
import sqlite3
from contextlib import closing
from pathlib import Path


class AccountSeedError(Exception):
    """Raised when account-seed input is unsafe or incompatible."""


def _readonly_connection(path):
    return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)


def _schema_signature(connection):
    table = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
    ).fetchone()
    if table is None or table[0] is None:
        raise AccountSeedError("The account seed or application database lacks users.")
    normalized_sql = re.sub(
        r'"([A-Za-z_][A-Za-z0-9_]*)"', r"\1", table[0]
    )
    columns = connection.execute("PRAGMA table_info(users)").fetchall()
    foreign_keys = connection.execute("PRAGMA foreign_key_list(users)").fetchall()
    indexes = connection.execute("PRAGMA index_list(users)").fetchall()
    index_signatures = []
    for index in indexes:
        index_columns = connection.execute(
            f'PRAGMA index_info("{index[1]}")'
        ).fetchall()
        index_signatures.append(
            (
                index[2],
                index[3],
                index[4],
                tuple((column[1], column[2]) for column in index_columns),
            )
        )
    return (
        " ".join(normalized_sql.split()).casefold(),
        tuple((row[1], row[2], row[3], row[4], row[5]) for row in columns),
        tuple(tuple(row) for row in foreign_keys),
        tuple(sorted(index_signatures)),
    )


def _check_database(connection):
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if integrity != "ok" or foreign_key_errors:
        raise AccountSeedError(
            "Database integrity validation failed "
            f"(foreign-key violations: {len(foreign_key_errors)})."
        )


def _read_users(connection):
    columns = [
        row[1] for row in connection.execute("PRAGMA table_info(users)").fetchall()
    ]
    quoted_columns = ", ".join(f'"{column}"' for column in columns)
    rows = connection.execute(
        f'SELECT {quoted_columns} FROM users ORDER BY id'
    ).fetchall()
    return columns, rows


def _conflict_summary(source_rows, target_rows, columns):
    target_by_id = {row["id"]: row for row in target_rows}
    target_unique = {name: {} for name in ("username", "email", "student_number")}
    for row in target_rows:
        for column in target_unique:
            value = row[column]
            if value is not None:
                target_unique[column].setdefault(value.casefold(), row["id"])

    already_present = 0
    missing_rows = []
    conflicts = set()
    for source_row in source_rows:
        existing = target_by_id.get(source_row["id"])
        if existing is not None:
            if all(existing[column] == source_row[column] for column in columns):
                already_present += 1
            else:
                conflicts.add(source_row["id"])
            continue

        for column in target_unique:
            value = source_row[column]
            if value is not None and value.casefold() in target_unique[column]:
                conflicts.add(source_row["id"])
        missing_rows.append(source_row)

    return already_present, missing_rows, conflicts


def recover_missing_accounts(seed_path, database_path, apply=False):
    """Validate a private users-only seed and optionally insert missing rows."""
    seed_path = Path(seed_path)
    database_path = Path(database_path)
    if seed_path.is_symlink() or database_path.is_symlink():
        raise AccountSeedError("Database paths must not be symbolic links.")
    seed_path = seed_path.resolve(strict=True)
    database_path = database_path.resolve(strict=True)
    if seed_path == database_path:
        raise AccountSeedError("Seed and application databases must be different.")

    seed_uri = seed_path.as_uri() + "?mode=ro"
    target_uri = database_path.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(seed_uri, uri=True)) as seed, closing(
        sqlite3.connect(database_path if apply else target_uri, uri=not apply)
    ) as target:
        seed.row_factory = sqlite3.Row
        target.row_factory = sqlite3.Row
        seed_tables = {
            row[0]
            for row in seed.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
        if seed_tables != {"users"}:
            raise AccountSeedError("Seed must contain only the users table.")
        if _schema_signature(seed) != _schema_signature(target):
            raise AccountSeedError("Account seed schema does not match the app.")

        columns, source_rows = _read_users(seed)
        _check_database(seed)
        if apply:
            target.execute("PRAGMA foreign_keys = ON")
            target.execute("BEGIN IMMEDIATE")
        try:
            _check_database(target)
            _, target_rows = _read_users(target)
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
            quoted_columns = ", ".join(f'"{column}"' for column in columns)
            target.execute("PRAGMA defer_foreign_keys = ON")
            target.executemany(
                f'INSERT INTO users ({quoted_columns}) VALUES ({placeholders})',
                [tuple(row[column] for column in columns) for row in missing_rows],
            )
            _check_database(target)
            actual_count = target.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]
            expected_count = len(target_rows) + len(missing_rows)
            if actual_count != expected_count:
                raise AccountSeedError("Account count validation failed.")
            target.commit()
            summary["added_accounts"] = len(missing_rows)
            return summary
        except Exception:
            if apply:
                target.rollback()
            raise
