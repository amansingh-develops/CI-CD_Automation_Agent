"""
Fix Result Model
=================
Pydantic model tracking the outcome of a fix attempt.

Fields:
    bug_report              — the BugReport that triggered this fix
    original_content        — original file content before fix
    patched_content         — LLM-generated patched file content
    patch_applied           — human-readable patch description
    diff                    — unified diff between original and patched
    confidence              — LLM self-reported confidence score (0.0–1.0)
    success                 — True if patch was accepted and is safe to apply
    needs_escalation        — True if confidence below threshold
    manual_required         — True if merge conflict markers detected
    context_level           — context strategy used (small / medium / large)
    provider_used           — which LLM provider generated this fix
    commit_sha              — filled by Git Agent after commit (empty until then)
    error_message           — error info if the fix attempt failed

Stability Safeguard Fields (Step 5.1):
    bug_signature           — file_path + line_number + sub_type hash
    patch_fingerprint       — bug_signature + patch_hash for repeat detection
    previous_attempt_detected — True if same fix was tried before
    escalation_reason       — standardised reason constant (see escalation_reasons.py)
    error_severity_hint     — syntax / import / type / logic / lint
"""
from pydantic import BaseModel
from .bug_report import BugReport


class FixResult(BaseModel):
    # --- Core fields ---
    bug_report: BugReport
    original_content: str = ""
    patched_content: str = ""
    patch_applied: str = ""
    diff: str = ""
    confidence: float = 0.0
    success: bool = False
    needs_escalation: bool = False
    manual_required: bool = False
    context_level: str = "small"
    provider_used: str = ""
    commit_sha: str = ""
    error_message: str = ""

    # --- Stability safeguard fields (Step 5.1) ---
    bug_signature: str = ""
    patch_fingerprint: str = ""
    previous_attempt_detected: bool = False
    escalation_reason: str = ""
    error_severity_hint: str = ""

    # --- Effectiveness field (Step 6.1) ---
    effectiveness_score: float = -1.0  # 1.0=removed, 0.5=changed, 0.0=unchanged, -1=not scored
