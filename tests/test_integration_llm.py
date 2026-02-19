"""
Integration Test — Real LLM Backend Flow
==========================================
Tests the FULL backend pipeline with real LLM API calls.

Flow tested:
    1. Raw build log → Failure Parser → BugReports
    2. BugReport → FixAgent → real Gemini/Groq call → FixResult
    3. Verify FixResult has valid patch, confidence, diff, fingerprint

Run with:
    python -m pytest tests/test_integration_llm.py -v -s

Uses REAL API keys from .env — requires network access.
"""
import asyncio
import os
import sys
import logging
import pytest

# Ensure backend is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.parser.failure_parser import parse_failure_log
from app.agents.fix_agent import FixAgent
from app.llm.client import LLMClient
from app.llm.router import LLMRouter, GEMINI_CONFIG, GROQ_CONFIG
from app.core.config import GEMINI_API_KEY, GROQ_API_KEY
from app.models.fix_result import FixResult

logging.basicConfig(level=logging.INFO, format="%(name)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Skip if no API keys configured
# ---------------------------------------------------------------------------
HAS_GEMINI = bool(GEMINI_API_KEY and GEMINI_API_KEY.strip())
HAS_GROQ = bool(GROQ_API_KEY and GROQ_API_KEY.strip())
HAS_ANY_KEY = HAS_GEMINI or HAS_GROQ

skip_no_keys = pytest.mark.skipif(
    not HAS_ANY_KEY,
    reason="No LLM API keys found in .env — skipping integration tests",
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Test Fixtures: Simulated build logs (Standard Python Traceback format)
# ---------------------------------------------------------------------------
PYTHON_SYNTAX_LOG = """
$ python -m pytest tests/ -v
FAILED tests/test_app.py::test_hello - SyntaxError

=== FAILURES ===
__________________________________ test_hello __________________________________

    def test_hello():
>       result = hello()

  File "app/main.py", line 5
    print("hello world"
                       ^
SyntaxError: invalid syntax

=== short test summary info ===
FAILED tests/test_app.py::test_hello
=== 1 failed in 0.12s ===
"""

PYTHON_IMPORT_LOG = """
$ python -m pytest tests/ -v
ERROR collecting tests/test_utils.py

ImportError while importing test module 'tests/test_utils.py'
  File "app/utils/helpers.py", line 3, in <module>
    from app.services.missing_module import do_stuff
ModuleNotFoundError: No module named 'app.services.missing_module'

=== short test summary info ===
ERROR tests/test_utils.py
=== 1 error in 0.05s ===
"""

PYTHON_TYPE_ERROR_LOG = """
$ python -m pytest tests/ -v
FAILED tests/test_calc.py::test_add

    def test_add():
>       result = add(1, "two")

  File "app/calc.py", line 10, in add
    return a + b
TypeError: unsupported operand type(s) for +: 'int' and 'str'

=== 1 failed in 0.08s ===
"""

# File content that goes with the syntax error
SYNTAX_ERROR_FILE = """\
import os
import sys

def hello():
    print("hello world"
    return 0

def goodbye():
    print("goodbye")
    return 1
"""

IMPORT_ERROR_FILE = """\
import os
import sys

from app.services.missing_module import do_stuff

def helper():
    return do_stuff()
"""

TYPE_ERROR_FILE = """\
import os

def add(a, b):
    \"\"\"Add two numbers.\"\"\"
    return a + b

def subtract(a, b):
    return a - b

def multiply(a, b):
    return a * b
"""


# ===========================================================================
# Test 1: Full Flow — Syntax Error
# ===========================================================================
@skip_no_keys
class TestFullFlowSyntaxError:
    """Parse a Python syntax error log → fix it with real LLM."""

    def test_parse_and_fix_syntax_error(self):
        async def run_test():
            # Step 1: Parse the log
            bugs = parse_failure_log(PYTHON_SYNTAX_LOG)
            logger.info("Parsed %d bugs from syntax error log", len(bugs))
            assert len(bugs) >= 1

            bug = bugs[0]
            logger.info(
                "Bug attributes: %s", dir(bug)
            )
            logger.info(
                "Bug: %s/%s in %s:%d (domain: %s)",
                bug.bug_type, bug.sub_type, bug.file_path, bug.line_number, getattr(bug, "domain", "MISSING")
            )
            assert bug.file_path == "app/main.py"
            assert bug.bug_type == "SYNTAX"

            # Step 2: Fix it with real LLM
            client = LLMClient()
            agent = FixAgent(client=client, locality_window=10)
            try:
                result: FixResult = await agent.fix(
                    bug_report=bug,
                    file_content=SYNTAX_ERROR_FILE,
                    attempt_number=1,
                )
                
                # Step 3: Validate result
                logger.info("=== FixResult ===")
                logger.info("  success: %s", result.success)
                logger.info("  confidence: %.2f", result.confidence)
                logger.info("  provider: %s", result.provider_used)
                logger.info("  context_level: %s", result.context_level)
                logger.info("  bug_signature: %s", result.bug_signature)
                logger.info("  patch_fingerprint: %s", result.patch_fingerprint)
                
                assert result.provider_used in ("gemini", "groq"), "Provider should be gemini or groq"
                assert result.confidence > 0.0, "Confidence should be non-zero"
                assert result.patched_content != "", "Patched content should not be empty"
                assert ")" in result.patched_content
            finally:
                await agent.close()

        asyncio.run(run_test())


# ===========================================================================
# Test 2: Full Flow — Import Error
# ===========================================================================
@skip_no_keys
class TestFullFlowImportError:
    """Parse a Python import error log → fix it with real LLM."""

    def test_parse_and_fix_import_error(self):
        async def run_test():
            bugs = parse_failure_log(PYTHON_IMPORT_LOG)
            assert len(bugs) >= 1
            bug = bugs[0]

            client = LLMClient()
            agent = FixAgent(client=client, locality_window=10)
            try:
                result = await agent.fix(
                    bug_report=bug,
                    file_content=IMPORT_ERROR_FILE,
                    attempt_number=1,
                )
                assert result.provider_used in ("gemini", "groq")
                assert result.patched_content != ""
                assert result.error_severity_hint == "import"
            finally:
                await agent.close()

        asyncio.run(run_test())


# ===========================================================================
# Test 3: Full Flow — Type Error
# ===========================================================================
@skip_no_keys
class TestFullFlowTypeError:
    """Parse a Python type error log → fix it with real LLM."""

    def test_parse_and_fix_type_error(self):
        async def run_test():
            bugs = parse_failure_log(PYTHON_TYPE_ERROR_LOG)
            assert len(bugs) >= 1
            bug = bugs[0]

            client = LLMClient()
            agent = FixAgent(client=client, locality_window=10)
            try:
                result = await agent.fix(
                    bug_report=bug,
                    file_content=TYPE_ERROR_FILE,
                    attempt_number=1,
                )
                assert result.provider_used in ("gemini", "groq")
                assert result.patched_content != ""
            finally:
                await agent.close()

        asyncio.run(run_test())


# ===========================================================================
# Test 4: Provider Fallback — Live
# ===========================================================================
@skip_no_keys
class TestProviderFallbackLive:
    """Verify the router correctly selects a provider and returns a response."""

    def test_router_selects_working_provider(self):
        router = LLMRouter()
        provider = router.get_provider()
        logger.info("Router selected provider: %s (model: %s)", provider.name, provider.model)
        assert provider.name in ("gemini", "groq")


# ===========================================================================
# Test 5: Context Escalation — Medium context with real LLM
# ===========================================================================
@skip_no_keys
class TestContextEscalationLive:
    """Verify medium context (attempt 2) sends full file to LLM."""

    def test_medium_context_attempt(self):
        async def run_test():
            bugs = parse_failure_log(PYTHON_SYNTAX_LOG)
            bug = bugs[0]

            client = LLMClient()
            agent = FixAgent(client=client, locality_window=10)
            try:
                result = await agent.fix(
                    bug_report=bug,
                    file_content=SYNTAX_ERROR_FILE,
                    attempt_number=2,  # Medium context
                )
                assert result.context_level == "medium"
                assert result.patched_content != ""
            finally:
                await agent.close()

        asyncio.run(run_test())


# ===========================================================================
# Test 6: Stability — Fingerprint consistency
# ===========================================================================
@skip_no_keys
class TestFingerprintConsistencyLive:
    """Verify bug signatures are stable across real LLM calls."""

    def test_bug_signature_stable(self):
        async def run_test():
            bugs = parse_failure_log(PYTHON_SYNTAX_LOG)
            bug = bugs[0]

            client = LLMClient()
            agent = FixAgent(client=client, locality_window=10)
            try:
                r1 = await agent.fix(bug_report=bug, file_content=SYNTAX_ERROR_FILE)
                # Bug signature should be deterministic regardless of LLM response
                from app.utils.fix_fingerprint import generate_bug_signature
                expected_sig = generate_bug_signature(bug)
                assert r1.bug_signature == expected_sig
            finally:
                await agent.close()

        asyncio.run(run_test())


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "--tb=short"])
