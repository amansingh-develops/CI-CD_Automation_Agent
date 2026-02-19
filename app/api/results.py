"""
GET /results
Returns the final results.json file containing metadata, failures, fixes, and score.
"""
from fastapi import APIRouter

router = APIRouter()

@router.get("/results")
async def get_results():
    return {"results": "not implemented"}
