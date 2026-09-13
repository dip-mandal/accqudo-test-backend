from datetime import datetime
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.subscription import UserSubscription, PackageTest, PaymentStatus
from app.models.test import Test


class AccessService:
    @classmethod
    async def can_user_access_test(cls, db: AsyncSession, user_id: int, test_id: int) -> bool:
        """
        Determines whether a user has permission to start an attempt.
        - Tests with ID <= 2 or explicitly marked free can be accessed by everyone.
        - Otherwise, checks for an active, non-expired UserSubscription covering this test or package.
        """
        # Free diagnostic/sample tests
        if test_id in [1, 2]:
            return True

        # Check if the test is bundled in any package
        package_test_stmt = select(PackageTest.package_id).where(PackageTest.test_id == test_id)
        res = await db.execute(package_test_stmt)
        associated_package_ids = res.scalars().all()

        # If the test is not attached to any package, allow open access by default
        if not associated_package_ids:
            return True

        # Check if user has an active, valid subscription for any of those packages
        now = datetime.utcnow()
        sub_stmt = select(UserSubscription).where(
            and_(
                UserSubscription.user_id == user_id,
                UserSubscription.package_id.in_(associated_package_ids),
                UserSubscription.payment_status == PaymentStatus.SUCCESS,
                UserSubscription.is_active == True,
                UserSubscription.expires_at > now,
            )
        )
        sub_res = await db.execute(sub_stmt)
        active_subscription = sub_res.scalar_one_or_none()

        return active_subscription is not None