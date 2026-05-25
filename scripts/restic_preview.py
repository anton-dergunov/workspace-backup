#!/usr/bin/env python3
"""
Preview which files restic would include or exclude from a backup.

Applies gitignore-style patterns from an exclude file (same semantics as
restic's --exclude-file). Does NOT need a configured restic repository.

Usage:
    restic_preview.py <source> [options]

Options:
    --exclude-file FILE      Exclude file (default: ~/.config/restic/excludes)
    --show included|excluded|both
    --min-size SIZE          e.g. 1MB, 500KB, 1024  (default: 0, show all)
    --summarize NAME         Directory name to summarize instead of listing in
                             detail (repeatable). E.g. --summarize .git
    --nobackup-file NAME     Exclude dirs containing this marker file
                             (default: .nobackup; mirrors restic's
                             --exclude-if-present). Pass "" to disable.
    --output FILE            Write report to FILE (.txt or .html); omit for console

Requires: pip install pathspec
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime
from html import escape as _esc
from pathlib import Path
from collections import defaultdict

try:
    import pathspec
except ImportError:
    sys.exit("Install pathspec:  pip install pathspec")


DEFAULT_EXCLUDE = Path("~/.config/restic/excludes").expanduser()

# ── File-type registry ────────────────────────────────────────────────────────
# Each row: (category, extensions, ansi_code, emoji, html_color)
_CAT_DEFS = [
    ("python",   [".py", ".pyx", ".pyi", ".pyw"],                  "\033[34m",  "🐍", "#3572A5"),
    ("js",       [".js", ".jsx", ".mjs", ".cjs"],                   "\033[33m",  "📜", "#C7A200"),
    ("ts",       [".ts", ".tsx"],                                    "\033[36m",  "📘", "#2B7489"),
    ("html",     [".html", ".htm", ".xhtml"],                        "\033[91m",  "🌐", "#E44D26"),
    ("css",      [".css", ".scss", ".sass", ".less"],                "\033[35m",  "🎨", "#563D7C"),
    ("json",     [".json", ".jsonc", ".json5"],                      "\033[32m",  "📋", "#4CAF50"),
    ("yaml",     [".yaml", ".yml"],                                  "\033[32m",  "📋", "#7B9F35"),
    ("toml",     [".toml"],                                          "\033[32m",  "📋", "#9C4221"),
    ("xml",      [".xml", ".xsd", ".xsl", ".svg"],                  "\033[32m",  "📋", "#4CAF50"),
    ("csv",      [".csv", ".tsv"],                                   "\033[92m",  "📊", "#43A047"),
    ("markdown", [".md", ".mdx", ".markdown"],                       "\033[37m",  "📝", "#555"),
    ("text",     [".txt", ".log"],                                   "\033[90m",  "📄", "#888"),
    ("pdf",      [".pdf"],                                           "\033[31m",  "📕", "#D32F2F"),
    ("image",    [".png", ".jpg", ".jpeg", ".gif", ".bmp",
                  ".webp", ".ico", ".tiff"],                         "\033[95m",  "🖼", "#9C27B0"),
    ("shell",    [".sh", ".bash", ".zsh", ".fish", ".ps1"],         "\033[93m",  "💻", "#89A050"),
    ("rust",     [".rs"],                                            "\033[31m",  "🦀", "#DEA584"),
    ("go",       [".go"],                                            "\033[96m",  "🐹", "#00ACD7"),
    ("c",        [".c", ".h"],                                       "\033[94m",  "⚡", "#555599"),
    ("cpp",      [".cpp", ".cc", ".cxx", ".hpp", ".hxx"],           "\033[94m",  "⚡", "#F34B7D"),
    ("csharp",   [".cs"],                                            "\033[34m",  "🔷", "#178600"),
    ("jvm",      [".java", ".kt", ".kts", ".scala", ".groovy"],     "\033[33m",  "☕", "#B07219"),
    ("ruby",     [".rb", ".rake", ".gemspec"],                       "\033[31m",  "💎", "#CC342D"),
    ("php",      [".php"],                                           "\033[35m",  "🐘", "#777BB4"),
    ("swift",    [".swift"],                                         "\033[91m",  "🍎", "#F05138"),
    ("r",        [".r", ".R"],                                       "\033[34m",  "📈", "#276DC3"),
    ("lua",      [".lua"],                                           "\033[34m",  "🌙", "#000080"),
    ("notebook", [".ipynb"],                                         "\033[93m",  "📓", "#DA5B0B"),
    ("archive",  [".zip", ".tar", ".gz", ".bz2", ".xz",
                  ".7z", ".rar", ".whl", ".egg"],                    "\033[90m",  "📦", "#888"),
    ("lock",     [".lock"],                                          "\033[90m",  "🔒", "#AAA"),
    ("binary",   [".so", ".dylib", ".dll", ".exe",
                  ".bin", ".o", ".a", ".wasm"],                      "\033[90m",  "⚙", "#888"),
]

def _build_maps():
    ext_cat: dict = {}
    ansi: dict = {None: ""}
    icon: dict = {None: "·"}
    color: dict = {None: "#999"}
    for cat, exts, a, i, c in _CAT_DEFS:
        for ext in exts:
            ext_cat[ext] = cat
        ansi[cat] = a
        icon[cat] = i
        color[cat] = c
    return ext_cat, ansi, icon, color

EXT_CATEGORY, _ANSI, _ICON, _HTML_COLOR = _build_maps()
_RESET = "\033[0m"


def get_category(path: Path):
    return EXT_CATEGORY.get(path.suffix.lower())


# ── Formatting helpers ────────────────────────────────────────────────────────

def fmt_size(n: int) -> str:
    for unit, threshold in [("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)]:
        if n >= threshold:
            return f"{n / threshold:.1f} {unit}"
    return f"{n}  B"  # extra space so "B" aligns with 2-char units (KB/MB/GB)


def parse_size(s: str) -> int:
    s = s.strip().upper().replace(" ", "")
    for suffix, mult in [("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10),
                         ("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10), ("B", 1)]:
        if s.endswith(suffix):
            return int(float(s[: -len(suffix)]) * mult)
    return int(s)


# ── Core logic ────────────────────────────────────────────────────────────────

def _dir_size(path: Path) -> int:
    """Total size of a directory tree via 'du -sk' (fast) or os.walk fallback."""
    try:
        out = subprocess.check_output(
            ["du", "-sk", str(path)], stderr=subprocess.DEVNULL, timeout=60
        )
        return int(out.decode().split()[0]) * 1024  # du -sk gives 1 KB blocks
    except Exception:
        total = 0
        for root, _, files in os.walk(path):
            for f in files:
                try:
                    total += (Path(root) / f).stat().st_size
                except OSError:
                    pass
        return total


def compute_dir_totals(files: list) -> dict:
    """
    Recursive byte total for directories that appear as direct parents in `files`.

    Bottom-up: O(F) to sum direct sizes, O(D log D + D*depth) to propagate.
    Each listed directory contributes one addition to its nearest listed ancestor.
    """
    direct: dict = defaultdict(int)
    for path, size in files:
        direct[path.parent] += size

    totals = dict(direct)
    for d in sorted(totals, key=lambda p: len(p.parts), reverse=True):
        for anc in d.parents:
            if anc in totals:
                totals[anc] += totals[d]
                break

    return totals


def _load_exclude(path: Path):
    """
    Parse the exclude file.

    Returns (spec, pat_specs) where pat_specs is a list of
    (pattern_string, single-pattern PathSpec) used to attribute which rule
    caused each exclusion.
    """
    if not path.exists():
        print(f"warning: exclude file not found: {path}", file=sys.stderr)
        return pathspec.PathSpec.from_lines("gitignore", []), []
    raw = [l for l in path.read_text().splitlines()
           if l.strip() and not l.startswith("#")]
    # Restic ignores trailing / in patterns (a trailing / is simply stripped).
    # pathspec/gitignore treats trailing / as "match directories only", which
    # would cause .env/ to miss a file named .env. Strip here to match restic.
    # See: https://restic.readthedocs.io/en/latest/040_backup.html#excluding-files
    raw = [l.rstrip("/") or l for l in raw]
    spec = pathspec.PathSpec.from_lines("gitignore", raw)
    pat_specs = [(p, pathspec.PathSpec.from_lines("gitignore", [p])) for p in raw]
    return spec, pat_specs


def collect(source: Path, spec: pathspec.PathSpec, pat_specs: list,
            summarize_names: set, nobackup_file: str = ".nobackup"):
    """
    Walk source and classify every entry.

    Returns:
      included      — [(Path, size)]        files to back up
      exc_dirs      — [(Path, size, rule)]  directories pruned entirely
      exc_files     — [(Path, size, rule)]  individual files matched by a pattern
      summarized    — [(Path, size)]        dirs collapsed to totals (--summarize)
      nobackup_dirs — [(Path, size)]        dirs excluded via marker file
    """
    included, exc_dirs, exc_files, summarized, nobackup_dirs = [], [], [], [], []
    skipped_symlinks = 0

    def find_rule(rel: str, is_dir: bool = False) -> str:
        for pat, pspec in pat_specs:
            if pspec.match_file(rel) or (is_dir and pspec.match_file(rel + "/")):
                return pat
        return "(unknown)"

    def walk(dirpath: Path):
        nonlocal skipped_symlinks
        try:
            entries = sorted(dirpath.iterdir(), key=lambda p: p.name.lower())
        except PermissionError:
            return
        for entry in entries:
            if entry.is_symlink():
                skipped_symlinks += 1
                continue
            rel = str(entry.relative_to(source))
            if entry.is_dir():
                if nobackup_file and (entry / nobackup_file).exists():
                    nobackup_dirs.append((entry, _dir_size(entry)))
                elif entry.name in summarize_names:
                    summarized.append((entry, _dir_size(entry)))
                elif spec.match_file(rel) or spec.match_file(rel + "/"):
                    exc_dirs.append((entry, _dir_size(entry), find_rule(rel, is_dir=True)))
                else:
                    walk(entry)
            elif entry.is_file():
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                if spec.match_file(rel):
                    exc_files.append((entry, size, find_rule(rel)))
                else:
                    included.append((entry, size))

    walk(source)
    if skipped_symlinks:
        print(f"note: skipped {skipped_symlinks} symlinks (restic backs them up as-is)",
              file=sys.stderr)
    return included, exc_dirs, exc_files, summarized, nobackup_dirs


# ── Text / console output ─────────────────────────────────────────────────────

def _write_file_section(out, files: list, source: Path, title: str,
                        min_size: int, use_color: bool, summarized=()):
    p = lambda s="": print(s, file=out)
    n_sum = len(summarized)
    suffix = f" + {n_sum} summarized" if n_sum else ""
    sep = "=" * 64
    p(f"\n{sep}")
    p(f"  {title}  ({len(files):,} files{suffix})")
    p(f"{sep}\n")

    if not files and not summarized:
        p("  (none)\n")
        return

    if files:
        dir_totals = compute_dir_totals(files)
        by_dir: dict = defaultdict(list)
        for path, size in files:
            by_dir[path.parent].append((path, size))

        for dirpath in sorted(by_dir):
            dir_total = dir_totals.get(dirpath, 0)
            if min_size and dir_total < min_size:
                continue

            rel = dirpath.relative_to(source)
            label = "./" if str(rel) == "." else f"{rel}/"
            p(f"{label:<55s}[{fmt_size(dir_total):>10s}]")

            shown = 0
            for path, size in sorted(by_dir[dirpath], key=lambda x: x[0].name.lower()):
                if min_size and size < min_size:
                    continue
                name = path.name
                if use_color and (cat := get_category(path)) and (ac := _ANSI.get(cat, "")):
                    pad = " " * max(0, 52 - len(name))
                    name_field = f"{ac}{name}{_RESET}{pad}"
                else:
                    name_field = f"{name:<52s}"
                p(f"  {name_field}  {fmt_size(size):>10s}")
                shown += 1

            if shown == 0 and min_size:
                n = len(by_dir[dirpath])
                p(f"  ({n} file{'s' if n != 1 else ''} below {fmt_size(min_size)})")
            p()

    if summarized:
        p("  --- Summarized (not listed in detail, excluded from stats) ---")
        for path, size in sorted(summarized):
            rel = str(path.relative_to(source)) + "/"
            p(f"  {rel:<53s}[{fmt_size(size):>10s}]")
        p()


def _write_stats(out, files: list, title: str, summarized=()):
    if not files:
        return
    p = lambda s="": print(s, file=out)
    by_ext: dict = defaultdict(lambda: [0, 0])
    total_size = 0
    for path, size in files:
        ext = path.suffix.lower() or "(no ext)"
        by_ext[ext][0] += 1
        by_ext[ext][1] += size
        total_size += size
    dirs = {path.parent for path, _ in files}
    sum_size = sum(s for _, s in summarized)

    p(f"--- {title} summary ---")
    p(f"  Files:       {len(files):,}")
    p(f"  Directories: {len(dirs):,}")
    p(f"  Total size:  {fmt_size(total_size)}")
    if summarized:
        p(f"  Summarized:  {fmt_size(sum_size)}  ({len(summarized)} dirs, not in stats above)")
        p(f"  Grand total: {fmt_size(total_size + sum_size)}")
    p(f"\n  {'Extension':<24} {'Count':>6}   {'Size':>12}")
    p(f"  {'-' * 46}")
    for ext, (count, size) in sorted(by_ext.items(), key=lambda x: -x[1][1]):
        p(f"  {ext:<24} {count:>6}   {fmt_size(size):>12}")
    p()


def _write_excluded_by_rule(out, exc_dirs: list, exc_files: list, source: Path):
    """Print excluded dirs and files together, grouped by the matching rule."""
    if not exc_dirs and not exc_files:
        return
    p = lambda s="": print(s, file=out)

    # Build unified list: (rel_label, size, rule)
    all_items = []
    for path, size, rule in exc_dirs:
        all_items.append((str(path.relative_to(source)) + "/", size, rule))
    for path, size, rule in exc_files:
        all_items.append((str(path.relative_to(source)), size, rule))

    by_rule: dict = defaultdict(list)
    for label, size, rule in all_items:
        by_rule[rule].append((label, size))

    rule_totals = {r: sum(s for _, s in items) for r, items in by_rule.items()}
    grand_total = sum(rule_totals.values())

    sep = "=" * 64
    p(f"\n{sep}")
    p(f"  EXCLUDED BY RULE  "
      f"({len(exc_dirs)} dirs, {len(exc_files)} files, {fmt_size(grand_total)} total)")
    p(f"{sep}\n")

    for rule in sorted(by_rule, key=lambda r: -rule_totals[r]):
        total = rule_totals[rule]
        p(f"  Rule: {rule:<51s}[{fmt_size(total):>10s}]")
        for label, size in sorted(by_rule[rule], key=lambda x: -x[1]):
            p(f"    {label:<55s}  {fmt_size(size):>10s}")
        p()


def _write_nobackup_dirs(out, nobackup_dirs: list, source: Path, nobackup_file: str):
    if not nobackup_dirs:
        return
    p = lambda s="": print(s, file=out)
    total = sum(s for _, s in nobackup_dirs)
    sep = "=" * 64
    p(f"\n{sep}")
    p(f"  EXCLUDED BY {nobackup_file}  ({len(nobackup_dirs)} dirs, {fmt_size(total)} total)")
    p(f"{sep}\n")
    for path, size in sorted(nobackup_dirs, key=lambda x: str(x[0])):
        rel = str(path.relative_to(source)) + "/"
        p(f"  {rel:<55s}[{fmt_size(size):>10s}]")
    p()


def write_text(out, included, exc_dirs, exc_files, summarized, nobackup_dirs,
               source, min_size, show, use_color, nobackup_file=".nobackup"):
    if show in ("included", "both"):
        _write_file_section(out, included, source, "INCLUDED FILES",
                            min_size, use_color, summarized)
        _write_stats(out, included, "Included", summarized)
    if show in ("excluded", "both"):
        _write_nobackup_dirs(out, nobackup_dirs, source, nobackup_file)
        _write_excluded_by_rule(out, exc_dirs, exc_files, source)


# ── HTML output ───────────────────────────────────────────────────────────────

_HTML_CSS = """\
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{
  font-family:'JetBrains Mono','SF Mono','Cascadia Code',Consolas,monospace;
  font-size:12px;line-height:1.6;color:#2d2d2d;background:#fff;
  padding:1.2em 2em 4em;max-width:1400px;
}
h1{font-size:14px;font-weight:700;margin-bottom:.3em}
h2{
  font-size:11px;font-weight:700;text-transform:uppercase;
  letter-spacing:.06em;color:#666;border-bottom:1px solid #e0e0e0;
  padding-bottom:.25em;margin:1.8em 0 .5em;
}
.meta{font-size:11px;color:#999;margin-bottom:1.2em}
.meta b{color:#666}
.count{font-weight:normal;color:#aaa;font-size:10px}

/* dir / rule headers */
.dh,.rh{
  display:flex;gap:.5em;align-items:baseline;
  padding:.15em .5em;font-weight:600;font-size:11px;
}
.dh{background:#f5f5f5;border-left:3px solid #ccc;color:#555;margin-top:.4em}
.rh{background:#eef2f7;border-left:3px solid #99b;color:#446;margin-top:.8em}
.dh-n,.rh-n{flex:1}
.dh-t,.rh-t{white-space:nowrap;min-width:7em;text-align:right;font-weight:normal;color:#999}

/* file row */
.fr{display:flex;align-items:baseline;padding:.05em .5em .05em 1.6em}
.fr:nth-child(odd){background:#fafafa}
.fi{width:1.4em;flex-shrink:0;display:inline-block;text-align:center}
.fn{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-right:.8em}
.fs{white-space:nowrap;min-width:7em;text-align:right;color:#aaa}
.note{padding-left:1.6em;color:#ccc;font-style:italic;font-size:11px}

/* summarized dirs */
.sum-block{margin:.3em 0 .6em}
.sum-row{display:flex;align-items:baseline;padding:.05em .5em .05em 1em;color:#999}
.sum-n{flex:1}
.sum-t{white-space:nowrap;min-width:7em;text-align:right}

/* stats */
.sm{color:#777;margin:.4em 0 .6em}
table.ext{border-collapse:collapse;font-size:11px}
table.ext th{text-align:left;color:#bbb;font-weight:normal;
             border-bottom:1px solid #eee;padding:.1em 1.5em .1em 0}
table.ext td{padding:.1em 1.5em .1em 0}
table.ext .r{text-align:right}
table.ext tr:nth-child(even) td{background:#fafafa}
"""


def _html_file_section(lines, files, source, title, min_size, summarized=()):
    w = lines.append
    n_sum = len(summarized)
    suffix = f", {n_sum} summarized" if n_sum else ""
    count_str = f"{len(files):,} files{suffix}" if (files or summarized) else "none"
    w(f'<h2>{_esc(title)} <span class="count">{count_str}</span></h2>')

    if files:
        dir_totals = compute_dir_totals(files)
        by_dir: dict = defaultdict(list)
        for path, size in files:
            by_dir[path.parent].append((path, size))

        for dirpath in sorted(by_dir):
            dir_total = dir_totals.get(dirpath, 0)
            if min_size and dir_total < min_size:
                continue
            rel = dirpath.relative_to(source)
            label = "./" if str(rel) == "." else f"{rel}/"
            w(f'<div class="dh"><span class="dh-n">📁 {_esc(label)}</span>'
              f'<span class="dh-t">[{_esc(fmt_size(dir_total))}]</span></div>')

            shown = 0
            for path, size in sorted(by_dir[dirpath], key=lambda x: x[0].name.lower()):
                if min_size and size < min_size:
                    continue
                cat = get_category(path)
                icon = _ICON.get(cat, "·")
                color = _HTML_COLOR.get(cat, "#999")
                w(f'<div class="fr">'
                  f'<span class="fi" style="color:{color}">{_esc(icon)}</span>'
                  f'<span class="fn">{_esc(path.name)}</span>'
                  f'<span class="fs">{_esc(fmt_size(size))}</span>'
                  f'</div>')
                shown += 1

            if shown == 0 and min_size:
                n = len(by_dir[dirpath])
                w(f'<div class="note">{n} file{"s" if n != 1 else ""} '
                  f'below {_esc(fmt_size(min_size))}</div>')

    if summarized:
        w('<div class="sum-block">'
          '<div class="note" style="margin:.5em 0 .2em">'
          'Summarized — contents not listed, excluded from stats</div>')
        for path, size in sorted(summarized):
            rel = str(path.relative_to(source)) + "/"
            w(f'<div class="sum-row">'
              f'<span class="sum-n">📁 {_esc(rel)}</span>'
              f'<span class="sum-t">[{_esc(fmt_size(size))}]</span>'
              f'</div>')
        w('</div>')


def _html_stats(lines, files, summarized=()):
    if not files:
        return
    w = lines.append
    by_ext: dict = defaultdict(lambda: [0, 0])
    total_size = 0
    for path, size in files:
        ext = path.suffix.lower() or "(no ext)"
        by_ext[ext][0] += 1
        by_ext[ext][1] += size
        total_size += size
    dirs = {path.parent for path, _ in files}
    sum_size = sum(s for _, s in summarized)

    summary = (f'Files: {len(files):,} &nbsp;·&nbsp; '
               f'Directories: {len(dirs):,} &nbsp;·&nbsp; '
               f'Total: {_esc(fmt_size(total_size))}')
    if summarized:
        summary += (f' &nbsp;·&nbsp; '
                    f'Summarized: {_esc(fmt_size(sum_size))} ({len(summarized)} dirs) &nbsp;·&nbsp; '
                    f'<b>Grand total: {_esc(fmt_size(total_size + sum_size))}</b>')
    w(f'<p class="sm">{summary}</p>')
    w('<table class="ext"><tr>'
      '<th>Extension</th><th class="r">Count</th><th class="r">Size</th>'
      '</tr>')
    for ext, (count, size) in sorted(by_ext.items(), key=lambda x: -x[1][1]):
        cat = EXT_CATEGORY.get(ext)
        icon = _ICON.get(cat, "·")
        color = _HTML_COLOR.get(cat, "#999")
        w(f'<tr>'
          f'<td><span style="color:{color}">{_esc(icon)}</span> {_esc(ext)}</td>'
          f'<td class="r">{count:,}</td>'
          f'<td class="r">{_esc(fmt_size(size))}</td>'
          f'</tr>')
    w('</table>')


def _html_excluded_by_rule(lines, exc_dirs, exc_files, source):
    if not exc_dirs and not exc_files:
        return
    w = lines.append

    all_items = []
    for path, size, rule in exc_dirs:
        all_items.append((str(path.relative_to(source)) + "/", size, rule, True))
    for path, size, rule in exc_files:
        all_items.append((str(path.relative_to(source)), size, rule, False))

    by_rule: dict = defaultdict(list)
    for label, size, rule, is_dir in all_items:
        by_rule[rule].append((label, size, is_dir))

    rule_totals = {r: sum(s for _, s, _ in items) for r, items in by_rule.items()}
    grand_total = sum(rule_totals.values())

    n_dirs, n_files = len(exc_dirs), len(exc_files)
    w(f'<h2>Excluded by Rule '
      f'<span class="count">{n_dirs} dirs, {n_files} files, '
      f'{_esc(fmt_size(grand_total))} total</span></h2>')

    for rule in sorted(by_rule, key=lambda r: -rule_totals[r]):
        total = rule_totals[rule]
        w(f'<div class="rh"><span class="rh-n">{_esc(rule)}</span>'
          f'<span class="rh-t">[{_esc(fmt_size(total))}]</span></div>')
        for label, size, is_dir in sorted(by_rule[rule], key=lambda x: -x[1]):
            icon = "📁" if is_dir else "📄"
            w(f'<div class="fr">'
              f'<span class="fi">{icon}</span>'
              f'<span class="fn">{_esc(label)}</span>'
              f'<span class="fs">{_esc(fmt_size(size))}</span>'
              f'</div>')


def _html_nobackup_dirs(lines, nobackup_dirs: list, source: Path, nobackup_file: str):
    if not nobackup_dirs:
        return
    w = lines.append
    total = sum(s for _, s in nobackup_dirs)
    count_str = f"{len(nobackup_dirs)} dirs, {fmt_size(total)} total"
    w(f'<h2>Excluded by {_esc(nobackup_file)} '
      f'<span class="count">{count_str}</span></h2>')
    for path, size in sorted(nobackup_dirs, key=lambda x: str(x[0])):
        rel = str(path.relative_to(source)) + "/"
        w(f'<div class="rh"><span class="rh-n">📁 {_esc(rel)}</span>'
          f'<span class="rh-t">[{_esc(fmt_size(size))}]</span></div>')


def generate_html(included, exc_dirs, exc_files, summarized, nobackup_dirs,
                  source, exclude_file, min_size, show,
                  nobackup_file=".nobackup") -> str:
    lines = []
    w = lines.append
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    w("<!DOCTYPE html>")
    w('<html lang="en"><head>')
    w('<meta charset="UTF-8">')
    w('<meta name="viewport" content="width=device-width,initial-scale=1">')
    w(f'<title>Backup Preview — {_esc(source.name)}</title>')
    w(f"<style>{_HTML_CSS}</style>")
    w("</head><body>")
    w("<h1>Backup Preview</h1>")
    w('<div class="meta">')
    w(f'<div><b>Source:</b> {_esc(str(source))}</div>')
    w(f'<div><b>Exclude file:</b> {_esc(str(exclude_file))}</div>')
    if nobackup_file:
        w(f'<div><b>Nobackup marker:</b> {_esc(nobackup_file)}</div>')
    if min_size:
        w(f'<div><b>Min size:</b> {_esc(fmt_size(min_size))}</div>')
    w(f'<div><b>Generated:</b> {ts}</div>')
    w("</div>")

    if show in ("included", "both"):
        _html_file_section(lines, included, source, "Included Files", min_size, summarized)
        _html_stats(lines, included, summarized)

    if show in ("excluded", "both"):
        _html_nobackup_dirs(lines, nobackup_dirs, source, nobackup_file)
        _html_excluded_by_rule(lines, exc_dirs, exc_files, source)

    w("</body></html>")
    return "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Preview restic backup coverage without a configured repository."
    )
    ap.add_argument("source", help="Source directory to preview")
    ap.add_argument(
        "--exclude-file",
        default=str(DEFAULT_EXCLUDE),
        metavar="FILE",
        help=f"Exclude file (default: {DEFAULT_EXCLUDE})",
    )
    ap.add_argument(
        "--show",
        choices=["included", "excluded", "both"],
        default="both",
        help="Which section to print (default: both)",
    )
    ap.add_argument(
        "--min-size",
        default="0",
        metavar="SIZE",
        help="Skip files/dirs below this size, e.g. 1MB, 500KB (default: 0)",
    )
    ap.add_argument(
        "--summarize",
        action="append",
        default=[],
        metavar="NAME",
        help="Directory name to collapse to a total (repeatable, e.g. --summarize .git)",
    )
    ap.add_argument(
        "--nobackup-file",
        default=".nobackup",
        metavar="NAME",
        help="Exclude dirs containing this marker file (default: .nobackup). "
             "Pass \"\" to disable.",
    )
    ap.add_argument(
        "--output",
        metavar="FILE",
        help="Write report to FILE; format detected from extension (.txt or .html)",
    )
    args = ap.parse_args()

    source = Path(args.source).expanduser().resolve()
    exclude_file = Path(args.exclude_file).expanduser().resolve()
    min_size = parse_size(args.min_size)
    summarize_names = set(args.summarize)
    nobackup_file = args.nobackup_file
    output_path = Path(args.output) if args.output else None

    if not source.is_dir():
        sys.exit(f"Not a directory: {source}")

    spec, pat_specs = _load_exclude(exclude_file)
    included, exc_dirs, exc_files, summarized, nobackup_dirs = collect(
        source, spec, pat_specs, summarize_names, nobackup_file
    )

    header_lines = [
        f"Source:       {source}",
        f"Exclude file: {exclude_file}",
        *(([f"Nobackup:     {nobackup_file}"] if nobackup_file else [])),
        *(([f"Min size:     {fmt_size(min_size)}"] if min_size else [])),
        *(([f"Summarize:    {', '.join(sorted(summarize_names))}"] if summarize_names else [])),
    ]

    if output_path is None:
        use_color = sys.stdout.isatty()
        for line in header_lines:
            print(line)
        write_text(sys.stdout, included, exc_dirs, exc_files, summarized, nobackup_dirs,
                   source, min_size, args.show, use_color, nobackup_file)
    elif output_path.suffix.lower() == ".html":
        html = generate_html(included, exc_dirs, exc_files, summarized, nobackup_dirs,
                             source, exclude_file, min_size, args.show, nobackup_file)
        output_path.write_text(html, encoding="utf-8")
        print(f"HTML report written to {output_path}", file=sys.stderr)
    else:
        with output_path.open("w", encoding="utf-8") as f:
            for line in header_lines:
                print(line, file=f)
            write_text(f, included, exc_dirs, exc_files, summarized, nobackup_dirs,
                       source, min_size, args.show, use_color=False,
                       nobackup_file=nobackup_file)
        print(f"TXT report written to {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
