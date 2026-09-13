import hmac
import hashlib
import razorpay
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.package import SubscriptionPackage, RazorpayOrder
from app.models.test import Test
from app.models.enrollment import TestEnrollment
from app.models.coupon import Coupon

router = APIRouter(tags=["Razorpay Payments"])

razorpay_client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


class CreateOrderRequest(BaseModel):
    package_id: Optional[int] = None
    test_id: Optional[int] = None
    coupon_code: Optional[str] = None


class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


@router.post("/razorpay/create-order")
async def create_razorpay_order(
    payload: CreateOrderRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generates an official Razorpay Order ID for a Package or Individual Test with active Coupon discount applied."""
    amount_inr = 0.0
    item_title = ""

    if payload.package_id:
        pkg_stmt = select(SubscriptionPackage).where(SubscriptionPackage.id == payload.package_id)
        pkg = (await db.execute(pkg_stmt)).scalar_one_or_none()
        if not pkg:
            raise HTTPException(status_code=404, detail="Package not found")
        amount_inr = float(pkg.price_inr)
        item_title = pkg.title
    elif payload.test_id:
        test_stmt = select(Test).where(Test.id == payload.test_id)
        test = (await db.execute(test_stmt)).scalar_one_or_none()
        if not test:
            raise HTTPException(status_code=404, detail="Test not found")
        amount_inr = 299.0
        item_title = test.title
    else:
        raise HTTPException(status_code=400, detail="Either package_id or test_id must be provided")

    discount_inr = 0.0
    if payload.coupon_code:
        code_clean = payload.coupon_code.strip().upper()
        coupon_stmt = select(Coupon).where(Coupon.code == code_clean, Coupon.is_active == True)
        coupon = (await db.execute(coupon_stmt)).scalar_one_or_none()
        
        if not coupon:
            raise HTTPException(status_code=404, detail="Invalid or inactive coupon code")
        
        now_dt = datetime.utcnow()
        if coupon.valid_from and now_dt < coupon.valid_from:
            raise HTTPException(status_code=400, detail="Coupon is not yet valid")
        if coupon.valid_until and now_dt > coupon.valid_until:
            raise HTTPException(status_code=400, detail="Coupon has expired")
        if coupon.usage_limit and coupon.times_used >= coupon.usage_limit:
            raise HTTPException(status_code=400, detail="Coupon usage limit reached")

        disc_pct = coupon.discount_percent if coupon.discount_percent is not None else (coupon.discount_percentage if hasattr(coupon, 'discount_percentage') and coupon.discount_percentage is not None else 0.0)
        pct_discount_inr = amount_inr * (disc_pct / 100.0)
        
        if coupon.max_discount_paise:
            max_disc_inr = coupon.max_discount_paise / 100.0
            discount_inr = min(pct_discount_inr, max_disc_inr)
        else:
            discount_inr = pct_discount_inr

        coupon.times_used += 1

    final_amount_inr = max(0.0, amount_inr - discount_inr)
    amount_in_paise = int(round(final_amount_inr * 100))

    try:
        order_data = {
            "amount": amount_in_paise,
            "currency": "INR",
            "receipt": f"rcpt_u{current_user.id}_p{payload.package_id or 0}_t{payload.test_id or 0}",
            "notes": {
                "user_id": current_user.id,
                "email": current_user.email,
                "title": item_title,
                "coupon": payload.coupon_code or ""
            }
        }
        rp_order = razorpay_client.order.create(data=order_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Razorpay Order Generation Failed: {str(e)}")

    new_order = RazorpayOrder(
        user_id=current_user.id,
        package_id=payload.package_id,
        test_id=payload.test_id,
        razorpay_order_id=rp_order["id"],
        amount_inr=final_amount_inr,
        currency="INR",
        status="CREATED",
    )
    db.add(new_order)
    await db.commit()

    return {
        "order_id": rp_order["id"],
        "amount": amount_in_paise,
        "currency": "INR",
        "key_id": settings.RAZORPAY_KEY_ID,
        "name": "Accqudo Examination Platform",
        "description": item_title,
        "discount_applied": int(discount_inr * 100),
        "prefill": {
            "name": current_user.full_name or "Candidate",
            "email": current_user.email,
        }
    }


@router.post("/razorpay/verify-payment")
async def verify_razorpay_payment(
    payload: VerifyPaymentRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Validates HMAC-SHA256 Razorpay signature.
    Upon validity, marks order PAID and enrolls student in all tests associated with the purchased package.
    """
    generated_signature = hmac.new(
        key=settings.RAZORPAY_KEY_SECRET.encode("utf-8"),
        msg=f"{payload.razorpay_order_id}|{payload.razorpay_payment_id}".encode("utf-8"),
        digestmod=hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(generated_signature, payload.razorpay_signature):
        raise HTTPException(status_code=400, detail="Invalid payment signature. Transaction rejected.")

    order_stmt = select(RazorpayOrder).where(RazorpayOrder.razorpay_order_id == payload.razorpay_order_id)
    order_record = (await db.execute(order_stmt)).scalar_one_or_none()
    if not order_record:
        raise HTTPException(status_code=404, detail="Order tracking record not found.")

    order_record.razorpay_payment_id = payload.razorpay_payment_id
    order_record.razorpay_signature = payload.razorpay_signature
    order_record.status = "PAID"

    enrolled_count = 0
    if order_record.package_id:
        pkg_stmt = (
            select(SubscriptionPackage)
            .options(selectinload(SubscriptionPackage.tests))
            .where(SubscriptionPackage.id == order_record.package_id)
        )
        pkg = (await db.execute(pkg_stmt)).scalar_one_or_none()
        if pkg and pkg.tests:
            for test in pkg.tests:
                check_stmt = select(TestEnrollment).where(
                    TestEnrollment.user_id == current_user.id,
                    TestEnrollment.test_id == test.id
                )
                if not (await db.execute(check_stmt)).scalars().first():
                    db.add(TestEnrollment(user_id=current_user.id, test_id=test.id, payment_status="COMPLETED"))
                    enrolled_count += 1
    elif order_record.test_id:
        check_stmt = select(TestEnrollment).where(
            TestEnrollment.user_id == current_user.id,
            TestEnrollment.test_id == order_record.test_id
        )
        if not (await db.execute(check_stmt)).scalars().first():
            db.add(TestEnrollment(user_id=current_user.id, test_id=order_record.test_id, payment_status="COMPLETED"))
            enrolled_count += 1

    await db.commit()

    return {
        "status": "success",
        "message": f"Payment successfully verified! Unlocked {enrolled_count} test papers.",
        "order_id": payload.razorpay_order_id
    }