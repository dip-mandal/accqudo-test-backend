from datetime import datetime, timedelta
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.v1.deps import get_db, get_current_user

from app.models.user import User
from app.models.subscription_package import SubscriptionPackage
from app.models.user_subscription import UserSubscription
from app.models.razorpay_order import RazorpayOrder


router = APIRouter(
    prefix="/team/sells",
    tags=["Team - Package Sales"],
)


# ============================================================
# Authorization
# ============================================================

def require_admin_or_super_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    role = str(getattr(current_user, "role", "") or "").upper().strip()

    if role not in {"ADMIN", "SUPER_ADMIN"}:
        raise HTTPException(
            status_code=403,
            detail="Admin or Super Admin access required.",
        )

    return current_user


# ============================================================
# Helpers
# ============================================================

def serialize_datetime(value: Any) -> str | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.isoformat()

    return str(value)


def safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# ============================================================
# Dashboard Overview
# ============================================================

@router.get("/dashboard")
def get_package_sales_dashboard(
    months: int = Query(
        default=12,
        ge=1,
        le=36,
        description="Number of months to include in the revenue trend.",
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_or_super_admin),
) -> Dict[str, Any]:

    # --------------------------------------------------------
    # Date range
    # --------------------------------------------------------

    now = datetime.utcnow()

    start_date = now - timedelta(days=months * 31)

    # --------------------------------------------------------
    # Active packages
    # --------------------------------------------------------

    active_packages_count = (
        db.query(func.count(SubscriptionPackage.id))
        .filter(
            SubscriptionPackage.is_active.is_(True)
        )
        .scalar()
        or 0
    )

    total_packages_count = (
        db.query(func.count(SubscriptionPackage.id))
        .scalar()
        or 0
    )

    # --------------------------------------------------------
    # Successful Razorpay orders
    #
    # NOTE:
    # We intentionally use razorpay_orders here because
    # that table contains:
    #
    # user_id
    # package_id
    # amount_inr
    # status
    # created_at
    # --------------------------------------------------------

    successful_statuses = {
        "paid",
        "success",
        "successful",
        "completed",
        "captured",
    }

    successful_orders = (
        db.query(RazorpayOrder)
        .filter(
            func.lower(RazorpayOrder.status).in_(
                successful_statuses
            )
        )
        .all()
    )

    total_sales = 0
    total_revenue = 0.0

    for order in successful_orders:
        total_sales += 1
        total_revenue += safe_float(order.amount_inr)

    # --------------------------------------------------------
    # Active subscriptions
    # --------------------------------------------------------

    active_subscription_statuses = {
        "active",
        "paid",
        "success",
        "successful",
    }

    active_subscriptions = (
        db.query(func.count(UserSubscription.id))
        .filter(
            UserSubscription.is_active.is_(True)
        )
        .scalar()
        or 0
    )

    # --------------------------------------------------------
    # Unique enrolled students
    # --------------------------------------------------------

    enrolled_students = (
        db.query(
            func.count(
                func.distinct(UserSubscription.user_id)
            )
        )
        .filter(
            UserSubscription.is_active.is_(True)
        )
        .scalar()
        or 0
    )

    # --------------------------------------------------------
    # Package performance
    # --------------------------------------------------------

    packages = (
        db.query(SubscriptionPackage)
        .order_by(
            SubscriptionPackage.created_at.desc()
        )
        .all()
    )

    package_rows: List[Dict[str, Any]] = []

    for package in packages:

        package_id = package.id

        # --------------------------------------------
        # Successful sales
        # --------------------------------------------

        package_orders = (
            db.query(RazorpayOrder)
            .filter(
                RazorpayOrder.package_id == package_id,
                func.lower(RazorpayOrder.status).in_(
                    successful_statuses
                ),
            )
            .all()
        )

        sales_count = len(package_orders)

        revenue = sum(
            safe_float(order.amount_inr)
            for order in package_orders
        )

        # --------------------------------------------
        # Active students
        # --------------------------------------------

        active_students = (
            db.query(
                func.count(
                    func.distinct(
                        UserSubscription.user_id
                    )
                )
            )
            .filter(
                UserSubscription.package_id == package_id,
                UserSubscription.is_active.is_(True),
            )
            .scalar()
            or 0
        )

        # --------------------------------------------
        # Total subscriptions
        # --------------------------------------------

        total_students = (
            db.query(
                func.count(
                    func.distinct(
                        UserSubscription.user_id
                    )
                )
            )
            .filter(
                UserSubscription.package_id == package_id
            )
            .scalar()
            or 0
        )

        # --------------------------------------------
        # Expired subscriptions
        # --------------------------------------------

        expired_students = (
            db.query(
                func.count(
                    func.distinct(
                        UserSubscription.user_id
                    )
                )
            )
            .filter(
                UserSubscription.package_id == package_id,
                UserSubscription.end_date < now,
            )
            .scalar()
            or 0
        )

        package_rows.append(
            {
                "id": safe_int(package.id),
                "title": str(package.title),
                "description": package.description,
                "tier": str(package.tier),
                "price": safe_float(package.price),
                "validity_days": safe_int(
                    package.validity_days
                ),
                "is_active": bool(package.is_active),
                "created_at": serialize_datetime(
                    package.created_at
                ),
                "sales_count": sales_count,
                "revenue": revenue,
                "active_students": safe_int(
                    active_students
                ),
                "total_students": safe_int(
                    total_students
                ),
                "expired_students": safe_int(
                    expired_students
                ),
            }
        )

    # --------------------------------------------------------
    # Monthly revenue
    # --------------------------------------------------------

    monthly_revenue: List[Dict[str, Any]] = []

    cursor = datetime(
        start_date.year,
        start_date.month,
        1,
    )

    while cursor <= now:

        if cursor.month == 12:
            next_month = datetime(
                cursor.year + 1,
                1,
                1,
            )
        else:
            next_month = datetime(
                cursor.year,
                cursor.month + 1,
                1,
            )

        month_orders = (
            db.query(RazorpayOrder)
            .filter(
                RazorpayOrder.created_at >= cursor,
                RazorpayOrder.created_at < next_month,
                func.lower(RazorpayOrder.status).in_(
                    successful_statuses
                ),
            )
            .all()
        )

        month_sales = len(month_orders)

        month_revenue = sum(
            safe_float(order.amount_inr)
            for order in month_orders
        )

        monthly_revenue.append(
            {
                "month": cursor.strftime("%Y-%m"),
                "label": cursor.strftime("%b %Y"),
                "sales": month_sales,
                "revenue": month_revenue,
            }
        )

        cursor = next_month

    # --------------------------------------------------------
    # Payment status summary
    # --------------------------------------------------------

    status_rows = (
        db.query(
            RazorpayOrder.status,
            func.count(RazorpayOrder.id),
            func.coalesce(
                func.sum(RazorpayOrder.amount_inr),
                0,
            ),
        )
        .group_by(RazorpayOrder.status)
        .all()
    )

    payment_status = []

    for status, count, amount in status_rows:
        payment_status.append(
            {
                "status": str(status or "unknown"),
                "count": safe_int(count),
                "amount": safe_float(amount),
            }
        )

    # --------------------------------------------------------
    # Recent sales
    # --------------------------------------------------------

    recent_orders = (
        db.query(
            RazorpayOrder,
            User,
            SubscriptionPackage,
        )
        .outerjoin(
            User,
            User.id == RazorpayOrder.user_id,
        )
        .outerjoin(
            SubscriptionPackage,
            SubscriptionPackage.id
            == RazorpayOrder.package_id,
        )
        .order_by(
            RazorpayOrder.created_at.desc()
        )
        .limit(25)
        .all()
    )

    recent_sales = []

    for order, user, package in recent_orders:

        recent_sales.append(
            {
                "id": safe_int(order.id),
                "user_id": safe_int(order.user_id),
                "student_name": (
                    getattr(user, "full_name", None)
                    or getattr(user, "email", None)
                    or "Student"
                ),
                "student_email": getattr(
                    user,
                    "email",
                    None,
                ),
                "package_id": (
                    safe_int(order.package_id)
                    if order.package_id is not None
                    else None
                ),
                "package_title": (
                    getattr(package, "title", None)
                    if package
                    else "Package"
                ),
                "amount": safe_float(
                    order.amount_inr
                ),
                "currency": (
                    getattr(order, "currency", None)
                    or "INR"
                ),
                "status": str(
                    order.status or "unknown"
                ),
                "razorpay_order_id": (
                    order.razorpay_order_id
                ),
                "razorpay_payment_id": (
                    order.razorpay_payment_id
                ),
                "created_at": serialize_datetime(
                    order.created_at
                ),
            }
        )

    # --------------------------------------------------------
    # Top package
    # --------------------------------------------------------

    top_package = None

    if package_rows:
        top_package = max(
            package_rows,
            key=lambda item: item["revenue"],
        )

    # --------------------------------------------------------
    # Return
    # --------------------------------------------------------

    return {
        "generated_at": now.isoformat(),

        "overview": {
            "active_packages": safe_int(
                active_packages_count
            ),
            "total_packages": safe_int(
                total_packages_count
            ),
            "total_sales": safe_int(total_sales),
            "total_revenue": safe_float(
                total_revenue
            ),
            "active_students": safe_int(
                active_subscriptions
            ),
            "unique_active_students": safe_int(
                enrolled_students
            ),
        },

        "top_package": top_package,

        "packages": package_rows,

        "monthly_revenue": monthly_revenue,

        "payment_status": payment_status,

        "recent_sales": recent_sales,
    }