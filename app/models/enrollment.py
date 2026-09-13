from sqlalchemy import Column, Integer, ForeignKey, DateTime, String
from sqlalchemy.sql import func
from app.core.database import Base

class TestEnrollment(Base):
    __tablename__ = "test_enrollments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    test_id = Column(Integer, ForeignKey("tests.id"), nullable=False, index=True)
    payment_status = Column(String(50), default="COMPLETED")  # COMPLETED | PENDING | REFUNDED
    enrolled_at = Column(DateTime(timezone=True), server_default=func.now())
    
