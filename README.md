# Autonomous CI/CD Healing Agent (Monorepo)

An intelligent, autonomous DevOps agent designed to detect, diagnose, and fix CI/CD failures automatically. This monorepo contains both the high-performance Python backend and the modern Cyberpunk-themed React dashboard.

## 🚀 Key Features

- **Execution-Driven Healing**: Errors are discovered by running the project in a Docker sandbox, ensuring 100% runtime accuracy.
- **Multi-Provider LLM Routing**: 
    - **Groq (Primary)**: High-speed, high-reliability engine for instant fixes.
    - **Gemini (Fallback)**: Advanced reasoning as a robust backup.
    - **OpenRouter (Deep Fallback)**: Access to various models with specialized 30s timeout and optimized retry handling.
- **Intelligent Throttling**: 6s inter-request delay to maximize reliability on free-tier rate limits.
- **Partial State Preservation**: Dashboard preserves all work (bugs found, commits made) even if an API run times out.
- **Cyberpunk Dashboard**: Real-time visual monitoring with Framer Motion animations and neon-glow aesthetics.

---

## 🏗️ Project Structure

The project is organized as a monorepo for a unified development experience:

```
/
├── backend/                # FastAPI Application
│   ├── app/                # Core logic (Agents, Executor, Parser, LLM)
│   ├── docker/             # Docker sandbox configurations
│   ├── workspace/          # Local mounting point for repo analysis
│   └── tests/              # Comprehensive Pytest suite
├── frontend/               # React (Vite) + Tailwind Dashboard
│   ├── src/                # Modern UI components and state logic
│   └── plugins/            # Visual-edits build system enhancements
└── scripts/                # Unified bootstrap and utility scripts
```

---

## 🛠️ Reliability & Performance

### Provider Routing Logic
The system implements a sophisticated fallback chain:
1. **Groq**: Primary provider for speed.
2. **Gemini**: Fallback if Groq is rate-limited (429).
3. **OpenRouter**: Secondary fallback utilizing `stepfun/step-3.5-flash:free` with a strict **30s timeout** and single-retry policy to ensure the pipeline never stalls.

### Fault Tolerance
- **API Guardrails**: Hard 480s ceiling for API requests.
- **Instant Push on Timeout**: If a run exceeds the timeout, the system automatically pushes all successful commits to GitHub BEFORE returning, ensuring no effort is wasted.
- **State Checkpoints**: The Orchestrator maintains an internal `_partial_state` updated after every successful fix.

---

## 💻 Tech Stack

### Backend
- **FastAPI**: Asynchronous API layer for high concurrency.
- **Pydantic**: Strict data validation and schema enforcement.
- **Docker SDK**: Native container orchestration for isolated execution.
- **LangGraph-inspired State**: Deterministic agent state management.

### Frontend
- **React (Vite)**: Lighting-fast development and build cycles.
- **Tailwind CSS**: Utility-first styling with a custom Cyberpunk theme.
- **Framer Motion**: Smooth, declarative animations.
- **Lucide React**: Premium icon set for a sleek interface.

---

## ⚙️ Setup & Installation

### Backend Setup
1. Navigate to `backend/`
2. `pip install -r requirements.txt`
3. Create a `.env` file (see `.env.example`):
   ```env
   GITHUB_TOKEN=your_token
   GROQ_API_KEY=your_key
   GEMINI_API_KEY=your_key
   OPENROUTER_API_KEY=your_key
   ```
4. Build the Docker sandbox image: `./scripts/build_sandbox.sh`
5. Run the server: `python main.py` (or `uvicorn main:app --port 8000`)

### Frontend Setup
1. Navigate to `frontend/`
2. `npm install`
3. Create a `.env` (or let it use defaults): `REACT_APP_BACKEND_URL=http://localhost:8000`
4. Run the development server: `npm run dev`

---

## 🧪 Verification

Run the full test suite to verify backend stability:
```bash
cd backend
python -m pytest tests/test_fix_agent_basic.py tests/test_orchestrator.py
```

---

*Powered by Autonomous DevOps — Unifying code and automation.*
