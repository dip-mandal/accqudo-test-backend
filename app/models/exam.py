import datetime
from sqlalchemy import Column, BigInteger, String, ForeignKey, DateTime, func
from sqlalchemy.orm import relationship
from app.core.database import Base

class Exam(Base):
    __tablename__ = "exams"
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    title = Column(String(255), nullable=False)
    code = Column(String(50), unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, server_default=func.now(), nullable=False)
    
    subjects = relationship("Subject", back_populates="exam", cascade="all, delete-orphan")

class Subject(Base):
    __tablename__ = "subjects"
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    exam_id = Column(BigInteger, ForeignKey("exams.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, server_default=func.now(), nullable=False)
    
    exam = relationship("Exam", back_populates="subjects")
    chapters = relationship("Chapter", back_populates="subject", cascade="all, delete-orphan")

class Chapter(Base):
    __tablename__ = "chapters"
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    subject_id = Column(BigInteger, ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, server_default=func.now(), nullable=False)
    
    subject = relationship("Subject", back_populates="chapters")
    topics = relationship("Topic", back_populates="chapter", cascade="all, delete-orphan")

class Topic(Base):
    __tablename__ = "topics"
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    chapter_id = Column(BigInteger, ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, server_default=func.now(), nullable=False)
    
    chapter = relationship("Chapter", back_populates="topics")