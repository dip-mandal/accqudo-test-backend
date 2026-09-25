from datetime import datetime, timedelta
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.v1.deps import get_db, get_current_user

from app.models.user import User
from app.models.package import Package
from app.models.payment import Payment
from app.models.subscription import Subscription


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
    """
    Only ADMIN and SUPER_ADMIN can access package sales.
    """

    role = str(
        getattr(current_user, "role", "") or ""
    ).strip().upper()

    # Support both the uppercase roles used by the current
    # Team Portal and lowercase roles if the database stores
    # them that way.
    if role not in {
        "ADMIN",
        "SUPER_ADMIN",
    }:
        raise HTTPException(
            status_code=403,
            detail="Admin or Super Admin access required.",
        )

    return current_user


# ============================================================
# Helpers
# ============================================================

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


def serialize_datetime(value: Any) -> str | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.isoformat()

    return str(value)


def get_attr(
    obj: Any,
    *names: str,
    default: Any = None,
) -> Any:
    """
    Safely retrieve a field from an existing ORM model.

    This keeps the sales endpoint tolerant of small naming
    differences in the existing models without changing
    those models.
    """

    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)

            if value is not None:
                return value

    return default


def normalized_status(value: Any) -> str:
    return str(value or "").strip().lower()


# ============================================================
# Successful payment statuses
# ============================================================

SUCCESSFUL_PAYMENT_STATUSES = {
    "paid",
    "success",
    "successful",
    "completed",
    "captured",
}

FAILED_PAYMENT_STATUSES = {
    "failed",
    "failure",
    "cancelled",
    "canceled",
}


# ============================================================
# Dashboard
# ============================================================

@router.get("/dashboard")
def get_package_sales_dashboard(
    months: int = Query(
        default=12,
        ge=1,
        le=36,
        description="Number of months to include in the report.",
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_admin_or_super_admin
    ),
) -> Dict[str, Any]:

    # --------------------------------------------------------
    # Current time
    # --------------------------------------------------------

    now = datetime.utcnow()

    start_date = now - timedelta(
        days=months * 31
    )

    # ========================================================
    # PACKAGES
    # ========================================================

    packages = (
        db.query(Package)
        .order_by(
            Package.created_at.desc()
        )
        .all()
    )

    active_packages = 0

    for package in packages:
        is_active = get_attr(
            package,
            "is_active",
            "isActive",
            default=True,
        )

        if bool(is_active):
            active_packages += 1

    # ========================================================
    # PAYMENTS
    # ========================================================

    all_payments = (
        db.query(Payment)
        .all()
    )

    successful_payments = []

    for payment in all_payments:

        status_value = normalized_status(
            get_attr(
                payment,
                "status",
                default="",
            )
        )

        if status_value in SUCCESSFUL_PAYMENT_STATUSES:
            successful_payments.append(
                payment
            )

    # --------------------------------------------------------
    # Total revenue
    # --------------------------------------------------------

    total_revenue = 0.0

    for payment in successful_payments:

        amount = get_attr(
            payment,
            "amount_inr",
            "amount",
            "price",
            "amount_paid",
            default=0,
        )

        total_revenue += safe_float(amount)

    total_sales = len(
        successful_payments
    )

    # ========================================================
    # SUBSCRIPTIONS
    # ========================================================

    all_subscriptions = (
        db.query(Subscription)
        .all()
    )

    active_subscriptions = []

    for subscription in all_subscriptions:

        is_active = get_attr(
            subscription,
            "is_active",
            "isActive",
            default=None,
        )

        status_value = normalized_status(
            get_attr(
                subscription,
                "status",
                default="",
            )
        )

        end_date = get_attr(
            subscription,
            "end_date",
            "endDate",
            "expires_at",
            "expiry_date",
            default=None,
        )

        currently_active = False

        if is_active is True:
            currently_active = True

        elif status_value in {
            "active",
            "paid",
            "success",
            "successful",
        }:
            currently_active = True

        if (
            end_date is not None
            and isinstance(end_date, datetime)
            and end_date < now
        ):
            currently_active = False

        if currently_active:
            active_subscriptions.append(
                subscription
            )

    # ========================================================
    # UNIQUE ACTIVE STUDENTS
    # ========================================================

    active_student_ids = set()

    for subscription in active_subscriptions:

        user_id = get_attr(
            subscription,
            "user_id",
            "userId",
            "student_id",
            "studentId",
            default=None,
        )

        if user_id is not None:
            active_student_ids.add(
                str(user_id)
            )

    # ========================================================
    # PACKAGE PERFORMANCE
    # ========================================================

    package_rows: List[
        Dict[str, Any]
    ] = []

    for package in packages:

        package_id = getattr(
            package,
            "id",
            None,
        )

        package_id_string = (
            str(package_id)
            if package_id is not None
            else None
        )

        # ----------------------------------------------------
        # Package sales
        # ----------------------------------------------------

        package_payments = []

        for payment in successful_payments:

            payment_package_id = get_attr(
                payment,
                "package_id",
                "packageId",
                default=None,
            )

            if (
                payment_package_id is not None
                and package_id_string is not None
                and str(payment_package_id)
                == package_id_string
            ):
                package_payments.append(
                    payment
                )

        sales_count = len(
            package_payments
        )

        package_revenue = 0.0

        for payment in package_payments:

            amount = get_attr(
                payment,
                "amount_inr",
                "amount",
                "price",
                "amount_paid",
                default=0,
            )

            package_revenue += safe_float(
                amount
            )

        # ----------------------------------------------------
        # Package subscriptions
        # ----------------------------------------------------

        package_subscriptions = []

        for subscription in all_subscriptions:

            subscription_package_id = get_attr(
                subscription,
                "package_id",
                "packageId",
                default=None,
            )

            if (
                subscription_package_id is not None
                and package_id_string is not None
                and str(subscription_package_id)
                == package_id_string
            ):
                package_subscriptions.append(
                    subscription
                )

        # ----------------------------------------------------
        # Unique students
        # ----------------------------------------------------

        all_student_ids = set()
        active_student_ids_for_package = set()
        expired_student_ids_for_package = set()

        for subscription in package_subscriptions:

            user_id = get_attr(
                subscription,
                "user_id",
                "userId",
                "student_id",
                "studentId",
                default=None,
            )

            if user_id is None:
                continue

            user_id_string = str(
                user_id
            )

            all_student_ids.add(
                user_id_string
            )

            is_active = get_attr(
                subscription,
                "is_active",
                "isActive",
                default=None,
            )

            status_value = normalized_status(
                get_attr(
                    subscription,
                    "status",
                    default="",
                )
            )

            end_date = get_attr(
                subscription,
                "end_date",
                "endDate",
                "expires_at",
                "expiry_date",
                default=None,
            )

            is_subscription_active = False

            if is_active is True:
                is_subscription_active = True

            elif status_value in {
                "active",
                "paid",
                "success",
                "successful",
            }:
                is_subscription_active = True

            if (
                end_date is not None
                and isinstance(end_date, datetime)
                and end_date < now
            ):
                is_subscription_active = False

            if is_subscription_active:
                active_student_ids_for_package.add(
                    user_id_string
                )
            else:
                expired_student_ids_for_package.add(
                    user_id_string
                )

        # ----------------------------------------------------
        # Package data
        # ----------------------------------------------------

        package_rows.append(
            {
                "id": package_id,
                "title": str(
                    get_attr(
                        package,
                        "title",
                        "name",
                        default="Package",
                    )
                ),
                "description": get_attr(
                    package,
                    "description",
                    default=None,
                ),
                "tier": str(
                    get_attr(
                        package,
                        "tier",
                        "package_type",
                        "type",
                        default="",
                    )
                ),
                "price": safe_float(
                    get_attr(
                        package,
                        "price_inr",
                        "price",
                        "amount",
                        default=0,
                    )
                ),
                "validity_days": safe_int(
                    get_attr(
                        package,
                        "validity_days",
                        "validityDays",
                        "duration_days",
                        default=0,
                    )
                ),
                "is_active": bool(
                    get_attr(
                        package,
                        "is_active",
                        "isActive",
                        default=True,
                    )
                ),
                "created_at": serialize_datetime(
                    get_attr(
                        package,
                        "created_at",
                        "createdAt",
                        default=None,
                    )
                ),
                "sales_count": sales_count,
                "revenue": package_revenue,
                "active_students": len(
                    active_student_ids_for_package
                ),
                "total_students": len(
                    all_student_ids
                ),
                "expired_students": len(
                    expired_student_ids_for_package
                ),
            }
        )

    # ========================================================
    # MONTHLY REVENUE
    # ========================================================

    monthly_revenue: List[
        Dict[str, Any]
    ] = []

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

        month_revenue = 0.0
        month_sales = 0

        for payment in successful_payments:

            created_at = get_attr(
                payment,
                "created_at",
                "createdAt",
                "paid_at",
                "payment_date",
                default=None,
            )

            if not isinstance(
                created_at,
                datetime,
            ):
                continue

            if (
                created_at >= cursor
                and created_at < next_month
            ):

                month_sales += 1

                amount = get_attr(
                    payment,
                    "amount_inr",
                    "amount",
                    "price",
                    "amount_paid",
                    default=0,
                )

                month_revenue += safe_float(
                    amount
                )

        monthly_revenue.append(
            {
                "month": cursor.strftime(
                    "%Y-%m"
                ),
                "label": cursor.strftime(
                    "%b %Y"
                ),
                "sales": month_sales,
                "revenue": month_revenue,
            }
        )

        cursor = next_month

    # ========================================================
    # PAYMENT STATUS SUMMARY
    # ========================================================

    payment_status_map: Dict[
        str,
        Dict[str, Any],
    ] = {}

    for payment in all_payments:

        status_value = normalized_status(
            get_attr(
                payment,
                "status",
                default="unknown",
            )
        )

        if not status_value:
            status_value = "unknown"

        if status_value not in payment_status_map:
            payment_status_map[
                status_value
            ] = {
                "status": status_value,
                "count": 0,
                "amount": 0.0,
            }

        payment_status_map[
            status_value
        ]["count"] += 1

        payment_status_map[
            status_value
        ]["amount"] += safe_float(
            get_attr(
                payment,
                "amount_inr",
                "amount",
                "price",
                "amount_paid",
                default=0,
            )
        )

    payment_status = list(
        payment_status_map.values()
    )

    # ========================================================
    # RECENT SALES
    # ========================================================

    recent_payment_objects = sorted(
        all_payments,
        key=lambda payment: (
            get_attr(
                payment,
                "created_at",
                "createdAt",
                "paid_at",
                default=datetime.min,
            )
            or datetime.min
        ),
        reverse=True,
    )[:25]

    recent_sales = []

    for payment in recent_payment_objects:

        user_id = get_attr(
            payment,
            "user_id",
            "userId",
            default=None,
        )

        package_id = get_attr(
            payment,
            "package_id",
            "packageId",
            default=None,
        )

        user = None

        if user_id is not None:
            user = (
                db.query(User)
                .filter(
                    User.id == user_id
                )
                .first()
            )

        package = None

        if package_id is not None:
            package = (
                db.query(Package)
                .filter(
                    Package.id == package_id
                )
                .first()
            )

        recent_sales.append(
            {
                "id": getattr(
                    payment,
                    "id",
                    None,
                ),
                "user_id": user_id,
                "student_name": (
                    get_attr(
                        user,
                        "full_name",
                        "name",
                        default="Student",
                    )
                    if user
                    else "Student"
                ),
                "student_email": (
                    get_attr(
                        user,
                        "email",
                        default=None,
                    )
                    if user
                    else None
                ),
                "package_id": package_id,
                "package_title": (
                    get_attr(
                        package,
                        "title",
                        "name",
                        default="Package",
                    )
                    if package
                    else "Package"
                ),
                "amount": safe_float(
                    get_attr(
                        payment,
                        "amount_inr",
                        "amount",
                        "price",
                        "amount_paid",
                        default=0,
                    )
                ),
                "currency": str(
                    get_attr(
                        payment,
                        "currency",
                        default="INR",
                    )
                ),
                "status": str(
                    get_attr(
                        payment,
                        "status",
                        default="unknown",
                    )
                ),
                "razorpay_order_id": get_attr(
                    payment,
                    "razorpay_order_id",
                    "order_id",
                    "razorpayOrderId",
                    default="",
                ),
                "razorpay_payment_id": get_attr(
                    payment,
                    "razorpay_payment_id",
                    "payment_id",
                    "razorpayPaymentId",
                    default=None,
                ),
                "created_at": serialize_datetime(
                    get_attr(
                        payment,
                        "created_at",
                        "createdAt",
                        "paid_at",
                        "payment_date",
                        default=None,
                    )
                ),
            }
        )

    # ========================================================
    # TOP PACKAGE
    # ========================================================

    top_package = None

    if package_rows:
        top_package = max(
            package_rows,
            key=lambda item: item[
                "revenue"
            ],
        )

    # ========================================================
    # RETURN
    # ========================================================

    return {
        "generated_at": now.isoformat(),

        "overview": {
            "active_packages": active_packages,
            "total_packages": len(
                packages
            ),
            "total_sales": total_sales,
            "total_revenue": total_revenue,
            "active_students": len(
                active_subscriptions
            ),
            "unique_active_students": len(
                active_student_ids
            ),
        },

        "top_package": top_package,

        "packages": package_rows,

        "monthly_revenue": monthly_revenue,

        "payment_status": payment_status,

        "recent_sales": recent_sales,
    }