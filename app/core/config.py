"""
Configuration
=============
Loads environment variables from .env file using python-dotenv.

Environment Variables:
    GEMINI_API_KEY       — Primary LLM provider API key (Google Gemini)
    GROQ_API_KEY         — Fallback LLM provider API key (Groq)
    GITHUB_TOKEN         — Required for pushing fixes and polling CI status
    RUN_RETRY_LIMIT      — Max autonomous fix loops (default: 5)
    DOCKER_IMAGE         — Sandbox container image name (default: rift-sandbox:latest)
    ENABLE_DEV_ENDPOINT  — Enable /dev/* diagnostic endpoints (default: false)

Execution Timeout Philosophy:
    DEFAULT_EXECUTION_TIMEOUT defines the max seconds a single build/test
    execution is allowed to run inside the sandbox container. This prevents
    runaway builds from blocking the healing loop. The value should balance
    giving complex builds enough time vs keeping total run under 5 minutes.

Retry Limit:
    RUN_RETRY_LIMIT controls the maximum number of Execute → Fix → Push
    cycles. After exhaustion, the agent stops and writes final results.json.
"""
import os
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
RUN_RETRY_LIMIT = int(os.getenv("RUN_RETRY_LIMIT", 5))
DOCKER_IMAGE = os.getenv("DOCKER_IMAGE", "rift-sandbox:latest")

# Execution timeout in seconds — max time for a single build/test run
DEFAULT_EXECUTION_TIMEOUT = 300
