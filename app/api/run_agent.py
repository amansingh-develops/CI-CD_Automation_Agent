"""
POST /run-agent
Accepts repository URL, team name and leader name.
Triggers the Orchestrator Agent to start the autonomous healing run.
"""
from fastapi import APIRouter

router = APIRouter()

@router.post("/run-agent")
async def run_agent():
    return {"message": "not implemented"}
