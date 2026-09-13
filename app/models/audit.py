from sqlalchemy import Column, BigInteger, String, Integer, Float, Boolean, DateTime
from sqlalchemy.sql import func
from app.core.database import Base

class Coupon(Base):
    __tablename__ = "coupons"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True, index=True)
    code = Column(String(50), unique=True, index=True, nullable=False)
    # Maps Python's discount_percentage attribute to the database column
    discount_percentage = Column("discount_percentage", Float, nullable=False, default=10.0)
    max_uses = Column("max_uses", Integer, nullable=False, default=100)
    times_used = Column(Integer, nullable=False, default=0)
    valid_until = Column(DateTime, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, server_default=func.now())


class AuditTrafficLog(Base):
    __tablename__ = "audit_traffic_logs"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True, index=True)
    path = Column(String(255), index=True, nullable=False)
    method = Column(String(10), nullable=False)
    status_code = Column(Integer, nullable=False)
    ip_address = Column(String(45), nullable=False)
    user_agent = Column(String(500), nullable=True)
    response_time_ms = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime, server_default=func.now(), index=True)