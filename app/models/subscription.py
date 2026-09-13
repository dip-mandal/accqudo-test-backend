import enum
from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Float,
    ForeignKey,
    DateTime,
    Table,
    Boolean,
    Enum as SqlEnum,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base

# Import canonical Payment and PaymentStatus from payment.py
from app.models.payment import Payment, PaymentStatus

# Aliases to satisfy any callers expecting Enum suffix
PaymentStatusEnum = PaymentStatus


class SubscriptionTier(str, enum.Enum):
    FREE = "FREE"
    TEST_SERIES_PRO = "TEST_SERIES_PRO"
    ALL_ACCESS_PASS = "ALL_ACCESS_PASS"


# Alias for legacy references
PackageTierEnum = SubscriptionTier


# Many-to-Many junction table
package_tests_table = Table(
    "package_tests",
    Base.metadata,
    Column(
        "package_id",
        BigInteger,
        ForeignKey("subscription_packages.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "test_id",
        BigInteger,
        ForeignKey("tests.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    extend_existing=True,
)


class PackageTest(Base):
    """Declarative wrapper for package_tests table to satisfy ORM queries."""
    __table__ = package_tests_table
    __table_args__ = {"extend_existing": True}


class SubscriptionPackage(Base):
    __tablename__ = "subscription_packages"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True, index=True)
    exam_id = Column(
        BigInteger,
        ForeignKey("exams.id"),
        nullable=False,
        default=1,
        index=True,
    )
    title = Column(String(255), nullable=False)
    description = Column(String(500), nullable=True)
    tier = Column(
        SqlEnum(SubscriptionTier),
        nullable=False,
        default=SubscriptionTier.TEST_SERIES_PRO,
    )
    # Maps Python attribute price_inr to MySQL column 'price'
    price_inr = Column("price", Float, nullable=False, default=0.0)
    validity_days = Column(Integer, nullable=False, default=365)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, server_default=func.now())

    # Relationship to Test model
    tests = relationship(
        "Test",
        secondary=package_tests_table,
        backref="subscription_packages",
    )


class UserSubscription(Base):
    __tablename__ = "user_subscriptions"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    package_id = Column(BigInteger, ForeignKey("subscription_packages.id"), nullable=False, index=True)
    tier = Column(
        SqlEnum(SubscriptionTier),
        nullable=False,
        default=SubscriptionTier.TEST_SERIES_PRO,
    )
    start_date = Column(DateTime(timezone=True), server_default=func.now())
    end_date = Column(DateTime(timezone=True), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    user = relationship("User", backref="subscriptions")
    package = relationship("SubscriptionPackage", backref="user_subscriptions")


class RazorpayOrder(Base):
    __tablename__ = "razorpay_orders"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    package_id = Column(BigInteger, ForeignKey("subscription_packages.id"), nullable=True)
    test_id = Column(BigInteger, ForeignKey("tests.id"), nullable=True)
    razorpay_order_id = Column(String(255), unique=True, index=True, nullable=False)
    razorpay_payment_id = Column(String(255), nullable=True)
    razorpay_signature = Column(String(255), nullable=True)
    amount_inr = Column(Float, nullable=False)
    currency = Column(String(10), default="INR")
    status = Column(String(50), default="CREATED")
    created_at = Column(DateTime, server_default=func.now())