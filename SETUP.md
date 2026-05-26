# Workspace Backup with Restic + ResticProfile + Rclone

Simple encrypted workstation backups to cloud storage using:

- restic
- resticprofile
- rclone

Tested on macOS.

---

# Why this setup?

This setup provides:

- Encrypted backups
- Deduplicated incremental snapshots
- Cloud storage support
- Scheduled automatic backups
- Include/exclude rules
- Simple restore workflow
- Human-readable configuration
- Works well for laptops and developer workstations

`resticprofile` acts as a wrapper around `restic` and makes it much easier to:

- store backup configuration
- schedule jobs
- define backup profiles
- manage retention policies
- run hooks
- inspect snapshots

---

# Install

Install required tools using Homebrew:

```bash
brew install restic
brew install resticprofile
brew install rclone
```

Optional (required for `restic mount` on macOS):

```bash
brew install --cask macfuse
```

---

# Configure Rclone

Configure a remote cloud provider:

```bash
rclone config
```

Example:
- Google Drive
- Dropbox
- S3
- OneDrive
- etc.

This guide assumes a Google Drive remote named:

```text
gdrive
```

Test it:

```bash
rclone lsd gdrive:
```

---

# Create Restic Password File

Create a password file:

```bash
mkdir -p ~/.config/restic
```

```bash
openssl rand -base64 32 > ~/.config/restic/password
```

Restrict permissions:

```bash
chmod 600 ~/.config/restic/password
```

---

# ResticProfile Configuration

Create config directory:

```bash
mkdir -p ~/.config/resticprofile
```

Copy:

```text
config/profiles.yaml.sample
```

to:

```text
~/.config/resticprofile/profiles.yaml
```

You can also keep the config inside the repository and create a symlink:

```bash
ln -s /path/to/repo/config/profiles.yaml \
      ~/.config/resticprofile/profiles.yaml
```

This makes the config easier to version control.

---

# Example Configuration

```yaml
version: "1"

default:
  repository: "rclone:gdrive:restic-backup-{{ .Hostname }}"
  password-file: "~/.config/restic/password"

  backup:
    source:
      - "~/my_dir1"
      - "~/my_dir2"

    tags:
      - workstation

    exclude-file:
      - "/path/to/repo/config/excludes.txt"
    exclude-if-present: ".nobackup"
    skip-if-unchanged: true

    schedule: hourly
    schedule-log: "/Users/YOUR_USERNAME/resticprofile-backup.log"
```

---

# Notes About This Configuration

## Repository Naming

```yaml
repository: "rclone:gdrive:restic-backup-{{ .Hostname }}"
```

`{{ .Hostname }}` automatically inserts the machine hostname.

This allows multiple machines to use the same cloud storage while keeping separate repositories.

Example:

```text
restic-backup-macbook
restic-backup-workstation
restic-backup-server
```

---

## Exclude Rules

Example:

```yaml
exclude-file:
  - "/path/to/repo/config/excludes.txt"
```

Exclude file syntax follows standard restic exclude rules.

Useful for:

- `.git`
- `node_modules`
- caches
- temporary files
- build artifacts

See included file [config/excludes.txt](config/excludes.txt) for an example.

TODO Mention usage of the preview script for curating this fil.

Reference:

https://restic.readthedocs.io/en/latest/040_backup.html

---

## `.nobackup`

```yaml
exclude-if-present: ".nobackup"
```

Any directory containing:

```text
.nobackup
```

will be skipped entirely.

Very useful for:
- temporary directories
- datasets
- experiments
- caches

---

## Scheduling

```yaml
schedule: hourly
```

Creates an hourly scheduled backup using:
- `launchd` on macOS
- `systemd` on Linux

---

## Schedule Logs

```yaml
schedule-log: "/Users/YOUR_USERNAME/resticprofile-backup.log"
```

Scheduled backup output is appended to this file.

Monitor live:

```bash
tail -f ~/resticprofile-backup.log
```

---

# Initialize Repository

Initialize the repository once:

```bash
resticprofile init
```

This creates the encrypted restic repository in the configured cloud storage.

---

# First Backup

Run the first backup:

```bash
resticprofile backup
```

The first backup may take a long time because all files are uploaded.

Subsequent backups are incremental and deduplicated.

---

# Dry Run

Preview which files would be backed up:

```bash
resticprofile --dry-run backup
```

Save output for analysis:

```bash
resticprofile --dry-run backup | tee dryrun.txt
```

---

# Schedule Automatic Backups

Install scheduled jobs:

```bash
resticprofile schedule
```

Check schedule status:

```bash
resticprofile status
```

Example output:

```text
Profile (or Group) default: backup schedule
===========================================
  Original form: hourly
Normalized form: *-*-* *:00:00
    Next elapse: Tue May 26 23:00:00 BST 2026

            service: local.resticprofile.default.backup
              state: not running
```

View launchd jobs:

```bash
launchctl list | grep restic
```

---

# Remove Scheduled Jobs

Disable scheduling:

```bash
resticprofile unschedule
```

Verify:

```bash
resticprofile status
```

---

# Useful Commands

## Backup

```bash
resticprofile backup
```

---

## Show Snapshots

```bash
resticprofile snapshots
```

---

## Repository Statistics

```bash
resticprofile stats
```

---

## List Files in Latest Snapshot

```bash
resticprofile ls latest
```

---

## Check Repository Integrity

```bash
resticprofile check
```

---

## Mount Repository (Optional)

Requires macFUSE on macOS.

Create mount point:

```bash
mkdir -p /tmp/restic
```

Mount:

```bash
resticprofile mount /tmp/restic
```

Unmount:

```bash
umount /tmp/restic
```

---

# Cleanup Old Restic Caches

Sometimes restic leaves old cache directories:

```bash
restic cache --cleanup
```

---

# Using Restic Directly

You can also interact with the repository using plain `restic`.

Example:

```bash
restic \
  -r rclone:gdrive:restic-backup-$(hostname) \
  --password-file ~/.config/restic/password \
  snapshots
```

However, `resticprofile` is usually more convenient because:
- repository location
- password file
- schedules
- hooks
- backup parameters

are already stored in the profile.

---

# Updating Configuration

Most configuration changes are picked up automatically on the next run.

If you change scheduling settings, re-run:

```bash
resticprofile schedule
```

to regenerate scheduled jobs.

---

# Restore

Restore the latest snapshot:

```bash
resticprofile restore latest --target /tmp/restore
```

Restore specific snapshot:

```bash
resticprofile restore <snapshot-id> --target /tmp/restore
```

---

# Security Notes

- Keep the password file safe
- Without the password, backups cannot be restored
- Restic encrypts all data before upload
- Cloud providers cannot read backup contents

---

# References

- Restic:
  https://restic.net/

- Restic documentation:
  https://restic.readthedocs.io/

- ResticProfile:
  https://creativeprojects.github.io/resticprofile/

- Rclone:
  https://rclone.org/
