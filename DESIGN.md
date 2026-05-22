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

---

### 3. Google Drive

Primary backup destination.

Configured via rclone remote.

Example remote name:

```text
gdrive
```

The remote name must be configurable.

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
├── logs/
│   ├── latest.log
│   └── history/
└── tests/
```

---

# Source Directory

Example:

```text
~/projects
```

The source directory must be configurable.

The backup should recursively process the entire tree.

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

Example:

```text
projects-backup-2026-05-22_18-30-00.tar.gz
```

Where projects is the name of the source dir
specified in config.

---

# Upload Mechanism

Use rclone.

Example:

```bash
rclone copy archive.tar.gz gdrive:project-backups
```

---

# Backup Metadata

The system should maintain metadata locally.

Example:

```text
state.json
```

Used for:
- previous backup size
- historical statistics
- retention tracking

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

Older backups should be automatically removed.

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

Example:

```yaml
source_directory: ~/projects

rclone_remote: gdrive
rclone_destination: project-backups

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

## Google Drive Scope

Use:
- Google Drive API only

Do NOT enable:
- Gmail API
- Photos API

The backup system should not gain access to email.

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

The focus is:
- source code
- ML projects
- research workflows
- transparent archives
- inspectable filtering
- lightweight operation
