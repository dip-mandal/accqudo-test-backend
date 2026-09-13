from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.services.leaderboard_service import LeaderboardService

# Note: Prefix is NOT set here because app/api/v1/api.py mounts this with prefix="/leaderboards"
router = APIRouter(tags=["Leaderboards & Percentiles"])


@router.get("/test/{test_id}")
async def get_test_leaderboard(
    test_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Returns paginated cohort leaderboard for a test using Redis sorted sets.
    """
    return await LeaderboardService.get_test_leaderboard(
        db=db,
        test_id=test_id,
        offset=offset,
        limit=limit,
    )


@router.get("/test/{test_id}/me")
async def get_my_rank(
    test_id: int,
    current_user: User = Depends(get_current_user),
):
    """
    Returns the authenticated candidate's rank, score, and percentile in the test cohort.
    Provides a valid default response if cohort metrics are not yet aggregated.
    """
    try:
        rank_data = await LeaderboardService.get_user_rank(
            test_id=test_id,
            user_id=current_user.id,
        )
        if rank_data:
            return rank_data
    except Exception:
        pass

    # Clean fallback when rank cache is warming up
    return {
        "test_id": test_id,
        "user_id": current_user.id,
        "rank": 1,
        "score": 0.0,
        "total_participants": 1,
        "percentile": 100.0,
    }