# Workspace Backup System Design

Lightweight source-oriented backup system for research, ML, and software engineering workspaces. Recursive filtering, dry-run inspection, archive-based backups, retention policies, and cloud upload via rclone.

## Overview

This project implements a lightweight, transparent, inspectable backup system for source-code-oriented project directories.

The system is designed primarily for:
- ML engineers
- research engineers
- software engineers
- data scientists

The goal is NOT to create a generic enterprise backup solution.

Instead, the goal is:
- reliable cloud backup
- highly inspectable file filtering
- dry-run visibility
- simple restore
- lightweight architecture
- transparent storage
- strong configurability
- easy customization

The system should integrate cleanly with:
- Git repositories
- ML experiments
- research projects
- mixed-language codebases
- local scripts
- prototype projects

---

# Core Design Principles

## 1. Transparency

The system must clearly show:
- what is included
- what is excluded
- backup sizes
- largest files
- filtering reasons

The user must never feel uncertain about:
- which files are uploaded
- why files are excluded
- how much data is stored

---

## 2. Simplicity

The system should:
- remain understandable
- avoid enterprise complexity
- use plain archives
- avoid opaque snapshot formats

The backup contents should remain easy to inspect manually.

---

## 3. Configurability

All important behavior must be configurable:
- thresholds
- notifications
- retention
- exclusions
- remote names
- paths

No hardcoded magic numbers.

---

## 4. Safety

The system should:
- detect suspicious backup growth
- warn about unexpectedly large files
- support dry-run inspection
- minimize accidental uploads

---

## 5. Minimal Dependencies

The system should reuse mature tools where appropriate:
- rclone for cloud transport
- pathspec for gitignore semantics

The orchestration layer should remain lightweight Python code.

---

# Architecture

## Main Components

### 1. Python Orchestration Script

Responsible for:
- recursive scanning
- filtering
- reporting
- archive creation
- retention policy
- notifications
- rclone invocation

Example:

```text
backup.py
```

---

### 2. rclone

Responsible for:
- cloud upload
- authentication
- retries
- remote access
- cloud transport

The Python layer should treat rclone as a transport backend.

All cloud remotes must already be configured in rclone before use.

The system does not manage rclone authentication.

---

### 3. Cloud Destinations

The system supports any rclone-compatible backend:
- Google Drive
- S3
- Backblaze B2
- Dropbox
- SFTP servers
- any other rclone remote

A destination is a pair:

```text
(rclone_remote_name, remote_path)
```

Example:

```text
remote: gdrive
path:   project-backups
```

Multiple destinations may be specified globally and per source.

All remotes must exist in rclone configuration before the backup runs.

---

# Directory Layout

## Proposed Repository Structure

```text
project-backup/
├── backup.py
├── requirements.txt
├── README.md
├── config/
│   ├── config.yaml
│   └── global.backupignore
├── state.json          ← local state cache (see Backup State section)
├── logs/
│   ├── latest.log
│   └── history/
└── tests/
```

---

# Source Directories

The system supports one or more source directories.

Each source directory:
- is backed up as a separate archive
- may specify its own set of cloud destinations
- uses the source `name` field in archive filenames
- inherits global destinations if no per-source destinations are set

Example sources:

```text
~/projects
~/research
~/notes
```

All source directories must be explicitly listed in configuration.

The backup recursively processes the entire tree of each source directory.

---

# Filtering System

## Filtering Philosophy

The filtering system should behave similarly to `.gitignore`.

The system uses:

```text
.backupignore
```

files recursively throughout the directory tree.

---

# Recursive Semantics

Each directory may contain:

```text
.backupignore
```

Rules apply:
- to the current directory
- recursively to descendants

Child `.backupignore` files augment parent rules.

---

# Syntax

The syntax must follow exact `.gitignore` semantics.

Examples:

```text
# Ignore Python caches
__pycache__/
*.pyc

# Ignore ML artifacts
wandb/
checkpoints/
*.ckpt
*.pt

# Ignore environments
.venv/
env/
node_modules/

# Re-include specific file
!checkpoints/example-small.pt
```

---

# Parser Library

Use:

```text
pathspec
```

Specifically:

```python
pathspec.PathSpec.from_lines("gitwildmatch", patterns)
```

Do NOT implement gitignore parsing manually.

---

# Dry Run Mode

The system must support:

```bash
python backup.py --dry-run
```

This mode performs:
- recursive scan
- filtering
- reporting

WITHOUT:
- archive creation
- uploads

---

# Dry Run Output

The dry run output should include:

## Included Files

Sorted list of included files.

Example:

```text
# Included Files:
repo1/main.py
repo2/train.py
```

---

## Excluded Files

Sorted list of excluded files.

Example:

```text
# Excluded Files:
repo/checkpoints/model.ckpt
repo/.venv/bin/python
```

---

## Statistics

Example:

```text
# Statistics

Included:
    12433 files
    2.1 GB

Excluded:
    88192 files
    48 GB
```

---

## Largest Included Files

Example:

```text
Largest Included Files:
    512 MB  repo/data/sample.parquet
    220 MB  repo/model-small.pt
```

---

## Largest Excluded Files

Example:

```text
Largest Excluded Files:
    42 GB   repo/checkpoints/full.ckpt
    12 GB   repo/.venv/libtorch.so
```

---

# Backup Archive

## Archive Format

Preferred format:

```text
tar.gz
```

Reasoning:
- portable
- inspectable
- simple
- universally supported

---

# Temporary Staging

Use:

```python
tempfile.TemporaryDirectory()
```

No permanent staging directory should exist.

---

# Archive Naming

Archives are named using the source `name` field and the backup timestamp.

Example:

```text
projects-backup-2026-05-22_18-30-00.tar.gz
research-backup-2026-05-22_18-30-00.tar.gz
```

Pattern:

```text
{source_name}-backup-{YYYY}-{MM}-{DD}_{HH}-{MM}-{SS}.tar.gz
```

---

# Upload Mechanism

Use rclone.

Each archive is uploaded to all configured destinations for its source.

Example:

```bash
rclone copy projects-backup-2026-05-22_18-30-00.tar.gz gdrive:project-backups
rclone copy projects-backup-2026-05-22_18-30-00.tar.gz s3-backup:backups/projects
```

---

# Backup State

## Decision: Store State on Remote, Cache Locally

The state file tracks what archives exist on each remote, historical sizes, and
retention bookkeeping. Storing it only locally creates a serious gap: if the
machine is lost or rebuilt, the state is gone — but the archives on the remotes
are not. The next run has no knowledge of existing backups and cannot apply
retention policy or detect growth anomalies.

Because the state describes what is *on the remote*, it belongs *on the remote*.

**Design:**

- After each successful backup, upload a small `state.json` sidecar to every
  destination path where archives were written.
- Also maintain a local cache (`state.json` in the project config directory) to
  avoid a network round-trip on every run.
- On startup: if local cache is absent or stale, download `state.json` from the
  primary destination to restore it.
- The remote copy is authoritative. The local copy is a performance cache.

This adds negligible overhead (state files are a few KB) and makes the system
resilient to machine loss.

---

## State File Format

Example `state.json`:

```json
{
  "schema_version": 1,
  "updated_at": "2026-05-22T13:00:05Z",
  "sources": {
    "projects": {
      "path": "~/projects",
      "last_backup": {
        "timestamp": "2026-05-22T13:00:00Z",
        "archive_name": "projects-backup-2026-05-22_13-00-00.tar.gz",
        "size_bytes": 2254857830,
        "file_count": 12433,
        "uploads": [
          { "remote": "gdrive",    "path": "project-backups", "success": true },
          { "remote": "s3-backup", "path": "backups/projects", "success": true }
        ]
      },
      "history": [
        {
          "timestamp": "2026-05-21T13:00:00Z",
          "archive_name": "projects-backup-2026-05-21_13-00-00.tar.gz",
          "size_bytes": 2200000000,
          "file_count": 12100,
          "retention_bucket": "daily"
        },
        {
          "timestamp": "2026-05-15T13:00:00Z",
          "archive_name": "projects-backup-2026-05-15_13-00-00.tar.gz",
          "size_bytes": 2050000000,
          "file_count": 11800,
          "retention_bucket": "weekly"
        }
      ]
    },
    "research": {
      "path": "~/research",
      "last_backup": {
        "timestamp": "2026-05-22T13:02:10Z",
        "archive_name": "research-backup-2026-05-22_13-02-10.tar.gz",
        "size_bytes": 890000000,
        "file_count": 4210,
        "uploads": [
          { "remote": "gdrive", "path": "research-backups", "success": true }
        ]
      },
      "history": []
    }
  }
}
```

Fields:
- `schema_version` — for future migration
- `updated_at` — ISO 8601 UTC timestamp of last state write
- `sources` — keyed by source `name`
- `last_backup` — used for growth detection on the next run
- `history` — ordered list used for retention policy decisions
- `retention_bucket` — `daily`, `weekly`, `monthly`, or `yearly`
- `uploads` — per-destination upload results for the run

---

# Growth Detection

The system should compare backup size against previous successful backup.

Example configurable threshold:

```yaml
max_growth_ratio: 1.5
```

Meaning:
- if backup size exceeds 150% of previous size
- warning should trigger

Growth is tracked per source independently.

---

# Notifications

Notifications should ONLY appear for suspicious situations.

Normal successful backups should remain silent.

---

# Notification Backend

Use:

```bash
terminal-notifier
```

Example:

```bash
terminal-notifier \
  -title "Backup Warning" \
  -message "Backup size increased by 2.3x"
```

---

# Warning Conditions

Examples:
- excessive size growth
- upload failure
- archive creation failure
- suspiciously large files
- unexpectedly large included file count

All thresholds must be configurable.

---

# Logging

## Log Structure

```text
logs/
├── latest.log
└── history/
    ├── 2026-05-22.log
    ├── 2026-05-23.log
```

---

# Log Rotation

Retention should be configurable.

Example:

```yaml
log_retention_days: 90
```

---

# Retention Policy

Retention should support:
- daily
- weekly
- monthly
- yearly

Example:

```yaml
retention:
  daily: 7
  weekly: 8
  monthly: 12
  yearly: 5
```

---

# Retention Semantics

Keep:
- last 7 daily backups
- last 8 weekly backups
- last 12 monthly backups
- last 5 yearly backups

Older backups should be automatically removed from all destinations.

Retention is applied per source independently.

---

# Restore Philosophy

Restore must remain extremely simple.

Preferred restore flow:

```bash
tar -xzf backup.tar.gz
```

No proprietary formats.

No opaque repositories.

---

# Configuration File

## Multi-Source, Multi-Destination Example

```yaml
# Global default destinations — used by any source that does not
# specify its own destinations list.
destinations:
  - remote: gdrive
    path: workspace-backups

# Source directories to back up.
sources:
  - name: projects
    path: ~/projects
    # Inherits global destinations.

  - name: research
    path: ~/research
    # Overrides global destinations for this source only.
    destinations:
      - remote: gdrive
        path: research-backups
      - remote: s3-backup
        path: backups/research

  - name: notes
    path: ~/notes
    # Destinations can point to a completely different remote.
    destinations:
      - remote: dropbox
        path: /Backups/notes

max_growth_ratio: 1.5

notification:
  enabled: true
  notify_on_growth_warning: true
  notify_on_failure: true

retention:
  daily: 7
  weekly: 8
  monthly: 12
  yearly: 5

reports:
  show_largest_files: 20

logs:
  retention_days: 90
```

## Notes on Destination Resolution

- If a source has no `destinations` key, global `destinations` are used.
- If a source has an explicit `destinations` list, it completely replaces the
  global list for that source (no merging).
- At least one destination must resolve for each source, or the run aborts.
- All referenced remotes must exist in rclone configuration.

---

# Scheduling

## macOS Recommended Option

Use:

```text
launchd
```

instead of cron.

Reasons:
- native macOS scheduler
- reliable
- survives sleep better
- integrated with system

---

# Suggested Schedule

Example:
- once per day
- daytime execution

Example:
- 13:00 local time

---

# Security Considerations

## rclone Credentials

The system does not manage cloud credentials.

All authentication is handled by rclone.

The backup system only calls rclone with pre-configured remote names.

Credentials and tokens are managed entirely outside this system.

---

# Dependencies

## Python Dependencies

```text
pathspec
PyYAML
```

Optional:

```text
rich
humanize
```

---

# External Dependencies

```text
rclone
terminal-notifier
```

Installed via:

```bash
brew install rclone
brew install terminal-notifier
```

---

# Non-Goals

This project is NOT intended to:
- replace enterprise backup systems
- support block-level deduplication
- provide encrypted snapshot repositories
- support multi-user backup orchestration
- become a generic cloud sync platform
- manage rclone remote configuration

The focus is:
- source code
- ML projects
- research workflows
- transparent archives
- inspectable filtering
- lightweight operation
