# Workspace Backup — Design

Lightweight Python orchestrator on top of restic for backing up source code,
ML projects, and research workspaces. Provides config-driven jobs, a
developer-friendly dry-run, safety guardrails, structured logging, and
scheduling management — all delegating actual storage to restic.

## Overview

This project is a thin orchestration layer on top of restic.

It is designed for:
- ML engineers
- research engineers
- software engineers
- data scientists

The goal is NOT to implement backup storage or archiving logic.

Instead, the goal is:
- own the job configuration (what to back up, where)
- provide a clear dry-run for curating exclude patterns
- enforce safety guardrails before and after each backup
- manage logs and notifications
- handle macOS scheduling

Everything else — storage format, deduplication, encryption, retention,
restore, state — is delegated to restic.

---

# Core Design Principles

## 1. Transparency

The system must clearly show:
- which files are included and excluded
- why files are excluded
- backup sizes and largest files
- guardrail warnings

## 2. Simplicity

- remain understandable
- avoid enterprise complexity
- delegate aggressively to restic
- keep the orchestrator small

## 3. Configurability

All important behavior must be configurable:
- jobs (source, repository, destinations)
- exclude patterns
- guardrail thresholds
- schedule interval
- notification and logging settings

No hardcoded magic numbers.

## 4. Safety

- pre-backup: flag large files before they are uploaded
- post-backup: detect unexpected size growth, new file extensions, file count jumps
- support dry-run inspection before any real backup

## 5. Minimal Dependencies

External tools:
- **restic** — all backup storage, deduplication, encryption, retention, restore
- **rclone** — cloud transport backend for restic (configured separately)

Python layer:
- **pathspec** — apply exclude patterns in dry-run mode (without a repo)
- **PyYAML** — config parsing
- optionally **rich**, **humanize** for nicer output

---

# Architecture

## Components

### backup.py

Single entry point. Subcommands:

```
backup.py dry-run [--job name]     scan source, show included/excluded files
backup.py run [--job name]         run backup jobs
backup.py schedule install         create and load launchd agent
backup.py schedule uninstall       unload and remove launchd agent
backup.py schedule status          show scheduling state
```

### restic

Handles everything storage-related:
- chunked, deduplicated, encrypted storage
- snapshot management
- retention policy (forget + prune)
- restore
- repository state (no separate state.json needed)

### rclone

Cloud transport for restic. Configured manually by the user before first use.
The orchestrator never invokes rclone directly — restic uses it as a backend
via `rclone:<remote>:<path>` repository URLs.

---

# Directory Layout

```
workspace-backup/
├── backup.py
├── requirements.txt
├── README.md
├── CLAUDE.md
├── config/
│   └── config.yaml
├── excludes.txt          ← restic-format global exclude patterns
├── logs/
│   ├── latest.log        ← symlink to most recent log
│   └── history/
│       ├── 2026-05-22_13-00-00.log
│       └── 2026-05-23_13-00-00.log
└── tests/
```

---

# Backup Jobs

Each job defines what to back up and where.

```yaml
jobs:
  - name: projects
    source: ~/projects
    repository: rclone:gdrive:restic-backups

  - name: research
    source: ~/research
    repository: rclone:s3-backup:restic-research
```

Each job maps to exactly one restic repository. Multiple destinations means
multiple jobs pointing to different repositories. All repositories must be
initialized before the first run (`restic -r <repo> init`).

---

# Exclude Patterns

## Global Exclude File

A single `excludes.txt` file applies to all jobs. Format follows restic's
exclude file syntax (similar to shell glob, one pattern per line, `#` for
comments).

Example `excludes.txt`:

```
# Python
.venv/
__pycache__/
*.pyc
*.egg-info/
.pytest_cache/
.mypy_cache/

# Node
node_modules/

# ML artifacts
wandb/
checkpoints/
*.ckpt
*.pt
*.safetensors

# macOS
.DS_Store

# Editors
.idea/
.vscode/
```

The exclude file path is configurable in `config.yaml`.

## Per-Directory Opt-Out

Per-directory ignore files are not supported. Instead, drop a `.nobackup`
file in any directory to exclude it entirely.

The orchestrator always passes `--exclude-if-present .nobackup` to every
restic invocation.

---

# Dry Run Mode

```bash
python backup.py dry-run
python backup.py dry-run --job projects
```

The dry-run is implemented by the orchestrator itself using `pathspec` to
apply the exclude patterns. It does NOT invoke restic and does NOT require a
repository to exist. This makes it usable before the first backup — the
primary use case is curating the exclude file for a new project or language.

Output: sorted list of included files, sorted list of excluded files,
statistics, largest included files, largest excluded files.

---

# Dry Run Output

```text
Job: projects  (~/projects)

--- Included Files (12,433) ---
projects/repo1/main.py
projects/repo1/train.py
...

--- Excluded Files (88,192) ---
projects/repo1/.venv/bin/python    [rule: .venv/]
projects/repo1/__pycache__/x.pyc   [rule: __pycache__/]
...

--- Statistics ---
Included:   12,433 files    2.1 GB
Excluded:   88,192 files   48.0 GB

--- Largest Included Files ---
512 MB  projects/data/sample.parquet
220 MB  projects/model-small.pt

--- Largest Excluded Files ---
42 GB   projects/checkpoints/full.ckpt
12 GB   projects/.venv/libtorch.so
```

Excluded lines include the matching rule to make it easy to audit.

---

# Guardrails

Guardrails are safety checks that run around each backup. They do not block
the backup — they emit warnings and send notifications.

## Pre-Backup Guardrails

Run before the backup starts, using a filesystem scan (no repo needed).

**Large file check:** warn if any included file exceeds `max_file_size_mb`.

```yaml
guardrails:
  max_file_size_mb: 100
```

## Post-Backup Guardrails

Run after the backup completes, using `restic stats` and `restic ls --json`
on the new and previous snapshots.

**Size growth ratio:** warn if the new snapshot is significantly larger than
the previous one.

```yaml
guardrails:
  max_growth_ratio: 1.5    # warn if >150% of previous snapshot size
```

**File count growth ratio:** warn if the number of backed-up files jumps
unexpectedly.

```yaml
guardrails:
  max_file_count_growth_ratio: 2.0
```

**New file extensions:** warn if the new snapshot contains file extensions
not present in the previous snapshot. This catches accidental inclusion of
new binary or artifact types.

```yaml
guardrails:
  warn_on_new_extensions: true
```

Example warning:
```
WARNING: new file extensions in snapshot abc123:
  .so   (14 files, 380 MB)
  .bin  (3 files, 12 MB)
```

## Post-Factum Cleanup

If a guardrail fires after a real backup and you want to remove the offending
files from all existing snapshots:

```bash
# Rewrite all snapshots excluding a pattern, remove originals immediately
restic -r <repo> rewrite --exclude "*.so" --forget
restic -r <repo> prune
```

`restic rewrite` creates new snapshots without the matched files. `--forget`
removes the originals in the same step. `prune` frees the storage.

---

# Notifications

Notifications only fire on warnings or failures. Silent on success.

Backend: `terminal-notifier` (macOS).

```bash
terminal-notifier -title "Backup Warning" -message "..."
```

Conditions that trigger a notification:
- any guardrail threshold exceeded
- backup failure
- repository unreachable

---

# Logging

Each run writes a timestamped log to `logs/history/`. The `logs/latest.log`
symlink always points to the most recent run.

```text
logs/
├── latest.log  →  history/2026-05-22_13-00-00.log
└── history/
    ├── 2026-05-22_13-00-00.log
    └── 2026-05-23_13-00-00.log
```

Log retention is configurable:

```yaml
logs:
  retention_days: 90
```

Logs older than `retention_days` are deleted on each run.

---

# Retention Policy

Delegated entirely to restic. After each successful backup:

```bash
restic forget \
  --keep-daily 7 \
  --keep-weekly 8 \
  --keep-monthly 12 \
  --keep-yearly 5 \
  --prune
```

Configurable in `config.yaml`:

```yaml
retention:
  daily: 7
  weekly: 8
  monthly: 12
  yearly: 5
```

---

# Restore

Delegated to restic. No custom restore logic needed.

```bash
# Restore latest snapshot to a directory
restic -r <repo> restore latest --target ~/restored

# Restore a single file (path is relative to backup root)
restic -r <repo> restore latest \
  --include /projects/repo/important.py \
  --target /tmp/restore

# Browse snapshots
restic -r <repo> snapshots
restic -r <repo> ls latest
```

---

# Configuration File

Full example `config/config.yaml`:

```yaml
jobs:
  - name: projects
    source: ~/projects
    repository: rclone:gdrive:restic-backups

  - name: research
    source: ~/research
    repository: rclone:s3-backup:restic-research

exclude_file: excludes.txt

schedule:
  min_backup_interval_hours: 20

guardrails:
  max_growth_ratio: 1.5
  max_file_size_mb: 100
  max_file_count_growth_ratio: 2.0
  warn_on_new_extensions: true

notification:
  enabled: true

retention:
  daily: 7
  weekly: 8
  monthly: 12
  yearly: 5

logs:
  retention_days: 90
```

---

# Scheduling

## Strategy for Irregular Laptop Use

A fixed daily trigger (e.g. 13:00 via `StartCalendarInterval`) silently skips
runs when the laptop is closed or unused. For irregular usage the correct
approach is a frequent trigger combined with a recency check in the script.

**Pattern:**
```
launchd fires every hour (StartInterval: 3600)
  → script queries last restic snapshot time
  → if age < min_backup_interval_hours: exit silently
  → if age ≥ min_backup_interval_hours: run backup
```

**Recommended interval:** `min_backup_interval_hours: 20`

This is effectively once per day, but not tied to a fixed time. On any day
the laptop is open for at least one hour, a backup will eventually happen.
Days where the machine is closed all day are simply missed — acceptable for a
source code backup that complements git.

**Why not more often?** For source code that is also pushed to git, once per
day is the right cadence. More frequent backups add repository churn and cloud
storage costs without meaningful safety improvement.

## Scheduling Management

```bash
python backup.py schedule install    # write and load launchd agent
python backup.py schedule uninstall  # unload and delete plist
python backup.py schedule status     # show agent state and last run time
```

The generated plist uses `StartInterval: 3600`. The recency check uses
`restic snapshots --latest 1 --json` to read the last snapshot timestamp.

macOS only. The plist is written to:
```
~/Library/LaunchAgents/com.user.workspace-backup.plist
```

---

# Security Considerations

The orchestrator does not manage credentials.

- restic repository passwords: set via `RESTIC_PASSWORD` env var or
  `RESTIC_PASSWORD_FILE` pointing to a file outside the repo
- rclone remotes: configured and authenticated separately via `rclone config`
- The orchestrator only reads `RESTIC_PASSWORD` (or its file) and passes
  repository URLs to restic

---

# Dependencies

## Python

```
pathspec    exclude pattern matching for dry-run
PyYAML      config parsing
```

Optional:
```
rich        prettier terminal output
humanize    human-readable sizes
```

## External

```
restic              backup engine
terminal-notifier   macOS notifications
```

rclone is required only if using cloud backends (Google Drive, S3, etc.):
```
rclone
```

Install:
```bash
brew install restic terminal-notifier
brew install rclone   # if using cloud backends
```

---

# Non-Goals

This project is NOT intended to:
- implement its own backup storage or archive format
- manage restic repository initialization or passwords
- configure rclone remotes
- replace enterprise backup systems
- support block-level deduplication (restic handles this)
- support multi-user orchestration
- run on non-macOS systems (scheduling layer is macOS-specific; core backup
  logic is portable)
