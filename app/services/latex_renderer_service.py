"""
Accqudo LaTeX / TikZ Renderer Service
-------------------------------------

Server-side renderer for complex LaTeX and TikZ content.

Architecture:

    User LaTeX
        |
        v
    LatexSanitizer
        |
        v
    Controlled LaTeX document
        |
        v
    pdflatex
        |
        v
    PDF
        |
        v
    pdftocairo
        |
        v
    SVG
        |
        v
    API response

IMPORTANT SECURITY NOTES
------------------------
This service assumes the underlying container is also hardened.

The application layer:
    - never uses shell=True
    - uses a temporary isolated directory
    - uses a strict timeout
    - disables shell escape
    - sanitizes LaTeX before compilation
    - limits input/output size
    - cleans up temporary files

The Docker/container layer should additionally:
    - run as a non-root user
    - ideally use a read-only filesystem
    - limit CPU/memory
    - restrict network access where possible
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.services.latex_sanitizer import (
    LatexSanitizer,
    LatexSecurityError,
)


class LatexRenderError(RuntimeError):
    """Base exception for LaTeX rendering failures."""


class LatexCompilationError(LatexRenderError):
    """Raised when pdflatex compilation fails."""


class LatexConversionError(LatexRenderError):
    """Raised when PDF -> SVG conversion fails."""


class LatexRenderTimeout(LatexRenderError):
    """Raised when LaTeX rendering exceeds the allowed timeout."""


@dataclass
class LatexRenderResult:
    """
    Result returned by the renderer.

    svg:
        Generated SVG markup.

    width:
        Optional SVG width.

    height:
        Optional SVG height.

    contains_tikz:
        Whether the input was detected as TikZ.

    """

    svg: str
    width: Optional[str] = None
    height: Optional[str] = None
    contains_tikz: bool = False


class LatexRendererService:
    """
    Controlled server-side LaTeX renderer.

    The service is intentionally stateless.
    """

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    LATEX_BINARY = os.getenv(
        "ACQ_LATEX_BINARY",
        "pdflatex",
    )

    PDF_TO_SVG_BINARY = os.getenv(
        "ACQ_PDF_TO_SVG_BINARY",
        "pdftocairo",
    )

    # Maximum time allowed for the complete render.
    RENDER_TIMEOUT_SECONDS = int(
        os.getenv(
            "ACQ_LATEX_RENDER_TIMEOUT",
            "8",
        )
    )

    # Maximum input size.
    MAX_LATEX_LENGTH = int(
        os.getenv(
            "ACQ_LATEX_MAX_LENGTH",
            "50000",
        )
    )

    # Maximum SVG response size.
    MAX_SVG_SIZE = int(
        os.getenv(
            "ACQ_LATEX_MAX_SVG_SIZE",
            "2000000",
        )
    )

    # Maximum generated PDF size.
    MAX_PDF_SIZE = int(
        os.getenv(
            "ACQ_LATEX_MAX_PDF_SIZE",
            "10000000",
        )
    )

    # Maximum stdout/stderr captured from TeX tools.
    MAX_PROCESS_OUTPUT = int(
        os.getenv(
            "ACQ_LATEX_MAX_PROCESS_OUTPUT",
            "100000",
        )
    )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    async def render(
        cls,
        latex: str,
    ) -> LatexRenderResult:
        """
        Render user-provided LaTeX/TikZ to SVG.

        Args:
            latex:
                LaTeX/TikZ source without a document wrapper.

        Returns:
            LatexRenderResult

        Raises:
            LatexSecurityError
            LatexCompilationError
            LatexConversionError
            LatexRenderTimeout
        """

        if not isinstance(latex, str):
            raise LatexRenderError(
                "LaTeX content must be a string."
            )

        if not latex.strip():
            raise LatexRenderError(
                "LaTeX content cannot be empty."
            )

        if len(latex) > cls.MAX_LATEX_LENGTH:
            raise LatexRenderError(
                "LaTeX content is too large."
            )

        # --------------------------------------------------------------
        # Step 1: Sanitize
        # --------------------------------------------------------------

        try:
            safe_latex = LatexSanitizer.sanitize(latex)
        except LatexSecurityError:
            # Re-raise security exceptions unchanged so the API layer
            # can return a clean 400 response.
            raise

        contains_tikz = LatexSanitizer.contains_tikz(
            safe_latex
        )

        # --------------------------------------------------------------
        # Step 2: Temporary isolated directory
        # --------------------------------------------------------------

        temp_dir = tempfile.mkdtemp(
            prefix="accqudo_latex_"
        )

        try:
            return await cls._render_in_directory(
                temp_dir=temp_dir,
                latex=safe_latex,
                contains_tikz=contains_tikz,
            )

        finally:
            # ----------------------------------------------------------
            # Always clean up.
            # ----------------------------------------------------------

            shutil.rmtree(
                temp_dir,
                ignore_errors=True,
            )

    # ------------------------------------------------------------------
    # Internal rendering pipeline
    # ------------------------------------------------------------------

    @classmethod
    async def _render_in_directory(
        cls,
        temp_dir: str,
        latex: str,
        contains_tikz: bool,
    ) -> LatexRenderResult:

        work_dir = Path(temp_dir)

        tex_path = work_dir / "document.tex"
        pdf_path = work_dir / "document.pdf"
        svg_prefix = work_dir / "document"

        # --------------------------------------------------------------
        # Generate controlled document.
        # --------------------------------------------------------------

        document = cls._build_tex_document(
            latex=latex,
            contains_tikz=contains_tikz,
        )

        tex_path.write_text(
            document,
            encoding="utf-8",
        )

        # --------------------------------------------------------------
        # Compile LaTeX -> PDF
        # --------------------------------------------------------------

        await cls._run_pdflatex(
            work_dir=work_dir,
            tex_path=tex_path,
        )

        # --------------------------------------------------------------
        # Validate PDF
        # --------------------------------------------------------------

        if not pdf_path.exists():
            raise LatexCompilationError(
                "LaTeX compilation completed without producing a PDF."
            )

        pdf_size = pdf_path.stat().st_size

        if pdf_size <= 0:
            raise LatexCompilationError(
                "Generated PDF is empty."
            )

        if pdf_size > cls.MAX_PDF_SIZE:
            raise LatexCompilationError(
                "Generated PDF exceeds the maximum allowed size."
            )

        # --------------------------------------------------------------
        # PDF -> SVG
        # --------------------------------------------------------------

        svg_path = await cls._convert_pdf_to_svg(
            work_dir=work_dir,
            pdf_path=pdf_path,
            svg_prefix=svg_prefix,
        )

        # --------------------------------------------------------------
        # Read SVG
        # --------------------------------------------------------------

        svg_bytes = svg_path.read_bytes()

        if not svg_bytes:
            raise LatexConversionError(
                "Generated SVG is empty."
            )

        if len(svg_bytes) > cls.MAX_SVG_SIZE:
            raise LatexConversionError(
                "Generated SVG exceeds the maximum allowed size."
            )

        svg = svg_bytes.decode(
            "utf-8",
            errors="replace",
        )

        # --------------------------------------------------------------
        # Basic SVG validation / sanitization
        # --------------------------------------------------------------

        svg = cls._sanitize_svg(svg)

        width, height = cls._extract_svg_dimensions(svg)

        return LatexRenderResult(
            svg=svg,
            width=width,
            height=height,
            contains_tikz=contains_tikz,
        )

    # ------------------------------------------------------------------
    # Controlled LaTeX document
    # ------------------------------------------------------------------

    @classmethod
    def _build_tex_document(
        cls,
        latex: str,
        contains_tikz: bool,
    ) -> str:
        """
        Build the complete TeX document controlled by Accqudo.

        Users only provide the body.

        They cannot control:
            - documentclass
            - package loading
            - shell escape
            - output configuration
        """

        packages = [
            r"\usepackage[utf8]{inputenc}",
            r"\usepackage[T1]{fontenc}",
            r"\usepackage{amsmath}",
            r"\usepackage{amssymb}",
            r"\usepackage{amsfonts}",
            r"\usepackage{mathtools}",
            r"\usepackage{xcolor}",
            r"\usepackage{graphicx}",
        ]

        if contains_tikz:
            packages.extend(
                [
                    r"\usepackage{tikz}",
                    r"\usetikzlibrary{calc}",
                    r"\usetikzlibrary{positioning}",
                    r"\usetikzlibrary{arrows.meta}",
                    r"\usetikzlibrary{shapes.geometric}",
                    r"\usetikzlibrary{decorations.pathreplacing}",
                    r"\usetikzlibrary{angles}",
                    r"\usetikzlibrary{quotes}",
                ]
            )

        package_block = "\n".join(packages)

        # We use standalone because we only need the rendered content,
        # not a full page.
        document = f"""
\\documentclass[border=4pt]{{standalone}}

{package_block}

\\pagestyle{{empty}}

\\begin{{document}}

{latex}

\\end{{document}}
"""

        return document.strip() + "\n"

    # ------------------------------------------------------------------
    # pdflatex
    # ------------------------------------------------------------------

    @classmethod
    async def _run_pdflatex(
        cls,
        work_dir: Path,
        tex_path: Path,
    ) -> None:

        command = [
            cls.LATEX_BINARY,

            # Do not allow shell command execution.
            "-no-shell-escape",

            # Stop instead of waiting for interactive input.
            "-interaction=nonstopmode",

            "-halt-on-error",

            # Keep all output inside the temporary directory.
            "-output-directory",
            str(work_dir),

            str(tex_path),
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(work_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        except FileNotFoundError as exc:
            raise LatexCompilationError(
                "pdflatex is not installed in the backend container."
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=cls.RENDER_TIMEOUT_SECONDS,
            )

        except asyncio.TimeoutError as exc:
            process.kill()

            try:
                await process.communicate()
            except Exception:
                pass

            raise LatexRenderTimeout(
                "LaTeX compilation exceeded the allowed timeout."
            ) from exc

        stdout_text = cls._limit_process_output(
            stdout
        )

        stderr_text = cls._limit_process_output(
            stderr
        )

        if process.returncode != 0:
            message = cls._extract_compilation_error(
                stdout_text=stdout_text,
                stderr_text=stderr_text,
            )

            raise LatexCompilationError(message)

    # ------------------------------------------------------------------
    # PDF -> SVG
    # ------------------------------------------------------------------

    @classmethod
    async def _convert_pdf_to_svg(
        cls,
        work_dir: Path,
        pdf_path: Path,
        svg_prefix: Path,
    ) -> Path:

        command = [
            cls.PDF_TO_SVG_BINARY,

            # SVG output.
            "-svg",

            # Single page only.
            "-f",
            "1",

            "-singlefile",

            str(pdf_path),

            str(svg_prefix),
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(work_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        except FileNotFoundError as exc:
            raise LatexConversionError(
                "pdftocairo is not installed in the backend container."
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=cls.RENDER_TIMEOUT_SECONDS,
            )

        except asyncio.TimeoutError as exc:
            process.kill()

            try:
                await process.communicate()
            except Exception:
                pass

            raise LatexRenderTimeout(
                "PDF to SVG conversion exceeded the allowed timeout."
            ) from exc

        stdout_text = cls._limit_process_output(
            stdout
        )

        stderr_text = cls._limit_process_output(
            stderr
        )

        if process.returncode != 0:
            detail = (
                stderr_text.strip()
                or stdout_text.strip()
                or "Unknown PDF to SVG conversion error."
            )

            raise LatexConversionError(
                f"PDF to SVG conversion failed: {detail}"
            )

        svg_path = Path(
            f"{svg_prefix}.svg"
        )

        if not svg_path.exists():
            raise LatexConversionError(
                "pdftocairo completed without producing an SVG."
            )

        return svg_path

    # ------------------------------------------------------------------
    # SVG validation
    # ------------------------------------------------------------------

    @classmethod
    def _sanitize_svg(
        cls,
        svg: str,
    ) -> str:
        """
        Apply defensive SVG cleanup.

        pdftocairo normally generates SVG that does not contain
        executable JavaScript, but the response should still be treated
        as untrusted output.

        We therefore remove:
            - XML declarations
            - DOCTYPE declarations
            - script elements
            - event-handler attributes
            - javascript: URLs
        """

        # Remove XML declaration.
        svg = re.sub(
            r"<\?xml[^>]*\?>",
            "",
            svg,
            flags=re.IGNORECASE,
        )

        # Remove DOCTYPE.
        svg = re.sub(
            r"<!DOCTYPE[^>]*>",
            "",
            svg,
            flags=re.IGNORECASE,
        )

        # Remove script blocks.
        svg = re.sub(
            r"<script\b[^>]*>.*?</script>",
            "",
            svg,
            flags=re.IGNORECASE | re.DOTALL,
        )

        # Remove inline event handlers:
        #
        # onclick=""
        # onload=""
        # onmouseover=""
        #
        svg = re.sub(
            r"\s+on[a-zA-Z]+\s*=\s*(?:\"[^\"]*\"|'[^']*')",
            "",
            svg,
            flags=re.IGNORECASE,
        )

        # Remove javascript: URLs.
        svg = re.sub(
            r"javascript\s*:",
            "",
            svg,
            flags=re.IGNORECASE,
        )

        # Remove external HTML/XML entities.
        svg = re.sub(
            r"<!ENTITY[^>]*>",
            "",
            svg,
            flags=re.IGNORECASE,
        )

        svg = svg.strip()

        if not svg:
            raise LatexConversionError(
                "SVG became empty after sanitization."
            )

        # Must contain an SVG root.
        if not re.search(
            r"<svg\b",
            svg,
            flags=re.IGNORECASE,
        ):
            raise LatexConversionError(
                "Generated output is not a valid SVG document."
            )

        return svg

    # ------------------------------------------------------------------
    # SVG metadata
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_svg_dimensions(
        svg: str,
    ) -> tuple[Optional[str], Optional[str]]:

        match = re.search(
            r"<svg\b([^>]*)>",
            svg,
            flags=re.IGNORECASE,
        )

        if not match:
            return None, None

        attributes = match.group(1)

        width_match = re.search(
            r'\bwidth\s*=\s*["\']([^"\']+)["\']',
            attributes,
            flags=re.IGNORECASE,
        )

        height_match = re.search(
            r'\bheight\s*=\s*["\']([^"\']+)["\']',
            attributes,
            flags=re.IGNORECASE,
        )

        width = (
            width_match.group(1)
            if width_match
            else None
        )

        height = (
            height_match.group(1)
            if height_match
            else None
        )

        return width, height

    # ------------------------------------------------------------------
    # Error handling helpers
    # ------------------------------------------------------------------

    @classmethod
    def _extract_compilation_error(
        cls,
        stdout_text: str,
        stderr_text: str,
    ) -> str:

        combined = (
            stderr_text.strip()
            + "\n"
            + stdout_text.strip()
        ).strip()

        if not combined:
            return (
                "LaTeX compilation failed without diagnostic output."
            )

        # Try to extract the most useful TeX error line.
        lines = [
            line.strip()
            for line in combined.splitlines()
            if line.strip()
        ]

        error_lines = [
            line
            for line in lines
            if line.startswith("!")
        ]

        if error_lines:
            return (
                "LaTeX compilation failed: "
                + error_lines[0][:2000]
            )

        # Fall back to last useful diagnostic lines.
        tail = lines[-10:]

        return (
            "LaTeX compilation failed:\n"
            + "\n".join(tail)[:4000]
        )

    @classmethod
    def _limit_process_output(
        cls,
        output: bytes,
    ) -> str:

        if not output:
            return ""

        decoded = output.decode(
            "utf-8",
            errors="replace",
        )

        if len(decoded) > cls.MAX_PROCESS_OUTPUT:
            decoded = (
                decoded[:cls.MAX_PROCESS_OUTPUT]
                + "\n[output truncated]"
            )

        return decoded