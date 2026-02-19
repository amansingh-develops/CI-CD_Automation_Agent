import uvicorn
from fastapi import FastAPI
from app.api.run_agent import router as run_agent_router
from app.api.status import router as status_router
from app.api.results import router as results_router
from app.api.dev_run_repo import router as dev_router

app = FastAPI(title="Autonomous CI/CD Healing Agent API")

# Health endpoint
@app.get("/health")
async def health_check():
    return {"status": "ok"}

# Register routers
app.include_router(run_agent_router, tags=["Agent"])
app.include_router(status_router, tags=["Agent"])
app.include_router(results_router, tags=["Agent"])
app.include_router(dev_router)

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
