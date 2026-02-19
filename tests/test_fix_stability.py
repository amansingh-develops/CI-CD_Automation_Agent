"""
Fix Stability Safeguard Tests
==============================
Tests for Step 5.1 stability safeguards.

Covers:
    - Fix fingerprint: same bug+patch → identical fingerprint
    - Patch locality: changes outside ±5 window → rejected
    - Strict JSON validation: missing fields → rejected (no auto-repair)
    - Escalation reasons: set correctly for each rejection path
    - Patch hash: deterministic, diff-only
    - Bug signature: stable across attempts
    - Fix history cache: record + detect repeats
    - Severity hints: mapped correctly

No real LLM calls.
"""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.models.bug_report import BugReport
from app.models.fix_result import FixResult
from app.agents.fix_agent import FixAgent
from app.llm.client import (
    LLMClient,
    LLMResponse,
    parse_llm_response,
    validate_llm_response_strict,
)
from app.utils.patch_hash import compute_patch_hash
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
    ALL_ESCALATION_REASONS,
    get_severity_hint,
)
from app.services.cache_service import FixHistoryCache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_bug(
    bug_type: str = "SYNTAX",
    sub_type: str = "invalid_syntax",
    file_path: str = "app/main.py",
    line_number: int = 5,
    domain: str = "backend_python",
    message: str = "SyntaxError: invalid syntax",
) -> BugReport:
    return BugReport(
        bug_type=bug_type,
        sub_type=sub_type,
        file_path=file_path,
        line_number=line_number,
        domain=domain,
        message=message,
    )


SAMPLE_FILE = """\
import os
import sys

def hello():
    print("hello world"
    return 0

def goodbye():
    print("goodbye")
    return 1
"""

SAMPLE_PATCHED = """\
import os
import sys

def hello():
    print("hello world")
    return 0

def goodbye():
    print("goodbye")
    return 1
"""

# Patch that modifies lines far from line 5
SAMPLE_PATCHED_FAR = """\
import os
import sys

def hello():
    print("hello world"
    return 0

def goodbye():
    print("goodbye, world!")
    return 42
"""


def _mock_response(
    patched: str = SAMPLE_PATCHED,
    confidence: float = 0.85,
    provider: str = "gemini",
) -> LLMResponse:
    return LLMResponse(
        patched_content=patched,
        confidence_score=confidence,
        provider_name=provider,
        success=True,
    )


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ===========================================================================
# 1. Fix Fingerprint Tests
# ===========================================================================
class TestFixFingerprint:
    """Same bug+patch → identical fingerprint, different patch → different."""

    def test_same_bug_same_diff_identical_fingerprint(self):
        bug = _make_bug()
        diff = "--- a/test.py\n+++ b/test.py\n- old\n+ new"
        fp1 = generate_fix_fingerprint(bug, diff)
        fp2 = generate_fix_fingerprint(bug, diff)
        assert fp1 == fp2
        assert len(fp1) == 16

    def test_same_bug_different_diff_different_fingerprint(self):
        bug = _make_bug()
        fp1 = generate_fix_fingerprint(bug, "diff A")
        fp2 = generate_fix_fingerprint(bug, "diff B")
        assert fp1 != fp2

    def test_different_bug_same_diff(self):
        bug_a = _make_bug(line_number=5)
        bug_b = _make_bug(line_number=10)
        diff = "same diff"
        fp1 = generate_fix_fingerprint(bug_a, diff)
        fp2 = generate_fix_fingerprint(bug_b, diff)
        assert fp1 != fp2

    def test_empty_diff_returns_bug_sig(self):
        bug = _make_bug()
        fp = generate_fix_fingerprint(bug, "")
        sig = generate_bug_signature(bug)
        assert fp == sig

    def test_fingerprint_exposed_in_fix_result(self):
        mock_client = MagicMock(spec=LLMClient)
        mock_client.call_with_fallback = AsyncMock(return_value=_mock_response())
        agent = FixAgent(client=mock_client, locality_window=100)
        result = _run(agent.fix(_make_bug(), SAMPLE_FILE))
        assert result.patch_fingerprint != ""
        assert result.bug_signature != ""


# ===========================================================================
# 2. Bug Signature Tests
# ===========================================================================
class TestBugSignature:
    """Bug signature stable across attempts."""

    def test_stable_across_calls(self):
        bug = _make_bug()
        sig1 = generate_bug_signature(bug)
        sig2 = generate_bug_signature(bug)
        assert sig1 == sig2

    def test_different_line_different_sig(self):
        sig1 = generate_bug_signature(_make_bug(line_number=5))
        sig2 = generate_bug_signature(_make_bug(line_number=10))
        assert sig1 != sig2

    def test_different_subtype_different_sig(self):
        sig1 = generate_bug_signature(_make_bug(sub_type="invalid_syntax"))
        sig2 = generate_bug_signature(_make_bug(sub_type="missing_colon"))
        assert sig1 != sig2


# ===========================================================================
# 3. Patch Locality — Pass
# ===========================================================================
class TestPatchLocalityPass:
    """Changes within ±5 of failing line → accepted."""

    def test_change_at_failing_line_accepted(self):
        # SAMPLE_PATCHED changes line 5 (adding closing paren), failing_line=5
        ok, reason = validate_patch_locality(
            SAMPLE_FILE, SAMPLE_PATCHED, failing_line=5, window=5,
        )
        assert ok is True

    def test_empty_diff_accepted(self):
        ok, _ = validate_patch_locality(SAMPLE_FILE, SAMPLE_FILE, failing_line=5)
        assert ok is True


# ===========================================================================
# 4. Patch Locality — Reject
# ===========================================================================
class TestPatchLocalityReject:
    """Changes outside ±5 of failing line → rejected."""

    def test_far_changes_rejected(self):
        # SAMPLE_PATCHED_FAR modifies lines 9-10, failing_line=5, window=2
        ok, reason = validate_patch_locality(
            SAMPLE_FILE, SAMPLE_PATCHED_FAR, failing_line=5, window=2,
        )
        assert ok is False
        assert "outside" in reason

    def test_agent_rejects_locality_violation(self):
        mock_client = MagicMock(spec=LLMClient)
        mock_client.call_with_fallback = AsyncMock(
            return_value=_mock_response(patched=SAMPLE_PATCHED_FAR)
        )
        agent = FixAgent(client=mock_client, locality_window=2)
        result = _run(agent.fix(_make_bug(line_number=5), SAMPLE_FILE))
        assert result.success is False
        assert result.needs_escalation is True
        assert result.escalation_reason == LOCALITY_VIOLATION


# ===========================================================================
# 5. Strict JSON Validation — Valid
# ===========================================================================
class TestStrictJsonValid:
    """All fields present → accepted."""

    def test_valid_strict_response(self):
        raw = json.dumps({
            "patched_content": "fixed code",
            "confidence_score": 0.9,
            "fix_reason": "Added missing parenthesis",
        })
        resp = validate_llm_response_strict(raw, "gemini")
        assert resp.success is True
        assert resp.patched_content == "fixed code"
        assert resp.confidence_score == 0.9
        assert resp.fix_reason == "Added missing parenthesis"
        assert resp.validation_error == ""


# ===========================================================================
# 6. Strict JSON Validation — Missing Fields
# ===========================================================================
class TestStrictJsonMissingFields:
    """Missing required fields → rejected, not auto-repaired."""

    def test_missing_patched_content(self):
        raw = json.dumps({"confidence_score": 0.9, "fix_reason": "reason"})
        resp = validate_llm_response_strict(raw, "gemini")
        assert resp.success is False
        assert "patched_content" in resp.validation_error

    def test_missing_confidence(self):
        raw = json.dumps({"patched_content": "code", "fix_reason": "reason"})
        resp = validate_llm_response_strict(raw, "gemini")
        assert resp.success is False
        assert "confidence_score" in resp.validation_error

    def test_missing_fix_reason(self):
        raw = json.dumps({"patched_content": "code", "confidence_score": 0.8})
        resp = validate_llm_response_strict(raw, "gemini")
        assert resp.success is False
        assert "fix_reason" in resp.validation_error

    def test_all_missing(self):
        raw = json.dumps({"unrelated_key": "value"})
        resp = validate_llm_response_strict(raw, "gemini")
        assert resp.success is False
        assert "patched_content" in resp.validation_error
        assert "confidence_score" in resp.validation_error
        assert "fix_reason" in resp.validation_error

    def test_invalid_json(self):
        resp = validate_llm_response_strict("not json at all", "gemini")
        assert resp.success is False
        assert "not valid JSON" in resp.validation_error

    def test_empty_response(self):
        resp = validate_llm_response_strict("", "gemini")
        assert resp.success is False
        assert resp.validation_error != ""

    def test_empty_patched_content(self):
        raw = json.dumps({"patched_content": "", "confidence_score": 0.8, "fix_reason": "r"})
        resp = validate_llm_response_strict(raw, "gemini")
        assert resp.success is False
        assert "patched_content" in resp.validation_error


# ===========================================================================
# 7. Escalation Reason Constants
# ===========================================================================
class TestEscalationReasons:
    """Escalation reasons set correctly for each rejection path."""

    def test_merge_conflict_reason(self):
        agent = FixAgent()
        content = "<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> branch\n"
        result = _run(agent.fix(_make_bug(), content))
        assert result.escalation_reason == MERGE_CONFLICT

    def test_out_of_scope_reason(self):
        agent = FixAgent()
        result = _run(agent.fix(
            _make_bug(file_path="frontend/app.js"),
            SAMPLE_FILE,
            working_directory="backend",
        ))
        assert result.escalation_reason == OUT_OF_SCOPE

    def test_diff_too_large_reason(self):
        big_patch = "\n".join(f"changed {i}" for i in range(100))
        mock_client = MagicMock(spec=LLMClient)
        mock_client.call_with_fallback = AsyncMock(
            return_value=_mock_response(patched=big_patch)
        )
        agent = FixAgent(client=mock_client, max_diff_lines=2, locality_window=200)
        result = _run(agent.fix(_make_bug(), SAMPLE_FILE))
        assert result.escalation_reason == DIFF_TOO_LARGE

    def test_low_confidence_reason(self):
        mock_client = MagicMock(spec=LLMClient)
        mock_client.call_with_fallback = AsyncMock(
            return_value=_mock_response(confidence=0.3)
        )
        agent = FixAgent(client=mock_client, confidence_threshold=0.6, locality_window=100)
        result = _run(agent.fix(_make_bug(), SAMPLE_FILE))
        assert result.escalation_reason == LOW_CONFIDENCE

    def test_success_has_no_escalation_reason(self):
        mock_client = MagicMock(spec=LLMClient)
        mock_client.call_with_fallback = AsyncMock(
            return_value=_mock_response(confidence=0.9)
        )
        agent = FixAgent(client=mock_client, locality_window=100)
        result = _run(agent.fix(_make_bug(), SAMPLE_FILE))
        assert result.escalation_reason == ""
        assert result.success is True

    def test_all_reasons_are_strings(self):
        for reason in ALL_ESCALATION_REASONS:
            assert isinstance(reason, str)
            assert reason == reason.upper()


# ===========================================================================
# 8. Patch Hash Tests
# ===========================================================================
class TestPatchHash:
    """Deterministic hash from diff only."""

    def test_deterministic(self):
        diff = "--- a/f.py\n+++ b/f.py\n-old\n+new"
        h1 = compute_patch_hash(diff)
        h2 = compute_patch_hash(diff)
        assert h1 == h2
        assert len(h1) == 16

    def test_different_diff_different_hash(self):
        h1 = compute_patch_hash("diff A")
        h2 = compute_patch_hash("diff B")
        assert h1 != h2

    def test_empty_diff_returns_empty(self):
        assert compute_patch_hash("") == ""
        assert compute_patch_hash("   ") == ""


# ===========================================================================
# 9. Severity Hints
# ===========================================================================
class TestSeverityHints:
    """Bug type → severity hint mapping."""

    def test_syntax(self):
        assert get_severity_hint("SYNTAX") == "syntax"

    def test_import(self):
        assert get_severity_hint("IMPORT") == "import"

    def test_type_error(self):
        assert get_severity_hint("TYPE_ERROR") == "type"

    def test_logic(self):
        assert get_severity_hint("LOGIC") == "logic"

    def test_linting(self):
        assert get_severity_hint("LINTING") == "lint"

    def test_unknown_defaults_to_syntax(self):
        assert get_severity_hint("UNKNOWN") == "syntax"

    def test_severity_in_fix_result(self):
        mock_client = MagicMock(spec=LLMClient)
        mock_client.call_with_fallback = AsyncMock(return_value=_mock_response())
        agent = FixAgent(client=mock_client, locality_window=100)
        result = _run(agent.fix(_make_bug(bug_type="IMPORT"), SAMPLE_FILE))
        assert result.error_severity_hint == "import"


# ===========================================================================
# 10. Fix History Cache
# ===========================================================================
class TestFixHistoryCache:
    """In-memory fix history cache."""

    def test_record_and_detect_repeat(self):
        cache = FixHistoryCache()
        cache.record("sig1", "fp1")
        assert cache.is_repeated("sig1", "fp1") is True
        assert cache.is_repeated("sig1", "fp2") is False

    def test_different_sig_not_repeated(self):
        cache = FixHistoryCache()
        cache.record("sig1", "fp1")
        assert cache.is_repeated("sig2", "fp1") is False

    def test_attempt_count(self):
        cache = FixHistoryCache()
        cache.record("sig1", "fp1")
        cache.record("sig1", "fp2")
        cache.record("sig1", "fp3")
        assert cache.get_attempt_count("sig1") == 3

    def test_clear(self):
        cache = FixHistoryCache()
        cache.record("sig1", "fp1")
        cache.clear()
        assert cache.is_repeated("sig1", "fp1") is False
        assert len(cache) == 0

    def test_empty_inputs_ignored(self):
        cache = FixHistoryCache()
        cache.record("", "")
        assert len(cache) == 0
        assert cache.is_repeated("", "") is False


# ===========================================================================
# 11. Repeat Detection in FixAgent
# ===========================================================================
class TestRepeatDetection:
    """Same fix proposed twice → second is rejected."""

    def test_same_patch_twice_detected(self):
        mock_client = MagicMock(spec=LLMClient)
        mock_client.call_with_fallback = AsyncMock(return_value=_mock_response())
        agent = FixAgent(client=mock_client, locality_window=100)

        # First attempt succeeds
        r1 = _run(agent.fix(_make_bug(), SAMPLE_FILE))
        assert r1.success is True

        # Second identical attempt detected as repeat
        r2 = _run(agent.fix(_make_bug(), SAMPLE_FILE))
        assert r2.success is False
        assert r2.previous_attempt_detected is True
        assert r2.escalation_reason == REPEATED_FIX

    def test_clear_fingerprints_allows_retry(self):
        mock_client = MagicMock(spec=LLMClient)
        mock_client.call_with_fallback = AsyncMock(return_value=_mock_response())
        agent = FixAgent(client=mock_client, locality_window=100)

        _run(agent.fix(_make_bug(), SAMPLE_FILE))
        agent.clear_fingerprints()

        r2 = _run(agent.fix(_make_bug(), SAMPLE_FILE))
        assert r2.success is True
        assert r2.previous_attempt_detected is False


# ===========================================================================
# 12. FixResult New Fields
# ===========================================================================
class TestFixResultNewFields:
    """Verify all new stability fields are present and populated."""

    def test_default_values(self):
        bug = _make_bug()
        result = FixResult(bug_report=bug)
        assert result.bug_signature == ""
        assert result.patch_fingerprint == ""
        assert result.previous_attempt_detected is False
        assert result.escalation_reason == ""
        assert result.error_severity_hint == ""

    def test_fields_populated_on_success(self):
        mock_client = MagicMock(spec=LLMClient)
        mock_client.call_with_fallback = AsyncMock(return_value=_mock_response())
        agent = FixAgent(client=mock_client, locality_window=100)
        result = _run(agent.fix(_make_bug(), SAMPLE_FILE))
        assert result.bug_signature != ""
        assert result.patch_fingerprint != ""
        assert result.error_severity_hint == "syntax"
