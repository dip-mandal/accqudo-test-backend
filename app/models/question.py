import enum
from sqlalchemy import Column, BigInteger, String, Enum, ForeignKey, JSON, Float, DateTime
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base

class QuestionType(str, enum.Enum):
    MCQ = "MCQ"
    MSQ = "MSQ"
    NAT = "NAT"

class Question(Base):
    __tablename__ = "questions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    topic_id = Column(BigInteger, ForeignKey("topics.id", ondelete="CASCADE"), nullable=False, index=True)
    question_type = Column(Enum(QuestionType), nullable=False)
    question_text = Column(String(5000), nullable=False)
    options = Column(JSON, nullable=True)
    evaluation_data = Column(JSON, nullable=False)
    solution_text = Column(String(5000), nullable=True)

    default_marks = Column(Float, default=1.0, server_default="1.0", nullable=False)
    default_negative_marks = Column(Float, default=0.0, server_default="0.0", nullable=False)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    topic = relationship("Topic", lazy="joined")