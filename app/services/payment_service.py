import uuid
from datetime import datetime, timedelta
from typing import Optional
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.payment import (
    Package,
    Payment,
    PaymentStatus,
    Subscription,
    SubscriptionStatus,
    PackageExpiryType,
)
from app.models.coupon import Coupon

class PaymentService:
    @staticmethod
    async def create_order(db: AsyncSession, user_id: int, package_id: int, coupon_code: Optional[str] = None) -> dict:
        stmt = select(Package).where(Package.id == package_id)
        result = await db.execute(stmt)
        package = result.scalar_one_or_none()

        if not package or not package.is_active:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Selected package is unavailable or inactive"
            )

        base_amount = package.price_paise - (package.discount_paise or 0)
        discount_amount = 0

        if coupon_code:
            coupon_stmt = select(Coupon).where(Coupon.code == coupon_code.strip().upper(), Coupon.is_active == True)
            coupon = (await db.execute(coupon_stmt)).scalar_one_or_none()
            
            if not coupon:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid or inactive coupon code")
            
            now_dt = datetime.utcnow()
            if coupon.valid_from and now_dt < coupon.valid_from:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Coupon is not yet valid")
            if coupon.valid_until and now_dt > coupon.valid_until:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Coupon has expired")
            if coupon.usage_limit and coupon.times_used >= coupon.usage_limit:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Coupon usage limit reached")

            pct_discount = int(base_amount * (coupon.discount_percent / 100.0))
            if coupon.max_discount_paise:
                discount_amount = min(pct_discount, coupon.max_discount_paise)
            else:
                discount_amount = pct_discount

            coupon.times_used += 1

        final_amount = max(0, int(base_amount - discount_amount))
        mock_order_id = f"order_{uuid.uuid4().hex[:16]}"
        now = datetime.utcnow()

        payment = Payment(
            user_id=user_id,
            package_id=package.id,
            razorpay_order_id=mock_order_id,
            amount_paise=final_amount,
            status=PaymentStatus.PENDING,
            created_at=now,
            updated_at=now
        )
        db.add(payment)
        await db.commit()
        await db.refresh(payment)

        return {
            "order_id": mock_order_id,
            "amount": final_amount,
            "currency": "INR",
            "package_title": package.title,
            "discount_applied": discount_amount
        }

    @staticmethod
    async def verify_and_activate(
        db: AsyncSession,
        order_id: str,
        payment_id: str,
        signature: str,
        secret: str
    ) -> Subscription:
        stmt = select(Payment).where(Payment.razorpay_order_id == order_id)
        result = await db.execute(stmt)
        payment = result.scalar_one_or_none()

        if not payment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Payment order not found"
            )

        if payment.status == PaymentStatus.SUCCESS:
            sub_stmt = select(Subscription).where(Subscription.payment_id == payment.id)
            sub_result = await db.execute(sub_stmt)
            return sub_result.scalar_one()

        package_stmt = select(Package).where(Package.id == payment.package_id)
        package = (await db.execute(package_stmt)).scalar_one()

        now = datetime.utcnow()
        if package.expiry_type == PackageExpiryType.DURATION:
            expiry = now + timedelta(days=package.validity_days or 365)
        elif package.fixed_expiry_date:
            expiry = package.fixed_expiry_date
        else:
            expiry = now + timedelta(days=365)

        payment.status = PaymentStatus.SUCCESS
        payment.razorpay_payment_id = payment_id
        payment.razorpay_signature = signature
        payment.updated_at = now

        subscription = Subscription(
            user_id=payment.user_id,
            package_id=package.id,
            payment_id=payment.id,
            start_date=now,
            expiry_date=expiry,
            status=SubscriptionStatus.ACTIVE,
            created_at=now,
            updated_at=now
        )
        db.add(subscription)
        await db.commit()
        await db.refresh(subscription)

        return subscription