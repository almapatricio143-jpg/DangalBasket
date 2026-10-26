
import base64
import os
import sqlite3
from pathlib import Path

from app import create_app
from account_recovery_seed import AccountSeedError, recover_missing_accounts


app = create_app()


def prepare_seed_path():
    """Decode a private Base64 seed secret into temporary storage if configured."""
    seed_path = os.environ.get("DANGALBASKET_ACCOUNT_RECOVERY_SEED")
    if not seed_path:
        return None

    path = Path(seed_path)
    if path.suffix.lower() != ".b64":
        return str(path)

    destination = Path("/tmp/dangalbasket-account-recovery-seed.sqlite3")
    encoded = path.read_text(encoding="ascii").strip()
    decoded = base64.b64decode(encoded, validate=True)

    with destination.open("wb") as output:
        output.write(decoded)

    return str(destination)


recovery_mode = os.environ.get(
    "DANGALBASKET_ACCOUNT_RECOVERY_MODE", ""
).strip().lower()

if recovery_mode:
    if recovery_mode not in {"dry-run", "apply"}:
        app.logger.error("Account recovery skipped: invalid recovery mode.")
    else:
        try:
            seed_path = prepare_seed_path()
            if not seed_path:
                raise AccountSeedError("Recovery seed path is not configured.")

            summary = recover_missing_accounts(
                seed_path,
                app.config["DATABASE"],
                apply=recovery_mode == "apply",
            )
        except (OSError, ValueError, sqlite3.Error, AccountSeedError):
            app.logger.exception(
                "Account recovery failed; no account data was logged."
            )
        else:
            if summary["conflicts"]:
                app.logger.error(
                    "Account recovery stopped on conflicts: "
                    "source=%d existing=%d missing=%d conflicts=%d",
                    summary["source_accounts"],
                    summary["already_present"],
                    summary["missing_accounts"],
                    summary["conflicts"],
                )
            elif recovery_mode == "dry-run":
                app.logger.info(
                    "Account recovery dry-run: source=%d existing=%d "
                    "missing=%d conflicts=0; no account rows changed.",
                    summary["source_accounts"],
                    summary["already_present"],
                    summary["missing_accounts"],
                )
            else:
                app.logger.info(
                    "Account recovery applied: source=%d existing=%d "
                    "added=%d conflicts=0.",
                    summary["source_accounts"],
                    summary["already_present"],
                    summary["added_accounts"],
                )
