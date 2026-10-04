"""
Accqudo LaTeX / TikZ Renderer Service
-------------------------------------

Server-side renderer for complex LaTeX and TikZ content.

Supported input:

1. LaTeX body content

    \\[
        x^2 + y^2 = z^2
    \\]

2. TikZ body content

    \\begin{tikzpicture}
        \\draw (0,0) -- (2,2);
    \\end{tikzpicture}

3. Complete LaTeX documents

    \\documentclass{standalone}
    \\usepackage{tikz}

    \\begin{document}

    \\begin{tikzpicture}
        ...
    \\end{tikzpicture}

    \\end{document}

The LatexSanitizer is responsible for validating and normalizing the
input. This service then creates an Accqudo-controlled LaTeX document,
compiles it to PDF, and converts the first page to SVG.

IMPORTANT SECURITY NOTES
------------------------

The application layer:

    - never uses shell=True
    - uses a temporary isolated directory
    - uses a strict timeout
    - disables shell escape
    - sanitizes LaTeX before compilation
    - limits input/output size
    - cleans up temporary files
    - sanitizes generated SVG

The Docker/container layer should additionally:

    - run as a non-root user where practical
    - limit CPU/memory
    - restrict network access where practical
    - use an isolated runtime
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


# ======================================================================
# Exceptions
# ======================================================================


class LatexRenderError(RuntimeError):
    """Base exception for LaTeX rendering failures."""


class LatexCompilationError(LatexRenderError):
    """Raised when pdflatex compilation fails."""


class LatexConversionError(LatexRenderError):
    """Raised when PDF -> SVG conversion fails."""


class LatexRenderTimeout(LatexRenderError):
    """Raised when LaTeX rendering exceeds the allowed timeout."""


# ======================================================================
# Result
# ======================================================================


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


# ======================================================================
# Renderer Service
# ======================================================================


class LatexRendererService:
    """
    Controlled server-side LaTeX renderer.

    The service is intentionally stateless.
    """

    # ------------------------------------------------------------------
    # Executables
    # ------------------------------------------------------------------

    LATEX_BINARY = os.getenv(
        "ACQ_LATEX_BINARY",
        "pdflatex",
    )

    PDF_TO_SVG_BINARY = os.getenv(
        "ACQ_PDF_TO_SVG_BINARY",
        "pdftocairo",
    )

    # ------------------------------------------------------------------
    # Limits
    # ------------------------------------------------------------------

    RENDER_TIMEOUT_SECONDS = int(
        os.getenv(
            "ACQ_LATEX_RENDER_TIMEOUT",
            "8",
        )
    )

    MAX_LATEX_LENGTH = int(
        os.getenv(
            "ACQ_LATEX_MAX_LENGTH",
            "50000",
        )
    )

    MAX_SVG_SIZE = int(
        os.getenv(
            "ACQ_LATEX_MAX_SVG_SIZE",
            "2000000",
        )
    )

    MAX_PDF_SIZE = int(
        os.getenv(
            "ACQ_LATEX_MAX_PDF_SIZE",
            "10000000",
        )
    )

    MAX_PROCESS_OUTPUT = int(
        os.getenv(
            "ACQ_LATEX_MAX_PROCESS_OUTPUT",
            "100000",
        )
    )

    # ------------------------------------------------------------------
    # Rendering configuration
    # ------------------------------------------------------------------

    BASE_LATEX_PACKAGES = (
        r"\usepackage[utf8]{inputenc}",
        r"\usepackage[T1]{fontenc}",
        r"\usepackage{amsmath}",
        r"\usepackage{amssymb}",
        r"\usepackage{amsfonts}",
        r"\usepackage{mathtools}",
        r"\usepackage{xcolor}",
        r"\usepackage{graphicx}",
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

        The input can be either a LaTeX body or a complete LaTeX
        document. LatexSanitizer normalizes complete documents into
        body content before compilation.

        Args:
            latex:
                LaTeX/TikZ source.

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
        # Security / normalization
        # --------------------------------------------------------------

        try:
            safe_latex = LatexSanitizer.sanitize(
                latex
            )
        except LatexSecurityError:
            # Preserve the security exception so the API layer can
            # return HTTP 400.
            raise

        # --------------------------------------------------------------
        # Detect TikZ AFTER sanitization.
        # --------------------------------------------------------------

        contains_tikz = LatexSanitizer.contains_tikz(
            safe_latex
        )

        # --------------------------------------------------------------
        # Temporary isolated working directory.
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
            # Always clean up all generated files.
            shutil.rmtree(
                temp_dir,
                ignore_errors=True,
            )

    # ==================================================================
    # Internal rendering pipeline
    # ==================================================================

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
        # Build controlled TeX document.
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
        # Compile LaTeX -> PDF.
        # --------------------------------------------------------------

        await cls._run_pdflatex(
            work_dir=work_dir,
            tex_path=tex_path,
        )

        # --------------------------------------------------------------
        # Validate generated PDF.
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
        # PDF -> SVG.
        # --------------------------------------------------------------

        svg_path = await cls._convert_pdf_to_svg(
            work_dir=work_dir,
            pdf_path=pdf_path,
            svg_prefix=svg_prefix,
        )

        # --------------------------------------------------------------
        # Read SVG.
        # --------------------------------------------------------------

        try:
            svg_bytes = svg_path.read_bytes()

        except OSError as exc:
            raise LatexConversionError(
                "Unable to read generated SVG."
            ) from exc

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
        # Sanitize generated SVG.
        # --------------------------------------------------------------

        svg = cls._sanitize_svg(svg)

        width, height = cls._extract_svg_dimensions(
            svg
        )

        return LatexRenderResult(
            svg=svg,
            width=width,
            height=height,
            contains_tikz=contains_tikz,
        )

    # ==================================================================
    # Controlled TeX document
    # ==================================================================

    @classmethod
    def _build_tex_document(
        cls,
        latex: str,
        contains_tikz: bool,
    ) -> str:
        """
        Build the complete TeX document controlled by Accqudo.

        User content is inserted only into the document body.

        The user cannot control:

            - documentclass
            - arbitrary package loading
            - shell escape
            - output directory
            - compiler flags
        """

        packages = list(
            cls.BASE_LATEX_PACKAGES
        )

        if contains_tikz:
            packages.append(
                r"\usepackage{tikz}"
            )

            # ----------------------------------------------------------
            # Load every TikZ library explicitly allowed by the
            # sanitizer.
            #
            # This keeps sanitizer and renderer behavior consistent.
            # ----------------------------------------------------------

            for library in sorted(
                LatexSanitizer.ALLOWED_TIKZ_LIBRARIES
            ):
                packages.append(
                    rf"\usetikzlibrary{{{library}}}"
                )

        package_block = "\n".join(
            packages
        )

        document = f"""
\\documentclass[border=4pt]{{standalone}}

{package_block}

\\pagestyle{{empty}}

\\begin{{document}}

{latex}

\\end{{document}}
"""

        return document.strip() + "\n"

    # ==================================================================
    # PDFLaTeX
    # ==================================================================

    @classmethod
    async def _run_pdflatex(
        cls,
        work_dir: Path,
        tex_path: Path,
    ) -> None:

        command = [
            cls.LATEX_BINARY,

            # ----------------------------------------------------------
            # NEVER allow TeX to execute shell commands.
            # ----------------------------------------------------------

            "-no-shell-escape",

            # ----------------------------------------------------------
            # Never wait for interactive input.
            # ----------------------------------------------------------

            "-interaction=nonstopmode",

            # ----------------------------------------------------------
            # Stop immediately on fatal compilation errors.
            # ----------------------------------------------------------

            "-halt-on-error",

            # ----------------------------------------------------------
            # Keep generated files inside the temporary directory.
            # ----------------------------------------------------------

            "-output-directory",
            str(work_dir),

            # ----------------------------------------------------------
            # Source document.
            # ----------------------------------------------------------

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

            raise LatexCompilationError(
                message
            )

    # ==================================================================
    # PDF -> SVG
    # ==================================================================

    @classmethod
    async def _convert_pdf_to_svg(
        cls,
        work_dir: Path,
        pdf_path: Path,
        svg_prefix: Path,
    ) -> Path:

        command = [
            cls.PDF_TO_SVG_BINARY,

            # ----------------------------------------------------------
            # SVG output.
            # ----------------------------------------------------------

            "-svg",

            # ----------------------------------------------------------
            # Render only the first page.
            # ----------------------------------------------------------

            "-f",
            "1",

            # ----------------------------------------------------------
            # IMPORTANT:
            #
            # Do NOT use "-singlefile" here.
            #
            # pdftocairo supports "-singlefile" only with raster
            # output formats such as PNG/JPEG/TIFF. It is invalid
            # when "-svg" is selected.
            #
            # With SVG output, pdftocairo automatically creates:
            #
            #     document.svg
            #
            # from the output prefix:
            #
            #     document
            # ----------------------------------------------------------

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

        # pdftocairo -svg <pdf> <prefix>
        # creates <prefix>.svg
        svg_path = Path(
            f"{svg_prefix}.svg"
        )

        if not svg_path.exists():
            raise LatexConversionError(
                "pdftocairo completed without producing an SVG."
            )

        return svg_path

    # ==================================================================
    # SVG validation / sanitization
    # ==================================================================

    @classmethod
    def _sanitize_svg(
        cls,
        svg: str,
    ) -> str:
        """
        Apply defensive SVG cleanup.

        pdftocairo normally produces SVG without executable
        JavaScript, but generated SVG is still treated as untrusted
        output before being inserted into the frontend.

        Removes:

            - XML declarations
            - DOCTYPE declarations
            - script elements
            - inline event handlers
            - javascript: URLs
            - XML entities
        """

        # --------------------------------------------------------------
        # XML declaration
        # --------------------------------------------------------------

        svg = re.sub(
            r"<\?xml[^>]*\?>",
            "",
            svg,
            flags=re.IGNORECASE,
        )

        # --------------------------------------------------------------
        # DOCTYPE
        # --------------------------------------------------------------

        svg = re.sub(
            r"<!DOCTYPE[^>]*>",
            "",
            svg,
            flags=re.IGNORECASE,
        )

        # --------------------------------------------------------------
        # Script elements
        # --------------------------------------------------------------

        svg = re.sub(
            r"<script\b[^>]*>.*?</script>",
            "",
            svg,
            flags=re.IGNORECASE | re.DOTALL,
        )

        # --------------------------------------------------------------
        # Inline event handlers:
        #
        # onclick=""
        # onload=""
        # onmouseover=""
        # --------------------------------------------------------------

        svg = re.sub(
            r'\s+on[a-zA-Z]+\s*=\s*("[^"]*"|\'[^\']*\')',
            "",
            svg,
            flags=re.IGNORECASE,
        )

        # --------------------------------------------------------------
        # javascript: URLs
        # --------------------------------------------------------------

        svg = re.sub(
            r"javascript\s*:",
            "",
            svg,
            flags=re.IGNORECASE,
        )

        # --------------------------------------------------------------
        # External XML entities
        # --------------------------------------------------------------

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

        # --------------------------------------------------------------
        # SVG root validation
        # --------------------------------------------------------------

        if not re.search(
            r"<svg\b",
            svg,
            flags=re.IGNORECASE,
        ):
            raise LatexConversionError(
                "Generated output is not a valid SVG document."
            )

        return svg

    # ==================================================================
    # SVG metadata
    # ==================================================================

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

    # ==================================================================
    # Compilation error handling
    # ==================================================================

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

        lines = [
            line.strip()
            for line in combined.splitlines()
            if line.strip()
        ]

        # --------------------------------------------------------------
        # Standard TeX error lines begin with !
        # --------------------------------------------------------------

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

        # --------------------------------------------------------------
        # Fall back to the final diagnostic lines.
        # --------------------------------------------------------------

        tail = lines[-10:]

        return (
            "LaTeX compilation failed:\n"
            + "\n".join(tail)[:4000]
        )

    # ==================================================================
    # Process output limits
    # ==================================================================

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