"""
Escalation Reasons
==================
Standardised constants for why a fix was rejected or needs escalation.

Used by FixResult.escalation_reason to give the orchestrator and dashboard
clean, machine-readable rejection reasons.
"""


# ---------------------------------------------------------------------------
# Escalation Reason Constants
# ---------------------------------------------------------------------------
DIFF_TOO_LARGE = "DIFF_TOO_LARGE"
LOW_CONFIDENCE = "LOW_CONFIDENCE"
OUT_OF_SCOPE = "OUT_OF_SCOPE"
LOCALITY_VIOLATION = "LOCALITY_VIOLATION"
INVALID_RESPONSE = "INVALID_RESPONSE"
REPEATED_FIX = "REPEATED_FIX"
MERGE_CONFLICT = "MERGE_CONFLICT"
LLM_FAILURE = "LLM_FAILURE"

# All valid reasons (for validation)
ALL_ESCALATION_REASONS = frozenset({
    DIFF_TOO_LARGE,
    LOW_CONFIDENCE,
    OUT_OF_SCOPE,
    LOCALITY_VIOLATION,
    INVALID_RESPONSE,
    REPEATED_FIX,
    MERGE_CONFLICT,
    LLM_FAILURE,
})


# ---------------------------------------------------------------------------
# Error Severity Hints (maps bug_type → severity hint)
# ---------------------------------------------------------------------------
SEVERITY_HINTS = {
    "SYNTAX": "syntax",
    "IMPORT": "import",
    "TYPE_ERROR": "type",
    "LOGIC": "logic",
    "LINTING": "lint",
    "INDENTATION": "syntax",
}


def get_severity_hint(bug_type: str) -> str:
    """
    Map a bug_type to an error severity hint.

    Parameters
    ----------
    bug_type : str
        One of the BUG_TYPES constants.

    Returns
    -------
    str
        Severity hint: "syntax", "import", "type", "logic", or "lint".
    """
    return SEVERITY_HINTS.get(bug_type, "syntax")
