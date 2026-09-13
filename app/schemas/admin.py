from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from app.models.question import QuestionType

class ExamCreate(BaseModel):
    title: str
    code: str

class SubjectCreate(BaseModel):
    exam_id: int
    name: str

class ChapterCreate(BaseModel):
    subject_id: int
    name: str

class TopicCreate(BaseModel):
    chapter_id: int
    name: str

class QuestionCreate(BaseModel):
    topic_id: int
    question_type: QuestionType
    question_text: str
    options: Optional[List[Dict[str, Any]]] = None
    evaluation_data: Dict[str, Any]
    solution_text: Optional[str] = None
    default_marks: float = 1.0
    default_negative_marks: float = 0.0