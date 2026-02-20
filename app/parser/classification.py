"""
Classification
==============
Maps raw error strings to the six allowed bug types and their sub_types.

Allowed Bug Types:
    LINTING, SYNTAX, LOGIC, TYPE_ERROR, IMPORT, INDENTATION

Classification Strategy:
    1. EXPLICIT TABLE FIRST — keyword / exact-name lookup (fast path)
    2. REGEX PATTERNS SECOND — for fuzzy or multi-word matches
    3. NEVER dynamic inference or LLM

Sub_types MUST match FIX_TEMPLATES keys in output_formatter.py exactly.
"""
import re
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Classification Result
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ClassificationResult:
    """Immutable result of classifying an error message."""
    bug_type: str
    sub_type: str
    confidence: float  # 0.0–1.0
    domain: str = "generic"


# ---------------------------------------------------------------------------
# Confidence Constants
# ---------------------------------------------------------------------------
CONF_HIGH = 0.95
CONF_MEDIUM = 0.75
CONF_LOW = 0.50


# ---------------------------------------------------------------------------
# 1. Explicit Keyword Table (fastest path)
# ---------------------------------------------------------------------------
# Maps lowercased error-class names → (bug_type, sub_type, confidence, domain)
_KEYWORD_MAP: dict[str, tuple[str, str, float, str]] = {
    # Python exceptions
    "syntaxerror":         ("SYNTAX",      "invalid_syntax",  CONF_HIGH,   "backend_python"),
    "indentationerror":    ("INDENTATION", "wrong_indent",    CONF_HIGH,   "backend_python"),
    "taberror":            ("INDENTATION", "mixed_indent",    CONF_HIGH,   "backend_python"),
    "importerror":         ("IMPORT",      "missing_import",  CONF_HIGH,   "backend_python"),
    "modulenotfounderror": ("IMPORT",      "missing_import",  CONF_HIGH,   "backend_python"),
    "typeerror":           ("TYPE_ERROR",  "type_mismatch",   CONF_HIGH,   "backend_python"),
    "attributeerror":      ("TYPE_ERROR",  "none_reference",  CONF_HIGH,   "backend_python"),
    "nameerror":           ("IMPORT",      "missing_import",  CONF_MEDIUM, "backend_python"),
    "assertionerror":      ("LOGIC",       "wrong_condition", CONF_HIGH,   "backend_python"),
    "indexerror":          ("LOGIC",       "off_by_one",      CONF_MEDIUM, "backend_python"),
    "keyerror":            ("LOGIC",       "wrong_condition", CONF_MEDIUM, "backend_python"),
    "valueerror":          ("TYPE_ERROR",  "type_mismatch",   CONF_MEDIUM, "backend_python"),
    "recursionerror":      ("LOGIC",       "infinite_loop",   CONF_HIGH,   "backend_python"),
    "zerodivisionerror":   ("LOGIC",       "wrong_operator",  CONF_MEDIUM, "backend_python"),

    # JS / Node exceptions
    "referenceerror":      ("IMPORT",      "missing_import",  CONF_HIGH,   "frontend_js"),
    "rangeerror":          ("LOGIC",       "off_by_one",      CONF_MEDIUM, "frontend_js"),

    # Language-agnostic / multi-language error names
    "compilationerror":    ("SYNTAX",      "invalid_syntax",  CONF_MEDIUM, "generic"),
    "linterror":           ("LINTING",     "lint_violation",   CONF_HIGH,   "generic"),
    "linkerror":           ("IMPORT",      "missing_import",  CONF_MEDIUM, "generic"),
}


# ---------------------------------------------------------------------------
# 2. Regex Patterns (second pass)
# ---------------------------------------------------------------------------
# Each entry: (compiled_regex, bug_type, sub_type, confidence, domain)
_REGEX_PATTERNS: list[tuple[re.Pattern, str, str, float, str]] = [
    # Python-style
    (re.compile(r"unexpected indent",          re.I), "INDENTATION", "over_indent",        CONF_HIGH,   "backend_python"),
    (re.compile(r"expected an indented block",  re.I), "INDENTATION", "under_indent",       CONF_HIGH,   "backend_python"),
    (re.compile(r"unindent does not match",    re.I), "INDENTATION", "wrong_indent",       CONF_HIGH,   "backend_python"),
    (re.compile(r"invalid syntax",             re.I), "SYNTAX",      "invalid_syntax",     CONF_HIGH,   "backend_python"),
    (re.compile(r"expected ':'",               re.I), "SYNTAX",      "missing_colon",      CONF_HIGH,   "backend_python"),
    (re.compile(r"missing [\)\]}>]",           re.I), "SYNTAX",      "missing_bracket",    CONF_MEDIUM, "backend_python"),
    (re.compile(r"unterminated.*paren",        re.I), "SYNTAX",      "missing_parenthesis", CONF_HIGH,   "backend_python"),
    (re.compile(r"no module named",            re.I), "IMPORT",      "missing_import",     CONF_HIGH,   "backend_python"),
    (re.compile(r"cannot find module",         re.I), "IMPORT",      "wrong_path",         CONF_HIGH,   "frontend_js"),
    (re.compile(r"module not found",           re.I), "IMPORT",      "missing_import",     CONF_HIGH,   "backend_python"),
    (re.compile(r"circular import",            re.I), "IMPORT",      "circular_import",    CONF_HIGH,   "backend_python"),
    (re.compile(r"relative import",            re.I), "IMPORT",      "relative_import",    CONF_MEDIUM, "backend_python"),
    (re.compile(r"is not defined",             re.I), "IMPORT",      "missing_import",     CONF_MEDIUM, "backend_python"),
    (re.compile(r"has no attribute",           re.I), "TYPE_ERROR",  "none_reference",     CONF_MEDIUM, "backend_python"),
    (re.compile(r"NoneType",                   re.I), "TYPE_ERROR",  "none_reference",     CONF_HIGH,   "backend_python"),
    (re.compile(r"incompatible type",          re.I), "TYPE_ERROR",  "incompatible_types", CONF_HIGH,   "backend_python"),
    (re.compile(r"cannot assign.*to",          re.I), "TYPE_ERROR",  "type_mismatch",      CONF_MEDIUM, "backend_python"),
    (re.compile(r"expected.*got",              re.I), "TYPE_ERROR",  "type_mismatch",      CONF_LOW,    "backend_python"),
    (re.compile(r"unreachable code",           re.I), "LOGIC",       "unreachable_code",   CONF_HIGH,   "backend_python"),
    (re.compile(r"assert(ion)?\s+(failed|error)", re.I), "LOGIC",   "wrong_condition",    CONF_HIGH,   "backend_python"),
    (re.compile(r"unused import",              re.I), "LINTING",     "unused_import",      CONF_HIGH,   "backend_python"),
    (re.compile(r"imported but unused",        re.I), "LINTING",     "unused_import",      CONF_HIGH,   "backend_python"),
    (re.compile(r"unused variable",            re.I), "LINTING",     "unused_variable",    CONF_HIGH,   "backend_python"),
    (re.compile(r"never used",                 re.I), "LINTING",     "unused_variable",    CONF_MEDIUM, "backend_python"),
    (re.compile(r"line too long",              re.I), "LINTING",     "line_too_long",      CONF_HIGH,   "backend_python"),
    (re.compile(r"trailing whitespace",        re.I), "LINTING",     "trailing_whitespace", CONF_HIGH,   "backend_python"),
    (re.compile(r"missing whitespace",         re.I), "LINTING",     "missing_whitespace", CONF_HIGH,   "backend_python"),
    (re.compile(r"multiple statements",        re.I), "LINTING",     "multiple_statements", CONF_HIGH,   "backend_python"),
]


# ---------------------------------------------------------------------------
# Bug Type Priority (for sorting)
# ---------------------------------------------------------------------------
BUG_TYPE_PRIORITY: dict[str, int] = {
    "SYNTAX":      0,
    "IMPORT":      1,
    "TYPE_ERROR":  2,
    "INDENTATION": 3,
    "LOGIC":       4,
    "LINTING":     5,
}


def priority_of(bug_type: str) -> int:
    """Return sort priority for a bug type (lower = higher priority)."""
    return BUG_TYPE_PRIORITY.get(bug_type, 99)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def classify_error(error_name: str, error_message: str = "") -> ClassificationResult:
    """
    Classify an error into (bug_type, sub_type, confidence, domain).

    Strategy:
        1. Normalize error_name to lowercase and look up in keyword table
        2. Scan error_message against regex patterns
        3. Fallback to generic SYNTAX / invalid_syntax with low confidence

    Parameters
    ----------
    error_name : str
        The exception class name or error label (e.g. "SyntaxError").
    error_message : str
        The full error message text for regex matching.

    Returns
    -------
    ClassificationResult
        Frozen dataclass with bug_type, sub_type, confidence, domain.
    """
    # --- Pass 1: keyword table ---
    key = error_name.strip().lower().replace(" ", "")
    if key in _KEYWORD_MAP:
        bt, st, conf, dom = _KEYWORD_MAP[key]
        return ClassificationResult(bug_type=bt, sub_type=st, confidence=conf, domain=dom)

    # --- Pass 2: regex on combined text ---
    combined = f"{error_name} {error_message}"
    for pattern, bt, st, conf, dom in _REGEX_PATTERNS:
        if pattern.search(combined):
            return ClassificationResult(bug_type=bt, sub_type=st, confidence=conf, domain=dom)

    # --- Pass 3: generic keyword fallback ---
    if key == "error":
        return ClassificationResult("SYNTAX", "invalid_syntax", CONF_LOW, "generic")
    if key == "warning":
        return ClassificationResult("LINTING", "unused_variable", CONF_LOW, "generic")

    # --- Final fallback ---
    return ClassificationResult(
        bug_type="SYNTAX",
        sub_type="invalid_syntax",
        confidence=CONF_LOW,
        domain="generic",
    )

