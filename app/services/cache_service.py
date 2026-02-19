"""
Cache Service
=============
Lightweight caching for deterministic items to reduce redundant computation.

Cacheable (deterministic):
    - LLM prompt templates (resolved per bug type)
    - LLM fix responses (keyed by file + line + error hash)
    - Project detection results (same repo → same type)

NOT Cacheable (non-deterministic):
    - Execution logs (change after each fix iteration)
    - Build exit codes (depend on current code state)
    - CI pipeline status (external system state)

Cache invalidation:
    - Cache is scoped to a single agent run
    - Cache cleared on new run-agent invocation
    - No persistent cross-run caching in v1

Fix History Cache (Step 5.1):
    - Stores bug_signature → last_patch_fingerprint mappings
    - In-memory only (no persistence required yet)
    - Allows orchestrator to detect repeated ineffective fixes
    - Cleared on new agent run via clear()
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class FixHistoryCache:
    """
    In-memory cache tracking fix attempts by bug signature.

    Stores:
        bug_signature → last_patch_fingerprint

    Usage:
        cache = FixHistoryCache()
        cache.record("sig123", "fp456")
        if cache.is_repeated("sig123", "fp456"):
            # Same fix was tried before
            ...
    """

    def __init__(self) -> None:
        # bug_signature → set of patch_fingerprints tried
        self._history: dict[str, set[str]] = {}

    def record(self, bug_signature: str, patch_fingerprint: str) -> None:
        """
        Record a fix attempt.

        Parameters
        ----------
        bug_signature : str
            Bug identity hash (file_path + line + sub_type).
        patch_fingerprint : str
            Fix fingerprint (bug_sig + patch_hash).
        """
        if not bug_signature or not patch_fingerprint:
            return
        if bug_signature not in self._history:
            self._history[bug_signature] = set()
        self._history[bug_signature].add(patch_fingerprint)

    def is_repeated(self, bug_signature: str, patch_fingerprint: str) -> bool:
        """
        Check if this exact fix was already tried.

        Parameters
        ----------
        bug_signature : str
            Bug identity hash.
        patch_fingerprint : str
            Fix fingerprint to check.

        Returns
        -------
        bool
            True if this exact fingerprint was seen for this bug before.
        """
        if not bug_signature or not patch_fingerprint:
            return False
        return patch_fingerprint in self._history.get(bug_signature, set())

    def get_attempt_count(self, bug_signature: str) -> int:
        """
        Get number of distinct fix attempts for a bug.

        Parameters
        ----------
        bug_signature : str
            Bug identity hash.

        Returns
        -------
        int
            Number of distinct patches tried for this bug.
        """
        return len(self._history.get(bug_signature, set()))

    def get_last_fingerprint(self, bug_signature: str) -> Optional[str]:
        """
        Get the most recently recorded fingerprint for a bug.

        Returns None if no attempts recorded.
        """
        fingerprints = self._history.get(bug_signature)
        if not fingerprints:
            return None
        # Sets are unordered but we return any element — caller should use is_repeated()
        return next(iter(fingerprints))

    def clear(self) -> None:
        """Clear all history for a new agent run."""
        self._history.clear()
        logger.debug("Fix history cache cleared")

    def __len__(self) -> int:
        """Total number of unique bug signatures tracked."""
        return len(self._history)
