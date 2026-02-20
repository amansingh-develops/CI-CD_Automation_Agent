"""
Fix Agent Unit Tests
====================
All tests mock the LLM — no real API calls.

Covers:
    - Patch generation structure
    - Diff computation
    - Diff size safety check
    - Confidence gating (above / below threshold)
    - Merge conflict detection
    - Working directory scope
    - Snippet extraction
    - Provider fallback
    - Prompt structure
    - Context level escalation
    - Fix priority ordering
"""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.bug_report import BugReport
from app.models.fix_result import FixResult
from app.agents.fix_agent import FixAgent
from app.llm.client import LLMClient, LLMResponse, parse_llm_response
from app.llm.router import LLMRouter, ProviderConfig, ProviderHealth, decide_context_level
from app.llm.prompts import (
    SYSTEM_PROMPT,
    get_system_prompt,
    build_user_prompt,
    DOMAIN_PROMPTS,
)
from app.parser.classification import BUG_TYPE_PRIORITY


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
    """Create a minimal BugReport for testing."""
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


def _mock_llm_response(
    patched: str = SAMPLE_PATCHED,
    confidence: float = 0.85,
    provider: str = "gemini",
    success: bool = True,
) -> LLMResponse:
    """Create a mock LLMResponse."""
    return LLMResponse(
        patched_content=patched,
        confidence_score=confidence,
        provider_name=provider,
        success=success,
    )


def _run(coro):
    """Run an async coroutine synchronously for testing."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


# ---------------------------------------------------------------------------
# 1. Patch Generation Structure
# ---------------------------------------------------------------------------
class TestPatchGenerationStructure:
    """Verify FixResult has all required fields when a fix is generated."""

    def test_fix_result_has_all_fields(self):
        mock_client = MagicMock(spec=LLMClient)
        mock_client.call_with_fallback = AsyncMock(
            return_value=_mock_llm_response()
        )
        agent = FixAgent(client=mock_client)
        bug = _make_bug()

        result = _run(agent.fix(bug, SAMPLE_FILE))

        assert isinstance(result, FixResult)
        assert result.bug_report == bug
        assert result.original_content == SAMPLE_FILE
        assert result.patched_content == SAMPLE_PATCHED
        assert result.diff != ""
        assert result.confidence == 0.85
        assert result.success is True
        assert result.needs_escalation is False
        assert result.manual_required is False
        assert result.context_level == "medium"
        assert result.provider_used == "gemini"
        assert result.patch_applied != ""
        assert result.error_message == ""

    def test_fix_result_on_llm_failure(self):
        mock_client = MagicMock(spec=LLMClient)
        mock_client.call_with_fallback = AsyncMock(
            return_value=LLMResponse(
                patched_content="",
                confidence_score=0.0,
                provider_name="gemini",
                success=False,
                error="All providers failed",
            )
        )
        agent = FixAgent(client=mock_client)

        result = _run(agent.fix(_make_bug(), SAMPLE_FILE))

        assert result.success is False
        assert result.needs_escalation is True
        assert result.error_message != ""


# ---------------------------------------------------------------------------
# 2. Diff Computation
# ---------------------------------------------------------------------------
class TestDiffComputation:
    """Verify unified diff is computed correctly."""

    def test_diff_shows_change(self):
        diff = FixAgent._compute_diff(SAMPLE_FILE, SAMPLE_PATCHED, "app/main.py")

        assert "--- a/app/main.py" in diff
        assert "+++ b/app/main.py" in diff
        # The fix adds closing parenthesis
        assert '-    print("hello world"' in diff
        assert '+    print("hello world")' in diff

    def test_diff_empty_when_same(self):
        diff = FixAgent._compute_diff(SAMPLE_FILE, SAMPLE_FILE, "app/main.py")
        assert diff == ""


# ---------------------------------------------------------------------------
# 3. Diff Size Check — Pass
# ---------------------------------------------------------------------------
class TestDiffSizeCheckPass:
    """Verify small diffs are accepted."""

    def test_small_diff_accepted(self):
        diff = FixAgent._compute_diff(SAMPLE_FILE, SAMPLE_PATCHED, "test.py")
        assert FixAgent._check_diff_size(diff, threshold=50) is True

    def test_empty_diff_accepted(self):
        assert FixAgent._check_diff_size("", threshold=5) is True


# ---------------------------------------------------------------------------
# 4. Diff Size Check — Reject
# ---------------------------------------------------------------------------
class TestDiffSizeCheckReject:
    """Verify large diffs are rejected."""

    def test_large_diff_rejected(self):
        # Create a diff with many changed lines
        original = "\n".join(f"line {i}" for i in range(60))
        patched = "\n".join(f"changed {i}" for i in range(60))
        diff = FixAgent._compute_diff(original, patched, "test.py")

        assert FixAgent._check_diff_size(diff, threshold=10) is False

    def test_agent_rejects_large_diff(self):
        # Create a patch that changes many lines
        big_patch = "\n".join(f"changed line {i}" for i in range(100))
        mock_client = MagicMock(spec=LLMClient)
        mock_client.call_with_fallback = AsyncMock(
            return_value=_mock_llm_response(patched=big_patch)
        )
        agent = FixAgent(client=mock_client, max_diff_lines=5)

        result = _run(agent.fix(_make_bug(), SAMPLE_FILE))

        assert result.success is False
        assert result.needs_escalation is True
        assert "safety threshold" in result.error_message


# ---------------------------------------------------------------------------
# 5. Confidence Above Threshold
# ---------------------------------------------------------------------------
class TestConfidenceAboveThreshold:
    """Verify high-confidence fixes are accepted."""

    def test_high_confidence_accepted(self):
        mock_client = MagicMock(spec=LLMClient)
        mock_client.call_with_fallback = AsyncMock(
            return_value=_mock_llm_response(confidence=0.9)
        )
        agent = FixAgent(client=mock_client, confidence_threshold=0.6)

        result = _run(agent.fix(_make_bug(), SAMPLE_FILE))

        assert result.success is True
        assert result.needs_escalation is False
        assert result.confidence == 0.9


# ---------------------------------------------------------------------------
# 6. Confidence Below Threshold
# ---------------------------------------------------------------------------
class TestConfidenceBelowThreshold:
    """Verify low-confidence fixes trigger escalation."""

    def test_low_confidence_triggers_escalation(self):
        mock_client = MagicMock(spec=LLMClient)
        mock_client.call_with_fallback = AsyncMock(
            return_value=_mock_llm_response(confidence=0.3)
        )
        agent = FixAgent(client=mock_client, confidence_threshold=0.6)

        result = _run(agent.fix(_make_bug(), SAMPLE_FILE))

        assert result.success is False
        assert result.needs_escalation is True
        assert result.confidence == 0.3

    def test_boundary_confidence_rejected(self):
        """Confidence exactly at threshold-1 is rejected."""
        mock_client = MagicMock(spec=LLMClient)
        mock_client.call_with_fallback = AsyncMock(
            return_value=_mock_llm_response(confidence=0.59)
        )
        agent = FixAgent(client=mock_client, confidence_threshold=0.6)

        result = _run(agent.fix(_make_bug(), SAMPLE_FILE))

        assert result.success is False
        assert result.needs_escalation is True


# ---------------------------------------------------------------------------
# 7. Merge Conflict Detection
# ---------------------------------------------------------------------------
class TestMergeConflictDetection:
    """Verify files with conflict markers are flagged for manual resolution."""

    def test_conflict_markers_detected(self):
        content_with_conflict = (
            "import os\n"
            "<<<<<<< HEAD\n"
            "x = 1\n"
            "=======\n"
            "x = 2\n"
            ">>>>>>> feature\n"
            "print(x)\n"
        )
        agent = FixAgent()

        result = _run(agent.fix(_make_bug(), content_with_conflict))

        assert result.manual_required is True
        assert result.success is False
        assert "conflict" in result.error_message.lower()

    def test_no_conflict_markers(self):
        assert FixAgent._has_conflict_markers(SAMPLE_FILE) is False

    def test_has_conflict_markers(self):
        content = "<<<<<<< HEAD\ncode\n>>>>>>> branch\n"
        assert FixAgent._has_conflict_markers(content) is True


# ---------------------------------------------------------------------------
# 8. Working Directory Scope Respected
# ---------------------------------------------------------------------------
class TestWorkingDirectoryScopeRespected:
    """Verify files outside working directory are rejected."""

    def test_file_outside_scope_rejected(self):
        agent = FixAgent()
        bug = _make_bug(file_path="frontend/src/App.js")

        result = _run(
            agent.fix(bug, SAMPLE_FILE, working_directory="backend")
        )

        assert result.success is False
        assert "outside working directory" in result.error_message

    def test_file_inside_scope_allowed(self):
        mock_client = MagicMock(spec=LLMClient)
        mock_client.call_with_fallback = AsyncMock(
            return_value=_mock_llm_response()
        )
        agent = FixAgent(client=mock_client)
        bug = _make_bug(file_path="backend/app/main.py")

        result = _run(
            agent.fix(bug, SAMPLE_FILE, working_directory="backend")
        )

        assert result.error_message == "" or "outside" not in result.error_message


# ---------------------------------------------------------------------------
# 9. Working Directory — No Scope
# ---------------------------------------------------------------------------
class TestWorkingDirectoryNoScope:
    """Verify all files allowed when no working directory is set."""

    def test_no_scope_allows_any_file(self):
        mock_client = MagicMock(spec=LLMClient)
        mock_client.call_with_fallback = AsyncMock(
            return_value=_mock_llm_response()
        )
        agent = FixAgent(client=mock_client)
        bug = _make_bug(file_path="anywhere/deep/nested/file.py")

        result = _run(agent.fix(bug, SAMPLE_FILE, working_directory=""))

        assert "outside" not in result.error_message

    def test_is_in_scope_no_restriction(self):
        assert FixAgent._is_in_scope("any/file.py", "") is True

    def test_is_in_scope_matching(self):
        assert FixAgent._is_in_scope("backend/app/main.py", "backend") is True

    def test_is_in_scope_not_matching(self):
        assert FixAgent._is_in_scope("frontend/src/App.js", "backend") is False


# ---------------------------------------------------------------------------
# 10. Snippet Extraction
# ---------------------------------------------------------------------------
class TestSnippetExtraction:
    """Verify correct ±3 lines are extracted."""

    def test_snippet_around_line_5(self):
        snippet = FixAgent._extract_snippet(SAMPLE_FILE, line_number=5)
        lines = snippet.strip().splitlines()

        # Should have ±3 lines around line 5 (lines 2-8)
        assert len(lines) >= 5
        # Line 5 should be marked with >>>
        marked = [l for l in lines if l.startswith(">>>")]
        assert len(marked) == 1
        assert "5" in marked[0]

    def test_snippet_at_line_1(self):
        snippet = FixAgent._extract_snippet(SAMPLE_FILE, line_number=1)
        lines = snippet.strip().splitlines()

        # Should start at line 1 (can't go negative)
        assert lines[0].startswith(">>>")
        assert "1" in lines[0]

    def test_snippet_at_end_of_file(self):
        snippet = FixAgent._extract_snippet(SAMPLE_FILE, line_number=100)
        lines = snippet.strip().splitlines()
        assert len(lines) >= 1

    def test_snippet_empty_content(self):
        snippet = FixAgent._extract_snippet("", line_number=1)
        assert snippet == ""


# ---------------------------------------------------------------------------
# 11. Provider Fallback
# ---------------------------------------------------------------------------
class TestProviderFallback:
    """Verify primary fails → fallback provider used."""

    def test_fallback_on_primary_failure(self):
        router = LLMRouter()

        # Make primary (groq) unhealthy
        for _ in range(4):
            router.report_failure("groq")

        provider = router.get_provider()
        assert provider.name == "gemini"

    def test_get_fallback_provider(self):
        router = LLMRouter()
        fallback = router.get_fallback_provider("groq")
        assert fallback is not None
        assert fallback.name == "gemini"

    def test_no_fallback_when_all_unhealthy(self):
        router = LLMRouter()
        for _ in range(4):
            router.report_failure("gemini")
            router.report_failure("groq")
            router.report_failure("openrouter")

        fallback = router.get_fallback_provider("gemini")
        assert fallback is None

    def test_health_reset(self):
        router = LLMRouter()
        for _ in range(4):
            router.report_failure("gemini")

        health = router.get_health("gemini")
        assert health is not None
        assert health.is_healthy is False

        router.reset()
        health = router.get_health("gemini")
        assert health is not None
        assert health.is_healthy is True


# ---------------------------------------------------------------------------
# 12. Prompt Structure
# ---------------------------------------------------------------------------
class TestPromptStructure:
    """Verify prompts contain required rules and structure."""

    def test_system_prompt_has_rules(self):
        assert "Fix ONLY the reported failure" in SYSTEM_PROMPT
        assert "Minimum diff only" in SYSTEM_PROMPT
        assert "Preserve ALL comments" in SYSTEM_PROMPT
        assert "Do NOT refactor" in SYSTEM_PROMPT
        assert "patched_content" in SYSTEM_PROMPT
        assert "confidence_score" in SYSTEM_PROMPT

    def test_system_prompt_domain_augmented(self):
        prompt = get_system_prompt("backend_python")
        assert SYSTEM_PROMPT in prompt
        # May or may not have domain context depending on skill file
        assert len(prompt) >= len(SYSTEM_PROMPT)

    def test_user_prompt_small_context(self):
        prompt = build_user_prompt(
            error_message="SyntaxError: invalid syntax",
            file_path="app/main.py",
            file_snippet=">>>    5 | print('hello'",
            bug_type="SYNTAX",
            context_level="small",
        )
        assert "ERROR TYPE: SYNTAX" in prompt
        assert "app/main.py" in prompt
        assert "SyntaxError" in prompt
        assert "CODE SNIPPET" in prompt
        assert "INSTRUCTIONS" in prompt

    def test_user_prompt_medium_context(self):
        prompt = build_user_prompt(
            error_message="ImportError: no module named foo",
            file_path="app/main.py",
            file_snippet="snippet",
            full_file_content="full file here",
            context_level="medium",
        )
        assert "FULL FILE CONTENT" in prompt
        assert "full file here" in prompt

    def test_user_prompt_large_context(self):
        prompt = build_user_prompt(
            error_message="TypeError",
            file_path="app/main.py",
            file_snippet="snippet",
            full_file_content="full content",
            ci_config_hint="working_directory: backend",
            context_level="large",
        )
        assert "CI CONFIG HINT" in prompt

    def test_user_prompt_previous_attempt(self):
        prompt = build_user_prompt(
            error_message="error",
            file_path="test.py",
            file_snippet="code",
            previous_attempt_info="tried adding import, didn't work",
        )
        assert "PREVIOUS FIX ATTEMPT FAILED" in prompt
        assert "different approach" in prompt


# ---------------------------------------------------------------------------
# 13. Context Level Escalation
# ---------------------------------------------------------------------------
class TestContextLevelEscalation:
    """Verify attempt number drives context size."""

    def test_attempt_1_is_medium(self):
        assert decide_context_level(1) == "medium"

    def test_attempt_2_is_medium(self):
        assert decide_context_level(2) == "medium"

    def test_attempt_3_is_large(self):
        assert decide_context_level(3) == "large"

    def test_attempt_5_is_large(self):
        assert decide_context_level(5) == "large"

    def test_context_level_in_fix_result(self):
        mock_client = MagicMock(spec=LLMClient)
        mock_client.call_with_fallback = AsyncMock(
            return_value=_mock_llm_response()
        )
        agent = FixAgent(client=mock_client)

        result = _run(agent.fix(_make_bug(), SAMPLE_FILE, attempt_number=2))
        assert result.context_level == "medium"


# ---------------------------------------------------------------------------
# 14. Fix Priority Order
# ---------------------------------------------------------------------------
class TestFixPriorityOrder:
    """Verify fix priority matches SYNTAX > IMPORT > TYPE_ERROR > ... > LINTING."""

    def test_priority_ordering(self):
        expected_order = ["SYNTAX", "IMPORT", "TYPE_ERROR", "INDENTATION", "LOGIC", "LINTING"]
        sorted_types = sorted(expected_order, key=lambda t: BUG_TYPE_PRIORITY.get(t, 99))
        assert sorted_types == expected_order

    def test_syntax_highest_priority(self):
        assert BUG_TYPE_PRIORITY["SYNTAX"] < BUG_TYPE_PRIORITY["IMPORT"]
        assert BUG_TYPE_PRIORITY["IMPORT"] < BUG_TYPE_PRIORITY["TYPE_ERROR"]
        assert BUG_TYPE_PRIORITY["TYPE_ERROR"] < BUG_TYPE_PRIORITY["LOGIC"]
        assert BUG_TYPE_PRIORITY["LOGIC"] < BUG_TYPE_PRIORITY["LINTING"]


# ---------------------------------------------------------------------------
# Bonus: LLM Response Parsing
# ---------------------------------------------------------------------------
class TestLLMResponseParsing:
    """Verify parse_llm_response handles various response formats."""

    def test_valid_json(self):
        raw = json.dumps({
            "patched_content": "fixed code",
            "confidence_score": 0.9,
        })
        resp = parse_llm_response(raw, "gemini")
        assert resp.patched_content == "fixed code"
        assert resp.confidence_score == 0.9
        assert resp.success is True

    def test_code_fence_format(self):
        raw = "```python\nprint('fixed')\n```\nCONFIDENCE: 0.95"
        resp = parse_llm_response(raw, "groq")
        assert resp.patched_content == "print('fixed')\n"
        assert resp.confidence_score == 0.95
        assert resp.success is True

    def test_code_fence_no_confidence(self):
        raw = "```python\nprint('fixed')\n```"
        resp = parse_llm_response(raw, "groq")
        assert resp.patched_content == "print('fixed')\n"
        assert resp.confidence_score == 0.7  # Default for fence
        assert resp.success is True

    def test_markdown_fenced_json(self):
        raw = '```json\n{"patched_content": "fixed", "confidence_score": 0.8}\n```'
        resp = parse_llm_response(raw, "groq")
        assert resp.patched_content == "fixed"
        assert resp.confidence_score == 0.8

    def test_empty_response(self):
        resp = parse_llm_response("", "gemini")
        assert resp.success is False
        assert resp.patched_content == ""

    def test_raw_text_fallback(self):
        resp = parse_llm_response("just some code here", "gemini")
        assert resp.patched_content == "just some code here"
        assert resp.confidence_score == 0.3  # Fallback confidence changed to 0.3

    def test_confidence_clamped(self):
        raw = json.dumps({"patched_content": "code", "confidence_score": 5.0})
        resp = parse_llm_response(raw, "gemini")
        assert resp.confidence_score == 1.0  # Clamped to max


# ---------------------------------------------------------------------------
# Bonus: Provider Health
# ---------------------------------------------------------------------------
class TestProviderHealth:
    """Verify provider health tracking."""

    def test_healthy_by_default(self):
        health = ProviderHealth()
        assert health.is_healthy is True
        assert health.consecutive_failures == 0

    def test_unhealthy_after_max_failures(self):
        health = ProviderHealth(max_failures=3)
        health.record_failure()
        health.record_failure()
        assert health.is_healthy is True
        health.record_failure()
        assert health.is_healthy is False

    def test_success_resets_counter(self):
        health = ProviderHealth(max_failures=3)
        health.record_failure()
        health.record_failure()
        health.record_success()
        assert health.consecutive_failures == 0
        assert health.is_healthy is True

    def test_reset_restores_health(self):
        health = ProviderHealth(max_failures=3)
        for _ in range(3):
            health.record_failure()
        assert health.is_healthy is False
        health.reset()
        assert health.is_healthy is True
