"""
Post-backup guardrails: check for unexpected growth, new file types, etc.
Runs as resticprofile run-after hook. Exit non-zero on guardrail violation.

Environment (set automatically by resticprofile):
  PROFILE_NAME  — resticprofile profile name

Usage in profiles.yaml:
  run-after:
    - "python3 /path/to/guardrails.py --max-growth-ratio 1.2 --max-new-files 100"
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

# Set at startup from CLI args / env
_PROFILE_NAME: str = ""
_RESTICPROFILE_CONFIG: str = ""


# ── Size parsing ──────────────────────────────────────────────────────────────

def _parse_size_to_bytes(s: str) -> int:
    """Parse size strings like '10MB', '5GiB', '1.5 KiB' to bytes."""
    units = {
        "b": 1,
        "kb": 1024, "kib": 1024,
        "mb": 1024**2, "mib": 1024**2,
        "gb": 1024**3, "gib": 1024**3,
        "tb": 1024**4, "tib": 1024**4,
    }
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]+)", s.strip())
    if not m:
        raise ValueError(f"Invalid size: {s!r}. Examples: 10MB, 5GiB, 500KB")
    value, unit = float(m.group(1)), m.group(2).lower()
    factor = units.get(unit)
    if factor is None:
        raise ValueError(f"Unknown size unit {unit!r} in {s!r}")
    return int(value * factor)


def _argparse_size(s: str) -> int:
    """Argparse type wrapper for _parse_size_to_bytes."""
    try:
        return _parse_size_to_bytes(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


# ── resticprofile runner ──────────────────────────────────────────────────────

def run_resticprofile(args: list) -> str:
    """Run resticprofile command and return stdout."""
    cmd = ["resticprofile"]
    if _RESTICPROFILE_CONFIG:
        cmd += ["--config", _RESTICPROFILE_CONFIG]
    cmd += ["--name", _PROFILE_NAME] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        print(f"ERROR: resticprofile command failed: {' '.join(cmd)}", file=sys.stderr)
        print(f"  stdout: {e.stdout}", file=sys.stderr)
        print(f"  stderr: {e.stderr}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("ERROR: resticprofile not found. Install with: brew install resticprofile", file=sys.stderr)
        sys.exit(1)


# ── Data retrieval ────────────────────────────────────────────────────────────

def _extract_json(output: str, opening: str):
    """
    Extract the first complete JSON value (array or object) from output that
    may contain resticprofile log lines before and after the JSON.
    Uses raw_decode to stop at the end of the first complete value.
    """
    start = output.find(opening)
    if start == -1:
        return None
    value, _ = json.JSONDecoder().raw_decode(output, start)
    return value


def get_snapshots(limit: int = 2) -> list:
    """Get the latest N snapshots as JSON."""
    output = run_resticprofile(["snapshots", f"--latest={limit}", "--json"])
    snapshots = _extract_json(output, "[")
    if snapshots is None:
        return []
    return sorted(snapshots, key=lambda s: s["time"])


def get_stats(snapshot_id: str) -> dict:
    """Get stats for a snapshot."""
    output = run_resticprofile(["stats", snapshot_id, "--json"])
    result = _extract_json(output, "{")
    return result if result is not None else {}


def get_files(snapshot_id: str) -> list:
    """Get file listing for a snapshot (newline-delimited JSON objects)."""
    output = run_resticprofile(["ls", snapshot_id, "--json"])
    files = []
    for line in output.strip().split("\n"):
        line = line.strip()
        if line.startswith("{"):
            try:
                files.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return files


def get_diff(snap1_id: str, snap2_id: str) -> dict:
    """
    Get diff stats between two snapshots.
    Returns dict with new_files (int) and added_bytes (int).
    """
    output = run_resticprofile(["diff", snap1_id, snap2_id])
    result = {"new_files": 0, "added_bytes": 0}
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("Files:"):
            m = re.search(r"(\d+)\s+new", line)
            if m:
                result["new_files"] = int(m.group(1))
        elif line.startswith("Added:"):
            m = re.match(r"Added:\s+(.+)$", line)
            if m:
                try:
                    result["added_bytes"] = _parse_size_to_bytes(m.group(1).strip())
                except ValueError:
                    pass
    return result


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


# ── Guardrail checks ──────────────────────────────────────────────────────────

def check_size_growth(config: dict, curr_stats: dict, prev_stats: dict) -> list:
    """Warn if snapshot grew more than max_growth_ratio vs previous."""
    warnings = []
    if not prev_stats or "total_size" not in prev_stats:
        return warnings
    prev_size = prev_stats.get("total_size", 0)
    curr_size = curr_stats.get("total_size", 0)
    if prev_size > 0:
        ratio = curr_size / prev_size
        if ratio > config["max_growth_ratio"]:
            warnings.append(
                f"SIZE_GROWTH: snapshot grew {ratio:.2f}x "
                f"({prev_size / (1<<30):.2f} GB → {curr_size / (1<<30):.2f} GB), "
                f"threshold is {config['max_growth_ratio']:.2f}x"
            )
    return warnings


def check_file_count_growth(config: dict, curr_files: list, prev_files: list) -> list:
    """Warn if file count grew more than max_file_count_growth_ratio vs previous."""
    warnings = []
    prev_count = len(prev_files) if prev_files else 0
    curr_count = len(curr_files)
    if prev_count > 0:
        ratio = curr_count / prev_count
        if ratio > config["max_file_count_growth_ratio"]:
            warnings.append(
                f"FILE_COUNT_GROWTH: count jumped {ratio:.2f}x "
                f"({prev_count:,} → {curr_count:,}), "
                f"threshold is {config['max_file_count_growth_ratio']:.2f}x"
            )
    return warnings


def check_new_extensions(config: dict, curr_exts: dict, prev_exts: dict) -> list:
    """Warn if new file extensions appeared that weren't in the previous backup."""
    warnings = []
    if not config.get("warn_on_new_extensions", True):
        return warnings
    if not prev_exts:
        return warnings
    new_exts = set(curr_exts.keys()) - set(prev_exts.keys())
    if new_exts:
        ext_list = ", ".join(sorted(new_exts))
        warning = f"NEW_EXTENSIONS: found {len(new_exts)} new file type(s): {ext_list}\n"
        for ext in sorted(new_exts):
            warning += f"  {ext}: {curr_exts[ext]} file(s)\n"
        warnings.append(warning.rstrip())
    return warnings


def check_new_files_absolute(config: dict, diff_stats: dict) -> list:
    """Warn if too many new files were added in this backup run."""
    warnings = []
    new_count = diff_stats.get("new_files", 0)
    if new_count > config["max_new_files"]:
        warnings.append(
            f"NEW_FILES: {new_count:,} new files added, "
            f"threshold is {config['max_new_files']:,}"
        )
    return warnings


def check_added_size(config: dict, diff_stats: dict) -> list:
    """Warn if too much data (new + changed) was added in this backup run."""
    warnings = []
    added = diff_stats.get("added_bytes", 0)
    max_bytes = config["max_added_size"]
    if added > max_bytes:
        warnings.append(
            f"ADDED_SIZE: {added / (1<<20):.1f} MiB added in this run, "
            f"threshold is {max_bytes / (1<<20):.1f} MiB"
        )
    return warnings


def check_total_size(config: dict, curr_stats: dict) -> list:
    """Warn if total snapshot size exceeds absolute limit."""
    warnings = []
    total = curr_stats.get("total_size", 0)
    max_bytes = config["max_total_size"]
    if total > max_bytes:
        warnings.append(
            f"TOTAL_SIZE: snapshot is {total / (1<<30):.2f} GB, "
            f"threshold is {max_bytes / (1<<30):.2f} GB"
        )
    return warnings


# ── Notification ──────────────────────────────────────────────────────────────

def notify(severity: str, message: str) -> None:
    """Send desktop notification via terminal-notifier (optional)."""
    try:
        subprocess.run(
            ["terminal-notifier", "-title", f"Backup {severity}", "-message", message],
            check=False,
            timeout=5,
        )
    except FileNotFoundError:
        pass  # terminal-notifier is optional


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Post-backup guardrails. Run as a resticprofile run-after hook.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--max-growth-ratio", type=float, default=1.2, metavar="RATIO",
        help="Max size growth ratio vs previous snapshot",
    )
    parser.add_argument(
        "--max-file-count-growth-ratio", type=float, default=1.2, metavar="RATIO",
        help="Max file count growth ratio vs previous snapshot",
    )
    parser.add_argument(
        "--max-new-files", type=int, default=100, metavar="N",
        help="Max number of new files added per backup run",
    )
    parser.add_argument(
        "--max-added-size", type=_argparse_size,
        default=_parse_size_to_bytes("10MB"), metavar="SIZE",
        help="Max total data added (new+changed) per run, e.g. 10MB, 1GiB",
    )
    parser.add_argument(
        "--max-total-size", type=_argparse_size,
        default=_parse_size_to_bytes("5GB"), metavar="SIZE",
        help="Max absolute total snapshot size, e.g. 5GB, 500MiB",
    )
    parser.add_argument(
        "--no-warn-on-new-extensions", action="store_true",
        help="Disable warnings for new file extensions",
    )
    parser.add_argument(
        "--resticprofile-config", default="", metavar="PATH",
        help="Path to resticprofile config file (uses resticprofile default if omitted)",
    )
    return parser.parse_args()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    global _PROFILE_NAME, _RESTICPROFILE_CONFIG

    args = parse_args()

    _PROFILE_NAME = os.environ.get("PROFILE_NAME", "")
    if not _PROFILE_NAME:
        print("ERROR: PROFILE_NAME environment variable not set.", file=sys.stderr)
        print("  Run this script as a resticprofile run-after hook.", file=sys.stderr)
        sys.exit(1)

    _RESTICPROFILE_CONFIG = args.resticprofile_config

    config = {
        "max_growth_ratio":            args.max_growth_ratio,
        "max_file_count_growth_ratio": args.max_file_count_growth_ratio,
        "max_new_files":               args.max_new_files,
        "max_added_size":              args.max_added_size,
        "max_total_size":              args.max_total_size,
        "warn_on_new_extensions":      not args.no_warn_on_new_extensions,
    }

    print("=" * 70)
    print("Running post-backup guardrails...")
    print("=" * 70)

    snapshots = get_snapshots(limit=2)
    if len(snapshots) < 1:
        print("ERROR: No snapshots found in repository", file=sys.stderr)
        sys.exit(1)

    current_snapshot = snapshots[-1]
    prev_snapshot = snapshots[-2] if len(snapshots) >= 2 else None

    print(f"\nCurrent snapshot:  {current_snapshot['id'][:8]} ({current_snapshot['time']})")
    if prev_snapshot:
        print(f"Previous snapshot: {prev_snapshot['id'][:8]} ({prev_snapshot['time']})")
    else:
        print("No previous snapshot to compare")

    curr_stats = get_stats(current_snapshot["id"])
    curr_files = get_files(current_snapshot["id"])
    curr_exts  = extract_extensions(curr_files)

    prev_stats = get_stats(prev_snapshot["id"]) if prev_snapshot else {}
    prev_files = get_files(prev_snapshot["id"]) if prev_snapshot else []
    prev_exts  = extract_extensions(prev_files)

    all_warnings = []

    print(f"\nSnapshot sizes:")
    print(f"  Current:  {curr_stats.get('total_size', 0) / (1<<30):.3f} GB  "
          f"({curr_stats.get('total_file_count', 0):,} files)")
    if prev_stats:
        print(f"  Previous: {prev_stats.get('total_size', 0) / (1<<30):.3f} GB  "
              f"({prev_stats.get('total_file_count', 0):,} files)")

    all_warnings.extend(check_size_growth(config, curr_stats, prev_stats))
    all_warnings.extend(check_file_count_growth(config, curr_files, prev_files))
    all_warnings.extend(check_new_extensions(config, curr_exts, prev_exts))
    all_warnings.extend(check_total_size(config, curr_stats))

    if prev_snapshot:
        diff_stats = get_diff(prev_snapshot["id"], current_snapshot["id"])
        print(f"  Added this run: {diff_stats['added_bytes'] / (1<<20):.2f} MiB  "
              f"({diff_stats['new_files']:,} new files)")
        all_warnings.extend(check_new_files_absolute(config, diff_stats))
        all_warnings.extend(check_added_size(config, diff_stats))

    if all_warnings:
        print("\n" + "=" * 70)
        print("GUARDRAIL VIOLATIONS DETECTED")
        print("=" * 70)
        for w in all_warnings:
            print(f"\n{w}")
        print()
        notify("WARNING", f"{len(all_warnings)} guardrail violation(s) detected after backup")
        sys.exit(1)
    else:
        print("\n✓ All guardrails passed")
        notify("Success", "Backup completed successfully")
        sys.exit(0)


if __name__ == "__main__":
    main()
