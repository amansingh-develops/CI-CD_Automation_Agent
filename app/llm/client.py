"""
LLM Client
==========
Unified asynchronous client wrapper for LLM providers.
Supports Gemini (primary) and Groq (fallback) providers.

Adaptive Context Strategy:
    - Start with SMALL context window by default (error + ±3 lines)
    - Escalate to MEDIUM if first fix attempt fails (error + full function)
    - Escalate to LARGE only as last resort (error + file + related files)
    - Smaller context = faster responses + lower cost + more precise fixes

Provider Fallback:
    - Primary: Google Gemini (via REST API)
    - Fallback: Groq (OpenAI-compatible endpoint)
    - Fallback triggers on: HTTP error, timeout, rate limit, empty response
    - Each provider has independent retry logic (max 2 retries per provider)

Confidence Gating:
    - LLM self-reports a confidence_score (0–1) with each fix
    - Fixes below the threshold are REJECTED before commit
    - Threshold is tunable via config (default: 0.6)
    - Low-confidence fixes trigger context escalation on next attempt

Minimal Diff Philosophy:
    - Prompt engineering enforces "fix only the broken lines ±3"
    - LLM must not refactor, rename, or reorganise unrelated code
    - LLM generates code changes ONLY — never evaluation output strings
"""
import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx

from app.llm.router import ProviderConfig, LLMRouter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM Response
# ---------------------------------------------------------------------------
@dataclass
class LLMResponse:
    """Parsed response from an LLM provider."""
    patched_content: str
    confidence_score: float
    provider_name: str
    raw_response: str = ""
    success: bool = True
    error: str = ""
    fix_reason: str = ""
    validation_error: str = ""


# ---------------------------------------------------------------------------
# Response Parsing
# ---------------------------------------------------------------------------
def parse_llm_response(raw: str, provider_name: str) -> LLMResponse:
    """
    Parse the LLM response JSON to extract patched_content and confidence_score.

    Falls back to treating the entire response as patched content if JSON
    parsing fails, with a reduced confidence of 0.5.

    Parameters
    ----------
    raw : str
        Raw text response from the LLM.
    provider_name : str
        Name of the provider that generated this response.

    Returns
    -------
    LLMResponse
        Parsed response with patched_content and confidence_score.
    """
    if not raw or not raw.strip():
        return LLMResponse(
            patched_content="",
            confidence_score=0.0,
            provider_name=provider_name,
            raw_response=raw,
            success=False,
            error="Empty response from LLM",
        )

    # Try to parse as JSON
    cleaned = raw.strip()

    # Strip markdown code fences if present
    if cleaned.startswith("```"):
        # Remove opening fence (```json or ```)
        first_newline = cleaned.find("\n")
        if first_newline != -1:
            cleaned = cleaned[first_newline + 1:]
        # Remove closing fence
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3].rstrip()

    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            patched = data.get("patched_content", "")
            confidence = float(data.get("confidence_score", 0.5))
            confidence = max(0.0, min(1.0, confidence))  # Clamp
            return LLMResponse(
                patched_content=patched,
                confidence_score=confidence,
                provider_name=provider_name,
                raw_response=raw,
            )
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    # Try regex extraction as last resort
    patched_match = re.search(
        r'"patched_content"\s*:\s*"((?:[^"\\]|\\.)*)"\s*',
        cleaned,
        re.DOTALL,
    )
    confidence_match = re.search(
        r'"confidence_score"\s*:\s*([\d.]+)',
        cleaned,
    )

    if patched_match:
        patched = patched_match.group(1)
        # Unescape JSON string escapes
        try:
            patched = json.loads(f'"{patched}"')
        except Exception:
            pass
        confidence = 0.5
        if confidence_match:
            try:
                confidence = max(0.0, min(1.0, float(confidence_match.group(1))))
            except ValueError:
                confidence = 0.5
        return LLMResponse(
            patched_content=patched,
            confidence_score=confidence,
            provider_name=provider_name,
            raw_response=raw,
        )

    # Final fallback: treat entire response as patched content
    logger.warning("Could not parse LLM JSON response, using raw text as patch")
    return LLMResponse(
        patched_content=cleaned,
        confidence_score=0.5,
        provider_name=provider_name,
        raw_response=raw,
    )


def validate_llm_response_strict(raw: str, provider_name: str) -> LLMResponse:
    """
    Strictly validate an LLM response against the required schema.

    Required fields:
        - patched_content (non-empty string)
        - confidence_score (float 0.0–1.0)
        - fix_reason (non-empty string)

    If any field is missing, the response is REJECTED — no auto-repair.

    Parameters
    ----------
    raw : str
        Raw response text from LLM.
    provider_name : str
        Provider that generated the response.

    Returns
    -------
    LLMResponse
        Validated response. On failure: success=False, validation_error set.
    """
    if not raw or not raw.strip():
        return LLMResponse(
            patched_content="",
            confidence_score=0.0,
            provider_name=provider_name,
            raw_response=raw,
            success=False,
            error="Empty response",
            validation_error="Empty response from LLM",
        )

    cleaned = raw.strip()

    # Strip markdown code fences
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        if first_newline != -1:
            cleaned = cleaned[first_newline + 1:]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3].rstrip()

    # Parse JSON
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as e:
        return LLMResponse(
            patched_content="",
            confidence_score=0.0,
            provider_name=provider_name,
            raw_response=raw,
            success=False,
            error=f"Invalid JSON: {e}",
            validation_error=f"Response is not valid JSON: {e}",
        )

    if not isinstance(data, dict):
        return LLMResponse(
            patched_content="",
            confidence_score=0.0,
            provider_name=provider_name,
            raw_response=raw,
            success=False,
            error="Response is not a JSON object",
            validation_error="Expected JSON object, got other type",
        )

    # Validate required fields
    missing: list[str] = []
    patched = data.get("patched_content", "")
    if not patched or not isinstance(patched, str) or not patched.strip():
        missing.append("patched_content")

    confidence_raw = data.get("confidence_score")
    if confidence_raw is None:
        missing.append("confidence_score")

    fix_reason = data.get("fix_reason", "")
    if not fix_reason or not isinstance(fix_reason, str) or not fix_reason.strip():
        missing.append("fix_reason")

    if missing:
        return LLMResponse(
            patched_content="",
            confidence_score=0.0,
            provider_name=provider_name,
            raw_response=raw,
            success=False,
            error=f"Missing required fields: {', '.join(missing)}",
            validation_error=f"Missing required fields: {', '.join(missing)}",
        )

    # Parse confidence
    try:
        confidence = max(0.0, min(1.0, float(confidence_raw)))
    except (ValueError, TypeError):
        return LLMResponse(
            patched_content="",
            confidence_score=0.0,
            provider_name=provider_name,
            raw_response=raw,
            success=False,
            error="confidence_score is not a valid number",
            validation_error="confidence_score is not a valid number",
        )

    return LLMResponse(
        patched_content=patched,
        confidence_score=confidence,
        provider_name=provider_name,
        raw_response=raw,
        success=True,
        fix_reason=fix_reason,
    )


# ---------------------------------------------------------------------------
# LLM Client
# ---------------------------------------------------------------------------
class LLMClient:
    """
    Async HTTP client for calling LLM providers.

    Supports Gemini (Google REST API) and Groq (OpenAI-compatible).

    Usage:
        client = LLMClient()
        response = await client.call("Fix this code...", "You are...", config)
        await client.close()
    """

    def __init__(self) -> None:
        self._http: Optional[httpx.AsyncClient] = None

    async def _get_http(self) -> httpx.AsyncClient:
        """Lazy-initialise the HTTP client."""
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        return self._http

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._http and not self._http.is_closed:
            await self._http.aclose()
            self._http = None

    async def call(
        self,
        user_prompt: str,
        system_prompt: str,
        provider: ProviderConfig,
    ) -> LLMResponse:
        """
        Send a prompt to the specified LLM provider.

        Parameters
        ----------
        user_prompt : str
            The user/fix prompt.
        system_prompt : str
            The system prompt with rules.
        provider : ProviderConfig
            Provider configuration (Gemini or Groq).

        Returns
        -------
        LLMResponse
            Parsed response from the provider.
        """
        for attempt in range(1, provider.max_retries + 1):
            try:
                if provider.name == "gemini":
                    raw = await self._call_gemini(user_prompt, system_prompt, provider)
                else:
                    raw = await self._call_openai_compatible(
                        user_prompt, system_prompt, provider
                    )

                response = parse_llm_response(raw, provider.name)
                if response.success and response.patched_content:
                    return response

                logger.warning(
                    "Provider %s attempt %d: empty or failed response",
                    provider.name, attempt,
                )

            except httpx.TimeoutException:
                logger.warning(
                    "Provider %s attempt %d: timeout", provider.name, attempt
                )
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                logger.warning(
                    "Provider %s attempt %d: HTTP %d", provider.name, attempt, status
                )
                if status == 429:  # Rate limit
                    break  # Don't retry, switch provider immediately
            except Exception as e:
                logger.warning(
                    "Provider %s attempt %d: %s", provider.name, attempt, e
                )

        return LLMResponse(
            patched_content="",
            confidence_score=0.0,
            provider_name=provider.name,
            success=False,
            error=f"All {provider.max_retries} retries exhausted for {provider.name}",
        )

    async def _call_gemini(
        self,
        user_prompt: str,
        system_prompt: str,
        provider: ProviderConfig,
    ) -> str:
        """Call Gemini REST API."""
        http = await self._get_http()
        url = (
            f"{provider.base_url}/models/{provider.model}:generateContent"
            f"?key={provider.api_key}"
        )
        payload = {
            "system_instruction": {
                "parts": [{"text": system_prompt}]
            },
            "contents": [
                {"parts": [{"text": user_prompt}]}
            ],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 8192,
            },
        }
        resp = await http.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()

        # Extract text from Gemini response
        try:
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    return parts[0].get("text", "")
        except (IndexError, KeyError, TypeError):
            pass
        return ""

    async def _call_openai_compatible(
        self,
        user_prompt: str,
        system_prompt: str,
        provider: ProviderConfig,
    ) -> str:
        """Call OpenAI-compatible API (Groq, etc.)."""
        http = await self._get_http()
        url = f"{provider.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": provider.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 8192,
        }
        resp = await http.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        # Extract text from OpenAI-compatible response
        try:
            choices = data.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")
        except (IndexError, KeyError, TypeError):
            pass
        return ""

    async def call_with_fallback(
        self,
        user_prompt: str,
        system_prompt: str,
        router: LLMRouter,
    ) -> LLMResponse:
        """
        Call LLM with automatic provider fallback.

        Tries the primary provider first, then falls back to the next
        healthy provider on failure.

        Parameters
        ----------
        user_prompt : str
            The user/fix prompt.
        system_prompt : str
            The system prompt with rules.
        router : LLMRouter
            Router for provider selection and health tracking.

        Returns
        -------
        LLMResponse
            Response from whichever provider succeeded, or a failure response.
        """
        primary = router.get_provider()
        response = await self.call(user_prompt, system_prompt, primary)

        if response.success and response.patched_content:
            router.report_success(primary.name)
            return response

        # Primary failed — try fallback
        router.report_failure(primary.name)
        fallback = router.get_fallback_provider(primary.name)

        if fallback:
            response = await self.call(user_prompt, system_prompt, fallback)
            if response.success and response.patched_content:
                router.report_success(fallback.name)
                return response
            router.report_failure(fallback.name)

        return LLMResponse(
            patched_content="",
            confidence_score=0.0,
            provider_name=primary.name,
            success=False,
            error="All providers failed",
        )
