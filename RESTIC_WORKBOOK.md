# Restic Workbook

Practical guide for evaluating restic against your backup workflow,
starting from your existing rclone/gdrive setup.

---

## Requirements coverage

How restic maps to the requirements in DESIGN.md, and what a thin
Python wrapper on top would need to handle.

| Requirement | Restic native | Thin wrapper needed |
|---|---|---|
| Backup jobs (source + destinations config) | No | Yes — read config, invoke restic per job |
| Multiple destinations per job | Partial — run twice with `-r` | Yes — loop over destinations |
| Any rclone remote as destination | Yes — `rclone:remote:path` backend | No |
| Global exclude file | Yes — `--exclude-file` | No |
| Per-directory `.backupignore` | No | Possible but complex; global covers 95% of cases |
| Dry-run mode | Yes — `--dry-run` | Partial — excluded files list needs extra work |
| Included files in dry-run output | Yes — `--verbose=2` | No |
| Excluded files in dry-run output | No | Yes — needs separate scan |
| Largest included / excluded files | No | Yes — post-process file list |
| Growth detection + configurable threshold | No | Yes — compare `restic stats` between snapshots |
| Notifications (terminal-notifier) | No | Yes |
| Logging with rotation | No | Yes |
| Retention policy (daily/weekly/monthly) | Yes — `restic forget --keep-*` | No |
| State / backup history | Yes — snapshots stored on remote | No — snapshots replace state.json entirely |
| Simple restore | Yes — `restic restore latest` | No |
| Deduplication | Yes — built-in | No |
| Encryption | Yes — built-in (AES-256) | No |
| Scheduling | No — handled by launchd | No |

### What the wrapper needs to do

With restic handling the hard parts, the wrapper becomes very small.
It only needs to:

1. Parse `config.yaml` and iterate over jobs
2. Invoke `restic backup -r rclone:remote:path --exclude-file ... source` per job+destination
3. For dry-run: scan the source tree, apply excludes, report included/excluded files and sizes
4. Compare `restic stats` of latest vs previous snapshot → growth detection → notification
5. Write a log file per run; rotate old logs
6. Send `terminal-notifier` on warnings or failures
7. After backup: run `restic forget --prune` per repository

Everything else — deduplication, retention tracking, encryption, retries,
the actual upload, state persistence — is handled by restic.

**Estimated wrapper size: ~200–300 lines of Python.** This is significantly
smaller than the original design because restic eliminates the need for
archive creation, state.json, rclone invocation, and retention logic.

### What restic snapshots replace

The original design required a `state.json` file to track backup history for
growth detection and retention. With restic this is unnecessary:

```bash
# Get last snapshot timestamp
restic snapshots --json --latest 1 | python3 -c \
  "import json,sys; s=json.load(sys.stdin); print(s[0]['time'] if s else '')"

# Get size stats for latest snapshot
restic stats latest --json

# Compare sizes between two snapshots
restic stats <snapshot-id> --json
```

The remote repository is the state. No separate tracking needed.

---

## How restic stores data

Restic uses **content-addressed chunked storage**, similar to git objects.

- Files are split into variable-size chunks (typically 512 KB–8 MB)
- Each chunk is hashed and stored exactly once across all snapshots
- A snapshot is a tree of references to chunks — not a full copy, not a diff
- The repository is **always encrypted** (AES-256); you own the key
- Everything is stored in the backend as opaque blobs

**What this means in practice:**

| Situation | What happens |
|---|---|
| File unchanged since last backup | Zero new data uploaded (chunks already exist) |
| File partially changed | Only the changed chunks are uploaded |
| File deleted from disk | Previous snapshots still restore it |
| Some remote blobs lost/corrupted | `restic check` detects it; only affected files unrestorable |

**Restoring** doesn't require anything except `restic` itself and your password.
The format is open and well-documented. There's no vendor lock-in beyond the
tool. If you lose some backend blobs, `restic check --read-data` will tell you
exactly which files are affected.

---

## Does it satisfy your requirements?

| Requirement | restic |
|---|---|
| Exclude .venv, checkpoints, etc. | Yes — global exclude file |
| Any rclone remote as destination | Yes — `rclone:gdrive:path` backend |
| Multiple destinations | Yes — run backup twice with different `-r` targets |
| Retention policy (daily/weekly/monthly) | Yes — `restic forget --keep-daily 7 ...` |
| Growth detection / size warnings | No — you'd script this yourself |
| Per-directory `.backupignore` | No — global only (see note below) |
| Simple restore | Yes — `restic restore latest --target /tmp/restore` |
| Dry run | Yes — `--dry-run` flag |
| Transparent format | No — encrypted opaque blobs |

**On per-directory ignore files:** restic doesn't support them natively.
Workaround: run `restic backup` once per project root (multiple paths or
a script), or use `--exclude-if-present .nobackup` to mark dirs to skip
by dropping a file in them. For most workflows a well-curated global
exclude file covers 95% of cases.

---

## Step 0 — Install restic

```bash
brew install restic
```

Verify:

```bash
restic version
```

---

## Step 1 — Understand where your data actually is

Before writing a single exclude rule, map what's big.

**Total size of your workspace:**

```bash
du -sh ~/projects
```

**Per top-level directory (sorted largest first):**

```bash
du -sh ~/projects/*/ | sort -rh
```

**Two levels deep — finds the real culprits:**

```bash
du -d 2 -h ~/projects | sort -rh | head -40
```

**Find all `.venv` directories and their sizes:**

```bash
find ~/projects -maxdepth 4 -name ".venv" -type d -prune \
  | xargs du -sh 2>/dev/null | sort -rh
```

**Find all `node_modules`:**

```bash
find ~/projects -maxdepth 4 -name "node_modules" -type d -prune \
  | xargs du -sh 2>/dev/null | sort -rh
```

**Find suspiciously large files (>100 MB):**

```bash
find ~/projects -type f -size +100M \
  | xargs du -sh 2>/dev/null | sort -rh
```

**Find common ML artifact directories:**

```bash
find ~/projects -maxdepth 5 \( \
  -name "wandb" -o -name "checkpoints" -o -name ".cache" \
  -o -name "data" -o -name "datasets" \
  \) -type d -prune \
  | xargs du -sh 2>/dev/null | sort -rh
```

Run these, look at the output, then move to Step 2.

---

## Step 2 — Create a global exclude file

Create `~/.config/restic/excludes`:

```bash
mkdir -p ~/.config/restic
```

Paste this starting point, then edit based on what you found in Step 1:

```
# === Python ===
.venv/
venv/
env/
.env/
__pycache__/
*.pyc
*.pyo
*.pyd
*.egg-info/
*.egg/
dist/
build/
.pytest_cache/
.mypy_cache/
.ruff_cache/
htmlcov/
.coverage

# === Node / JS ===
node_modules/
.next/
.nuxt/
dist/
out/

# === ML / Research ===
wandb/
mlruns/
checkpoints/
*.ckpt
*.pt
*.pth
*.safetensors
*.gguf
.cache/

# === Large data (uncomment if you want to exclude) ===
# data/
# datasets/
# *.parquet
# *.csv
# *.hdf5
# *.h5

# === macOS ===
.DS_Store
.AppleDouble
.LSOverride

# === Editors ===
.idea/
*.iml
.vscode/

# === Misc ===
*.log
*.tmp
.Trash/
```

---

## Step 3 — Dry run iteration loop

This is the core workflow. Repeat until the numbers look right.

> **Prerequisite:** `RESTIC_REPOSITORY` must be set and the repository must be
> initialized (Step 4) before these commands work. Restic opens the repository
> even on dry runs. If you haven't done Step 4 yet, use the `du`/`find` commands
> from Step 1 for file-size exploration — they need no repository.

**See everything that would be included:**

```bash
restic backup --dry-run --verbose=2 \
  --exclude-file ~/.config/restic/excludes \
  ~/projects 2>&1 | less
```

**Just the summary stats (files + size):**

```bash
restic backup --dry-run \
  --exclude-file ~/.config/restic/excludes \
  ~/projects
```

Output looks like:
```
Would save 12,341 files, 2.134 GiB
```

**Verify specific directories are being excluded:**

Restic does not print excluded files — they are silently dropped. The way to
confirm an exclusion is working is to search the included output for it and
expect no match:

```bash
# If this returns nothing, the directory is excluded ✓
restic backup --dry-run --verbose=2 \
  --exclude-file ~/.config/restic/excludes \
  ~/projects 2>&1 | grep "\.venv"

restic backup --dry-run --verbose=2 \
  --exclude-file ~/.config/restic/excludes \
  ~/projects 2>&1 | grep "\.DS_Store"
```

**Compare total files on disk vs files restic would include:**

```bash
echo "Total files on disk:"
find ~/projects -type f | wc -l

echo "Files restic would back up:"
restic backup --dry-run --verbose=2 \
  --exclude-file ~/.config/restic/excludes \
  ~/projects 2>&1 | grep -c ", saved in"
```

The difference between these two numbers is your excluded file count.

**Find large files that might slip through your exclude rules:**

```bash
find ~/projects -type f -size +50M \
  | xargs du -sh 2>/dev/null | sort -rh | head -20
```

Then for any file that looks surprising, check if restic would include it:

```bash
restic backup --dry-run --verbose=2 \
  --exclude-file ~/.config/restic/excludes \
  ~/projects 2>&1 | grep "some-large-file.bin"
# no output = excluded ✓, output = will be backed up
```

**Iteration pattern:**

```
run dry-run → spot something unexpected → add rule to excludes → repeat
```

Until `Would save X files, Y GiB` looks correct.

---

## Step 4 — Initialize the restic repository on Google Drive

You already have `gdrive` configured in rclone. Pick a remote path:

```bash
restic -r rclone:gdrive:restic-backups init
```

Restic will ask you to set a password. Store it somewhere safe
(password manager). Without it, the backup is unrecoverable.

Optionally set it as an env var to avoid being prompted every run:

```bash
export RESTIC_PASSWORD="your-password-here"
export RESTIC_REPOSITORY="rclone:gdrive:restic-backups"
```

Put those in `~/.zshenv` or a `.env` file you source manually.

---

## Step 5 — First real backup

```bash
restic backup \
  --exclude-file ~/.config/restic/excludes \
  ~/projects
```

Watch the progress. First run uploads everything; subsequent runs upload
only new/changed chunks.

---

## Step 6 — Verify the backup

**List snapshots:**

```bash
restic snapshots
```

**Check integrity of the repository:**

```bash
restic check
```

**Browse what's inside a snapshot:**

```bash
restic ls latest
```

**Test restore of a single file:**

```bash
restic restore latest \
  --include /projects/some-repo/important.py \
  --target /tmp/test-restore
```

---

## Step 7 — Set up retention policy

After several backups, prune old ones:

```bash
restic forget \
  --keep-daily 7 \
  --keep-weekly 8 \
  --keep-monthly 12 \
  --keep-yearly 5 \
  --prune
```

`--prune` actually removes the data; without it, `forget` only removes
the snapshot references.

---

## Step 8 — Automate with launchd (macOS)

Create `~/Library/LaunchAgents/com.user.restic-backup.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.user.restic-backup</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-c</string>
    <string>
      export RESTIC_PASSWORD="your-password";
      export RESTIC_REPOSITORY="rclone:gdrive:restic-backups";
      restic backup --exclude-file ~/.config/restic/excludes ~/projects
      &amp;&amp; restic forget --keep-daily 7 --keep-weekly 8
        --keep-monthly 12 --keep-yearly 5 --prune
    </string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>13</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/tmp/restic-backup.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/restic-backup.err</string>
</dict>
</plist>
```

Load it:

```bash
launchctl load ~/Library/LaunchAgents/com.user.restic-backup.plist
```

---

## Quick reference

```bash
# Dry run (see what would be backed up)
restic backup --dry-run --exclude-file ~/.config/restic/excludes ~/projects

# Real backup
restic backup --exclude-file ~/.config/restic/excludes ~/projects

# List snapshots
restic snapshots

# Restore latest to a directory
restic restore latest --target ~/restored

# Retention cleanup
restic forget --keep-daily 7 --keep-weekly 8 --keep-monthly 12 --keep-yearly 5 --prune

# Check repository integrity
restic check
```
