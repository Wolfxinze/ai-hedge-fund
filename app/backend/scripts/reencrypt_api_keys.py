"""CLI: bulk re-encrypt plaintext API key rows (PRD v4 §9.10 / issue #25).

Usage
-----
    python -m app.backend.scripts.reencrypt_api_keys [--dry-run] [--verbose]

Exit codes
----------
0   Success (sweep ran; see stdout for counts).
2   KEY_ENCRYPTION is not enabled — refusing to run (fail loud, never silent no-op).
1   Unexpected runtime error (typed error + "no rows committed" on stderr; --verbose adds a traceback).

The sweep is idempotent: already-encrypted rows are skipped (byte-identical).
Run with --dry-run to preview what would change without committing.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from collections.abc import Sequence

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
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="On error, also print a full traceback to stderr (default: typed one-liner).",
    )
    args = parser.parse_args(argv)

    if not is_encryption_enabled():
        print(
            "refusing: KEY_ENCRYPTION is not enabled; set KEY_ENCRYPTION=on and ensure " "the master key is provisioned before running the re-encrypt sweep",
            file=sys.stderr,
        )
        return 2

    db = SessionLocal()
    try:
        cipher = get_cipher(db)

        if args.dry_run:
            result = reencrypt_plaintext_api_keys(db, cipher, commit=False)
            db.rollback()
            print(f"[dry-run] scanned={result.scanned} upgraded={result.upgraded} " f"skipped_encrypted={result.skipped_encrypted} skipped_empty={result.skipped_empty}")
        else:
            result = reencrypt_plaintext_api_keys(db, cipher, commit=True)
            print(f"scanned={result.scanned} upgraded={result.upgraded} " f"skipped_encrypted={result.skipped_encrypted} skipped_empty={result.skipped_empty}")
    except Exception as exc:
        # Fail loud with the error TYPE + message (not a bare str) so the operator can tell a
        # CryptoError apart from, say, an OperationalError at a glance. The sweep already rolls
        # back internally on any error; rollback here too (belt-and-suspenders) so the explicit
        # "no rows committed" line below is truthful regardless of where the failure originated.
        db.rollback()
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("no rows committed (rolled back)", file=sys.stderr)
        if args.verbose:
            traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
