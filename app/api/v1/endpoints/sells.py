from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.v1.deps import get_db, get_current_user

from app.models.user import User
from app.models.package import (
    SubscriptionPackage,
    UserSubscription,
    Payment,
    RazorpayOrder,
)


router = APIRouter(
    prefix="/team/sells",
    tags=["Team - Package Sales"],
)


# ============================================================
# Constants
# ============================================================

SUCCESSFUL_PAYMENT_STATUSES = {
    "paid",
    "success",
    "successful",
    "completed",
    "captured",
}

ACTIVE_SUBSCRIPTION_STATUSES = {
    "active",
    "paid",
    "success",
    "successful",
    "completed",
}


# ============================================================
# Authorization
# ============================================================

def require_admin_or_super_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Package sales information is restricted to
    ADMIN and SUPER_ADMIN users.
    """

    role = str(
        getattr(current_user, "role", "") or ""
    ).strip().upper()

    if role not in {
        "ADMIN",
        "SUPER_ADMIN",
        "SUPERADMIN",
    }:
        raise HTTPException(
            status_code=403,
            detail="Admin or Super Admin access required.",
        )

    return current_user


# ============================================================
# Generic helpers
# ============================================================

def get_value(
    obj: Any,
    *field_names: str,
    default: Any = None,
) -> Any:
    """
    Read the first available/non-null field from an ORM object.

    This allows the endpoint to work with the existing models
    without modifying their field names.
    """

    if obj is None:
        return default

    for field_name in field_names:
        if hasattr(obj, field_name):
            value = getattr(obj, field_name)

            if value is not None:
                return value

    return default


def to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_status(value: Any) -> str:
    return str(value or "").strip().lower()


def serialize_datetime(value: Any) -> Optional[str]:
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.isoformat()

    return str(value)


def get_object_id(obj: Any) -> Any:
    return getattr(obj, "id", None)


def is_successful_payment(payment: Any) -> bool:
    status_value = normalize_status(
        get_value(
            payment,
            "status",
            "payment_status",
            default="",
        )
    )

    return status_value in SUCCESSFUL_PAYMENT_STATUSES


def is_active_subscription(
    subscription: Any,
    now: datetime,
) -> bool:

    status_value = normalize_status(
        get_value(
            subscription,
            "status",
            "subscription_status",
            default="",
        )
    )

    is_active = get_value(
        subscription,
        "is_active",
        "isActive",
        default=None,
    )

    # Explicit false always means inactive.
    if is_active is False:
        return False

    active = False

    if is_active is True:
        active = True

    elif status_value in ACTIVE_SUBSCRIPTION_STATUSES:
        active = True

    # Check expiry/end date if the model contains one.
    expiry = get_value(
        subscription,
        "end_date",
        "endDate",
        "expires_at",
        "expiry_date",
        "valid_until",
        default=None,
    )

    if isinstance(expiry, datetime):
        if expiry < now:
            return False

    return active


def get_package_title(package: Any) -> str:
    return str(
        get_value(
            package,
            "title",
            "name",
            "package_name",
            "display_name",
            default="Package",
        )
    )


def get_package_price(package: Any) -> float:
    return to_float(
        get_value(
            package,
            "price_inr",
            "price",
            "amount",
            "amount_inr",
            "selling_price",
            default=0,
        )
    )


def get_payment_amount(payment: Any) -> float:
    return to_float(
        get_value(
            payment,
            "amount_inr",
            "amount",
            "price",
            "amount_paid",
            "paid_amount",
            default=0,
        )
    )


def get_package_id_from_payment(payment: Any) -> Any:
    return get_value(
        payment,
        "package_id",
        "packageId",
        "subscription_package_id",
        "subscriptionPackageId",
        default=None,
    )


def get_package_id_from_subscription(
    subscription: Any,
) -> Any:
    return get_value(
        subscription,
        "package_id",
        "packageId",
        "subscription_package_id",
        "subscriptionPackageId",
        default=None,
    )


def get_user_id_from_subscription(
    subscription: Any,
) -> Any:
    return get_value(
        subscription,
        "user_id",
        "userId",
        "student_id",
        "studentId",
        default=None,
    )


def get_user_id_from_payment(
    payment: Any,
) -> Any:
    return get_value(
        payment,
        "user_id",
        "userId",
        "student_id",
        "studentId",
        default=None,
    )


def get_created_at(obj: Any) -> Any:
    return get_value(
        obj,
        "created_at",
        "createdAt",
        "paid_at",
        "payment_date",
        "purchased_at",
        default=None,
    )


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

    now = datetime.utcnow()

    # --------------------------------------------------------
    # Calculate report start date
    # --------------------------------------------------------

    start_date = now - timedelta(
        days=months * 31
    )

    # ========================================================
    # LOAD EXISTING DATA
    # ========================================================

    packages = (
        db.query(SubscriptionPackage)
        .all()
    )

    subscriptions = (
        db.query(UserSubscription)
        .all()
    )

    payments = (
        db.query(Payment)
        .all()
    )

    # ========================================================
    # ACTIVE PACKAGES
    # ========================================================

    active_packages = 0

    for package in packages:

        package_active = get_value(
            package,
            "is_active",
            "isActive",
            default=True,
        )

        if bool(package_active):
            active_packages += 1

    # ========================================================
    # SUCCESSFUL PAYMENTS
    # ========================================================

    successful_payments = [
        payment
        for payment in payments
        if is_successful_payment(payment)
    ]

    # ========================================================
    # TOTAL REVENUE
    # ========================================================

    total_revenue = sum(
        get_payment_amount(payment)
        for payment in successful_payments
    )

    total_sales = len(
        successful_payments
    )

    # ========================================================
    # ACTIVE SUBSCRIPTIONS
    # ========================================================

    active_subscriptions = [
        subscription
        for subscription in subscriptions
        if is_active_subscription(
            subscription,
            now,
        )
    ]

    # ========================================================
    # UNIQUE ACTIVE STUDENTS
    # ========================================================

    active_student_ids = set()

    for subscription in active_subscriptions:

        user_id = get_user_id_from_subscription(
            subscription
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

        package_id = get_object_id(
            package
        )

        package_id_string = (
            str(package_id)
            if package_id is not None
            else None
        )

        # ----------------------------------------------------
        # Package payments
        # ----------------------------------------------------

        package_payments = []

        for payment in successful_payments:

            payment_package_id = (
                get_package_id_from_payment(
                    payment
                )
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

        package_revenue = sum(
            get_payment_amount(payment)
            for payment in package_payments
        )

        # ----------------------------------------------------
        # Package subscriptions
        # ----------------------------------------------------

        package_subscriptions = []

        for subscription in subscriptions:

            subscription_package_id = (
                get_package_id_from_subscription(
                    subscription
                )
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
        # Student counts
        # ----------------------------------------------------

        total_student_ids = set()
        active_student_ids_for_package = set()
        expired_student_ids_for_package = set()

        for subscription in package_subscriptions:

            user_id = get_user_id_from_subscription(
                subscription
            )

            if user_id is None:
                continue

            user_id_string = str(
                user_id
            )

            total_student_ids.add(
                user_id_string
            )

            if is_active_subscription(
                subscription,
                now,
            ):
                active_student_ids_for_package.add(
                    user_id_string
                )
            else:
                expired_student_ids_for_package.add(
                    user_id_string
                )

        # ----------------------------------------------------
        # Package record
        # ----------------------------------------------------

        package_rows.append(
            {
                "id": package_id,

                "title": get_package_title(
                    package
                ),

                "description": get_value(
                    package,
                    "description",
                    default=None,
                ),

                "tier": str(
                    get_value(
                        package,
                        "tier",
                        "package_tier",
                        "package_type",
                        "type",
                        default="",
                    )
                ),

                "price": get_package_price(
                    package
                ),

                "validity_days": to_int(
                    get_value(
                        package,
                        "validity_days",
                        "validityDays",
                        "duration_days",
                        "duration",
                        default=0,
                    )
                ),

                "is_active": bool(
                    get_value(
                        package,
                        "is_active",
                        "isActive",
                        default=True,
                    )
                ),

                "created_at": serialize_datetime(
                    get_value(
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
                    total_student_ids
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

            created_at = get_created_at(
                payment
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

                month_revenue += (
                    get_payment_amount(
                        payment
                    )
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

    for payment in payments:

        status_value = normalize_status(
            get_value(
                payment,
                "status",
                "payment_status",
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
        ]["amount"] += get_payment_amount(
            payment
        )

    payment_status = list(
        payment_status_map.values()
    )

    # ========================================================
    # RECENT SALES
    # ========================================================

    sorted_payments = sorted(
        payments,
        key=lambda payment: (
            get_created_at(payment)
            if isinstance(
                get_created_at(payment),
                datetime,
            )
            else datetime.min
        ),
        reverse=True,
    )

    recent_payment_objects = (
        sorted_payments[:25]
    )

    recent_sales = []

    for payment in recent_payment_objects:

        user_id = get_user_id_from_payment(
            payment
        )

        package_id = get_package_id_from_payment(
            payment
        )

        # ----------------------------------------------------
        # Student
        # ----------------------------------------------------

        user = None

        if user_id is not None:

            user = (
                db.query(User)
                .filter(
                    User.id == user_id
                )
                .first()
            )

        # ----------------------------------------------------
        # Package
        # ----------------------------------------------------

        package = None

        if package_id is not None:

            package = (
                db.query(
                    SubscriptionPackage
                )
                .filter(
                    SubscriptionPackage.id
                    == package_id
                )
                .first()
            )

        recent_sales.append(
            {
                "id": get_object_id(
                    payment
                ),

                "user_id": user_id,

                "student_name": (
                    get_value(
                        user,
                        "full_name",
                        "name",
                        default="Student",
                    )
                    if user
                    else "Student"
                ),

                "student_email": (
                    get_value(
                        user,
                        "email",
                        default=None,
                    )
                    if user
                    else None
                ),

                "package_id": package_id,

                "package_title": (
                    get_package_title(
                        package
                    )
                    if package
                    else "Package"
                ),

                "amount": get_payment_amount(
                    payment
                ),

                "currency": str(
                    get_value(
                        payment,
                        "currency",
                        default="INR",
                    )
                ),

                "status": str(
                    get_value(
                        payment,
                        "status",
                        "payment_status",
                        default="unknown",
                    )
                ),

                "razorpay_order_id": get_value(
                    payment,
                    "razorpay_order_id",
                    "order_id",
                    "razorpayOrderId",
                    default=None,
                ),

                "razorpay_payment_id": get_value(
                    payment,
                    "razorpay_payment_id",
                    "payment_id",
                    "razorpayPaymentId",
                    default=None,
                ),

                "created_at": serialize_datetime(
                    get_created_at(
                        payment
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
    # PROFIT
    # ========================================================
    #
    # IMPORTANT:
    # At this stage the database model information available
    # does not establish a package cost/expense field.
    #
    # Therefore we do NOT invent an expense value.
    #
    # "gross_revenue" is the actual successful payment revenue.
    # "profit" is returned as None until an actual expense/cost
    # field exists in the database.
    #
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

            "gross_revenue": total_revenue,

            "profit": None,

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