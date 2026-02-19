"""
Patch Hash Utility
==================
Generate deterministic hash from a unified diff string only.

Rules:
    - Hash the diff only, never the full file content.
    - Use SHA-256 truncated to 16 hex chars for compactness.
    - Deterministic: same diff always produces same hash.
    - O(diff size) — no heavy computation.
"""
import hashlib


def compute_patch_hash(diff: str) -> str:
    """
    Generate a deterministic hash from a unified diff.

    Parameters
    ----------
    diff : str
        Unified diff string (output of difflib.unified_diff).

    Returns
    -------
    str
        16-character hex hash of the diff. Empty string if diff is empty.
    """
    if not diff or not diff.strip():
        return ""

    return hashlib.sha256(diff.encode("utf-8")).hexdigest()[:16]
