from pydantic import BaseModel
from typing import Optional, Any, List
from datetime import datetime

class QuestionSnapshotPublic(BaseModel):
    id: int
    order: int
    question_type: str
    question_text: Any
    options: Optional[Any] = None
    marks: float
    negative_marks: float
    student_response: Optional[Any] = None
    is_visited: bool
    is_marked_for_review: bool

    class Config:
        from_attributes = True

class AttemptSessionResponse(BaseModel):
    attempt_id: int
    test_id: int
    started_at: datetime
    expires_at: datetime
    questions: List[QuestionSnapshotPublic]

class SaveAnswerRequest(BaseModel):
    snapshot_id: int
    response: Any
    is_visited: bool = True
    is_marked_for_review: bool = False