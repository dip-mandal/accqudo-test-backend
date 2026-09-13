import datetime
from sqlalchemy import Column, BigInteger, String, Integer, Float, Boolean, DateTime, func
from app.core.database import Base

class Coupon(Base):
    __tablename__ = "coupons"
    __table_args__ = {'extend_existing': True}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(50), unique=True, index=True, nullable=False)
    discount_percent = Column(Float, nullable=False)
    max_discount_paise = Column(Integer, nullable=True)
    valid_from = Column(DateTime, nullable=True)
    valid_until = Column(DateTime, nullable=True)
    usage_limit = Column(Integer, default=100, nullable=False)
    times_used = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    
    
