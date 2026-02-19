"""
Static Analysis Service
=======================
Deterministic bug detection layer.

NO LLM ALLOWED HERE. This module runs pylint, pyflakes, mypy, and AST
inspection against a cloned repository and converts their raw output
into structured BugReport objects.

STRICT DETERMINISM CONTRACT:
  - No LLM calls.
  - No dynamic inference of bug types.
  - All mappings are explicit and documented.
  - Unknown errors are logged but never formatted.
  - Partial results are returned if a tool crashes.

OUTPUT CONTRACT:
  analyze_repository(repo_path) -> List[BugReport]
  Sorted by (file_path, line_number).  Deduplicated.
"""

import ast
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import List

from app.models.bug_report import BugReport
from app.utils.domain_classifier import classify_domain

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Directories to ignore during analysis
# ---------------------------------------------------------------------------
IGNORE_DIRS: set[str] = {
    ".venv", "venv", "node_modules", ".git",
    "dist", "build", "__pycache__", ".mypy_cache",
    ".pytest_cache", "env", ".tox", "eggs", ".eggs",
}


# ---------------------------------------------------------------------------
# Pylint error-code → (bug_type, sub_type) mapping
# ---------------------------------------------------------------------------
# Only codes we know how to handle are listed. Unknown codes are skipped.
PYLINT_MAP: dict[str, tuple[str, str]] = {
    # LINTING
    "W0611": ("LINTING", "unused_import"),
    "W0612": ("LINTING", "unused_variable"),
    "C0301": ("LINTING", "line_too_long"),
    "C0303": ("LINTING", "trailing_whitespace"),
    "C0321": ("LINTING", "multiple_statements"),
    "C0326": ("LINTING", "missing_whitespace"),

    # SYNTAX
    "E0001": ("SYNTAX", "invalid_syntax"),

    # IMPORT
    "E0401": ("IMPORT", "missing_import"),
    "E0611": ("IMPORT", "wrong_path"),

    # INDENTATION
    "W0311": ("INDENTATION", "wrong_indent"),
    "W0312": ("INDENTATION", "mixed_indent"),
}


# ---------------------------------------------------------------------------
# Pyflakes message-pattern → (bug_type, sub_type) mapping
# ---------------------------------------------------------------------------
PYFLAKES_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"imported but unused"),            "LINTING", "unused_import"),
    (re.compile(r"local variable .+ is assigned .+ but never used"),
                                                    "LINTING", "unused_variable"),
    (re.compile(r"undefined name"),                 "IMPORT",  "missing_import"),
    (re.compile(r"redefinition of unused"),          "LINTING", "unused_import"),
    (re.compile(r"unable to detect undefined names"),"SYNTAX", "invalid_syntax"),
]


# ---------------------------------------------------------------------------
# Mypy message-pattern → (bug_type, sub_type) mapping
# ---------------------------------------------------------------------------
MYPY_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"Incompatible types"),             "TYPE_ERROR", "incompatible_types"),
    (re.compile(r"Incompatible return value"),       "TYPE_ERROR", "wrong_return_type"),
    (re.compile(r"has incompatible type"),           "TYPE_ERROR", "type_mismatch"),
    (re.compile(r"Item .+ of .+ has no attribute"),  "TYPE_ERROR", "none_reference"),
    (re.compile(r"Missing return statement"),        "TYPE_ERROR", "wrong_return_type"),
    (re.compile(r"Cannot find implementation or stub"), "IMPORT", "wrong_path"),
    (re.compile(r"No library stub file"),            "IMPORT", "wrong_path"),
    (re.compile(r"Module .+ has no attribute"),      "IMPORT", "wrong_path"),
]


# ===================================================================
# File Discovery
# ===================================================================
def discover_python_files(repo_path: str) -> List[str]:
    """
    Recursively walk repo_path, returning relative .py file paths.
    Skips IGNORE_DIRS.  Never hardcodes file paths.
    """
    repo = Path(repo_path)
    py_files: list[str] = []
    for root, dirs, files in os.walk(repo):
        # Prune ignored directories in-place
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for fname in files:
            if fname.endswith(".py"):
                abs_path = Path(root) / fname
                rel = abs_path.relative_to(repo).as_posix()
                py_files.append(rel)
    return sorted(py_files)


# ===================================================================
# Path Normalisation
# ===================================================================
def normalize_path(raw_path: str, repo_path: str) -> str:
    """
    Convert a tool-output path to a repo-relative, forward-slash path.
    """
    try:
        p = Path(raw_path).resolve()
        repo = Path(repo_path).resolve()
        return p.relative_to(repo).as_posix()
    except (ValueError, RuntimeError):
        # Already relative or outside repo – normalise slashes only
        return raw_path.replace("\\", "/")


# ===================================================================
# Pylint Runner & Parser
# ===================================================================
def run_pylint(repo_path: str, py_files: List[str]) -> List[BugReport]:
    """Run pylint on discovered files; parse JSON output into BugReports."""
    if not py_files:
        return []

    reports: list[BugReport] = []
    abs_files = [str(Path(repo_path) / f) for f in py_files]

    try:
        result = subprocess.run(
            ["python", "-m", "pylint", "--output-format=json", "--disable=all",
             "--enable=W0611,W0612,C0301,C0303,C0321,C0326,E0001,E0401,E0611,W0311,W0312",
             *abs_files],
            capture_output=True, text=True, timeout=120,
            cwd=repo_path,
        )
        raw = result.stdout.strip()
        if not raw:
            return []

        issues = json.loads(raw)
        for issue in issues:
            code = issue.get("message-id", "")
            mapping = PYLINT_MAP.get(code)
            if mapping is None:
                logger.debug("pylint: unmapped code %s – skipping", code)
                continue

            bug_type, sub_type = mapping
            fpath = normalize_path(issue.get("path", ""), repo_path)
            line = issue.get("line", 0)
            msg = issue.get("message", "")

            reports.append(BugReport(
                bug_type=bug_type,
                sub_type=sub_type,
                file_path=fpath,
                line_number=max(line, 1),
                domain=classify_domain(fpath),
                tool="pylint",
                message=msg,
            ))
    except FileNotFoundError:
        logger.warning("pylint not installed – skipping")
    except subprocess.TimeoutExpired:
        logger.warning("pylint timed out – returning partial results")
    except (json.JSONDecodeError, Exception) as exc:
        logger.warning("pylint parse error: %s", exc)

    return reports


# ===================================================================
# Pyflakes Runner & Parser
# ===================================================================
def run_pyflakes(repo_path: str, py_files: List[str]) -> List[BugReport]:
    """Run pyflakes on discovered files; regex-parse output into BugReports."""
    if not py_files:
        return []

    reports: list[BugReport] = []
    abs_files = [str(Path(repo_path) / f) for f in py_files]

    try:
        result = subprocess.run(
            ["python", "-m", "pyflakes", *abs_files],
            capture_output=True, text=True, timeout=120,
            cwd=repo_path,
        )
        output = (result.stdout + result.stderr).strip()
        if not output:
            return []

        # Typical line:  path/to/file.py:10: 'os' imported but unused
        line_re = re.compile(r"^(.+?):(\d+):\d*\s*(.+)$", re.MULTILINE)
        for match in line_re.finditer(output):
            raw_path, line_str, msg = match.group(1), match.group(2), match.group(3)

            bug_type, sub_type = None, None
            for pattern, bt, st in PYFLAKES_PATTERNS:
                if pattern.search(msg):
                    bug_type, sub_type = bt, st
                    break

            if bug_type is None:
                logger.debug("pyflakes: unmapped message '%s' – skipping", msg)
                continue

            fpath = normalize_path(raw_path, repo_path)
            reports.append(BugReport(
                bug_type=bug_type,
                sub_type=sub_type,
                file_path=fpath,
                line_number=max(int(line_str), 1),
                domain=classify_domain(fpath),
                tool="pyflakes",
                message=msg,
            ))
    except FileNotFoundError:
        logger.warning("pyflakes not installed – skipping")
    except subprocess.TimeoutExpired:
        logger.warning("pyflakes timed out – returning partial results")
    except Exception as exc:
        logger.warning("pyflakes error: %s", exc)

    return reports


# ===================================================================
# Mypy Runner & Parser
# ===================================================================
def run_mypy(repo_path: str, py_files: List[str]) -> List[BugReport]:
    """Run mypy on discovered files; regex-parse output into BugReports."""
    if not py_files:
        return []

    reports: list[BugReport] = []
    abs_files = [str(Path(repo_path) / f) for f in py_files]

    try:
        result = subprocess.run(
            ["python", "-m", "mypy", "--no-error-summary",
             "--no-color", "--show-column-numbers", *abs_files],
            capture_output=True, text=True, timeout=120,
            cwd=repo_path,
        )
        output = result.stdout.strip()
        if not output:
            return []

        # Typical line: file.py:10:5: error: Incompatible types [assignment]
        line_re = re.compile(r"^(.+?):(\d+):\d+:\s*error:\s*(.+)$", re.MULTILINE)
        for match in line_re.finditer(output):
            raw_path, line_str, msg = match.group(1), match.group(2), match.group(3)

            bug_type, sub_type = None, None
            for pattern, bt, st in MYPY_PATTERNS:
                if pattern.search(msg):
                    bug_type, sub_type = bt, st
                    break

            if bug_type is None:
                logger.debug("mypy: unmapped message '%s' – skipping", msg)
                continue

            fpath = normalize_path(raw_path, repo_path)
            reports.append(BugReport(
                bug_type=bug_type,
                sub_type=sub_type,
                file_path=fpath,
                line_number=max(int(line_str), 1),
                domain=classify_domain(fpath),
                tool="mypy",
                message=msg,
            ))
    except FileNotFoundError:
        logger.warning("mypy not installed – skipping")
    except subprocess.TimeoutExpired:
        logger.warning("mypy timed out – returning partial results")
    except Exception as exc:
        logger.warning("mypy error: %s", exc)

    return reports


# ===================================================================
# AST Fallback Checks
# ===================================================================
def run_ast_checks(repo_path: str, py_files: List[str]) -> List[BugReport]:
    """
    Simple deterministic AST-based checks for syntax errors.
    Catches files that cannot parse at all.
    """
    reports: list[BugReport] = []
    repo = Path(repo_path)

    for rel_path in py_files:
        abs_path = repo / rel_path
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
            ast.parse(source, filename=rel_path)
        except SyntaxError as exc:
            line_no = exc.lineno or 1
            msg = str(exc.msg) if exc.msg else "syntax error"

            # Classify the syntax error sub_type
            sub_type = "invalid_syntax"
            msg_lower = msg.lower()
            if "expected ':'" in msg_lower or "expected ':')" in msg_lower:
                sub_type = "missing_colon"
            elif "unexpected indent" in msg_lower:
                sub_type = "unexpected_indent"
            elif "(" in msg_lower and ")" in msg_lower and "bracket" in msg_lower:
                sub_type = "missing_bracket"
            elif "parenthesis" in msg_lower:
                sub_type = "missing_parenthesis"

            fpath = rel_path.replace("\\", "/")
            reports.append(BugReport(
                bug_type="SYNTAX",
                sub_type=sub_type,
                file_path=fpath,
                line_number=max(line_no, 1),
                domain=classify_domain(fpath),
                tool="ast",
                message=msg,
            ))
        except Exception as exc:
            logger.debug("ast: error reading %s: %s", rel_path, exc)

    return reports


# ===================================================================
# Deduplication
# ===================================================================
def deduplicate(reports: List[BugReport]) -> List[BugReport]:
    """
    Remove duplicate BugReports.
    A report is a duplicate if (file_path, line_number, sub_type) already exists.
    The first occurrence wins.
    """
    seen: set[tuple[str, int, str]] = set()
    unique: list[BugReport] = []
    for r in reports:
        key = (r.file_path, r.line_number, r.sub_type)
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


# ===================================================================
# Public Entry Point
# ===================================================================
def analyze_repository(repo_path: str) -> List[BugReport]:
    """
    Run full static analysis pipeline on a repository.

    Steps:
    1. Discover .py files (skip ignored directories)
    2. Run pylint, pyflakes, mypy, and AST checks
    3. Merge all results
    4. Deduplicate
    5. Sort by (file_path, line_number)

    Returns
    -------
    List[BugReport]
        Deterministic, sorted, deduplicated list of detected bugs.
    """
    logger.info("Starting static analysis on: %s", repo_path)

    py_files = discover_python_files(repo_path)
    logger.info("Discovered %d Python files", len(py_files))

    if not py_files:
        logger.warning("No Python files found in %s", repo_path)
        return []

    # Run all tools — each is resilient (returns partial on failure)
    all_reports: list[BugReport] = []
    all_reports.extend(run_pylint(repo_path, py_files))
    all_reports.extend(run_pyflakes(repo_path, py_files))
    all_reports.extend(run_mypy(repo_path, py_files))
    all_reports.extend(run_ast_checks(repo_path, py_files))

    # Deduplicate and sort
    unique = deduplicate(all_reports)
    unique.sort(key=lambda r: (r.file_path, r.line_number))

    logger.info("Analysis complete: %d unique issues detected", len(unique))
    return unique
