"""
Bug Report Model
================
Pydantic model for structured bug information.
This is the contract between the static analysis layer and all downstream consumers.

Fields:
    bug_type        — one of six allowed types (LINTING, SYNTAX, etc.)
    sub_type        — key into FIX_TEMPLATES[bug_type] (e.g. "unused_import")
    file_path       — relative to repo root, forward slashes
    line_number     — integer >= 1
    domain          — specialist routing domain (backend_python, frontend_js, etc.)
    tool            — source tool name (pylint, pyflakes, mypy, ast)
    message         — raw tool message for debugging (never shown in evaluation output)
"""
from typing import Optional
from pydantic import BaseModel


class BugReport(BaseModel):
    bug_type: str
    sub_type: str
    file_path: str
    line_number: int
    domain: str = "generic"
    test_name: Optional[str] = None
    confidence: float = 0.0
    tool: str = ""
    message: str = ""
