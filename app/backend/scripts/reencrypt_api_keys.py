"""CLI: bulk re-encrypt plaintext API key rows (PRD v4 §9.10 / issue #25).

Usage
-----
    python -m app.backend.scripts.reencrypt_api_keys [--dry-run]

Exit codes
----------
0   Success (sweep ran; see stdout for counts).
2   KEY_ENCRYPTION is not enabled — refusing to run (fail loud, never silent no-op).
1   Unexpected runtime error.

The sweep is idempotent: already-encrypted rows are skipped (byte-identical).
Run with --dry-run to preview what would change without committing.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from app.backend.database.connection import SessionLocal
from app.backend.services.crypto import get_cipher, is_encryption_enabled
from app.backend.services.key_migration import reencrypt_plaintext_api_keys


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bulk re-encrypt plaintext API keys under the current master key.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would change without committing to the database.",
    )
    args = parser.parse_args(argv)

    if not is_encryption_enabled():
        print(
            "refusing: KEY_ENCRYPTION is not enabled; set KEY_ENCRYPTION=on and ensure "
            "the master key is provisioned before running the re-encrypt sweep",
            file=sys.stderr,
        )
        return 2

    db = SessionLocal()
    try:
        cipher = get_cipher(db)

        if args.dry_run:
            result = reencrypt_plaintext_api_keys(db, cipher, commit=False)
            db.rollback()
            print(
                f"[dry-run] scanned={result.scanned} upgraded={result.upgraded} "
                f"skipped_encrypted={result.skipped_encrypted} skipped_empty={result.skipped_empty}"
            )
        else:
            result = reencrypt_plaintext_api_keys(db, cipher, commit=True)
            print(
                f"scanned={result.scanned} upgraded={result.upgraded} "
                f"skipped_encrypted={result.skipped_encrypted} skipped_empty={result.skipped_empty}"
            )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
