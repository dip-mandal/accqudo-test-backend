from datetime import datetime
from sqlalchemy import Column, BigInteger, String, Integer, Float, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship
from app.core.database import Base

class Test(Base):
    __tablename__ = "tests"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    exam_id = Column(BigInteger, ForeignKey("exams.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    duration_minutes = Column(Integer, nullable=False)
    total_marks = Column(Float, nullable=False)
    instructions = Column(JSON, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    test_questions = relationship("TestQuestion", back_populates="test", cascade="all, delete-orphan")

class TestQuestion(Base):
    __tablename__ = "test_questions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    test_id = Column(BigInteger, ForeignKey("tests.id", ondelete="CASCADE"), nullable=False)
    question_id = Column(BigInteger, ForeignKey("questions.id", ondelete="CASCADE"), nullable=False)
    order = Column(Integer, nullable=False)
    marks = Column(Float, nullable=False)
    negative_marks = Column(Float, default=0.0, nullable=False)

    test = relationship("Test", back_populates="test_questions")
    question = relationship("Question")