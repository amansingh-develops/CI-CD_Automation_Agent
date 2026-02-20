"""
Configuration
=============
Loads environment variables from .env file using python-dotenv.

Environment Variables:
    GEMINI_API_KEY       — Primary LLM provider API key (Google Gemini)
    GROQ_API_KEY         — Fallback LLM provider API key (Groq)
    OPENROUTER_API_KEY   — Second fallback LLM provider (OpenRouter free models)
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
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
RUN_RETRY_LIMIT = int(os.getenv("RUN_RETRY_LIMIT", 5))
DOCKER_IMAGE = os.getenv("DOCKER_IMAGE", "rift-sandbox:latest")

# Dynamic Docker images: project_type → language-specific image
# Each entry is overridable via env var (e.g. DOCKER_IMAGE_PYTHON=python:3.12)
DOCKER_IMAGE_MAP: dict[str, str] = {
    "python":         os.getenv("DOCKER_IMAGE_PYTHON",  "python:3.11-slim"),
    "node":           os.getenv("DOCKER_IMAGE_NODE",    "node:20-slim"),
    "java":           os.getenv("DOCKER_IMAGE_JAVA",    "maven:3.9-eclipse-temurin-17"),
    "go":             os.getenv("DOCKER_IMAGE_GO",      "golang:1.22-bookworm"),
    "rust":           os.getenv("DOCKER_IMAGE_RUST",    "rust:1.77-slim"),
    "docker_project": os.getenv("DOCKER_IMAGE_DOCKER",  DOCKER_IMAGE),
}

# Execution timeout in seconds — max time for a single build/test run
DEFAULT_EXECUTION_TIMEOUT = 300

# Commit safety
MAX_COMMITS_PER_RUN = int(os.getenv("MAX_COMMITS_PER_RUN", 20))

# Naming conventions
TEAM_NAME = os.getenv("TEAM_NAME", "ANONYMOUS")
LEADER_NAME = os.getenv("LEADER_NAME", "AGENT")

# Provider health cooldown
PROVIDER_COOLDOWN_THRESHOLD = int(os.getenv("PROVIDER_COOLDOWN_THRESHOLD", 4))
PROVIDER_COOLDOWN_SKIP_COUNT = int(os.getenv("PROVIDER_COOLDOWN_SKIP_COUNT", 5))

# Patch safety
PATCH_TRUNCATION_RATIO = float(os.getenv("PATCH_TRUNCATION_RATIO", 0.3))

# Per-bug retry limit
PER_BUG_RETRY_LIMIT = int(os.getenv("PER_BUG_RETRY_LIMIT", 5))
