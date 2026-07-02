# API-Key Encryption Runbook

Operational procedures for the two API-key encryption CLIs (PRD [§9.10](prd-observing-pools.md)):

| Script | Module | Purpose |
| --- | --- | --- |
| Sweep | `python -m app.backend.scripts.reencrypt_api_keys` | Encrypt any still-**plaintext** rows under the current master key (`enc:v1:` tag). Idempotent — already-encrypted rows are skipped byte-for-byte. |
| Rotation | `python -m app.backend.scripts.rotate_master_key` | Re-encrypt every `enc:v1:` row from the **current** master key to a **new** one, in a single all-or-nothing transaction. Pure key swap — the `enc:v1:` tag is preserved. |

Both refuse loudly (never a silent no-op) and never print key material. Both accept `--dry-run` (preview counts, commit nothing) and `--verbose` (add a traceback on error).

## Invariant

Key material is supplied **only** via environment variables — never on the command line (it would land in shell history and `ps` output):

- Current master key: OS keyring item `master_key`, else `AHF_MASTER_KEY`.
- New master key (rotation target): `AHF_MASTER_KEY_NEW` — a fresh `Fernet.generate_key()` value.

## ⚠️ Mid-rotation data-loss warning (issue #66-A)

A **running backend holds the OLD master key cached in memory** for the life of the process. Until you repoint the master source **and restart**, that process keeps encrypting *new* writes under the OLD key. If you retire the old key before the restart, those in-flight rows can no longer be decrypted — **data loss**.

There is **no cross-process lock** guarding this. The only safe procedure is to **quiesce/stop the backend** across the rotate → repoint → restart window (steps 3–5 below). Do not retire the old key until verification (step 6) passes.

## Rotation — ordered steps

Run these in order. Do not skip the dry-run or the quiesce.

1. **Set the new key (env only).** Generate a new key and export it as `AHF_MASTER_KEY_NEW` — never pass it on the CLI:
   ```bash
   export AHF_MASTER_KEY_NEW="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
   ```
2. **Dry-run.** Preview the counts and confirm the current + new keys resolve; nothing is committed:
   ```bash
   python -m app.backend.scripts.rotate_master_key --dry-run
   ```
   If `skipped_plaintext > 0`, run the sweep first (see below) so those rows are encrypted before you rotate.
3. **Quiesce / stop the backend.** Stop every process that holds the old master key cached (see the warning above). This begins the no-write window.
4. **Rotate.** Re-encrypt all rows to the new key (single transaction, all-or-nothing):
   ```bash
   python -m app.backend.scripts.rotate_master_key
   ```
   Exit `0` with `rotated=N` means the DB rows are now under the new key.
5. **Repoint the master key and restart.** Point the master source (keyring item `master_key` or `AHF_MASTER_KEY`) at the `AHF_MASTER_KEY_NEW` value, then restart the backend so it loads the new key. This ends the no-write window.
6. **Verify.** Confirm the restarted backend reads keys correctly (e.g. `is_set` / `masked_tail` on the keys endpoint; no `CryptoError` in logs). A `--dry-run` rotation now reports `rotated=0` (nothing left under the old key).
7. **Retire the old key.** Only after verification passes, remove the old key value from your secret store / backups.

## Sweep — encrypting plaintext rows

Run the sweep to bring plaintext rows under the master key (e.g. after enabling `KEY_ENCRYPTION`, or when a rotation dry-run reports `skipped_plaintext > 0`):

```bash
python -m app.backend.scripts.reencrypt_api_keys --dry-run   # preview
python -m app.backend.scripts.reencrypt_api_keys             # commit
```

The sweep is idempotent and does **not** touch already-encrypted rows. It does not rotate the master key — run the rotation procedure above for that.

## Exit codes

Both scripts share the same convention:

| Code | Meaning |
| --- | --- |
| `0` | Success — the run completed; see stdout for counts and (rotation) repoint next-steps. |
| `1` | Unexpected runtime error — e.g. a malformed key, a row that will not decrypt, or (rotation) a new key identical to the old. Typed error + `no rows committed` on stderr; `--verbose` adds a traceback. Nothing is committed. |
| `2` | Refused (fail-loud precondition) — `KEY_ENCRYPTION` is not enabled, or (rotation) `AHF_MASTER_KEY_NEW` is unset. Never a silent no-op. |
