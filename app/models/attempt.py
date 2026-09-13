import enum
from datetime import datetime
from sqlalchemy import Column, BigInteger, String, Enum, DateTime, ForeignKey, Float, Integer, Boolean, JSON
from sqlalchemy.orm import relationship
from app.core.database import Base

class AttemptStatus(str, enum.Enum):
    IN_PROGRESS = "IN_PROGRESS"
    SUBMITTED = "SUBMITTED"
    AUTO_SUBMITTED = "AUTO_SUBMITTED"
    EXPIRED = "EXPIRED"

class TestAttempt(Base):
    __tablename__ = "test_attempts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    test_id = Column(BigInteger, ForeignKey("tests.id", ondelete="CASCADE"), nullable=False, index=True)
    attempt_number = Column(Integer, default=1, nullable=False)
    is_rank_eligible = Column(Boolean, default=False, nullable=False)
    
    status = Column(Enum(AttemptStatus), default=AttemptStatus.IN_PROGRESS, nullable=False)
    
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    submitted_at = Column(DateTime, nullable=True)
    
    total_score = Column(Float, default=0.0, nullable=False)
    correct_count = Column(Integer, default=0, nullable=False)
    incorrect_count = Column(Integer, default=0, nullable=False)
    unanswered_count = Column(Integer, default=0, nullable=False)

    snapshots = relationship("AttemptQuestionSnapshot", back_populates="attempt", cascade="all, delete-orphan")

class AttemptQuestionSnapshot(Base):
    __tablename__ = "attempt_question_snapshots"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    attempt_id = Column(BigInteger, ForeignKey("test_attempts.id", ondelete="CASCADE"), nullable=False, index=True)
    question_id = Column(BigInteger, ForeignKey("questions.id", ondelete="SET NULL"), nullable=True)
    
    order = Column(Integer, nullable=False)
    
    question_type = Column(String(50), nullable=False)
    question_text = Column(JSON, nullable=False)
    options = Column(JSON, nullable=True)
    evaluation_data = Column(JSON, nullable=False)
    
    marks = Column(Float, nullable=False)
    negative_marks = Column(Float, nullable=False)
    
    # Student Response State
    student_response = Column(JSON, nullable=True)
    is_visited = Column(Boolean, default=False, nullable=False)
    is_marked_for_review = Column(Boolean, default=False, nullable=False)
    
    obtained_marks = Column(Float, default=0.0, nullable=False)
    is_correct = Column(Boolean, nullable=True)

    attempt = relationship("TestAttempt", back_populates="snapshots")
    question = relationship("Question")