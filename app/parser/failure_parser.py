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

# Patterns that indicate a file is part of the Python stdlib or runtime
_STDLIB_PATTERNS: list[str] = [
    "/lib/python",
    "/lib64/python",
    "/cpython",
    "/Lib/",           # Windows stdlib path
    "\\Lib\\",         # Windows stdlib path (backslash)
]


def _is_stdlib_path(file_path: str) -> bool:
    """Return True if the file path looks like a Python stdlib or runtime file."""
    for pattern in _STDLIB_PATTERNS:
        if pattern in file_path:
            return True
    return False


def _should_ignore(file_path: str) -> bool:
    """Return True if the file path is in an ignored directory or is a stdlib file."""
    normalized = file_path.replace("\\", "/")
    for pattern in _IGNORE_PATTERNS:
        if f"/{pattern}/" in f"/{normalized}/":
            return True
    # Also filter stdlib / runtime paths
    if _is_stdlib_path(file_path):
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

# SyntaxError message that contains the REAL source file:
# e.g. "invalid syntax (calculator.py, line 7)" or "invalid syntax (src/calculator.py, line 7)"
_SYNTAX_ERR_SOURCE = re.compile(
    r'\(([^)]+\.py),\s*line\s+(\d+)\)',
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

# Pytest short traceback: path:line: in <module> (used in --tb=short and collection errors)
_PYTEST_SHORT_TB = re.compile(
    r'^([^\s:]+\.\w+):(\d+):\s+in\s+',
    re.MULTILINE,
)

# Pytest E-line: E   ErrorType: message  (prefixed with 'E' and whitespace)
_PYTEST_E_LINE = re.compile(
    r'^E\s{3,}(\w+(?:Error|Exception|Warning)):\s*(.+)$',
    re.MULTILINE,
)

# Java/Maven: [ERROR] path:[line,col] message OR path:[line] error: message
_JAVA_ERROR = re.compile(
    r'^\[ERROR\]\s+(.+?):\[?(\d+)(?:,\d+)?\]?\s+(.+)$',
    re.MULTILINE,
)

# Gradle/Java compiler: path:line: error: message
_JAVA_COMPILER = re.compile(
    r'^(.+?\.java):(\d+):\s*error:\s*(.+)$',
    re.MULTILINE,
)

# Go compiler: path:line:col: message  (Go errors don't have "error:" prefix)
_GO_ERROR = re.compile(
    r'^(.+?\.go):(\d+)(?::\d+)?:\s+(.+)$',
    re.MULTILINE,
)

# Rust/Cargo: error[E0xxx]: message  with  --> path:line:col on next line
_RUST_ERROR_HEADER = re.compile(
    r'^error(?:\[E\d+\])?:\s*(.+)$',
    re.MULTILINE,
)
_RUST_LOCATION = re.compile(
    r'^\s*-->\s*(.+?):(\d+)(?::\d+)?$',
    re.MULTILINE,
)

# Rust warning: warning[...]: message  with  --> path:line:col
_RUST_WARNING_HEADER = re.compile(
    r'^warning(?:\[\w+\])?:\s*(.+)$',
    re.MULTILINE,
)

# Universal: any line with error/Error/ERROR + nearby path:line
_UNIVERSAL_ERROR_LINE = re.compile(
    r'^.*\b(?:error|Error|ERROR|FATAL|fatal|FAIL|fail(?:ed)?|panic)\b.*$',
    re.MULTILINE,
)
_UNIVERSAL_PATH_LINE = re.compile(
    r'([^\s:"]+\.[a-zA-Z0-9]{1,10}):(\d+)',
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
            line_num = int(closest_tb.group(2))

            # --- KEY FIX: SyntaxError traceback resolution ---
            # When Python hits a SyntaxError during import/compilation,
            # the traceback often ends at a stdlib file (e.g., ast.py,
            # compile(), importlib). The REAL file is embedded in the
            # error message: "invalid syntax (calculator.py, line 7)"
            if _should_ignore(path) or _is_stdlib_path(raw_path):
                # Try to extract the real source file from the error message
                source_match = _SYNTAX_ERR_SOURCE.search(error_message)
                if source_match:
                    real_file = source_match.group(1).strip()
                    real_line = int(source_match.group(2))
                    real_path = normalize_path(real_file, workspace_path)
                    if not _should_ignore(real_path):
                        matches.append(_RawMatch(
                            file_path=real_path,
                            line_number=real_line,
                            error_name=error_name,
                            error_message=error_message,
                            confidence=CONF_HIGH,
                        ))
                        logger.debug(
                            "Resolved stdlib traceback %s → user file %s:%d",
                            path, real_path, real_line,
                        )
                else:
                    # Also try scanning ALL traceback locations before this
                    # error for a user file (non-stdlib, non-ignored)
                    for tb2 in reversed(tb_locations):
                        if tb2.start() < error_pos:
                            alt_path = normalize_path(tb2.group(1), workspace_path)
                            if not _should_ignore(alt_path) and not _is_stdlib_path(tb2.group(1)):
                                matches.append(_RawMatch(
                                    file_path=alt_path,
                                    line_number=int(tb2.group(2)),
                                    error_name=error_name,
                                    error_message=error_message,
                                    confidence=CONF_MEDIUM,
                                ))
                                break
                continue

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


def _extract_pytest_collection_errors(log: str, workspace_path: str) -> list[_RawMatch]:
    """
    Extract errors from pytest collection / short-traceback output.

    Handles the common pytest format:
        path.py:N: in <module>
            <source line>
        E   ImportError: cannot import name 'foo'

    This pattern appears for ANY language tested through pytest,
    as well as collection-phase errors for missing modules, syntax errors, etc.
    """
    matches: list[_RawMatch] = []

    # Find all E-prefixed error lines
    e_lines = list(_PYTEST_E_LINE.finditer(log))
    # Find all short-traceback path references
    tb_refs = list(_PYTEST_SHORT_TB.finditer(log))

    if not e_lines:
        return matches

    for e_match in e_lines:
        error_name = e_match.group(1)
        error_message = e_match.group(2).strip()
        e_pos = e_match.start()

        # Find the closest preceding short-traceback path reference
        # that points to a USER file (not stdlib/site-packages)
        best_tb = None
        for tb in tb_refs:
            if tb.start() < e_pos:
                candidate_path = normalize_path(tb.group(1), workspace_path)
                if not _should_ignore(candidate_path):
                    best_tb = tb
            else:
                break

        if best_tb:
            raw_path = best_tb.group(1)
            path = normalize_path(raw_path, workspace_path)
            line_num = int(best_tb.group(2))

            matches.append(_RawMatch(
                file_path=path,
                line_number=line_num,
                error_name=error_name,
                error_message=error_message,
                confidence=CONF_HIGH,
            ))
        else:
            # No path context found — still record the error with
            # a fallback search for ANY path:line pattern in nearby text
            nearby = log[max(0, e_pos - 500):e_pos]
            path_match = re.search(r'([^\s:]+\.\w+):(\d+)', nearby)
            if path_match:
                raw_path = path_match.group(1)
                path = normalize_path(raw_path, workspace_path)
                if not _should_ignore(path):
                    matches.append(_RawMatch(
                        file_path=path,
                        line_number=int(path_match.group(2)),
                        error_name=error_name,
                        error_message=error_message,
                        confidence=CONF_MEDIUM,
                    ))

    return matches


def _extract_java_errors(log: str, workspace_path: str) -> list[_RawMatch]:
    """
    Extract errors from Java / Maven / Gradle build output.

    Handles:
        [ERROR] /src/Main.java:[15,10] cannot find symbol
        src/Main.java:15: error: cannot find symbol
        [ERROR] Failed to execute goal org.apache.maven...
    """
    matches: list[_RawMatch] = []

    # Maven [ERROR] format
    for m in _JAVA_ERROR.finditer(log):
        raw_path = m.group(1).strip()
        path = normalize_path(raw_path, workspace_path)
        if _should_ignore(path):
            continue
        line_num = int(m.group(2))
        message = m.group(3).strip()

        # Extract error type from message if present
        error_name = "CompilationError"
        if "cannot find symbol" in message.lower():
            error_name = "ImportError"
        elif "package" in message.lower() and "does not exist" in message.lower():
            error_name = "ImportError"
        elif "incompatible types" in message.lower():
            error_name = "TypeError"

        matches.append(_RawMatch(
            file_path=path,
            line_number=line_num,
            error_name=error_name,
            error_message=message,
            confidence=CONF_HIGH,
        ))

    # Java compiler format (javac / Gradle)
    for m in _JAVA_COMPILER.finditer(log):
        raw_path = m.group(1).strip()
        path = normalize_path(raw_path, workspace_path)
        if _should_ignore(path):
            continue
        line_num = int(m.group(2))
        message = m.group(3).strip()

        error_name = "CompilationError"
        if "cannot find symbol" in message.lower():
            error_name = "ImportError"
        elif "incompatible types" in message.lower():
            error_name = "TypeError"

        matches.append(_RawMatch(
            file_path=path,
            line_number=line_num,
            error_name=error_name,
            error_message=message,
            confidence=CONF_HIGH,
        ))

    return matches


def _extract_go_errors(log: str, workspace_path: str) -> list[_RawMatch]:
    """
    Extract errors from Go compiler output.

    Handles:
        ./main.go:12:5: undefined: fmt.Printlnn
        main.go:8:2: imported and not used: "fmt"
        cmd/server.go:25:10: cannot use x (type string) as type int
    """
    matches: list[_RawMatch] = []

    for m in _GO_ERROR.finditer(log):
        raw_path = m.group(1).strip()
        path = normalize_path(raw_path, workspace_path)
        if _should_ignore(path):
            continue

        line_num = int(m.group(2))
        message = m.group(3).strip()

        # Skip non-error lines (Go test output, build info)
        msg_lower = message.lower()
        if any(skip in msg_lower for skip in [
            "ok ", "fail\t", "pass", "---", "===",
            "build constraints", "# command-line",
        ]):
            continue

        # Classify Go-specific error names
        error_name = "CompilationError"
        if "undefined:" in msg_lower:
            error_name = "NameError"
        elif "imported and not used" in msg_lower:
            error_name = "ImportError"
        elif "cannot use" in msg_lower:
            error_name = "TypeError"
        elif "syntax error" in msg_lower:
            error_name = "SyntaxError"
        elif "not enough arguments" in msg_lower or "too many arguments" in msg_lower:
            error_name = "TypeError"
        elif "undeclared" in msg_lower:
            error_name = "NameError"
        elif "redeclared" in msg_lower:
            error_name = "SyntaxError"

        matches.append(_RawMatch(
            file_path=path,
            line_number=line_num,
            error_name=error_name,
            error_message=message,
            confidence=CONF_HIGH,
        ))

    return matches


def _extract_rust_errors(log: str, workspace_path: str) -> list[_RawMatch]:
    """
    Extract errors from Rust / Cargo compiler output.

    Handles:
        error[E0425]: cannot find value `x` in this scope
         --> src/main.rs:5:20
        error: expected one of `!`, `.`, `::`, `;`, `?`
         --> src/lib.rs:10:5
        warning[unused_variables]: unused variable: `x`
         --> src/main.rs:3:9
    """
    matches: list[_RawMatch] = []

    # Find all error headers and their locations
    error_headers = list(_RUST_ERROR_HEADER.finditer(log))
    warning_headers = list(_RUST_WARNING_HEADER.finditer(log))
    locations = list(_RUST_LOCATION.finditer(log))

    # Process errors
    for header in error_headers:
        message = header.group(1).strip()
        header_end = header.end()

        # Skip the "aborting due to X previous errors" summary line
        if "aborting due to" in message.lower():
            continue
        if "could not compile" in message.lower():
            continue

        # Find the nearest --> location after this error header
        best_loc = None
        for loc in locations:
            if loc.start() > header_end:
                # Must be within 300 chars (usually on the next or next-few lines)
                if loc.start() - header_end < 300:
                    best_loc = loc
                break

        if best_loc:
            raw_path = best_loc.group(1).strip()
            path = normalize_path(raw_path, workspace_path)
            if _should_ignore(path):
                continue
            line_num = int(best_loc.group(2))

            # Classify Rust errors
            error_name = "CompilationError"
            msg_lower = message.lower()
            if "cannot find" in msg_lower:
                error_name = "NameError"
            elif "expected" in msg_lower:
                error_name = "SyntaxError"
            elif "mismatched types" in msg_lower:
                error_name = "TypeError"
            elif "unused" in msg_lower:
                error_name = "LintError"
            elif "borrow" in msg_lower or "lifetime" in msg_lower:
                error_name = "TypeError"
            elif "unresolved import" in msg_lower:
                error_name = "ImportError"

            matches.append(_RawMatch(
                file_path=path,
                line_number=line_num,
                error_name=error_name,
                error_message=message,
                confidence=CONF_HIGH,
            ))

    # Process warnings (lower confidence)
    for header in warning_headers:
        message = header.group(1).strip()
        header_end = header.end()

        # Skip summary warnings
        if "generated" in message.lower() and "warning" in message.lower():
            continue

        best_loc = None
        for loc in locations:
            if loc.start() > header_end and loc.start() - header_end < 300:
                best_loc = loc
                break

        if best_loc:
            raw_path = best_loc.group(1).strip()
            path = normalize_path(raw_path, workspace_path)
            if _should_ignore(path):
                continue
            line_num = int(best_loc.group(2))

            error_name = "LintError"
            matches.append(_RawMatch(
                file_path=path,
                line_number=line_num,
                error_name=error_name,
                error_message=message,
                confidence=CONF_MEDIUM,
            ))

    return matches


def _extract_universal_errors(log: str, workspace_path: str) -> list[_RawMatch]:
    """
    Universal catch-all extractor for ANY language.

    Strategy: find lines containing error/FAIL keywords, then look for
    the nearest path:line reference within ±3 lines. This catches:
        - C/C++ compiler errors
        - PHP errors
        - Ruby errors
        - Swift/Kotlin errors
        - Any custom build tool output

    Lower confidence since this is a fuzzy heuristic.
    """
    matches: list[_RawMatch] = []
    lines = log.splitlines()

    # Pre-scan: collect all path:line references with their line indices
    path_refs: list[tuple[int, str, int]] = []  # (line_idx, path, line_num)
    for i, line in enumerate(lines):
        for pm in _UNIVERSAL_PATH_LINE.finditer(line):
            raw_path = pm.group(1)
            if raw_path and not raw_path.startswith("http"):
                path_refs.append((i, raw_path, int(pm.group(2))))

    # Find error lines and pair with nearest path:line reference
    for m in _UNIVERSAL_ERROR_LINE.finditer(log):
        error_line_text = m.group(0).strip()

        # Skip false positives — common non-error lines
        lower = error_line_text.lower()
        if any(skip in lower for skip in [
            "error handling", "error_handler", "onerror",
            "if error", "catch error", "error =",
            "no error", "without error", "0 errors",
            "error free", "fix error", "handle error",
            "error report", "error log", "---",
            "passed", "ok ", "success",
        ]):
            continue

        # Calculate which line index this match is on
        error_line_idx = log[:m.start()].count('\n')

        # Extract error message — strip common prefixes
        message = error_line_text
        for prefix in ["[ERROR]", "[error]", "ERROR:", "error:", "FATAL:", "fatal:", "FAIL:"]:
            if message.startswith(prefix):
                message = message[len(prefix):].strip()
                break

        # Find nearest path:line within ±3 lines
        best_ref = None
        best_dist = 999
        for ref_idx, ref_path, ref_line in path_refs:
            dist = abs(ref_idx - error_line_idx)
            if dist <= 3 and dist < best_dist:
                path = normalize_path(ref_path, workspace_path)
                if not _should_ignore(path):
                    best_ref = (path, ref_line)
                    best_dist = dist

        # Also check if the error line itself contains a path:line
        inline_match = _UNIVERSAL_PATH_LINE.search(error_line_text)
        if inline_match:
            raw_path = inline_match.group(1)
            path = normalize_path(raw_path, workspace_path)
            if not _should_ignore(path):
                best_ref = (path, int(inline_match.group(2)))

        if best_ref:
            matches.append(_RawMatch(
                file_path=best_ref[0],
                line_number=best_ref[1],
                error_name="CompilationError",
                error_message=message,
                confidence=CONF_LOW,
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

        # Always run pytest collection extractor — works for ANY language
        # tested through pytest (Python, JS via plugins, etc.)
        raw_matches.extend(_extract_pytest_collection_errors(full_log, workspace_path))

        # Language-specific extractors for non-Python/Node projects
        if project_type == "java":
            raw_matches.extend(_extract_java_errors(full_log, workspace_path))
        elif project_type == "go":
            raw_matches.extend(_extract_go_errors(full_log, workspace_path))
        elif project_type == "rust":
            raw_matches.extend(_extract_rust_errors(full_log, workspace_path))
        else:
            # Run all language extractors when project type is unknown
            raw_matches.extend(_extract_java_errors(full_log, workspace_path))
            raw_matches.extend(_extract_go_errors(full_log, workspace_path))
            raw_matches.extend(_extract_rust_errors(full_log, workspace_path))

        # Universal catch-all — always runs last (lowest confidence)
        raw_matches.extend(_extract_universal_errors(full_log, workspace_path))

    except Exception as e:
        logger.warning("Error during log extraction: %s", e, exc_info=True)

    # --- Filter out false positives ---
    valid_matches: list[_RawMatch] = []
    for raw in raw_matches:
        # Skip entries with invalid line numbers
        if raw.line_number < 1:
            logger.debug("Skipping match with invalid line_number=%d: %s", raw.line_number, raw.file_path)
            continue
        # Skip entries whose file_path doesn't look like a real file
        # (must contain a dot for extension OR a slash for path separator)
        if "." not in raw.file_path and "/" not in raw.file_path:
            logger.debug("Skipping match with non-file path: %s", raw.file_path)
            continue
        valid_matches.append(raw)
    raw_matches = valid_matches

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

