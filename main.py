import uvicorn
import time
import logging
from fastapi import FastAPI, Request
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.run_agent import router as run_agent_router
from app.api.status import router as status_router
from app.api.results import router as results_router
from app.api.dev_run_repo import router as dev_router
from app.api.analyze_repository import router as analyze_router
from app.utils.logging_config import setup_logging

# Initialize enhanced logging
setup_logging(level=logging.INFO)
logger = logging.getLogger("main")

app = FastAPI(title="Autonomous CI/CD Healing Agent API")

# ---------------------------------------------------------------------------
# Logging Middleware
# ---------------------------------------------------------------------------
class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        client_host = request.client.host if request.client else "unknown"
        logger.info(f"Incoming: {request.method} {request.url.path} from {client_host}")
        
        try:
            response = await call_next(request)
            process_time = (time.time() - start_time) * 1000
            logger.info(
                f"Outgoing: {request.method} {request.url.path} - "
                f"Status: {response.status_code} - "
                f"Time: {process_time:.2f}ms"
            )
            return response
        except Exception as e:
            process_time = (time.time() - start_time) * 1000
            logger.error(f"Request failed: {request.method} {request.url.path} - Error: {str(e)}")
            raise e

app.add_middleware(LoggingMiddleware)

# ---------------------------------------------------------------------------
# CORS — allow the React frontend (port 3000) to call the backend (port 8000)
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Health endpoint
@app.get("/health")
async def health_check():
    return {"status": "ok"}

# Register routers
app.include_router(run_agent_router, tags=["Agent"])
app.include_router(status_router, tags=["Agent"])
app.include_router(results_router, tags=["Agent"])
app.include_router(dev_router)
app.include_router(analyze_router)

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
