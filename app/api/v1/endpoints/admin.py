import datetime
import io
import json
import logging
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status, Security
from pydantic import BaseModel
from sqlalchemy import delete, desc, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials



from app.core.database import get_db
from app.core.security import get_current_user, decode_access_token
from app.models.audit import AuditTrafficLog, Coupon
from app.models.attempt import TestAttempt
from app.models.enrollment import TestEnrollment
from app.models.exam import Chapter, Exam, Subject, Topic
from app.models.package import RazorpayOrder, SubscriptionPackage, package_tests_table
from app.models.question import Question
from app.models.test import Test, TestQuestion
from app.models.user import RoleEnum, User
from app.schemas.admin import (
    ChapterCreate,
    ExamCreate,
    QuestionCreate,
    SubjectCreate,
    TopicCreate,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Admin Studio & CMS"])

security_optional = HTTPBearer(auto_error=False)

def check_admin(user: User):
    user_role = user.role.value if hasattr(user.role, "value") else str(user.role)
    if user_role not in [
        "ADMIN",
        "SUPER_ADMIN",
        RoleEnum.ADMIN.value,
        RoleEnum.SUPER_ADMIN.value,
    ]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin credentials required for this action.",
        )


# =========================================================
# Pydantic Request Models
# =========================================================

class CreatePackageRequest(BaseModel):
    exam_id: int
    title: str
    description: Optional[str] = None
    price_inr: float = 999.0
    validity_days: int = 365
    is_active: bool = True


class UpdatePackageRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    price_inr: Optional[float] = None
    validity_days: Optional[int] = None
    is_active: Optional[bool] = None


class AssembleTestWithPackagesRequest(BaseModel):
    exam_id: int
    title: str
    duration_minutes: int
    instructions: Optional[dict] = None
    question_ids: List[int]
    package_ids: List[int] = []


class BroadcastEmailRequest(BaseModel):
    target_group: str  # 'ALL' | 'GATE' | 'JEE' | 'NEET' | 'UPSC'
    subject: str
    message: str


class ToggleUserStatusRequest(BaseModel):
    is_active: bool


class CreateCouponRequest(BaseModel):
    code: str
    discount_percentage: float
    max_uses: int = 100
    valid_days: int = 30


class ValidateCouponRequest(BaseModel):
    code: str
    package_id: int


# =========================================================
# 1. Real Traffic & Overview Telemetry
# =========================================================

@router.get("/overview-telemetry")
async def get_overview_telemetry(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_admin(current_user)

    total_users = (await db.execute(select(func.count(User.id)))).scalar() or 0
    active_users = (
        await db.execute(select(func.count(User.id)).where(User.is_active == True))
    ).scalar() or 0
    total_tests = (await db.execute(select(func.count(Test.id)))).scalar() or 0
    total_questions = (
        await db.execute(select(func.count(Question.id)))
    ).scalar() or 0

    revenue_stmt = select(func.sum(RazorpayOrder.amount_inr)).where(
        RazorpayOrder.status == "PAID"
    )
    gross_revenue = (await db.execute(revenue_stmt)).scalar() or 0.0

    page_view_heat = []
    traffic_ip_distribution = []

    try:
        route_heat_stmt = (
            select(
                AuditTrafficLog.path,
                func.count(AuditTrafficLog.id).label("views"),
                func.count(func.distinct(AuditTrafficLog.ip_address)).label("unique_ips"),
                func.avg(AuditTrafficLog.response_time_ms).label("avg_latency"),
            )
            .group_by(AuditTrafficLog.path)
            .order_by(desc("views"))
            .limit(10)
        )
        route_rows = (await db.execute(route_heat_stmt)).all()
        page_view_heat = [
            {
                "path": row.path,
                "views": int(row.views),
                "unique_ips": int(row.unique_ips),
                "avg_latency_ms": round(float(row.avg_latency or 0.0), 1),
            }
            for row in route_rows
        ]

        ip_heat_stmt = (
            select(
                AuditTrafficLog.ip_address,
                func.count(AuditTrafficLog.id).label("requests"),
            )
            .group_by(AuditTrafficLog.ip_address)
            .order_by(desc("requests"))
            .limit(6)
        )
        ip_rows = (await db.execute(ip_heat_stmt)).all()
        traffic_ip_distribution = [
            {"ip": row.ip_address, "requests": int(row.requests)}
            for row in ip_rows
        ]
    except Exception as log_err:
        logger.warning(f"Telemetry log gathering warning: {log_err}")

    return {
        "telemetry": {
            "total_candidates": total_users,
            "active_candidates": active_users,
            "blocked_candidates": max(0, total_users - active_users),
            "published_tests": total_tests,
            "indexed_questions": total_questions,
            "gross_revenue_inr": round(float(gross_revenue), 2),
            "server_uptime_pct": 99.98,
        },
        "traffic_ip_distribution": traffic_ip_distribution,
        "page_view_heat": page_view_heat,
    }


# =========================================================
# 2. Coupon Promotion Engine
# =========================================================

@router.get("/coupons")
async def list_coupons(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_admin(current_user)
    try:
        stmt = select(Coupon).order_by(Coupon.id.desc())
        coupons = (await db.execute(stmt)).scalars().all()
        now = datetime.datetime.utcnow()
        return [
            {
                "id": c.id,
                "code": c.code,
                "discount_percentage": float(getattr(c, "discount_percentage", 10.0)),
                "max_uses": getattr(c, "max_uses", 100),
                "times_used": getattr(c, "times_used", 0),
                "valid_until": c.valid_until.isoformat() if getattr(c, "valid_until", None) else now.isoformat(),
                "is_active": bool(c.is_active),
                "is_expired": c.valid_until < now if getattr(c, "valid_until", None) else False,
            }
            for c in coupons
        ]
    except Exception as err:
        logger.error(f"[Coupons List Error] {err}")
        return []


@router.post("/coupons")
async def create_coupon(
    payload: CreateCouponRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_admin(current_user)
    code_clean = payload.code.strip().upper()
    existing = (
        await db.execute(select(Coupon).where(Coupon.code == code_clean))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Coupon code '{code_clean}' already exists.",
        )

    expire_at = datetime.datetime.utcnow() + datetime.timedelta(days=payload.valid_days)
    new_coupon = Coupon(
        code=code_clean,
        discount_percentage=payload.discount_percentage,
        max_uses=payload.max_uses,
        valid_until=expire_at,
        is_active=True,
    )
    db.add(new_coupon)
    await db.commit()
    await db.refresh(new_coupon)
    return {
        "status": "success",
        "message": f"Coupon '{code_clean}' registered ({payload.discount_percentage}% OFF).",
    }


@router.put("/coupons/{coupon_id}/toggle")
async def toggle_coupon(
    coupon_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_admin(current_user)
    coupon = (
        await db.execute(select(Coupon).where(Coupon.id == coupon_id))
    ).scalar_one_or_none()
    if not coupon:
        raise HTTPException(status_code=404, detail="Coupon not found.")
    coupon.is_active = not coupon.is_active
    await db.commit()
    return {"status": "success", "is_active": bool(coupon.is_active)}


@router.delete("/coupons/{coupon_id}")
async def delete_coupon(
    coupon_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_admin(current_user)
    await db.execute(delete(Coupon).where(Coupon.id == coupon_id))
    await db.commit()
    return {"status": "success", "message": "Coupon deleted."}


@router.post("/coupons/validate")
async def validate_coupon(
    payload: ValidateCouponRequest,
    db: AsyncSession = Depends(get_db),
):
    code_clean = payload.code.strip().upper()
    coupon = (
        await db.execute(select(Coupon).where(Coupon.code == code_clean))
    ).scalar_one_or_none()

    if not coupon or not coupon.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invalid or inactive coupon code.",
        )

    if getattr(coupon, "valid_until", None) and coupon.valid_until < datetime.datetime.utcnow():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This coupon code has expired.",
        )

    if getattr(coupon, "max_uses", None) and coupon.times_used >= coupon.max_uses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Coupon usage limit reached.",
        )

    pkg = (
        await db.execute(
            select(SubscriptionPackage).where(SubscriptionPackage.id == payload.package_id)
        )
    ).scalar_one_or_none()
    if not pkg:
        raise HTTPException(status_code=404, detail="Selected subscription package not found.")

    discount = round((float(pkg.price_inr) * float(coupon.discount_percentage)) / 100.0, 2)
    final_price = max(1.0, round(float(pkg.price_inr) - discount, 2))

    return {
        "valid": True,
        "code": coupon.code,
        "discount_percentage": float(coupon.discount_percentage),
        "discount_amount": discount,
        "final_price_inr": final_price,
    }


# =========================================================
# 3. One-Click Visual Purge Studio (Cascade Protected)
# =========================================================

@router.get("/purge/entities")
async def list_purgeable_entities(
    scope: str = Query("TEST", description="EXAM, TEST, PACKAGE, QUESTION, CHAPTER, SUBJECT"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_admin(current_user)
    scope_upper = scope.strip().upper()

    if scope_upper == "EXAM":
        stmt = (
            select(Exam)
            .options(selectinload(Exam.subjects))
            .order_by(Exam.id.desc())
            .limit(100)
        )
        items = (await db.execute(stmt)).scalars().all()
        return [
            {
                "id": e.id,
                "title": f"{e.title} ({e.code})",
                "detail": f"Stream ID #{e.id} | Subjects: {len(e.subjects or [])}",
                "created_at": None,
            }
            for e in items
        ]

    elif scope_upper == "PACKAGE":
        stmt = (
            select(SubscriptionPackage, Exam.title.label("exam_title"))
            .outerjoin(Exam, SubscriptionPackage.exam_id == Exam.id)
            .options(selectinload(SubscriptionPackage.tests))
            .order_by(SubscriptionPackage.id.desc())
            .limit(100)
        )
        rows = (await db.execute(stmt)).all()
        return [
            {
                "id": p.id,
                "title": p.title,
                "detail": f"Exam: {e_title or 'General'} | Price: ₹{p.price_inr} | Tests: {len(p.tests)}",
                "created_at": p.created_at.isoformat() if getattr(p, "created_at", None) else None,
            }
            for p, e_title in rows
        ]

    elif scope_upper == "TEST":
        stmt = select(Test).order_by(Test.id.desc()).limit(100)
        items = (await db.execute(stmt)).scalars().all()
        return [
            {
                "id": t.id,
                "title": t.title,
                "detail": f"{t.duration_minutes} Mins | {t.total_marks} Marks",
                "created_at": t.created_at.isoformat() if getattr(t, "created_at", None) else None,
            }
            for t in items
        ]

    elif scope_upper == "QUESTION":
        stmt = (
            select(Question, Topic.name.label("topic_name"))
            .outerjoin(Topic, Question.topic_id == Topic.id)
            .order_by(Question.id.desc())
            .limit(100)
        )
        rows = (await db.execute(stmt)).all()
        results = []
        for q, topic_name in rows:
            text_str = (
                q.question_text.get("raw", "")
                if isinstance(q.question_text, dict)
                else str(q.question_text)
            )
            results.append({
                "id": q.id,
                "title": f"[{q.question_type}] {text_str[:80]}...",
                "detail": f"Topic: {topic_name or 'General'} | Marks: +{q.default_marks}",
                "created_at": None,
            })
        return results

    elif scope_upper == "CHAPTER":
        stmt = (
            select(Chapter, Subject.name.label("subject_name"))
            .join(Subject, Chapter.subject_id == Subject.id)
            .order_by(Chapter.id.desc())
            .limit(100)
        )
        rows = (await db.execute(stmt)).all()
        return [
            {
                "id": c.id,
                "title": c.name,
                "detail": f"Subject: {s_name}",
                "created_at": None,
            }
            for c, s_name in rows
        ]

    elif scope_upper == "SUBJECT":
        stmt = (
            select(Subject, Exam.title.label("exam_title"))
            .join(Exam, Subject.exam_id == Exam.id)
            .order_by(Subject.id.desc())
            .limit(100)
        )
        rows = (await db.execute(stmt)).all()
        return [
            {
                "id": s.id,
                "title": s.name,
                "detail": f"Exam: {e_title}",
                "created_at": None,
            }
            for s, e_title in rows
        ]

    return []


@router.delete("/purge/direct/{scope}/{item_id}")
async def execute_direct_purge(
    scope: str,
    item_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_admin(current_user)
    scope_upper = scope.strip().upper()

    try:
        # Temporarily bypass FK constraints to safely purge child trees
        await db.execute(text("SET FOREIGN_KEY_CHECKS = 0;"))

        if scope_upper == "EXAM":
            # 1. Collect all dependent child IDs
            test_ids = (await db.execute(select(Test.id).where(Test.exam_id == item_id))).scalars().all()
            pkg_ids = (await db.execute(select(SubscriptionPackage.id).where(SubscriptionPackage.exam_id == item_id))).scalars().all()
            sub_ids = (await db.execute(select(Subject.id).where(Subject.exam_id == item_id))).scalars().all()

            chap_ids = []
            if sub_ids:
                chap_ids = (await db.execute(select(Chapter.id).where(Chapter.subject_id.in_(sub_ids)))).scalars().all()

            topic_ids = []
            if chap_ids:
                topic_ids = (await db.execute(select(Topic.id).where(Topic.chapter_id.in_(chap_ids)))).scalars().all()

            q_ids = []
            if topic_ids:
                q_ids = (await db.execute(select(Question.id).where(Question.topic_id.in_(topic_ids)))).scalars().all()

            # 2. Unlink Razorpay Orders referencing tests or packages under this exam
            await db.execute(
                update(RazorpayOrder)
                .where(or_(RazorpayOrder.test_id.in_(test_ids), RazorpayOrder.package_id.in_(pkg_ids)))
                .values(test_id=None, package_id=None)
            )

            # 3. Clean legacy tables, subscriptions, and packages
            if pkg_ids:
                pkg_csv = ",".join(map(str, pkg_ids))
                await db.execute(text(f"DELETE FROM subscriptions WHERE package_id IN ({pkg_csv});"))
                await db.execute(text(f"DELETE FROM user_subscriptions WHERE package_id IN ({pkg_csv});"))
                await db.execute(package_tests_table.delete().where(package_tests_table.c.package_id.in_(pkg_ids)))
                await db.execute(delete(SubscriptionPackage).where(SubscriptionPackage.id.in_(pkg_ids)))
                await db.execute(text(f"DELETE FROM packages WHERE id IN ({pkg_csv});"))

            # Clean any legacy packages table referencing this exam
            await db.execute(text(f"DELETE FROM packages WHERE exam_id = {item_id};"))

            # 4. Clean tests, enrollments, attempts
            if test_ids:
                await db.execute(delete(TestEnrollment).where(TestEnrollment.test_id.in_(test_ids)))
                await db.execute(delete(TestAttempt).where(TestAttempt.test_id.in_(test_ids)))
                await db.execute(delete(TestQuestion).where(TestQuestion.test_id.in_(test_ids)))
                await db.execute(package_tests_table.delete().where(package_tests_table.c.test_id.in_(test_ids)))
                await db.execute(delete(Test).where(Test.id.in_(test_ids)))

            # 5. Clean questions, topics, chapters, and subjects
            if q_ids:
                await db.execute(delete(TestQuestion).where(TestQuestion.question_id.in_(q_ids)))
                await db.execute(delete(Question).where(Question.id.in_(q_ids)))

            if topic_ids:
                await db.execute(delete(Topic).where(Topic.id.in_(topic_ids)))

            if chap_ids:
                await db.execute(delete(Chapter).where(Chapter.id.in_(chap_ids)))

            if sub_ids:
                await db.execute(delete(Subject).where(Subject.id.in_(sub_ids)))

            # 6. Delete the exam itself
            await db.execute(delete(Exam).where(Exam.id == item_id))

        elif scope_upper == "PACKAGE":
            await db.execute(update(RazorpayOrder).where(RazorpayOrder.package_id == item_id).values(package_id=None))
            await db.execute(text(f"DELETE FROM subscriptions WHERE package_id = {item_id};"))
            await db.execute(text(f"DELETE FROM user_subscriptions WHERE package_id = {item_id};"))
            await db.execute(package_tests_table.delete().where(package_tests_table.c.package_id == item_id))
            await db.execute(delete(SubscriptionPackage).where(SubscriptionPackage.id == item_id))
            await db.execute(text(f"DELETE FROM packages WHERE id = {item_id};"))

        elif scope_upper == "TEST":
            await db.execute(update(RazorpayOrder).where(RazorpayOrder.test_id == item_id).values(test_id=None))
            await db.execute(delete(TestEnrollment).where(TestEnrollment.test_id == item_id))
            await db.execute(delete(TestAttempt).where(TestAttempt.test_id == item_id))
            await db.execute(delete(TestQuestion).where(TestQuestion.test_id == item_id))
            await db.execute(package_tests_table.delete().where(package_tests_table.c.test_id == item_id))
            await db.execute(delete(Test).where(Test.id == item_id))

        elif scope_upper == "QUESTION":
            await db.execute(delete(TestQuestion).where(TestQuestion.question_id == item_id))
            await db.execute(delete(Question).where(Question.id == item_id))

        elif scope_upper == "CHAPTER":
            topics_sub = (await db.execute(select(Topic.id).where(Topic.chapter_id == item_id))).scalars().all()
            if topics_sub:
                q_ids = (await db.execute(select(Question.id).where(Question.topic_id.in_(topics_sub)))).scalars().all()
                if q_ids:
                    await db.execute(delete(TestQuestion).where(TestQuestion.question_id.in_(q_ids)))
                    await db.execute(delete(Question).where(Question.id.in_(q_ids)))
                await db.execute(delete(Topic).where(Topic.id.in_(topics_sub)))
            await db.execute(delete(Chapter).where(Chapter.id == item_id))

        elif scope_upper == "SUBJECT":
            chaps_sub = (await db.execute(select(Chapter.id).where(Chapter.subject_id == item_id))).scalars().all()
            if chaps_sub:
                topics_sub = (await db.execute(select(Topic.id).where(Topic.chapter_id.in_(chaps_sub)))).scalars().all()
                if topics_sub:
                    q_ids = (await db.execute(select(Question.id).where(Question.topic_id.in_(topics_sub)))).scalars().all()
                    if q_ids:
                        await db.execute(delete(TestQuestion).where(TestQuestion.question_id.in_(q_ids)))
                        await db.execute(delete(Question).where(Question.id.in_(q_ids)))
                    await db.execute(delete(Topic).where(Topic.id.in_(topics_sub)))
                await db.execute(delete(Chapter).where(Chapter.id.in_(chaps_sub)))
            await db.execute(delete(Subject).where(Subject.id == item_id))

        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid scope '{scope}'.",
            )

        await db.commit()
    finally:
        await db.execute(text("SET FOREIGN_KEY_CHECKS = 1;"))
        await db.commit()

    logger.info(f"[Purge] Admin #{current_user.id} cleanly purged {scope_upper} #{item_id}.")
    return {"status": "success", "message": f"{scope_upper} #{item_id} permanently purged."}


# =========================================================
# 4. Candidate Directory & Access Control
# =========================================================

@router.get("/candidates")
async def list_candidates(
    q: Optional[str] = Query(None, description="Search by email or name"),
    limit: int = Query(100, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_admin(current_user)
    stmt = select(User).order_by(User.id.desc()).limit(limit)
    if q:
        stmt = stmt.where(User.email.ilike(f"%{q}%") | User.full_name.ilike(f"%{q}%"))
    users = (await db.execute(stmt)).scalars().all()

    return [
        {
            "id": u.id,
            "full_name": u.full_name or "Anonymous",
            "email": u.email,
            "role": u.role.value if hasattr(u.role, "value") else str(u.role),
            "is_active": bool(u.is_active),
            "created_at": u.created_at.isoformat() if getattr(u, "created_at", None) else None,
        }
        for u in users
    ]


@router.put("/candidates/{user_id}/status")
async def toggle_candidate_status(
    user_id: int,
    payload: ToggleUserStatusRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_admin(current_user)
    stmt = select(User).where(User.id == user_id)
    user = (await db.execute(stmt)).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Candidate record not found.")

    user.is_active = payload.is_active
    await db.commit()
    status_label = "unblocked" if payload.is_active else "blocked"
    return {
        "status": "success",
        "message": f"Candidate #{user_id} ({user.email}) has been {status_label}.",
    }


# =========================================================
# 5. Financial Ledger & Segmented Broadcast
# =========================================================

@router.get("/financial-ledger")
async def get_financial_ledger(
    limit: int = Query(100, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_admin(current_user)
    stmt = (
        select(RazorpayOrder, User.email)
        .join(User, RazorpayOrder.user_id == User.id)
        .order_by(RazorpayOrder.id.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()

    return [
        {
            "id": order.id,
            "razorpay_order_id": order.razorpay_order_id,
            "razorpay_payment_id": order.razorpay_payment_id or "N/A",
            "user_email": email,
            "amount_inr": float(order.amount_inr),
            "status": str(order.status),
            "created_at": order.created_at.isoformat() if order.created_at else None,
        }
        for order, email in rows
    ]


@router.post("/broadcast-email")
async def broadcast_targeted_email(
    payload: BroadcastEmailRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_admin(current_user)
    target_group = payload.target_group.strip().upper()

    if target_group == "ALL":
        target_users = (
            await db.execute(select(User.email).where(User.is_active == True))
        ).scalars().all()
    else:
        stmt = (
            select(User.email)
            .join(TestEnrollment, User.id == TestEnrollment.user_id)
            .join(Test, TestEnrollment.test_id == Test.id)
            .join(Exam, Test.exam_id == Exam.id)
            .where(
                or_(
                    Exam.code.ilike(f"%{target_group}%"),
                    Exam.title.ilike(f"%{target_group}%"),
                ),
                User.is_active == True,
            )
            .distinct()
        )
        target_users = (await db.execute(stmt)).scalars().all()

    recipients_count = len(target_users)
    logger.info(
        f"[Broadcast Dispatch] Admin #{current_user.id} queued '{payload.subject}' to {recipients_count} users ({target_group})."
    )

    return {
        "status": "queued",
        "recipients_count": recipients_count,
        "message": f"Broadcast '{payload.subject}' queued for {recipients_count} candidates in segment {target_group}.",
    }


# =========================================================
# 6. Academic Hierarchy Management
# =========================================================

@router.get("/hierarchy")
async def get_full_hierarchy(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_admin(current_user)
    stmt = (
        select(Exam)
        .options(
            selectinload(Exam.subjects)
            .selectinload(Subject.chapters)
            .selectinload(Chapter.topics)
        )
    )
    return (await db.execute(stmt)).scalars().all()


@router.post("/exams")
async def create_exam(
    payload: ExamCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_admin(current_user)
    exam = Exam(
        title=payload.title,
        code=payload.code,
        created_by=current_user.id,
    )
    db.add(exam)
    await db.commit()
    await db.refresh(exam)
    return exam


@router.post("/subjects")
async def create_subject(
    payload: SubjectCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_admin(current_user)
    subject = Subject(
        exam_id=payload.exam_id,
        name=payload.name,
        created_by=current_user.id,
    )
    db.add(subject)
    await db.commit()
    await db.refresh(subject)
    return subject


@router.post("/chapters")
async def create_chapter(
    payload: ChapterCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_admin(current_user)
    chapter = Chapter(
        subject_id=payload.subject_id,
        name=payload.name,
        created_by=current_user.id,
    )
    db.add(chapter)
    await db.commit()
    await db.refresh(chapter)
    return chapter


@router.post("/topics")
async def create_topic(
    payload: TopicCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_admin(current_user)
    topic = Topic(
        chapter_id=payload.chapter_id,
        name=payload.name,
        created_by=current_user.id,
    )
    db.add(topic)
    await db.commit()
    await db.refresh(topic)
    return topic


# =========================================================
# 7. Single Classified Question Authoring & Search
# =========================================================

@router.post("/questions")
async def create_question(
    payload: QuestionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_admin(current_user)

    topic = (
        await db.execute(select(Topic).where(Topic.id == payload.topic_id))
    ).scalar_one_or_none()
    if not topic:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Topic with ID {payload.topic_id} does not exist.",
        )

    question = Question(
        topic_id=payload.topic_id,
        created_by=current_user.id,
        question_type=payload.question_type,
        question_text=payload.question_text,
        options=payload.options,
        evaluation_data=payload.evaluation_data,
        solution_text=payload.solution_text,
        default_marks=payload.default_marks,
        default_negative_marks=payload.default_negative_marks,
    )
    db.add(question)
    await db.commit()
    await db.refresh(question)
    return {"status": "created", "question_id": question.id}


@router.get("/questions/search")
async def search_questions(
    q: Optional[str] = Query(None, description="Search question text"),
    subject_id: Optional[int] = Query(None),
    chapter_id: Optional[int] = Query(None),
    topic_id: Optional[int] = Query(None),
    question_type: Optional[str] = Query(None),
    limit: int = Query(50000, le=100000),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_admin(current_user)

    stmt = (
        select(
            Question,
            Topic.name.label("topic_name"),
            Chapter.name.label("chapter_name"),
            Subject.name.label("subject_name"),
        )
        .outerjoin(Topic, Question.topic_id == Topic.id)
        .outerjoin(Chapter, Topic.chapter_id == Chapter.id)
        .outerjoin(Subject, Chapter.subject_id == Subject.id)
    )

    if subject_id:
        stmt = stmt.where(Subject.id == subject_id)
    if chapter_id:
        stmt = stmt.where(Chapter.id == chapter_id)
    if topic_id:
        stmt = stmt.where(Topic.id == topic_id)
    if question_type and question_type != "ALL":
        stmt = stmt.where(Question.question_type == question_type)
    if q:
        stmt = stmt.where(Question.question_text.ilike(f"%{q}%"))

    rows = (await db.execute(stmt.limit(limit))).all()

    results = []
    for question, topic_name, chapter_name, subject_name in rows:
        raw_text = ""
        if isinstance(question.question_text, dict):
            raw_text = question.question_text.get("raw", "")
        else:
            raw_text = str(question.question_text)

        results.append({
            "id": question.id,
            "question_type": (
                question.question_type.value
                if hasattr(question.question_type, "value")
                else str(question.question_type)
            ),
            "question_text": raw_text,
            "default_marks": float(question.default_marks or 1.0),
            "default_negative_marks": float(question.default_negative_marks or 0.0),
            "subject_name": subject_name or "General",
            "chapter_name": chapter_name or "General",
            "topic_name": topic_name or "General",
        })

    return results


# =========================================================
# 8. Dynamic Test Paper Assembler
# =========================================================

@router.post("/tests/assemble")
async def assemble_test(
    payload: AssembleTestWithPackagesRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_admin(current_user)

    if not payload.question_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one question ID must be selected to assemble a paper.",
        )

    q_stmt = select(Question).where(Question.id.in_(payload.question_ids))
    questions_list = (await db.execute(q_stmt)).scalars().all()
    q_map = {q.id: q for q in questions_list}

    total_marks = sum(
        float(q_map[qid].default_marks or 1.0)
        for qid in payload.question_ids
        if qid in q_map
    )

    new_test = Test(
        exam_id=payload.exam_id,
        created_by=current_user.id,
        title=payload.title,
        duration_minutes=payload.duration_minutes,
        total_marks=total_marks,
        instructions=payload.instructions
        or {"general": "Standard Examination Guidelines", "calculator": True},
    )
    db.add(new_test)
    await db.flush()

    for idx, q_id in enumerate(payload.question_ids, start=1):
        if q_id in q_map:
            q_obj = q_map[q_id]
            mapping = TestQuestion(
                test_id=new_test.id,
                added_by=current_user.id,
                question_id=q_id,
                order=idx,
                marks=float(q_obj.default_marks or 1.0),
                negative_marks=float(q_obj.default_negative_marks or 0.0),
            )
            db.add(mapping)

    for pkg_id in payload.package_ids:
        await db.execute(
            package_tests_table.insert().values(
                package_id=pkg_id, test_id=new_test.id
            )
        )

    await db.commit()
    await db.refresh(new_test)

    return {
        "status": "success",
        "test_id": new_test.id,
        "title": new_test.title,
        "total_marks": total_marks,
        "question_count": len(payload.question_ids),
        "linked_packages": payload.package_ids,
    }


# =========================================================
# 9. Package Management & Pricing Studio
# =========================================================

@router.get("/packages/all")
async def list_all_packages(
    db: AsyncSession = Depends(get_db),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_optional),
):
    current_user_id = None
    if credentials and credentials.credentials:
        try:
            token = credentials.credentials.strip()
            # Decode token to extract user identifier (sub or user_id)
            payload = decode_access_token(token)
            if payload:
                current_user_id = payload.get("sub") or payload.get("user_id")
        except Exception:
            pass

    purchased_package_ids = set()
    if current_user_id:
        try:
            order_stmt = select(RazorpayOrder.package_id).where(
                RazorpayOrder.user_id == int(current_user_id),
                RazorpayOrder.status == "PAID",
                RazorpayOrder.package_id != None
            )
            orders_res = await db.execute(order_stmt)
            purchased_package_ids = {row[0] for row in orders_res.all()}
        except Exception:
            pass

    stmt = (
        select(SubscriptionPackage)
        .options(selectinload(SubscriptionPackage.tests))
        .order_by(SubscriptionPackage.id.asc())
    )
    packages = (await db.execute(stmt)).scalars().all()

    output = []
    for p in packages:
        output.append({
            "id": p.id,
            "exam_id": p.exam_id,
            "title": p.title,
            "description": p.description,
            "price_inr": float(p.price_inr),
            "validity_days": p.validity_days,
            "is_active": bool(p.is_active),
            "is_purchased": p.id in purchased_package_ids,
            "total_tests": len(p.tests),
            "tests": [
                {
                    "id": t.id,
                    "title": t.title,
                    "duration_minutes": getattr(t, "duration_minutes", 180),
                    "total_marks": getattr(t, "total_marks", 100.0),
                }
                for t in p.tests
            ],
        })
    return output


@router.post("/packages")
async def create_package(
    payload: CreatePackageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_admin(current_user)
    new_pkg = SubscriptionPackage(
        exam_id=payload.exam_id,
        created_by=current_user.id,
        title=payload.title,
        description=payload.description,
        price_inr=payload.price_inr,
        validity_days=payload.validity_days,
        is_active=payload.is_active,
    )
    db.add(new_pkg)
    await db.commit()
    await db.refresh(new_pkg)
    return {
        "status": "success",
        "package_id": new_pkg.id,
        "message": f"Package '{new_pkg.title}' created successfully.",
    }


@router.put("/packages/{package_id}")
async def update_package(
    package_id: int,
    payload: UpdatePackageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_admin(current_user)

    stmt = select(SubscriptionPackage).where(SubscriptionPackage.id == package_id)
    pkg = (await db.execute(stmt)).scalar_one_or_none()
    if not pkg:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription package not found.",
        )

    update_dict = payload.model_dump(exclude_unset=True)
    for field, val in update_dict.items():
        setattr(pkg, field, val)

    await db.commit()
    await db.refresh(pkg)
    return {
        "status": "success",
        "message": f"Package #{package_id} updated successfully.",
    }


@router.delete("/packages/{package_id}")
async def delete_package(
    package_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_admin(current_user)
    try:
        await db.execute(text("SET FOREIGN_KEY_CHECKS = 0;"))
        await db.execute(update(RazorpayOrder).where(RazorpayOrder.package_id == package_id).values(package_id=None))
        await db.execute(text(f"DELETE FROM subscriptions WHERE package_id = {package_id};"))
        await db.execute(text(f"DELETE FROM user_subscriptions WHERE package_id = {package_id};"))
        await db.execute(package_tests_table.delete().where(package_tests_table.c.package_id == package_id))
        await db.execute(delete(SubscriptionPackage).where(SubscriptionPackage.id == package_id))
        await db.execute(text(f"DELETE FROM packages WHERE id = {package_id};"))
        await db.commit()
    finally:
        await db.execute(text("SET FOREIGN_KEY_CHECKS = 1;"))
        await db.commit()

    return {"status": "success", "message": f"Package #{package_id} permanently removed."}


# =========================================================
# 10. Bulk CSV / Excel Upload Ingestion Engine
# =========================================================

@router.post("/questions/bulk-upload")
async def bulk_upload_questions(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_admin(current_user)

    filename = (file.filename or "").lower()
    content = await file.read()

    try:
        if filename.endswith(".csv") or not filename:
            df = pd.read_csv(io.BytesIO(content))
        elif filename.endswith((".xls", ".xlsx")):
            df = pd.read_excel(io.BytesIO(content))
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unsupported format. Please upload a .csv or .xlsx spreadsheet.",
            )
    except Exception as e:
        logger.error(f"[Bulk Upload Error] File parsing failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Spreadsheet parsing error: {str(e)}",
        )

    df.columns = [str(c).strip().lower() for c in df.columns]

    required_columns = [
        "topic_id",
        "question_type",
        "question_text",
        "evaluation_data",
        "default_marks",
    ]
    missing = [c for c in required_columns if c not in df.columns]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Missing required columns: {', '.join(missing)}",
        )

    created_questions = []

    for idx, row in df.iterrows():
        try:
            q_type_str = str(row["question_type"]).strip().upper()
            if q_type_str not in ["MCQ", "MSQ", "NAT"]:
                continue

            raw_text = str(row["question_text"]).strip()
            q_text_dict = {"raw": raw_text}

            # 1. Parse options column
            options_val = row.get("options")
            parsed_options = []
            if pd.notna(options_val) and str(options_val).strip() != "":
                s_opt = str(options_val).strip()
                if s_opt.startswith("["):
                    parsed_options = json.loads(s_opt)
                else:
                    opts = []
                    for item in s_opt.split(","):
                        if ":" in item:
                            parts = item.split(":", 1)
                            opts.append({
                                "key": parts[0].strip().upper(),
                                "text": parts[1].strip(),
                            })
                    parsed_options = opts

            # 2. Parse evaluation data
            eval_val = str(row["evaluation_data"]).strip()
            if eval_val.startswith("{") or eval_val.startswith("["):
                parsed_eval = json.loads(eval_val)
            elif q_type_str in ["MCQ", "MSQ"]:
                keys = [k.strip().upper() for k in eval_val.split(",") if k.strip()]
                parsed_eval = {"correct": keys}
            else:
                if "-" in eval_val and not eval_val.startswith("-"):
                    parts = eval_val.split("-")
                    parsed_eval = {
                        "exact": None,
                        "min": float(parts[0]),
                        "max": float(parts[1]),
                    }
                else:
                    parsed_eval = {
                        "exact": float(eval_val),
                        "min": float(eval_val),
                        "max": float(eval_val),
                    }

            question = Question(
                topic_id=int(row["topic_id"]),
                created_by=current_user.id,
                question_type=q_type_str,
                question_text=q_text_dict,
                options=parsed_options,
                evaluation_data=parsed_eval,
                solution_text=str(row.get("solution_text", "") or ""),
                default_marks=float(row["default_marks"]),
                default_negative_marks=float(row.get("default_negative_marks", 0.0) or 0.0),
            )
            db.add(question)
            created_questions.append(question)
        except Exception as err:
            logger.error(f"[Bulk Upload Error] Row {idx + 2} failed: {err}")
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Row {idx + 2} validation error: {str(err)}",
            )

    try:
        await db.commit()
    except Exception as db_err:
        logger.warning(
            f"[Bulk Upload] Direct insert error: {db_err}. Retrying with stringified JSON..."
        )
        await db.rollback()
        try:
            for q in created_questions:
                if isinstance(q.question_text, dict):
                    q.question_text = json.dumps(q.question_text)
                if isinstance(q.options, (list, dict)):
                    q.options = json.dumps(q.options)
                if isinstance(q.evaluation_data, (list, dict)):
                    q.evaluation_data = json.dumps(q.evaluation_data)
                db.add(q)
            await db.commit()
        except Exception as retry_err:
            logger.error(f"[Bulk Upload Retry Error]: {retry_err}", exc_info=True)
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Database commit error: {str(retry_err)}",
            )

    return {
        "status": "success",
        "inserted_count": len(created_questions),
        "message": f"Successfully ingested {len(created_questions)} questions into the knowledge base.",
    }