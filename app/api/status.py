"""
GET /status
Provides progress polling for the frontend to monitor the current agent activity.
"""
from fastapi import APIRouter

router = APIRouter()

@router.get("/status")
async def get_status():
    return {"status": "not implemented"}
