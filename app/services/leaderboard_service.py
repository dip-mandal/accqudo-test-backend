from typing import Any, Dict, List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import get_redis
from app.models.attempt import TestAttempt
from app.models.user import User


class LeaderboardService:
    @staticmethod
    def get_leaderboard_key(test_id: int) -> str:
        return f"leaderboard:test:{test_id}"

    @classmethod
    async def record_attempt_score(cls, attempt: TestAttempt) -> bool:
        """
        Records the score only if it is the candidate's rank-eligible first attempt.
        Uses NX (not-exists) flag so later re-attempts never overwrite official rank scores.
        """
        if not attempt.is_rank_eligible:
            return False

        r = get_redis()
        key = cls.get_leaderboard_key(attempt.test_id)
        # ZADD key NX score member
        await r.zadd(key, {str(attempt.user_id): float(attempt.total_score)}, nx=True)
        return True

    @classmethod
    async def get_test_leaderboard(
        cls,
        db: AsyncSession,
        test_id: int,
        offset: int = 0,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """
        Retrieves paginated leaderboard rankings with user details and percentiles.
        """
        r = get_redis()
        key = cls.get_leaderboard_key(test_id)

        # 1. Total cohort size
        total_participants = await r.zcard(key)
        if total_participants == 0:
            return {
                "test_id": test_id,
                "total_participants": 0,
                "leaderboard": [],
            }

        # 2. Fetch top candidates by score descending
        top_entries = await r.zrevrange(
            key, offset, offset + limit - 1, withscores=True
        )

        if not top_entries:
            return {
                "test_id": test_id,
                "total_participants": total_participants,
                "leaderboard": [],
            }

        # 3. Rehydrate candidate display names from MySQL
        user_ids = [int(entry[0]) for entry in top_entries]
        users_stmt = select(User.id, User.full_name).where(User.id.in_(user_ids))
        users_res = await db.execute(users_stmt)
        user_map = {row.id: row.full_name for row in users_res.all()}

        leaderboard: List[Dict[str, Any]] = []
        for rank_idx, (uid_str, score) in enumerate(top_entries, start=offset + 1):
            uid = int(uid_str)
            full_name = user_map.get(uid) or "Anonymous Candidate"
            
            # Standard competitive exam percentile calculation
            if total_participants > 1:
                # Count candidates who scored strictly lower than this score
                strictly_lower = await r.zcount(key, "-inf", f"({score}")
                percentile = round((strictly_lower / total_participants) * 100, 2)
            else:
                percentile = 100.0

            leaderboard.append({
                "rank": rank_idx,
                "user_id": uid,
                "name": full_name,
                "score": float(score),
                "percentile": percentile,
            })

        return {
            "test_id": test_id,
            "total_participants": total_participants,
            "leaderboard": leaderboard,
        }

    @classmethod
    async def get_user_rank(cls, test_id: int, user_id: int) -> Dict[str, Any]:
        """
        Calculates individual candidate rank and percentile within the test cohort.
        """
        r = get_redis()
        key = cls.get_leaderboard_key(test_id)
        user_key = str(user_id)

        rank_zero_based = await r.zrevrank(key, user_key)
        if rank_zero_based is None:
            return {
                "is_ranked": False,
                "reason": "No official attempt recorded for this assessment",
            }

        rank = rank_zero_based + 1
        score = await r.zscore(key, user_key)
        total = await r.zcard(key)

        if total > 1 and score is not None:
            strictly_lower = await r.zcount(key, "-inf", f"({score}")
            percentile = round((strictly_lower / total) * 100, 2)
        else:
            percentile = 100.0

        return {
            "is_ranked": True,
            "rank": rank,
            "total_participants": total,
            "score": float(score) if score is not None else 0.0,
            "percentile": percentile,
        }