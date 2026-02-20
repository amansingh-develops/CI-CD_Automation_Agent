"""
Fix Agent
=========
Generates minimal code fixes using LLM based on detected bug reports.
Uses unified LLM client with Gemini/Groq providers.

Core Philosophy:
    - Fix only the reported failure
    - Minimum diff only
    - Preserve comments
    - Do not refactor unrelated code
    - Do not change function signatures unless required by error

Safety Rules:
    - Never modify CI pipeline files automatically
    - Never insert destructive shell commands
    - Never change dependency versions broadly
    - Detect merge conflict markers → flag for human, never auto-fix
    - Respect CI-config working directory scope

Stability Safeguards (Step 5.1):
    - Fix fingerprinting to detect repeated identical fixes
    - Patch locality validation to reject unrelated edits
    - Standardised escalation reasons for clean orchestrator decisions
    - Error severity hints for future confidence threshold tuning

The FixAgent does NOT:
    - Format output strings (that's output_formatter's job)
    - Run builds (that's executor's job)
    - Commit changes (that's git_agent's job)
    - Interpret CI YAML deeply (that's ci_config_reader's job)

FixAgent only proposes code patches.
"""
import difflib
import logging
import re
import asyncio
from typing import Optional

from app.models.bug_report import BugReport
from app.models.fix_result import FixResult
from app.llm.client import LLMClient, LLMResponse
from app.llm.router import LLMRouter, decide_context_level
from app.llm.prompts import get_system_prompt, build_user_prompt
from app.utils.fix_fingerprint import generate_bug_signature, generate_fix_fingerprint
from app.utils.patch_locality import validate_patch_locality
from app.utils.escalation_reasons import (
    DIFF_TOO_LARGE,
    LOW_CONFIDENCE,
    OUT_OF_SCOPE,
    LOCALITY_VIOLATION,
    INVALID_RESPONSE,
    REPEATED_FIX,
    MERGE_CONFLICT,
    LLM_FAILURE,
    get_severity_hint,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Merge Conflict Markers
# ---------------------------------------------------------------------------
_CONFLICT_MARKER_RE = re.compile(r'^(<{7}|>{7})\s', re.MULTILINE)


# ---------------------------------------------------------------------------
# Fix Agent
# ---------------------------------------------------------------------------
class FixAgent:
    """
    Generates minimal code patches from BugReports using an LLM.

    Parameters
    ----------
    confidence_threshold : float
        Minimum confidence score to accept a fix (default: 0.6).
    max_diff_lines : int
        Maximum number of changed lines allowed in a patch (default: 50).
    locality_window : int
        Maximum lines ± from error line where changes are allowed (default: 20).
    router : LLMRouter or None
        Provider router (auto-created if not provided).
    client : LLMClient or None
        HTTP client (auto-created if not provided).
    """

    def __init__(
        self,
        confidence_threshold: float = 0.0,
        max_diff_lines: int = 50,
        locality_window: int = 20,
        router: Optional[LLMRouter] = None,
        client: Optional[LLMClient] = None,
    ) -> None:
        self.confidence_threshold = confidence_threshold
        self.max_diff_lines = max_diff_lines
        self.locality_window = locality_window
        self.router = router or LLMRouter()
        self.client = client or LLMClient()
        # In-memory set of previously seen fix fingerprints (for repeat detection)
        self._seen_fingerprints: set[str] = set()

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------
    async def fix(
        self,
        bug_report: BugReport,
        file_content: str,
        error_message: str = "",
        test_name: str = "",
        previous_attempt_info: str = "",
        working_directory: str = "",
        attempt_number: int = 1,
        full_file_content: str = "",
        related_file_content: str = "",
        ci_config_hint: str = "",
    ) -> FixResult:
        """
        Generate a minimal code fix for the given BugReport.

        Steps:
            1. Compute bug_signature and error_severity_hint
            2. Check for merge conflict markers → manual_required
            3. Check working directory scope → reject if out of scope
            4. Extract snippet (±3 lines)
            5. Build prompt
            6. Call LLM with provider fallback
            7. Parse response (check validation_error)
            8. Compute diff
            9. Safety: reject if diff too large
            10. Safety: patch locality validation
            11. Fingerprint the fix, detect repeats
            12. Confidence gate
            13. Return FixResult (does NOT write files)

        Parameters
        ----------
        bug_report : BugReport
            The bug to fix.
        file_content : str
            Full content of the failing file.
        error_message : str
            Error message from build log. Falls back to bug_report.message.
        test_name : str
            Optional failing test name.
        previous_attempt_info : str
            Info about a prior failed fix attempt.
        working_directory : str
            CI-config working directory scope (empty = no restriction).
        attempt_number : int
            1-based attempt number (drives context escalation).
        full_file_content : str
            Full file content for medium/large context.
        related_file_content : str
            Related file content for medium/large context.
        ci_config_hint : str
            CI config hint for large context.

        Returns
        -------
        FixResult
            The fix result with patch, diff, confidence, and safety flags.
        """
        effective_error = error_message or bug_report.message

        # --- Step 1: Compute signatures and hints ---
        bug_sig = generate_bug_signature(bug_report)
        severity_hint = get_severity_hint(bug_report.bug_type)

        # --- Step 2: Merge conflict check ---
        if self._has_conflict_markers(file_content):
            logger.warning("Merge conflict markers detected in %s", bug_report.file_path)
            return FixResult(
                bug_report=bug_report,
                original_content=file_content,
                manual_required=True,
                error_message="Merge conflict markers detected — manual resolution required",
                bug_signature=bug_sig,
                escalation_reason=MERGE_CONFLICT,
                error_severity_hint=severity_hint,
            )

        # --- Step 3: Working directory scope check ---
        if not self._is_in_scope(bug_report.file_path, working_directory):
            logger.info(
                "File %s outside working directory scope %s, skipping",
                bug_report.file_path, working_directory,
            )
            return FixResult(
                bug_report=bug_report,
                original_content=file_content,
                error_message=(
                    f"File {bug_report.file_path} is outside working directory "
                    f"scope '{working_directory}'"
                ),
                bug_signature=bug_sig,
                escalation_reason=OUT_OF_SCOPE,
                error_severity_hint=severity_hint,
            )

        # --- Step 4: Extract snippet ---
        snippet = self._extract_snippet(file_content, bug_report.line_number, context=10)

        # --- Step 5: Determine context level ---
        # If the file is small, always provide the full content even in attempt 1
        num_lines = len(file_content.splitlines())
        if num_lines < 300:
            context_level = "medium"
        else:
            context_level = decide_context_level(attempt_number)

        # --- Step 6: Build prompt ---
        system_prompt = get_system_prompt(bug_report.domain)
        user_prompt = build_user_prompt(
            error_message=effective_error,
            file_path=bug_report.file_path,
            file_snippet=snippet,
            bug_type=bug_report.bug_type,
            sub_type=bug_report.sub_type,
            test_name=test_name,
            previous_attempt_info=previous_attempt_info,
            context_level=context_level,
            full_file_content=full_file_content or file_content,
            related_file_content=related_file_content,
            ci_config_hint=ci_config_hint,
        )

        # --- Step 7: Call LLM ---
        # Add a short sleep to stay under free-tier TPM limits when multiple bugs are processed
        await asyncio.sleep(3.0)
        try:
            llm_response: LLMResponse = await self.client.call_with_fallback(
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                router=self.router,
            )
        except Exception as e:
            logger.error("LLM call failed: %s", e, exc_info=True)
            return FixResult(
                bug_report=bug_report,
                original_content=file_content,
                context_level=context_level,
                error_message=f"LLM call failed: {e}",
                bug_signature=bug_sig,
                escalation_reason=LLM_FAILURE,
                error_severity_hint=severity_hint,
            )

        # --- Step 8: Check LLM response ---
        if not llm_response.success or not llm_response.patched_content:
            return FixResult(
                bug_report=bug_report,
                original_content=file_content,
                context_level=context_level,
                provider_used=llm_response.provider_name,
                error_message=llm_response.error or "LLM returned empty response",
                needs_escalation=True,
                bug_signature=bug_sig,
                escalation_reason=INVALID_RESPONSE if llm_response.validation_error else LLM_FAILURE,
                error_severity_hint=severity_hint,
            )

        patched = llm_response.patched_content

        # --- Step 9: Compute diff ---
        diff = self._compute_diff(file_content, patched, bug_report.file_path)

        # --- Step 10: Safety check — diff size ---
        if not self._check_diff_size(diff, self.max_diff_lines):
            logger.warning(
                "Diff too large for %s, rejecting patch",
                bug_report.file_path,
            )
            return FixResult(
                bug_report=bug_report,
                original_content=file_content,
                patched_content=patched,
                diff=diff,
                confidence=llm_response.confidence_score,
                context_level=context_level,
                provider_used=llm_response.provider_name,
                error_message="Patch diff exceeds safety threshold",
                needs_escalation=True,
                bug_signature=bug_sig,
                escalation_reason=DIFF_TOO_LARGE,
                error_severity_hint=severity_hint,
            )

        # --- Step 11: Patch locality validation ---
        # If the file is small, give the LLM more room (effectively disable window check)
        effective_window = 100 if len(file_content.splitlines()) < 100 else self.locality_window
        
        locality_ok, locality_reason = validate_patch_locality(
            original_content=file_content,
            patched_content=patched,
            failing_line=bug_report.line_number,
            diff=diff,
            window=effective_window,
        )
        if not locality_ok:
            logger.warning("Locality violation in %s: %s", bug_report.file_path, locality_reason)
            return FixResult(
                bug_report=bug_report,
                original_content=file_content,
                patched_content=patched,
                diff=diff,
                confidence=llm_response.confidence_score,
                context_level=context_level,
                provider_used=llm_response.provider_name,
                error_message=locality_reason,
                needs_escalation=True,
                bug_signature=bug_sig,
                escalation_reason=LOCALITY_VIOLATION,
                error_severity_hint=severity_hint,
            )

        # --- Step 12: Fingerprint and repeat detection ---
        fingerprint = generate_fix_fingerprint(bug_report, diff)
        is_repeat = fingerprint in self._seen_fingerprints
        if fingerprint:
            self._seen_fingerprints.add(fingerprint)

        if is_repeat:
            logger.warning("Repeated fix detected for %s (fingerprint: %s)", bug_report.file_path, fingerprint)
            return FixResult(
                bug_report=bug_report,
                original_content=file_content,
                patched_content=patched,
                diff=diff,
                confidence=llm_response.confidence_score,
                context_level=context_level,
                provider_used=llm_response.provider_name,
                error_message="Repeated fix detected — same patch was proposed before",
                needs_escalation=True,
                previous_attempt_detected=True,
                bug_signature=bug_sig,
                patch_fingerprint=fingerprint,
                escalation_reason=REPEATED_FIX,
                error_severity_hint=severity_hint,
            )

        # --- Step 13: Confidence gating ---
        below_threshold = llm_response.confidence_score < self.confidence_threshold

        return FixResult(
            bug_report=bug_report,
            original_content=file_content,
            patched_content=patched,
            patch_applied=f"Fix {bug_report.bug_type}/{bug_report.sub_type} in {bug_report.file_path}",
            diff=diff,
            confidence=llm_response.confidence_score,
            success=not below_threshold,
            needs_escalation=below_threshold,
            context_level=context_level,
            provider_used=llm_response.provider_name,
            bug_signature=bug_sig,
            patch_fingerprint=fingerprint,
            escalation_reason=LOW_CONFIDENCE if below_threshold else "",
            error_severity_hint=severity_hint,
        )

    def clear_fingerprints(self) -> None:
        """Clear the seen fingerprints set (for new agent run)."""
        self._seen_fingerprints.clear()

    def has_seen_fingerprint(self, fingerprint: str) -> bool:
        """Check if a fingerprint has been seen before (for testing)."""
        return fingerprint in self._seen_fingerprints

    # -------------------------------------------------------------------
    # Internal Helpers
    # -------------------------------------------------------------------
    @staticmethod
    def _extract_snippet(content: str, line_number: int, context: int = 3) -> str:
        """
        Extract ±context lines around the given line number.

        Parameters
        ----------
        content : str
            Full file content.
        line_number : int
            1-based line number of the error.
        context : int
            Number of lines to include above and below (default: 3).

        Returns
        -------
        str
            The extracted snippet with line numbers prefixed.
        """
        lines = content.splitlines()
        if not lines:
            return ""

        # Clamp to valid range (1-based → 0-based)
        idx = max(0, min(line_number - 1, len(lines) - 1))
        start = max(0, idx - context)
        end = min(len(lines), idx + context + 1)

        snippet_lines: list[str] = []
        for i in range(start, end):
            line_num = i + 1
            prefix = ">>>" if line_num == line_number else "   "
            snippet_lines.append(f"{prefix} {line_num:4} | {lines[i]}")

        return "\n".join(snippet_lines)

    @staticmethod
    def _compute_diff(original: str, patched: str, file_path: str) -> str:
        """
        Compute a unified diff between original and patched content.

        Parameters
        ----------
        original : str
            Original file content.
        patched : str
            Patched file content.
        file_path : str
            File path for diff headers.

        Returns
        -------
        str
            Unified diff string.
        """
        original_lines = original.splitlines(keepends=True)
        patched_lines = patched.splitlines(keepends=True)

        diff = difflib.unified_diff(
            original_lines,
            patched_lines,
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            lineterm="",
        )
        return "\n".join(diff)

    @staticmethod
    def _check_diff_size(diff: str, threshold: int) -> bool:
        """
        Check if the diff is within the safety threshold.

        Parameters
        ----------
        diff : str
            Unified diff string.
        threshold : int
            Maximum number of added/removed lines allowed.

        Returns
        -------
        bool
            True if diff is within threshold, False if too large.
        """
        if not diff:
            return True

        changed_lines = 0
        for line in diff.splitlines():
            if line.startswith("---") or line.startswith("+++"):
                continue
            if line.startswith("+") or line.startswith("-"):
                changed_lines += 1

        return changed_lines <= threshold

    @staticmethod
    def _has_conflict_markers(content: str) -> bool:
        """
        Check if file content contains Git merge conflict markers.

        Parameters
        ----------
        content : str
            File content to scan.

        Returns
        -------
        bool
            True if conflict markers found.
        """
        return bool(_CONFLICT_MARKER_RE.search(content))

    @staticmethod
    def _is_in_scope(file_path: str, working_directory: str) -> bool:
        """
        Check if a file path is within the CI-config working directory scope.

        Parameters
        ----------
        file_path : str
            Repo-relative file path (forward slashes).
        working_directory : str
            Working directory scope from CI config. Empty = no restriction.

        Returns
        -------
        bool
            True if file is in scope (or no scope restriction).
        """
        if not working_directory:
            return True

        # Normalise paths
        normalized_file = file_path.replace("\\", "/").strip("/")
        normalized_scope = working_directory.replace("\\", "/").strip("/")

        return normalized_file.startswith(normalized_scope)

    async def close(self) -> None:
        """Clean up the LLM client."""
        if self.client:
            await self.client.close()
