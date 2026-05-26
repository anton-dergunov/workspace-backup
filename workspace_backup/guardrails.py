"""
Post-backup guardrails: check for unexpected growth, new file types, etc.
Runs as resticprofile run-after hook. Exit non-zero on guardrail violation.

Environment:
  RESTIC_REPOSITORY  — repo URL
  RESTIC_PASSWORD    — password (optional, set in env)
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from collections import defaultdict


def run_restic(args: list) -> str:
    """Run restic command and return JSON output."""
    cmd = ["restic"] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            check=True
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        print(f"ERROR: restic command failed: {' '.join(cmd)}", file=sys.stderr)
        print(f"  stdout: {e.stdout}", file=sys.stderr)
        print(f"  stderr: {e.stderr}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("ERROR: restic not found. Install with: brew install restic", file=sys.stderr)
        sys.exit(1)


def get_snapshots(limit: int = 2) -> list:
    """Get the latest N snapshots as JSON."""
    output = run_restic(["snapshots", f"--latest={limit}", "--json"])
    snapshots = json.loads(output)
    return sorted(snapshots, key=lambda s: s["time"])  # oldest first


def get_stats(snapshot_id: str) -> dict:
    """Get stats for a snapshot."""
    output = run_restic(["stats", snapshot_id, "--json"])
    return json.loads(output)


def get_files(snapshot_id: str) -> list:
    """Get file listing for a snapshot (with names and extensions only)."""
    output = run_restic(["ls", snapshot_id, "--json"])
    # restic ls --json outputs newline-delimited JSON objects, not an array
    files = []
    for line in output.strip().split("\n"):
        if line.strip():
            files.append(json.loads(line))
    return files


def extract_extensions(files: list) -> dict:
    """Extract unique extensions and their counts from file listing."""
    extensions = defaultdict(int)
    for entry in files:
        if entry.get("type") == "file" and entry.get("name"):
            name = entry["name"]
            if "." in name:
                ext = "." + name.split(".")[-1].lower()
                extensions[ext] += 1
    return dict(extensions)


def check_size_growth(config: dict, current_stats: dict, prev_stats: dict) -> list:
    """Check if backup grew too much since last snapshot."""
    warnings = []
    max_growth = config.get("max_growth_ratio", 1.5)

    if not prev_stats or "total_size" not in prev_stats:
        return warnings  # No previous snapshot to compare

    prev_size = prev_stats.get("total_size", 0)
    curr_size = current_stats.get("total_size", 0)

    if prev_size > 0:
        growth_ratio = curr_size / prev_size
        if growth_ratio > max_growth:
            warnings.append(
                f"SIZE_GROWTH: snapshot grew {growth_ratio:.2f}x "
                f"({prev_size / (1<<30):.1f} GB → {curr_size / (1<<30):.1f} GB), "
                f"threshold is {max_growth:.2f}x"
            )

    return warnings


def check_file_count_growth(config: dict, curr_files: list, prev_files: list) -> list:
    """Check if file count jumped unexpectedly."""
    warnings = []
    max_growth = config.get("max_file_count_growth_ratio", 2.0)

    prev_count = len(prev_files) if prev_files else 0
    curr_count = len(curr_files)

    if prev_count > 0:
        growth_ratio = curr_count / prev_count
        if growth_ratio > max_growth:
            warnings.append(
                f"FILE_COUNT_GROWTH: count jumped {growth_ratio:.2f}x "
                f"({prev_count:,} → {curr_count:,}), threshold is {max_growth:.2f}x"
            )

    return warnings


def check_new_extensions(config: dict, curr_exts: dict, prev_exts: dict) -> list:
    """Check for new file extensions that weren't in previous backup."""
    warnings = []

    if not config.get("warn_on_new_extensions", True):
        return warnings

    if not prev_exts:
        return warnings  # No previous snapshot to compare

    new_exts = set(curr_exts.keys()) - set(prev_exts.keys())

    if new_exts:
        ext_list = ", ".join(sorted(new_exts))
        warning = f"NEW_EXTENSIONS: found {len(new_exts)} new file types: {ext_list}\n"
        for ext in sorted(new_exts):
            warning += f"  {ext}: {curr_exts[ext]} files\n"
        warnings.append(warning.rstrip())

    return warnings


def notify(severity: str, message: str) -> None:
    """Send notification via terminal-notifier."""
    try:
        subprocess.run(
            ["terminal-notifier", "-title", f"Backup {severity}", "-message", message],
            check=False,
            timeout=5
        )
    except FileNotFoundError:
        print(f"Note: terminal-notifier not installed (brew install terminal-notifier)", file=sys.stderr)


def load_guardrail_config() -> dict:
    """Load guardrail thresholds from config file or env."""
    # Could load from config.yaml, but for now use defaults + env overrides
    return {
        "max_growth_ratio": float(os.getenv("GUARDRAIL_MAX_GROWTH_RATIO", "1.5")),
        "max_file_count_growth_ratio": float(os.getenv("GUARDRAIL_MAX_FILE_COUNT_GROWTH_RATIO", "2.0")),
        "warn_on_new_extensions": os.getenv("GUARDRAIL_WARN_ON_NEW_EXTENSIONS", "true").lower() == "true",
    }


def main():
    """Run guardrail checks on the latest backup."""
    print("=" * 70)
    print("Running post-backup guardrails...")
    print("=" * 70)

    config = load_guardrail_config()

    # Get latest two snapshots
    snapshots = get_snapshots(limit=2)

    if len(snapshots) < 1:
        print("ERROR: No snapshots found in repository", file=sys.stderr)
        sys.exit(1)

    current_snapshot = snapshots[-1]
    prev_snapshot = snapshots[-2] if len(snapshots) >= 2 else None

    print(f"\nCurrent snapshot: {current_snapshot['id'][:8]} ({current_snapshot['time']})")
    if prev_snapshot:
        print(f"Previous snapshot: {prev_snapshot['id'][:8]} ({prev_snapshot['time']})")
    else:
        print("No previous snapshot to compare")

    # Get stats and file listings
    curr_stats = get_stats(current_snapshot["id"])
    curr_files = get_files(current_snapshot["id"])
    curr_exts = extract_extensions(curr_files)

    prev_stats = get_stats(prev_snapshot["id"]) if prev_snapshot else {}
    prev_files = get_files(prev_snapshot["id"]) if prev_snapshot else []
    prev_exts = extract_extensions(prev_files)

    # Run checks
    all_warnings = []

    print(f"\nSnapshot sizes:")
    print(f"  Current:  {curr_stats.get('total_size', 0) / (1<<30):.2f} GB ({curr_stats.get('total_file_count', 0):,} files)")
    if prev_stats:
        print(f"  Previous: {prev_stats.get('total_size', 0) / (1<<30):.2f} GB ({prev_stats.get('total_file_count', 0):,} files)")

    all_warnings.extend(check_size_growth(config, curr_stats, prev_stats))
    all_warnings.extend(check_file_count_growth(config, curr_files, prev_files))
    all_warnings.extend(check_new_extensions(config, curr_exts, prev_exts))

    # Report results
    if all_warnings:
        print("\n" + "=" * 70)
        print("⚠️  GUARDRAIL VIOLATIONS DETECTED")
        print("=" * 70)
        for w in all_warnings:
            print(f"\n{w}")
        print()

        # Send notification
        message = f"{len(all_warnings)} guardrail violation(s) detected after backup"
        notify("WARNING", message)

        # Exit non-zero to mark job as failed
        sys.exit(1)
    else:
        print("\n✓ All guardrails passed")
        notify("Success", "Backup completed successfully")
        sys.exit(0)


if __name__ == "__main__":
    main()
