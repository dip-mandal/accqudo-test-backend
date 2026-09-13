import enum
from sqlalchemy import (
    Column,
    BigInteger,
    String,
    Enum,
    DateTime,
    ForeignKey,
    Float,
    Boolean,
    JSON,
    Integer,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base


class PackageExpiryType(str, enum.Enum):
    DURATION = "DURATION"      # e.g., 365 days from purchase
    FIXED_DATE = "FIXED_DATE"  # e.g., until 2027-02-15
    EXAM_DATE = "EXAM_DATE"    # dynamically tracks official exam date


class SubscriptionStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"


class PaymentStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class Package(Base):
    __tablename__ = "packages"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    exam_id = Column(BigInteger, ForeignKey("exams.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    description = Column(String(1000), nullable=True)
    price_paise = Column(Integer, nullable=False)  # e.g., 99900 = ₹999.00
    discount_paise = Column(Integer, default=0, server_default="0", nullable=False)
    
    expiry_type = Column(Enum(PackageExpiryType), default=PackageExpiryType.DURATION, server_default=PackageExpiryType.DURATION.value, nullable=False)
    validity_days = Column(Integer, nullable=True)
    fixed_expiry_date = Column(DateTime, nullable=True)
    
    is_active = Column(Boolean, default=True, server_default="1", nullable=False)
    features = Column(JSON, nullable=True)
    
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    exam = relationship("Exam")
    payments = relationship("Payment", back_populates="package")
    subscriptions = relationship("Subscription", back_populates="package")


class Coupon(Base):
    __tablename__ = "coupons"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(50), unique=True, nullable=False, index=True)
    discount_percent = Column(Float, nullable=False)  # e.g., 50.0 for 50%
    max_discount_paise = Column(Integer, nullable=True)
    valid_from = Column(DateTime, nullable=False)
    valid_until = Column(DateTime, nullable=False)
    usage_limit = Column(Integer, default=1000, server_default="1000", nullable=False)
    times_used = Column(Integer, default=0, server_default="0", nullable=False)
    is_active = Column(Boolean, default=True, server_default="1", nullable=False)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class Payment(Base):
    __tablename__ = "payments"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    package_id = Column(BigInteger, ForeignKey("packages.id", ondelete="RESTRICT"), nullable=False)
    
    razorpay_order_id = Column(String(100), unique=True, nullable=False, index=True)
    razorpay_payment_id = Column(String(100), unique=True, nullable=True, index=True)
    razorpay_signature = Column(String(255), nullable=True)
    
    amount_paise = Column(Integer, nullable=False)
    status = Column(Enum(PaymentStatus), default=PaymentStatus.PENDING, server_default=PaymentStatus.PENDING.value, nullable=False)
    
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    user = relationship("User")
    package = relationship("Package", back_populates="payments")
    subscription = relationship("Subscription", back_populates="payment", uselist=False)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    package_id = Column(BigInteger, ForeignKey("packages.id", ondelete="RESTRICT"), nullable=False)
    payment_id = Column(BigInteger, ForeignKey("payments.id", ondelete="RESTRICT"), nullable=False, unique=True)
    
    start_date = Column(DateTime, server_default=func.now(), nullable=False)
    expiry_date = Column(DateTime, nullable=False, index=True)
    status = Column(Enum(SubscriptionStatus), default=SubscriptionStatus.ACTIVE, server_default=SubscriptionStatus.ACTIVE.value, nullable=False, index=True)
    
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    user = relationship("User")
    package = relationship("Package", back_populates="subscriptions")
    payment = relationship("Payment", back_populates="subscription")