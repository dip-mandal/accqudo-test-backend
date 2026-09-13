from pydantic import BaseModel
from typing import List

class TestQuestionMapping(BaseModel):
    question_id: int
    order: int
    marks: float
    negative_marks: float

class CreateTestRequest(BaseModel):
    exam_id: int
    title: str
    duration_minutes: int
    instructions: str
    questions: List[TestQuestionMapping]