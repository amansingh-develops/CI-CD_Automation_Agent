"""
Failure Parser
==============
Converts raw build/test logs into structured BugReport objects.

Pipeline:
    1. Split log into lines
    2. Detect failure sections / candidate error lines
    3. Extract file path and line number
    4. Normalize file paths (workspace-relative, forward slashes)
    5. Classify bug_type + sub_type via classification layer
    6. Deduplicate by (file_path, line_number, sub_type)
    7. Sort by bug_type priority (SYNTAX first → LINTING last)

Contract:
    - DETERMINISTIC: same log → same BugReports, always.
    - No LLM allowed in this layer.
    - Regex and heuristic pattern matching only.
    - Tolerant: partial results on parse failure, never crashes.
"""
import re
import os
import logging
from dataclasses import dataclass, field
from typing import Optional

from app.models.bug_report import BugReport
from app.parser.classification import (
    classify_error,
    priority_of,
    ClassificationResult,
    CONF_HIGH,
    CONF_MEDIUM,
    CONF_LOW,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path Ignore Rules
# ---------------------------------------------------------------------------
_IGNORE_PATTERNS: list[str] = [
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "site-packages",
    ".git",
]


def _should_ignore(file_path: str) -> bool:
    """Return True if the file path is in an ignored directory."""
    normalized = file_path.replace("\\", "/")
    for pattern in _IGNORE_PATTERNS:
        if f"/{pattern}/" in f"/{normalized}/":
            return True
    return False


# ---------------------------------------------------------------------------
# Path Normalization
# ---------------------------------------------------------------------------
def normalize_path(raw_path: str, workspace_path: str = "") -> str:
    """
    Convert an absolute or messy path to a clean workspace-relative path.

    Steps:
        1. Replace backslashes with forward slashes
        2. Strip quotes and whitespace
        3. Remove workspace prefix if present
        4. Remove leading slashes

    Parameters
    ----------
    raw_path : str
        The raw file path extracted from a log line.
    workspace_path : str
        The workspace root directory to strip.

    Returns
    -------
    str
        Clean, workspace-relative path with forward slashes.
    """
    path = raw_path.strip().strip("'\"")
    path = path.replace("\\", "/")

    if workspace_path:
        ws = workspace_path.replace("\\", "/").rstrip("/")
        if path.startswith(ws):
            path = path[len(ws):]

    # Also strip /workspace/ prefix from Docker container paths
    if path.startswith("/workspace/"):
        path = path[len("/workspace/"):]

    path = path.lstrip("/")
    return path


# ---------------------------------------------------------------------------
# Error Line Extraction Patterns
# ---------------------------------------------------------------------------
# Python traceback: File "path", line N
_PY_TRACEBACK = re.compile(
    r'File\s+"([^"]+)",\s+line\s+(\d+)',
    re.IGNORECASE,
)

# Python / generic error label: ErrorType: message
_PY_ERROR_LABEL = re.compile(
    r'^(\w+Error|\w+Exception|SyntaxError|IndentationError|TabError):\s*(.+)$',
    re.MULTILINE,
)

# Node / JS stack trace: at ... (path:line:col) or path:line:col
_NODE_STACK = re.compile(
    r'(?:at\s+.+?\s+\()?([^\s()]+):(\d+):\d+\)?',
)

# Generic compiler: path:line: error: message
_GENERIC_COMPILER = re.compile(
    r'^(.+?):(\d+):\s*(?:error|warning):\s*(.+)$',
    re.MULTILINE,
)

# Pytest test name: FAILED tests/test_foo.py::TestClass::test_method
_PYTEST_FAILED = re.compile(
    r'FAILED\s+(\S+?)::(\S+)',
)


# ---------------------------------------------------------------------------
# Core Extraction: Candidate Error Lines
# ---------------------------------------------------------------------------
@dataclass
class _RawMatch:
    """Internal: a candidate error extracted from logs before classification."""
    file_path: str
    line_number: int
    error_name: str
    error_message: str
    test_name: Optional[str] = None
    confidence: float = CONF_MEDIUM


def _extract_python_errors(log: str, workspace_path: str) -> list[_RawMatch]:
    """Extract errors from Python tracebacks."""
    matches: list[_RawMatch] = []

    # Find all traceback file references
    tb_locations = list(_PY_TRACEBACK.finditer(log))
    error_labels = list(_PY_ERROR_LABEL.finditer(log))

    if not tb_locations:
        return matches

    # Pair the last file reference before each error label
    for error_match in error_labels:
        error_pos = error_match.start()
        error_name = error_match.group(1)
        error_message = error_match.group(2).strip()

        # Find closest preceding file reference
        closest_tb = None
        for tb in tb_locations:
            if tb.start() < error_pos:
                closest_tb = tb
            else:
                break

        if closest_tb:
            raw_path = closest_tb.group(1)
            path = normalize_path(raw_path, workspace_path)

            if _should_ignore(path):
                continue

            line_num = int(closest_tb.group(2))
            matches.append(_RawMatch(
                file_path=path,
                line_number=line_num,
                error_name=error_name,
                error_message=error_message,
                confidence=CONF_HIGH,
            ))

    # If we found traceback locations but no error labels,
    # extract from the traceback lines themselves
    if tb_locations and not matches:
        for tb in tb_locations:
            raw_path = tb.group(1)
            path = normalize_path(raw_path, workspace_path)
            if _should_ignore(path):
                continue
            matches.append(_RawMatch(
                file_path=path,
                line_number=int(tb.group(2)),
                error_name="error",
                error_message="error detected at this location",
                confidence=CONF_LOW,
            ))

    return matches


def _extract_node_errors(log: str, workspace_path: str) -> list[_RawMatch]:
    """Extract errors from Node.js / JavaScript stack traces."""
    matches: list[_RawMatch] = []

    for m in _NODE_STACK.finditer(log):
        raw_path = m.group(1)
        path = normalize_path(raw_path, workspace_path)

        if _should_ignore(path):
            continue

        # Skip internal node paths
        if path.startswith("internal/") or path.startswith("node:"):
            continue

        line_num = int(m.group(2))

        # Try to find error name on the preceding line
        line_start = log.rfind("\n", 0, m.start()) + 1
        line_text = log[line_start:m.start()].strip()

        error_name = "error"
        error_message = line_text
        # Check if line contains a known JS error type
        for err_type in ("TypeError", "ReferenceError", "SyntaxError", "RangeError"):
            if err_type in log[max(0, m.start() - 200):m.end()]:
                error_name = err_type
                break

        matches.append(_RawMatch(
            file_path=path,
            line_number=line_num,
            error_name=error_name,
            error_message=error_message,
            confidence=CONF_MEDIUM,
        ))

    return matches


def _extract_generic_errors(log: str, workspace_path: str) -> list[_RawMatch]:
    """Extract errors from generic compiler output (path:line: error: msg)."""
    matches: list[_RawMatch] = []

    for m in _GENERIC_COMPILER.finditer(log):
        raw_path = m.group(1)
        path = normalize_path(raw_path, workspace_path)

        if _should_ignore(path):
            continue

        line_num = int(m.group(2))
        message = m.group(3).strip()

        matches.append(_RawMatch(
            file_path=path,
            line_number=line_num,
            error_name="error",
            error_message=message,
            confidence=CONF_MEDIUM,
        ))

    return matches


def _extract_test_names(log: str) -> dict[str, str]:
    """Extract test names from pytest FAILED lines. Returns file_path → test_name."""
    result: dict[str, str] = {}
    for m in _PYTEST_FAILED.finditer(log):
        file_path = m.group(1)
        test_name = m.group(2)
        result[file_path] = test_name
    return result


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------
def _deduplicate(reports: list[BugReport]) -> list[BugReport]:
    """
    Deduplicate BugReports by (file_path, line_number, sub_type).
    Keep the report with the highest confidence.
    """
    best: dict[tuple[str, int, str], BugReport] = {}
    for r in reports:
        key = (r.file_path, r.line_number, r.sub_type)
        if key not in best or r.confidence > best[key].confidence:
            best[key] = r
    return list(best.values())


# ---------------------------------------------------------------------------
# Priority Sorting
# ---------------------------------------------------------------------------
def _sort_by_priority(reports: list[BugReport]) -> list[BugReport]:
    """Sort BugReports by bug_type priority (SYNTAX first → LINTING last)."""
    return sorted(reports, key=lambda r: (priority_of(r.bug_type), r.file_path, r.line_number))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def parse_failure_log(
    full_log: str,
    workspace_path: str = "",
    project_type: Optional[str] = None,
) -> list[BugReport]:
    """
    Parse a build/test log into structured BugReport objects.

    Pipeline:
        1. Extract candidate errors (Python, Node, generic)
        2. Classify each via classification layer
        3. Attach test names if available
        4. Deduplicate
        5. Sort by priority

    Parameters
    ----------
    full_log : str
        Complete build/test output from the executor.
    workspace_path : str
        Workspace root for path normalization.
    project_type : str | None
        Detected project type (used to prioritize extraction).

    Returns
    -------
    list[BugReport]
        Sorted, deduplicated list of structured bug reports.
        Empty list if no errors detected. Never raises.
    """
    if not full_log or not full_log.strip():
        return []

    raw_matches: list[_RawMatch] = []

    try:
        # Extract from all patterns — order by project type preference
        if project_type == "node":
            raw_matches.extend(_extract_node_errors(full_log, workspace_path))
            raw_matches.extend(_extract_generic_errors(full_log, workspace_path))
            raw_matches.extend(_extract_python_errors(full_log, workspace_path))
        else:
            # Default: Python first (most common), then generic, then Node
            raw_matches.extend(_extract_python_errors(full_log, workspace_path))
            raw_matches.extend(_extract_generic_errors(full_log, workspace_path))
            raw_matches.extend(_extract_node_errors(full_log, workspace_path))

    except Exception as e:
        logger.warning("Error during log extraction: %s", e, exc_info=True)

    # Extract test names
    test_names = _extract_test_names(full_log)

    # Classify and build BugReports
    reports: list[BugReport] = []
    for raw in raw_matches:
        try:
            classification = classify_error(raw.error_name, raw.error_message)

            report = BugReport(
                file_path=raw.file_path,
                line_number=raw.line_number,
                bug_type=classification.bug_type,
                sub_type=classification.sub_type,
                message=raw.error_message,
                domain=classification.domain,
                test_name=raw.test_name or test_names.get(raw.file_path),
                confidence=min(raw.confidence, classification.confidence),
            )
            reports.append(report)

        except Exception as e:
            logger.warning("Failed to classify error: %s", e, exc_info=True)
            continue

    # Deduplicate and sort
    reports = _deduplicate(reports)
    reports = _sort_by_priority(reports)

    logger.info("Parsed %d BugReport(s) from log (%d chars)", len(reports), len(full_log))
    return reports

