"""
Output Formatter
================
THE SINGLE SOURCE OF TRUTH for all evaluation output strings.

STRICT DETERMINISM CONTRACT:
  - This module NEVER calls an LLM.
  - This module NEVER reads environment variables.
  - This module NEVER modifies wording dynamically.
  - Given the same inputs, it ALWAYS returns the exact same output string.

INTEGRATION CONTRACT (for all upstream layers):
  Callers must supply:
    bug_type   : str  — one of BUG_TYPES (e.g. "LINTING")
    file_path  : str  — relative to repo root (e.g. "src/app.py")
    line_number: int  — exact line number reported by analyser
    sub_type   : str  — key within FIX_TEMPLATES[bug_type]

  The formatter converts:
    sub_type → fix_description → final evaluation string

  The formatter OUTPUTS exactly (byte-for-byte):
    {BUG_TYPE} error in {file_path} line {line_number} → Fix: {fix_description}
"""

# ---------------------------------------------------------------------------
# Unicode Arrow (Critical)
# ---------------------------------------------------------------------------
# U+2192 RIGHTWARDS ARROW.
# NEVER use the ASCII sequence "->".
# NEVER type the arrow character inline elsewhere in any module.
# ALWAYS import this constant from here.
ARROW = "\u2192"


# ---------------------------------------------------------------------------
# Bug Type Constants
# ---------------------------------------------------------------------------
class BugType:
    """Supported bug type identifiers.  Values must remain UPPERCASE strings."""
    LINTING     = "LINTING"
    SYNTAX      = "SYNTAX"
    LOGIC       = "LOGIC"
    TYPE_ERROR  = "TYPE_ERROR"
    IMPORT      = "IMPORT"
    INDENTATION = "INDENTATION"


# Authoritative set used for validation lookups.
BUG_TYPES: set[str] = {
    BugType.LINTING,
    BugType.SYNTAX,
    BugType.LOGIC,
    BugType.TYPE_ERROR,
    BugType.IMPORT,
    BugType.INDENTATION,
}


# ---------------------------------------------------------------------------
# Fix Templates
# ---------------------------------------------------------------------------
# Structure:  FIX_TEMPLATES[bug_type][sub_type] = fix_description
#
# RULES:
#   - All fix_description values MUST be lowercase sentences.
#   - They are matched verbatim by the evaluation harness.
#   - LLM selects the sub_type; this dict selects the description.
#   - Never let an LLM generate the description string directly.
# ---------------------------------------------------------------------------
FIX_TEMPLATES: dict[str, dict[str, str]] = {

    BugType.LINTING: {
        "unused_import":    "remove unused import statement",
        "unused_variable":  "remove or utilise the unused variable",
        "line_too_long":    "shorten line to comply with maximum line length",
        "missing_whitespace": "add required whitespace around operator",
        "trailing_whitespace": "remove trailing whitespace from line",
        "multiple_statements": "split multiple statements onto separate lines",
    },

    BugType.SYNTAX: {
        "missing_colon":    "add missing colon at end of statement",
        "missing_bracket":  "add missing closing bracket",
        "missing_parenthesis": "add missing closing parenthesis",
        "unexpected_indent": "remove unexpected indentation",
        "invalid_syntax":   "correct invalid syntax on reported line",
    },

    BugType.LOGIC: {
        "wrong_operator":   "replace operator with correct logical operator",
        "wrong_condition":  "correct boolean condition to match intended logic",
        "off_by_one":       "adjust loop or index boundary by one",
        "unreachable_code": "remove or reposition unreachable code block",
        "infinite_loop":    "add correct termination condition to loop",
    },

    BugType.TYPE_ERROR: {
        "type_mismatch":    "cast variable to the expected type",
        "none_reference":   "add none check before accessing attribute",
        "wrong_return_type": "update return value to match declared type",
        "incompatible_types": "align variable types to resolve incompatibility",
    },

    BugType.IMPORT: {
        "missing_import":   "add missing import statement at top of file",
        "wrong_path":       "correct the import path to match module location",
        "circular_import":  "refactor to remove circular import dependency",
        "relative_import":  "convert to absolute import path",
    },

    BugType.INDENTATION: {
        "wrong_indent":     "fix indentation to use consistent spaces",
        "mixed_indent":     "convert mixed tabs and spaces to spaces only",
        "over_indent":      "reduce indentation to match surrounding block level",
        "under_indent":     "increase indentation to match surrounding block level",
    },
}


# ---------------------------------------------------------------------------
# Validation Helpers
# ---------------------------------------------------------------------------
def validate_bug_type(bug_type: str) -> None:
    """
    Raises ValueError if bug_type is not a recognised BUG_TYPES member.
    Call before passing bug_type into format_output().
    """
    if not isinstance(bug_type, str):
        raise TypeError(f"bug_type must be str, got {type(bug_type).__name__}")
    if bug_type not in BUG_TYPES:
        raise ValueError(
            f"Unknown bug_type '{bug_type}'. "
            f"Allowed values: {sorted(BUG_TYPES)}"
        )


def validate_line_number(line_number: int) -> None:
    """
    Raises ValueError if line_number is not a positive integer.
    """
    if not isinstance(line_number, int):
        raise TypeError(f"line_number must be int, got {type(line_number).__name__}")
    if line_number < 1:
        raise ValueError(f"line_number must be >= 1, got {line_number}")


def validate_file_path(file_path: str) -> None:
    """
    Raises ValueError if file_path is empty or not a string.
    Only lightweight checks — does NOT touch the filesystem.
    """
    if not isinstance(file_path, str):
        raise TypeError(f"file_path must be str, got {type(file_path).__name__}")
    if not file_path.strip():
        raise ValueError("file_path must not be empty or whitespace-only")


def validate_sub_type(bug_type: str, sub_type: str) -> None:
    """
    Raises ValueError if sub_type is not a recognised key for the given bug_type.
    Validates bug_type first, then checks FIX_TEMPLATES[bug_type] for the sub_type.
    """
    validate_bug_type(bug_type)
    if not isinstance(sub_type, str):
        raise TypeError(f"sub_type must be str, got {type(sub_type).__name__}")
    type_map = FIX_TEMPLATES.get(bug_type, {})
    if sub_type not in type_map:
        raise ValueError(
            f"Unknown sub_type '{sub_type}' for bug_type '{bug_type}'. "
            f"Allowed sub_types: {sorted(type_map.keys())}"
        )


# ---------------------------------------------------------------------------
# Template Resolver
# ---------------------------------------------------------------------------
def resolve_fix_description(bug_type: str, sub_type: str) -> str:
    """
    Look up the fix description for a given (bug_type, sub_type) pair.

    Parameters
    ----------
    bug_type : str
        One of the BUG_TYPES constants (e.g. "LINTING").
    sub_type : str
        Key within FIX_TEMPLATES[bug_type] (e.g. "unused_import").

    Returns
    -------
    str
        The canonical lowercase fix description string.

    Raises
    ------
    ValueError
        If bug_type or sub_type is not found in FIX_TEMPLATES.

    Notes
    -----
    This function is the gatekeeper that prevents LLM-hallucinated
    free-text descriptions from reaching the output string.
    """
    validate_bug_type(bug_type)

    type_map = FIX_TEMPLATES.get(bug_type)
    # Guard: every BugType entry must exist in FIX_TEMPLATES
    if type_map is None:  # pragma: no cover
        raise ValueError(f"FIX_TEMPLATES has no entry for bug_type '{bug_type}'")

    description = type_map.get(sub_type)
    if description is None:
        raise ValueError(
            f"Unknown sub_type '{sub_type}' for bug_type '{bug_type}'. "
            f"Allowed sub_types: {sorted(type_map.keys())}"
        )

    return description


# ---------------------------------------------------------------------------
# Core Format Function
# ---------------------------------------------------------------------------
def format_output(
    bug_type: str,
    file_path: str,
    line_number: int,
    fix_description: str,
) -> str:
    """
    Generate the canonical evaluation output string.

    Output format (byte-perfect):
        {BUG_TYPE} error in {file_path} line {line_number} → Fix: {fix_description}

    Rules enforced here:
      - "error in"  is always lowercase.
      - "line"      is always lowercase.
      - "Fix:"      has a capital F and a colon — no deviation allowed.
      - Single spaces only between every token.
      - No trailing whitespace.
      - No newline character appended.
      - ARROW constant (U+2192) is used — never the ASCII sequence "->".

    Parameters
    ----------
    bug_type        : str  — must be a BUG_TYPES member.
    file_path       : str  — relative path from repo root.
    line_number     : int  — must be >= 1.
    fix_description : str  — canonical lowercase phrase (from resolve_fix_description).

    Returns
    -------
    str
        The formatted evaluation string, ready for output.
    """
    # Run all validators before touching the template.
    validate_bug_type(bug_type)
    validate_file_path(file_path)
    validate_line_number(line_number)

    if not isinstance(fix_description, str) or not fix_description.strip():
        raise ValueError("fix_description must be a non-empty string")

    return (
        f"{bug_type} error in {file_path} line {line_number}"
        f" {ARROW} Fix: {fix_description}"
    )


# ---------------------------------------------------------------------------
# Convenience: End-to-End Helper
# ---------------------------------------------------------------------------
def format_bug(
    bug_type: str,
    file_path: str,
    line_number: int,
    sub_type: str,
) -> str:
    """
    Convenience wrapper that resolves sub_type → description and formats output.

    Upstream agents should call THIS function, not format_output() directly.

    Parameters
    ----------
    bug_type    : str — one of BUG_TYPES.
    file_path   : str — relative path from repo root.
    line_number : int — must be >= 1.
    sub_type    : str — key into FIX_TEMPLATES[bug_type].

    Returns
    -------
    str
        Final evaluation string.
    """
    fix_description = resolve_fix_description(bug_type, sub_type)
    return format_output(bug_type, file_path, line_number, fix_description)
