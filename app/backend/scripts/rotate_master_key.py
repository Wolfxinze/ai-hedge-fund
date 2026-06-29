"""CLI: rotate the API-key master key (PRD v4 §9.10 / issue #25, item 1b).

Re-encrypts every ``enc:v1:`` API-key row from the CURRENT master key to a NEW one, in a single
all-or-nothing transaction. The new key is supplied out-of-band via ``AHF_MASTER_KEY_NEW`` (never
on the command line, to keep it out of shell history). The rotation keeps the ``enc:v1:`` tag — it
is a pure key swap, not an algorithm change.

Usage
-----
    AHF_MASTER_KEY_NEW=<new Fernet key> \\
        python -m app.backend.scripts.rotate_master_key [--dry-run] [--verbose]

Exit codes
----------
0   Success (rotation ran; see stdout for counts + repoint next-steps).
2   Refused: KEY_ENCRYPTION is not enabled, OR AHF_MASTER_KEY_NEW is unset (fail loud, never a
    silent no-op and never rotate to an absent/invented key).
1   Unexpected runtime error — e.g. a malformed new key, or a row that will not decrypt under the
    current master key (typed error + "no rows committed" on stderr; --verbose adds a traceback).

Run with --dry-run to preview the counts without committing. After a real rotation, repoint the
master key (OS keyring item ``master_key`` or ``AHF_MASTER_KEY``) to the new value and restart;
the old key can then be retired.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from collections.abc import Sequence

from app.backend.database.connection import SessionLocal
from app.backend.services.crypto import build_fernet, is_encryption_enabled, resolve_master_fernet
from app.backend.services.key_migration import rotate_api_key_master

_NEW_KEY_ENV = "AHF_MASTER_KEY_NEW"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rotate the API-key master key: re-encrypt all rows from the current key to a new key.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the rotation counts without committing to the database.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="On error, also print a full traceback to stderr (default: typed one-liner).",
    )
    args = parser.parse_args(argv)

    if not is_encryption_enabled():
        print(
            "refusing: KEY_ENCRYPTION is not enabled; rotation only applies to encrypted rows — " "set KEY_ENCRYPTION=on with the current master key provisioned before rotating",
            file=sys.stderr,
        )
        return 2

    new_key = (os.environ.get(_NEW_KEY_ENV) or "").strip()
    if not new_key:
        print(
            f"refusing: {_NEW_KEY_ENV} is not set; provide the new master key (a Fernet.generate_key() " f"value) via {_NEW_KEY_ENV} — rotation never invents or omits the target key",
            file=sys.stderr,
        )
        return 2

    db = SessionLocal()
    try:
        old_fernet = resolve_master_fernet()
        new_fernet = build_fernet(new_key, source=_NEW_KEY_ENV)

        if args.dry_run:
            result = rotate_api_key_master(db, old_fernet=old_fernet, new_fernet=new_fernet, commit=False)
            # Defense-in-depth, mirroring the re-encrypt sweep: with commit=False nothing is persisted
            # and the `finally: db.close()` discards the uncommitted transaction anyway — kept to make
            # the no-persist intent explicit at the call site. Do not remove it to "simplify".
            db.rollback()
            print(f"[dry-run] scanned={result.scanned} rotated={result.rotated} " f"skipped_plaintext={result.skipped_plaintext} skipped_empty={result.skipped_empty}")
        else:
            result = rotate_api_key_master(db, old_fernet=old_fernet, new_fernet=new_fernet, commit=True)
            print(f"scanned={result.scanned} rotated={result.rotated} " f"skipped_plaintext={result.skipped_plaintext} skipped_empty={result.skipped_empty}")
            if result.rotated:
                # Next-steps: the DB rows are now under the new key, but the resolved master is still
                # the old one for any running process. The operator must repoint the source + restart.
                print(f"rotation complete — now repoint the master key (OS keyring item 'master_key' or " f"AHF_MASTER_KEY) to the {_NEW_KEY_ENV} value and restart; the old key can then be retired.")
            else:
                # Nothing was re-keyed — do NOT advise retiring the old key (a no-op on this DB); that
                # would erode trust in the message for the run that actually rotates rows.
                print("no encrypted rows were rotated — nothing to repoint (run the re-encrypt sweep first if rows are still plaintext).")
    except Exception as exc:
        # Fail loud with the error TYPE + message (not a bare str) so a CryptoError (wrong/malformed
        # key) is distinguishable from, say, an OperationalError at a glance.
        # This rollback is redundant-but-intentional defense-in-depth: rotate_api_key_master already
        # rolls back + re-raises on any error, and `finally: db.close()` discards uncommitted state.
        # We keep it so the explicit "no rows committed" line is truthful even if a future caller path
        # skips one of those guarantees — never remove it to "simplify".
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
