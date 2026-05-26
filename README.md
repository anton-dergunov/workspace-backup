# workspace-backup

[![Tests](https://github.com/anton-dergunov/workspace-backup/actions/workflows/test.yml/badge.svg)](https://github.com/anton-dergunov/workspace-backup/actions/workflows/test.yml)

A thin layer on top of [restic](https://restic.net), [resticprofile](https://creativeprojects.github.io/resticprofile/), and [rclone](https://rclone.org) for backing up source code, ML projects, and research workspaces.

## The three tools

| Tool | Purpose |
|------|---------|
| **[restic](https://restic.net)** | Deduplicating backup engine: encryption, snapshots, incremental backups, restore |
| **[resticprofile](https://creativeprojects.github.io/resticprofile/)** | Config file + scheduling wrapper for restic. Define backup jobs and schedules once, run them reliably |
| **[rclone](https://rclone.org)** | Cloud storage abstraction: Google Drive, S3, Dropbox, Azure, etc. Handles auth and transport |

This repo adds two utilities to make managing restic backups easier.

---

## Getting started

**See [SETUP.md](SETUP.md)** for installation and first-run instructions. It covers installing the three tools above, configuring your cloud remote, initializing a restic repository, and running your first backup.

---

## What this repo adds

### Preview script

**Purpose:** walk a directory tree with your restic exclude file and report which files would be included vs excluded — *without needing a configured restic repository*.

The goal is to curate your exclude file so the restic repository stays small. ML and research workspaces especially accumulate large artifacts (`.ckpt`, `.pt`, `.safetensors`, model caches, datasets) that should be excluded.

The repo includes `config/excludes.txt` as a starting point for Python and ML workspaces (venv, `__pycache__`, wandb, mlruns, model checkpoints, etc.).

**Usage:**

```bash
python scripts/preview.py <source-directory> [options]

Options:
  --exclude-file FILE       Path to exclude patterns (default: ~/.config/restic/excludes)
  --show included|excluded|both  What to display (default: both)
  --min-size SIZE           Skip files below this size (e.g. 1MB, 500KB)
  --summarize NAME          Collapse directories (e.g. .git); repeatable
  --output FILE             Write report to .txt or .html file; omit for console
```

**Examples:**

```bash
# Quick preview of what's included
python scripts/preview.py ~/projects --show included

# HTML report with large artifacts collapsed
python scripts/preview.py ~/projects --summarize .git --summarize node_modules --output /tmp/report.html

# Focus on large files
python scripts/preview.py ~/projects --min-size 50MB
```

### Guardrails (work in progress)

A resticprofile `run-after` hook that warns when something unusual happened during a backup.

**What it checks:**
- Snapshot size grew more than 150% vs the previous snapshot
- File count grew more than 200% vs the previous snapshot
- New file extensions appeared that weren't in the previous snapshot

**Status:** Currently has a known bug and is disabled in `profiles.yaml.sample`. It will be re-enabled and improved in a future update.

**Notifications:** Sends macOS desktop alerts via `terminal-notifier`. Optional — only needed if you use guardrails.

```bash
# Install (optional, only for guardrails)
brew install terminal-notifier
```

**Configuration:** Set environment variables when invoking the guardrail hook in your resticprofile config:

```
GUARDRAIL_MAX_GROWTH_RATIO=1.5
GUARDRAIL_MAX_FILE_COUNT_GROWTH_RATIO=2.0
GUARDRAIL_WARN_ON_NEW_EXTENSIONS=true
```

---

## Requirements

- Python 3.10+
- [restic](https://restic.net) — `brew install restic`
- [resticprofile](https://creativeprojects.github.io/resticprofile/) — `brew install resticprofile`
- [rclone](https://rclone.org) — `brew install rclone`
- [terminal-notifier](https://github.com/julienXX/terminal-notifier) — `brew install terminal-notifier` (optional, for guardrails only)
