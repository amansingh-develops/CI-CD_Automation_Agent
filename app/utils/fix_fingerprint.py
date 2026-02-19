"""
Fix Fingerprint Utility
========================
Generates stable fingerprints for fix attempts to detect repeated fixes.

A fingerprint combines:
    - file_path
    - line_number
    - sub_type
    - patch_hash (from diff)

This allows the orchestrator to detect when the same fix is being
retried across iterations, and stop wasting cycles.

Bug Signature:
    file_path + line_number + sub_type
    Identifies the same bug across attempts (ignoring patch content).

Fix Fingerprint:
    bug_signature + patch_hash
    Identifies the exact same fix being proposed again.
"""
import hashlib
from app.models.bug_report import BugReport
from app.utils.patch_hash import compute_patch_hash


def generate_bug_signature(bug_report: BugReport) -> str:
    """
    Generate a stable signature for a bug.

    Identifies the same bug across fix attempts, regardless of
    what patch was proposed.

    Parameters
    ----------
    bug_report : BugReport
        The bug to fingerprint.

    Returns
    -------
    str
        Deterministic bug signature string.
    """
    raw = f"{bug_report.file_path}:{bug_report.line_number}:{bug_report.sub_type}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def generate_fix_fingerprint(bug_report: BugReport, patch_diff: str) -> str:
    """
    Generate a stable fingerprint for a fix attempt.

    Combines bug identity with patch content to detect repeated
    identical fixes across iterations.

    Parameters
    ----------
    bug_report : BugReport
        The bug that was fixed.
    patch_diff : str
        Unified diff of the proposed fix.

    Returns
    -------
    str
        Deterministic fingerprint string. Empty if no diff provided.
    """
    bug_sig = generate_bug_signature(bug_report)
    patch_hash = compute_patch_hash(patch_diff)

    if not patch_hash:
        return bug_sig

    combined = f"{bug_sig}:{patch_hash}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]
