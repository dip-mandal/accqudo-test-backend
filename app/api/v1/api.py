from fastapi import APIRouter

from app.api.v1.endpoints import (
    attempts,
    auth,
    payments,
    leaderboards,
    analytics,
    admin,
    storage,
    super_admin,
    team,
    sells,
    latex,
)

from app.api.v1.team import contribution
from app.api.v1.team.progress import router as team_progress_router


api_router = APIRouter()


# ----------------------------------------------------------------------
# Authentication
# ----------------------------------------------------------------------

api_router.include_router(
    auth.router,
    prefix="/auth",
    tags=["Authentication"],
)


# ----------------------------------------------------------------------
# Attempts
# ----------------------------------------------------------------------

api_router.include_router(
    attempts.router,
    prefix="/attempts",
    tags=["Attempts"],
)


# ----------------------------------------------------------------------
# Payments
# ----------------------------------------------------------------------

api_router.include_router(
    payments.router,
    prefix="/payments",
    tags=["Payments"],
)


# ----------------------------------------------------------------------
# Leaderboards
# ----------------------------------------------------------------------

api_router.include_router(
    leaderboards.router,
    prefix="/leaderboards",
    tags=["Leaderboards"],
)


# ----------------------------------------------------------------------
# Analytics
# ----------------------------------------------------------------------

api_router.include_router(
    analytics.router,
    prefix="/analytics",
    tags=["Analytics"],
)


# ----------------------------------------------------------------------
# Admin
# ----------------------------------------------------------------------

api_router.include_router(
    admin.router,
    prefix="/admin",
    tags=["Admin Studio & CMS"],
)


# ----------------------------------------------------------------------
# Sales
# ----------------------------------------------------------------------

api_router.include_router(
    sells.router,
)


# ----------------------------------------------------------------------
# Team contribution
# ----------------------------------------------------------------------

api_router.include_router(
    contribution.router,
)


# ----------------------------------------------------------------------
# Team progress
# ----------------------------------------------------------------------

api_router.include_router(
    team_progress_router,
)


# ----------------------------------------------------------------------
# Storage / R2
# ----------------------------------------------------------------------

api_router.include_router(
    storage.router,
    prefix="/storage",
    tags=["Storage"],
)


# ----------------------------------------------------------------------
# Team / Question Authoring
# ----------------------------------------------------------------------

api_router.include_router(
    team.router,
    tags=["Team & Authoring"],
)


# ----------------------------------------------------------------------
# Super Admin
# ----------------------------------------------------------------------

api_router.include_router(
    super_admin.router,
    tags=["Super Admin"],
)


# ----------------------------------------------------------------------
# LaTeX / TikZ Rendering
# ----------------------------------------------------------------------

api_router.include_router(
    latex.router,
    prefix="/latex",
    tags=["LaTeX Rendering"],
)