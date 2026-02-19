"""
LLM Prompts
===========
Centralised store for Fix Agent system and user prompts.

Prompt Design Rules:
    - "Fix only reported lines ±3" — hard constraint in every prompt
    - "Avoid refactoring" — explicit instruction to preserve structure
    - "Preserve comments" — comments are never removed or modified
    - "Return only the patched file content" — no explanations in output

Domain-Specific Prompt Routing:
    - Each domain (backend_python, frontend_js, database, config, generic)
      has a specialist system prompt loaded from skills/ directory
    - Domain is determined by BugReport.domain field (set by classifier)
    - Generic prompt used when domain is unknown or not classified

Minimal Diff Enforcement:
    - Prompt includes explicit: "Do NOT change lines outside ±3 of the error"
    - Prompt includes: "Do NOT rename variables or restructure code"
    - Prompt includes: "Return the COMPLETE file with ONLY the fix applied"

Confidence Instruction:
    - Prompt asks LLM to self-report a confidence score (0–1)
    - Structured output format enforced via JSON schema
"""
import os
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Skills directory (domain-specific prompts)
# ---------------------------------------------------------------------------
_SKILLS_DIR = os.path.join(os.path.dirname(__file__), "..", "skills")


def _load_skill(filename: str) -> str:
    """Load a skill file from the skills directory. Returns empty string on failure."""
    try:
        path = os.path.join(_SKILLS_DIR, filename)
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# System Prompts
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are a minimal CI auto-fixer. Your ONLY job is to fix the reported issue.\n"
    "\n"
    "HARD RULES — you MUST follow ALL of these:\n"
    "1. Fix ONLY the reported failure. Nothing else.\n"
    "2. Minimum diff only — change as few lines as possible.\n"
    "3. Preserve ALL comments exactly as they are.\n"
    "4. Do NOT refactor, rename, or reorganise any unrelated code.\n"
    "5. Do NOT change function signatures unless the error requires it.\n"
    "6. Do NOT change lines outside ±3 of the reported error line.\n"
    "7. Do NOT add explanations, apologies, or markdown formatting.\n"
    "8. Do NOT insert destructive shell commands or modify CI pipeline files.\n"
    "9. Do NOT change dependency versions unless the error is about a missing dependency.\n"
    "\n"
    "RESPONSE FORMAT — you MUST respond with ONLY valid JSON:\n"
    '{"patched_content": "<the complete file with your fix applied>", '
    '"confidence_score": <float between 0.0 and 1.0>}\n'
    "\n"
    "No other text. No markdown code fences. Just the JSON object."
)


# Domain-specific system prompt extensions
DOMAIN_PROMPTS: dict[str, str] = {
    "backend_python": _load_skill("backend_python.md"),
    "frontend_js": _load_skill("frontend_js.md"),
    "database": _load_skill("database.md"),
    "config": _load_skill("config.md"),
    "generic": _load_skill("generic.md"),
}


def get_system_prompt(domain: str = "generic") -> str:
    """
    Return the full system prompt, optionally augmented with domain-specific skills.

    Parameters
    ----------
    domain : str
        The BugReport.domain value (e.g. "backend_python", "frontend_js").

    Returns
    -------
    str
        Complete system prompt string.
    """
    base = SYSTEM_PROMPT
    domain_hint = DOMAIN_PROMPTS.get(domain, DOMAIN_PROMPTS.get("generic", ""))
    if domain_hint:
        base += f"\n\nDOMAIN CONTEXT:\n{domain_hint}"
    return base


# ---------------------------------------------------------------------------
# User Prompt Builder
# ---------------------------------------------------------------------------
def build_user_prompt(
    error_message: str,
    file_path: str,
    file_snippet: str,
    bug_type: str = "",
    sub_type: str = "",
    test_name: str = "",
    previous_attempt_info: str = "",
    context_level: str = "small",
    full_file_content: str = "",
    related_file_content: str = "",
    ci_config_hint: str = "",
) -> str:
    """
    Build the user prompt sent to the LLM.

    Context Levels:
        - small  (default): error message + file snippet ±3 lines
        - medium: error message + entire file + related import file
        - large:  error message + multiple files + CI config hint + test file

    Parameters
    ----------
    error_message : str
        The error/failure message from the build log.
    file_path : str
        Path to the failing file.
    file_snippet : str
        Code snippet around the error (±3 lines by default).
    bug_type : str
        Classified bug type (SYNTAX, IMPORT, etc.).
    sub_type : str
        Classified sub-type (missing_import, invalid_syntax, etc.).
    test_name : str
        Optional test name that failed.
    previous_attempt_info : str
        Optional info about a prior fix attempt that didn't work.
    context_level : str
        One of "small", "medium", "large".
    full_file_content : str
        Full file content (used in medium/large context).
    related_file_content : str
        Content of related import files (used in medium/large context).
    ci_config_hint : str
        CI config context (used in large context only).

    Returns
    -------
    str
        Formatted user prompt string.
    """
    parts: list[str] = []

    # Error description
    parts.append(f"ERROR TYPE: {bug_type}")
    if sub_type:
        parts.append(f"ERROR SUB-TYPE: {sub_type}")
    parts.append(f"FILE: {file_path}")
    parts.append(f"ERROR MESSAGE:\n{error_message}")

    if test_name:
        parts.append(f"FAILING TEST: {test_name}")

    if previous_attempt_info:
        parts.append(
            f"PREVIOUS FIX ATTEMPT FAILED:\n{previous_attempt_info}\n"
            "Do NOT repeat the same fix. Try a different approach."
        )

    # Context-level content
    if context_level == "small":
        parts.append(f"CODE SNIPPET (around error):\n```\n{file_snippet}\n```")
    elif context_level == "medium":
        content = full_file_content if full_file_content else file_snippet
        parts.append(f"FULL FILE CONTENT:\n```\n{content}\n```")
        if related_file_content:
            parts.append(f"RELATED IMPORT FILE:\n```\n{related_file_content}\n```")
    elif context_level == "large":
        content = full_file_content if full_file_content else file_snippet
        parts.append(f"FULL FILE CONTENT:\n```\n{content}\n```")
        if related_file_content:
            parts.append(f"RELATED FILES:\n```\n{related_file_content}\n```")
        if ci_config_hint:
            parts.append(f"CI CONFIG HINT:\n{ci_config_hint}")

    # Hard rules reminder
    parts.append(
        "INSTRUCTIONS:\n"
        "- Return the COMPLETE file content with ONLY the fix applied.\n"
        "- Do NOT change any lines outside ±3 of the error.\n"
        "- Do NOT rename variables or restructure code.\n"
        "- Preserve ALL comments.\n"
        "- Respond with ONLY valid JSON: "
        '{"patched_content": "...", "confidence_score": 0.0-1.0}'
    )

    return "\n\n".join(parts)
