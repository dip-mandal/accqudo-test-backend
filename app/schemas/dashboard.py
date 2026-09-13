from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

class AttemptHistoryItem(BaseModel):
    attempt_id: int
    test_id: int
    test_title: str
    score: float
    total_marks: float
    percentage: float
    accuracy: float
    submitted_at: Optional[datetime]
    duration_taken_seconds: int

class StudentDashboardSummary(BaseModel):
    total_attempts: int
    tests_completed: int
    average_score_percentage: float
    overall_accuracy: float
    history: List[AttemptHistoryItem]