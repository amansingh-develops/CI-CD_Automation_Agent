"""
Merge Conflict Detector
=======================
Scans repository files for unresolved Git merge conflict markers.

Strategy:
    - Scan for <<<<<< / ====== / >>>>>> markers
    - Report file path and line number
    - NEVER auto-fix — flag to human

A responsible agent should never auto-resolve merge conflicts.
That's a semantic decision only a human developer can make.
"""
import os
import re
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Structure
# ---------------------------------------------------------------------------
@dataclass
class MergeConflict:
    """A detected merge conflict in a file."""
    file_path: str    # Repo-relative, forward slashes
    line_number: int  # Line where the conflict starts (<<<<<<< marker)
    ours_branch: str  # Branch name from <<<<<<< marker
    theirs_branch: str  # Branch name from >>>>>>> marker


# ---------------------------------------------------------------------------
# Merge Conflict Patterns
# ---------------------------------------------------------------------------
_CONFLICT_START = re.compile(r'^<{7}\s*(.*)$', re.MULTILINE)
_CONFLICT_SEPARATOR = re.compile(r'^={7}$', re.MULTILINE)
_CONFLICT_END = re.compile(r'^>{7}\s*(.*)$', re.MULTILINE)


# File extensions to scan (text files)
_SCAN_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift",
    ".kt", ".scala", ".yml", ".yaml", ".json", ".toml", ".cfg",
    ".ini", ".md", ".txt", ".html", ".css", ".scss", ".xml",
    ".sh", ".bash", ".zsh", ".bat", ".ps1",
    ".env", ".gitignore", ".dockerignore",
}

# Directories to skip
_SKIP_DIRS = {
    "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".git", ".tox", ".mypy_cache", ".pytest_cache", "site-packages",
    ".next", "target", "vendor", "bin", "obj",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def detect_merge_conflicts(
    workspace_path: str,
    max_files: int = 500,
) -> list[MergeConflict]:
    """
    Scan workspace for unresolved Git merge conflict markers.

    Parameters
    ----------
    workspace_path : str
        Repository root directory.
    max_files : int
        Maximum number of files to scan (prevents runaway on huge repos).

    Returns
    -------
    list[MergeConflict]
        All detected merge conflicts. Empty list if none found.
        Never raises — returns partial results on error.
    """
    conflicts: list[MergeConflict] = []
    files_scanned = 0

    try:
        for root, dirs, files in os.walk(workspace_path):
            # Skip ignored directories
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]

            for fname in sorted(files):
                if files_scanned >= max_files:
                    logger.info("Reached max_files limit (%d), stopping scan", max_files)
                    return conflicts

                # Only scan known text extensions
                _, ext = os.path.splitext(fname)
                if ext.lower() not in _SCAN_EXTENSIONS:
                    continue

                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, workspace_path).replace("\\", "/")

                try:
                    with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                except Exception:
                    continue

                files_scanned += 1
                _scan_file_for_conflicts(content, rel_path, conflicts)

    except Exception as e:
        logger.warning("Error scanning for merge conflicts: %s", e, exc_info=True)

    logger.info("Scanned %d files, found %d merge conflict(s)", files_scanned, len(conflicts))
    return conflicts


def _scan_file_for_conflicts(
    content: str,
    file_path: str,
    conflicts: list[MergeConflict],
) -> None:
    """Scan a single file's content for conflict markers."""
    starts = list(_CONFLICT_START.finditer(content))
    ends = list(_CONFLICT_END.finditer(content))

    for start_match in starts:
        line_number = content[:start_match.start()].count("\n") + 1
        ours_branch = start_match.group(1).strip()

        # Find matching end marker
        theirs_branch = ""
        for end_match in ends:
            if end_match.start() > start_match.start():
                theirs_branch = end_match.group(1).strip()
                break

        conflicts.append(MergeConflict(
            file_path=file_path,
            line_number=line_number,
            ours_branch=ours_branch,
            theirs_branch=theirs_branch,
        ))


def has_merge_conflicts(workspace_path: str) -> bool:
    """Quick check: does the workspace have any merge conflicts?"""
    return len(detect_merge_conflicts(workspace_path, max_files=100)) > 0
