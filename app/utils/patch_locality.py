"""
Patch Locality Validator
========================
Validates that a patch only modifies lines within a configurable window
around the reported failing line.

Prevents unrelated edits by rejecting patches that touch code far
from the error location.

Performance: O(diff size) — no AST parsing, no extra LLM calls.
"""
import re
import logging

logger = logging.getLogger(__name__)

# Regex to extract line numbers from unified diff hunk headers
# Format: @@ -start,count +start,count @@
_HUNK_HEADER_RE = re.compile(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', re.MULTILINE)


def validate_patch_locality(
    original_content: str,
    patched_content: str,
    failing_line: int,
    diff: str = "",
    window: int = 5,
) -> tuple[bool, str]:
    """
    Check that patch modifications are within ±window of the failing line.

    Parameters
    ----------
    original_content : str
        Original file content.
    patched_content : str
        Patched file content.
    failing_line : int
        1-based line number of the reported failure.
    diff : str
        Pre-computed unified diff (optional; computed from contents if empty).
    window : int
        Number of lines above/below the failing line to allow changes (default: 5).

    Returns
    -------
    tuple[bool, str]
        (is_valid, reason) — True if all changes within window, else False with reason.
    """
    if not diff:
        # Compute diff if not provided
        import difflib
        original_lines = original_content.splitlines(keepends=True)
        patched_lines = patched_content.splitlines(keepends=True)
        diff = "\n".join(difflib.unified_diff(
            original_lines, patched_lines,
            fromfile="a/file", tofile="b/file", lineterm="",
        ))

    if not diff.strip():
        return True, "No changes detected"

    # Extract changed line numbers from diff
    changed_lines: list[int] = []
    current_old_line = 0
    current_new_line = 0

    for line in diff.splitlines():
        hunk_match = _HUNK_HEADER_RE.match(line)
        if hunk_match:
            current_old_line = int(hunk_match.group(1))
            current_new_line = int(hunk_match.group(2))
            continue

        if line.startswith("---") or line.startswith("+++"):
            continue

        if line.startswith("-"):
            changed_lines.append(current_old_line)
            current_old_line += 1
        elif line.startswith("+"):
            changed_lines.append(current_new_line)
            current_new_line += 1
        else:
            current_old_line += 1
            current_new_line += 1

    if not changed_lines:
        return True, "No line changes detected"

    # Check all changed lines are within ±window of failing_line
    min_allowed = max(1, failing_line - window)
    max_allowed = failing_line + window

    out_of_bounds = [ln for ln in changed_lines if ln < min_allowed or ln > max_allowed]

    if out_of_bounds:
        reason = (
            f"Patch modifies lines {out_of_bounds} which are outside "
            f"±{window} window of failing line {failing_line} "
            f"(allowed: {min_allowed}–{max_allowed})"
        )
        logger.warning(reason)
        return False, reason

    return True, "All changes within locality window"
