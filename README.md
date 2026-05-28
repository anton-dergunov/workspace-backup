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

### Guardrails

A resticprofile `run-after` hook that warns when something unusual happened in a backup. Run after every successful backup; exits non-zero on violation so resticprofile marks the job as failed.

**What it checks:**

| Check | Flag | Default |
|-------|------|---------|
| Snapshot size grew too fast vs previous | `--max-growth-ratio` | 1.2× |
| File count grew too fast vs previous | `--max-file-count-growth-ratio` | 1.2× |
| New file extensions appeared | _(always on)_ | warn |
| Too many new files added in one run | `--max-new-files` | 100 |
| Too much net data added in one run¹ | `--max-added-size` | 10 MB |
| Too much net data removed in one run¹ | `--max-removed-size` | 50 MB |
| Too many files net-removed in one run² | `--max-removed-files` | 100 |
| Total snapshot too large | `--max-total-size` | 5 GB |

¹ "Net" means file modifications are excluded — only genuine additions or deletions count. Moving files within the backup scope is also net-zero.
² Same net logic: files moved within scope count as both added and removed, so net ≈ 0.

**Usage in `profiles.yaml`:**

```yaml
run-after:
  - >-
    python3 /path/to/workspace-backup/scripts/guardrails.py
    --max-growth-ratio 1.2
    --max-file-count-growth-ratio 1.2
    --max-new-files 100
    --max-added-size 10MB
    --max-removed-size 50MB
    --max-removed-files 100
    --max-total-size 5GB
    --log "~/resticprofile-guardrails.log"
    --log-keep-runs 100
    --notify-short "apprise -t '{title}' -b '{message}' macosx://"
    --notify-long "apprise -t '{title}' -b '{details}' 'mailto://user:pass@smtp.example.com'"
```

See `config/profiles.yaml.sample` for the full annotated example.

**Notifications:**

Notifications use command templates with `{placeholder}` substitution. Any tool can be used:

```bash
# macOS desktop notification via apprise
--notify-short "apprise -t '{title}' -b '{message}' macosx://"

# Email via apprise
--notify-long "apprise -t '{title}' -b '{details}' 'mailto://user:pass@smtp.example.com'"

# macOS desktop notification via terminal-notifier
--notify-short "terminal-notifier -title '{title}' -message '{message}'"
```

Available placeholders: `{title}`, `{message}` (compact summary), `{details}` (full report with file listings), `{profile}`, `{status}`, `{violations}`.

Both `--notify-short` and `--notify-long` are repeatable. **Notifications only fire on violations.** The log file (`--log`) is written on every run.

**If guardrails flag a path that shouldn't have been backed up:**

1. Add an exclude pattern to prevent it from being included in future backups.
2. Remove it from existing snapshots with `restic rewrite` (requires restic ≥ 0.16):

```bash
# Remove from latest snapshot only
resticprofile rewrite --exclude /path/to/dir latest

# Remove from all snapshots
resticprofile rewrite --exclude /path/to/dir --all
```

Then clean up orphaned data:

```bash
resticprofile prune
```

If `prune` fails with a stale lock error, unlock first:

```bash
resticprofile unlock && resticprofile prune
```

Run `resticprofile check` afterward to verify repository integrity.

Install [apprise](https://github.com/caronc/apprise) for multi-platform notifications (macOS, email, Slack, etc.):

```bash
pip install apprise
```

---

## Requirements

- Python 3.10+
- [restic](https://restic.net) — `brew install restic`
- [resticprofile](https://creativeprojects.github.io/resticprofile/) — `brew install resticprofile`
- [rclone](https://rclone.org) — `brew install rclone`
- [apprise](https://github.com/caronc/apprise) — `pip install apprise` (optional, for guardrail notifications)

---

## Running tests

Install dev dependencies first:

```bash
pip install -e ".[dev]"
```

| Command | What runs | External tools needed |
|---------|-----------|----------------------|
| `pytest tests/test_guardrails_unit.py -v` | Guardrails unit tests | none |
| `pytest tests/test_output_formats.py -v` | Preview output format tests | none |
| `pytest tests/test_preview_integration.py -v` | Preview vs restic comparison | restic |
| `pytest tests/test_guardrails_integration.py -v` | Guardrails end-to-end tests | restic + resticprofile |
| `pytest -v` | All of the above | restic + resticprofile |

Tests that require missing tools are automatically skipped rather than failing.
