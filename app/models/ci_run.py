"""
CI Run Model
Pydantic model for tracking metadata of a CI/CD pipeline run.
"""
from pydantic import BaseModel
from datetime import datetime

class CIRun(BaseModel):
    run_id: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    iteration: int
