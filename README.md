# workspace-backup

[![Tests](https://github.com/anton-dergunov/workspace-backup/actions/workflows/test.yml/badge.svg)](https://github.com/anton-dergunov/workspace-backup/actions/workflows/test.yml)

Lightweight Python orchestrator on top of [restic](https://restic.net) for
backing up source code, ML projects, and research workspaces.

Restic handles storage, deduplication, encryption, retention, and restore.
This tool handles everything around it: job configuration, a developer-friendly
dry-run for curating exclude patterns, safety guardrails, structured logging,
and macOS scheduling.

---

## What it does

- **Config-driven jobs** — define source directories and restic repositories
  in a single YAML file
- **Dry-run mode** — scan source tree and show exactly which files are included
  and excluded (with the matching rule), no repository needed
- **Guardrails** — warn before backup (large files), warn after backup (size
  growth ratio, new file extensions, file count jumps)
- **Logging** — per-run timestamped logs with configurable retention
- **Scheduling** — install/uninstall/status for a launchd agent that runs
  hourly and skips if a recent backup already happened

---

## Requirements

- macOS (scheduling layer; core backup logic is portable)
- [restic](https://restic.net) — `brew install restic`
- [rclone](https://rclone.org) — `brew install rclone` (if using cloud backends)
- [terminal-notifier](https://github.com/julienXX/terminal-notifier) — `brew install terminal-notifier`
- Python 3.10+

---

## Quick start

**1. Install dependencies**

```bash
brew install restic rclone terminal-notifier
pip install -r requirements.txt
```

**2. Configure rclone remote** (if backing up to cloud)

```bash
rclone config
```

**3. Initialize restic repository**

```bash
export RESTIC_PASSWORD="your-password"
restic -r rclone:gdrive:restic-backups init
```

**4. Edit config**

```bash
cp config/config.yaml.example config/config.yaml
# edit jobs, repository URLs, thresholds
```

**5. Dry-run to check what would be backed up**

```bash
python backup.py dry-run
```

Review included/excluded files. Edit `excludes.txt` and repeat until the
numbers look right.

**6. Run first backup**

```bash
python backup.py run
```

**7. Set up automatic scheduling**

```bash
python backup.py schedule install
python backup.py schedule status
```

---

## Dry-run output

```
Job: projects  (~/projects)

--- Included Files (12,433) ---
projects/repo1/main.py
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

--- Largest Excluded Files ---
42 GB   projects/checkpoints/full.ckpt
```

---

## Excluding files

Edit `excludes.txt`. Uses restic's exclude file syntax (shell glob patterns,
one per line, `#` for comments).

To exclude a single directory without editing the global file, drop an empty
`.nobackup` file in it:

```bash
touch some-large-dir/.nobackup
```

The orchestrator always passes `--exclude-if-present .nobackup` to restic.

---

## Guardrails

The following warnings are emitted and sent as macOS notifications:

| Guardrail | When |
|---|---|
| Large file | pre-backup — any included file exceeds `max_file_size_mb` |
| Size growth | post-backup — new snapshot is >150% of previous (configurable) |
| New extensions | post-backup — file extensions not seen in previous snapshot |
| File count growth | post-backup — file count grows beyond expected ratio |

To remove files from past snapshots after a guardrail fires:

```bash
restic -r <repo> rewrite --exclude "*.so" --forget
restic -r <repo> prune
```

---

## Scheduling

The launchd agent fires every hour. The script checks the last restic snapshot
time and skips if a backup occurred within `min_backup_interval_hours` (default
20 hours). This handles irregular laptop usage without missing days.

```bash
python backup.py schedule install    # create and load agent
python backup.py schedule uninstall  # remove agent
python backup.py schedule status     # show state and last run
```

---

## Restore

Restore is handled directly by restic:

```bash
# Restore latest snapshot
restic -r rclone:gdrive:restic-backups restore latest --target ~/restored

# List snapshots
restic -r rclone:gdrive:restic-backups snapshots
```

---

## Configuration reference

See `config/config.yaml` and `DESIGN.md` for full documentation.
