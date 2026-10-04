"""
Accqudo LaTeX Rendering API
---------------------------

Endpoint for rendering complex LaTeX / TikZ content into SVG.

Route:

    POST /api/v1/latex/render

Authentication:
    Requires a logged-in Accqudo user.

The original LaTeX is never stored or modified by this endpoint.
The endpoint only returns a generated SVG preview.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.security import get_current_user
from app.models.user import User
from app.services.latex_renderer_service import (
    LatexCompilationError,
    LatexConversionError,
    LatexRenderError,
    LatexRenderTimeout,
    LatexRendererService,
)
from app.services.latex_sanitizer import LatexSecurityError


router = APIRouter(
    tags=["LaTeX Rendering"],
)


# ----------------------------------------------------------------------
# Request / Response Schemas
# ----------------------------------------------------------------------


class LatexRenderRequest(BaseModel):
    """
    Request body for LaTeX rendering.
    """

    latex: str = Field(
        ...,
        min_length=1,
        max_length=50_000,
        description="LaTeX or TikZ source content.",
    )


class LatexRenderResponse(BaseModel):
    """
    Rendered SVG response.
    """

    success: bool = True

    svg: str

    width: Optional[str] = None

    height: Optional[str] = None

    contains_tikz: bool = False


# ----------------------------------------------------------------------
# Render Endpoint
# ----------------------------------------------------------------------


@router.post(
    "/render",
    response_model=LatexRenderResponse,
    status_code=status.HTTP_200_OK,
)
async def render_latex(
    payload: LatexRenderRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Render LaTeX/TikZ into SVG.

    This endpoint is intentionally authenticated because server-side
    LaTeX compilation is a computationally expensive operation.

    The authenticated user does not need to have an admin role here.
    The frontend can therefore use the same renderer for authenticated
    authoring workflows.

    If you later want to restrict rendering to ADMIN/SUPER_ADMIN users,
    the authorization check can be tightened without changing the
    rendering service.
    """

    # --------------------------------------------------------------
    # Basic validation
    # --------------------------------------------------------------

    latex = payload.latex.strip()

    if not latex:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="LaTeX content cannot be empty.",
        )

    # --------------------------------------------------------------
    # Render
    # --------------------------------------------------------------

    try:
        result = await LatexRendererService.render(
            latex=latex,
        )

    # --------------------------------------------------------------
    # Security rejection
    # --------------------------------------------------------------

    except LatexSecurityError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    # --------------------------------------------------------------
    # Compilation timeout
    # --------------------------------------------------------------

    except LatexRenderTimeout as exc:
        raise HTTPException(
            status_code=status.HTTP_408_REQUEST_TIMEOUT,
            detail=(
                "LaTeX rendering took too long. "
                "Please simplify the expression or diagram."
            ),
        ) from exc

    # --------------------------------------------------------------
    # LaTeX compilation failure
    # --------------------------------------------------------------

    except LatexCompilationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    # --------------------------------------------------------------
    # PDF -> SVG failure
    # --------------------------------------------------------------

    except LatexConversionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    # --------------------------------------------------------------
    # Generic renderer error
    # --------------------------------------------------------------

    except LatexRenderError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    # --------------------------------------------------------------
    # Unexpected server error
    # --------------------------------------------------------------

    except Exception as exc:
        # Do not expose internal exception details to the browser.
        #
        # Your application logging middleware/logger can capture the
        # actual traceback separately.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "An unexpected error occurred while rendering "
                "the LaTeX content."
            ),
        ) from exc

    # --------------------------------------------------------------
    # Return SVG
    # --------------------------------------------------------------

    return LatexRenderResponse(
        success=True,
        svg=result.svg,
        width=result.width,
        height=result.height,
        contains_tikz=result.contains_tikz,
    )