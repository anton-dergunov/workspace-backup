# Guardrails runbook — investigating & remediating violations

When `guardrails.py` reports a violation it also emails the exact commands to run,
pre-filled with the snapshot IDs and an example path for that run. This document
is the same guidance in generic form, so you can use it any time.

Replace the placeholders below:

| Placeholder       | Meaning                                              |
|-------------------|------------------------------------------------------|
| `<CURRENT>`       | snapshot ID from the failed run (the newest one)     |
| `<PREV>`          | the previous snapshot ID it was compared against     |
| `<PATH>`          | an absolute path inside a snapshot                    |
| `<PATTERN>`       | a restic exclude pattern, e.g. `*.parquet`, `/dir/**`|
| `<RESTORE_DIR>`   | a local directory to restore files into              |

All commands go through `resticprofile --name <profile>` so they pick up the
repository and credentials from your profile. Add `--config <file>` if your
config isn't in the default location.

---

## 1. Investigate — what changed

```bash
# Everything that changed between the two snapshots (+ added, - removed, M modified)
resticprofile --name <profile> diff <PREV> <CURRENT>

# All files in the new snapshot, with sizes — spot the large/unexpected ones
resticprofile --name <profile> ls -l <CURRENT>

# Snapshot statistics
resticprofile --name <profile> stats <CURRENT>
```

For **size** violations (`SIZE_GROWTH`, `TOTAL_SIZE`, `ADDED_SIZE`), compare the
raw (deduplicated) data each snapshot actually adds:

```bash
resticprofile --name <profile> stats <CURRENT> --mode raw-data
resticprofile --name <profile> stats <PREV>    --mode raw-data
```

For **new file types** (`NEW_EXTENSIONS`), find every file of that type:

```bash
resticprofile --name <profile> find '*.<EXT>'
```

---

## 2. Remediate — remove unwanted data

Use this when files were backed up that shouldn't have been (`NEW_FILES`,
`ADDED_SIZE`, `FILE_COUNT_GROWTH`, `NEW_EXTENSIONS`, and size growth caused by
junk). `restic rewrite` **rewrites snapshot history** — preview first.

> Requires restic ≥ 0.16. `--exclude` takes restic patterns (e.g. `*.parquet`
> or `/path/to/dir/**`), not just exact paths.

```bash
# 1. Preview removing a path from the latest snapshot (no changes are made)
resticprofile --name <profile> rewrite --dry-run --exclude '<PATTERN>' <CURRENT>

# 2. Remove it from the latest snapshot
#    --forget replaces the original; without it, restic keeps a copy tagged 'rewrite'
resticprofile --name <profile> rewrite --forget --exclude '<PATTERN>' <CURRENT>

# 3. Remove it from ALL snapshots in history (omit the snapshot ID)
resticprofile --name <profile> rewrite --forget --exclude '<PATTERN>'

# 4. Reclaim disk space — rewrite only unlinks snapshots, prune deletes the data
resticprofile --name <profile> prune
```

5. **Stop it from being backed up again:** add `<PATTERN>` to
   [config/excludes.txt](config/excludes.txt). Use
   [scripts/preview.py](scripts/preview.py) to confirm the pattern excludes what
   you expect before the next backup.

If the violation was just too many old snapshots accumulating, drop them per your
retention policy instead:

```bash
resticprofile --name <profile> forget --prune
```

---

## 3. Remediate — recover removed data

Use this when files disappeared unexpectedly (`REMOVED_FILES`, `REMOVED_SIZE`)
and the deletion was **not** intentional. Restore them from the previous snapshot:

```bash
resticprofile --name <profile> restore <PREV> --include '<PATTERN>' --target <RESTORE_DIR>
```

If the deletion was intentional (you really did delete those files), no action is
needed — the guardrail is just flagging the size of the change.

---

## 4. Housekeeping

```bash
# If a command fails with a stale lock
resticprofile --name <profile> unlock

# Verify repository integrity after rewriting/pruning
resticprofile --name <profile> check
```

---

## If resticprofile intercepts a flag

A few restic flags collide with resticprofile's own. If a command above is
misparsed, run `restic` directly after exporting the repository credentials:

```bash
export RESTIC_REPOSITORY=...   # same values your profile uses
export RESTIC_PASSWORD=...
restic rewrite --forget --exclude '<PATTERN>' <CURRENT>
```
