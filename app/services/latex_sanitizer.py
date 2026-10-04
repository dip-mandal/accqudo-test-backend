"""
Accqudo LaTeX Sanitizer
-----------------------

Security layer for server-side LaTeX/TikZ rendering.

IMPORTANT:
- Never compile raw user-provided LaTeX directly.
- This sanitizer intentionally rejects commands that can access the
  filesystem, execute shell commands, or modify the compilation environment.
- The renderer should still run inside a restricted temporary directory
  with a timeout and resource limits.
"""

from __future__ import annotations

import re
from typing import Iterable


class LatexSecurityError(ValueError):
    """Raised when LaTeX content contains a blocked construct."""


class LatexSanitizer:
    """
    Validates and sanitizes LaTeX before server-side compilation.

    The goal is NOT to implement a complete LaTeX parser.

    Instead, this class provides a defensive first layer that:
      1. Rejects dangerous TeX primitives.
      2. Rejects arbitrary document/package injection.
      3. Rejects shell/file access commands.
      4. Validates selected TikZ libraries.
      5. Removes harmless wrapper commands that the backend owns.
    """

    # ------------------------------------------------------------------
    # Hard limits
    # ------------------------------------------------------------------

    MAX_INPUT_LENGTH = 50_000

    # ------------------------------------------------------------------
    # Commands which must never appear in user-controlled LaTeX.
    #
    # These are intentionally conservative.
    # ------------------------------------------------------------------

    BLOCKED_COMMANDS = {
        # Shell / OS execution
        "write18",
        "immediate",
        "input",
        "include",
        "openin",
        "openout",
        "closein",
        "closeout",
        "read",
        "write",
        "ifeof",

        # TeX expansion / primitive manipulation
        "csname",
        "endcsname",
        "expandafter",
        "noexpand",
        "xdef",
        "gdef",
        "edef",
        "let",
        "futurelet",
        "afterassignment",
        "aftergroup",

        # Environment / document manipulation
        "documentclass",
        "usepackage",
        "RequirePackage",
        "LoadClass",
        "PassOptionsToPackage",

        # Shell escape related
        "ShellEscape",
        "pdfshellescape",

        # File/system-related packages/macros
        "verbatiminput",
        "lstinputlisting",

        # External references
        "href",
        "url",
    }

    # ------------------------------------------------------------------
    # Raw textual patterns that should be blocked even if they don't
    # map cleanly to a command name.
    # ------------------------------------------------------------------

    BLOCKED_PATTERNS = [
        # TeX shell escape
        re.compile(r"\\+write18\b", re.IGNORECASE),
        re.compile(r"\\+immediate\s*\\+write18\b", re.IGNORECASE),

        # Shell escape flags
        re.compile(r"--shell-escape\b", re.IGNORECASE),
        re.compile(r"-shell-escape\b", re.IGNORECASE),
        re.compile(r"shell_escape", re.IGNORECASE),

        # Common dangerous filesystem primitives
        re.compile(r"\\+openout\b", re.IGNORECASE),
        re.compile(r"\\+openin\b", re.IGNORECASE),
        re.compile(r"\\+input\b", re.IGNORECASE),
        re.compile(r"\\+include\b", re.IGNORECASE),
        re.compile(r"\\+verbatiminput\b", re.IGNORECASE),

        # Environment-variable / OS tricks
        re.compile(r"\\+sys_get_shell\b", re.IGNORECASE),
        re.compile(r"\\+@@input\b", re.IGNORECASE),

        # URL/file URI attempts
        re.compile(r"file://", re.IGNORECASE),
        re.compile(r"file:", re.IGNORECASE),

        # LaTeX escape/environment manipulation
        re.compile(r"\\+catcode\b", re.IGNORECASE),
        re.compile(r"\\+endlinechar\b", re.IGNORECASE),
        re.compile(r"\\+newlinechar\b", re.IGNORECASE),
    ]

    # ------------------------------------------------------------------
    # TikZ libraries we are willing to enable.
    #
    # This list can be expanded later when Accqudo needs more TikZ
    # functionality.
    # ------------------------------------------------------------------

    ALLOWED_TIKZ_LIBRARIES = {
        "arrows",
        "arrows.meta",
        "automata",
        "calc",
        "decorations",
        "decorations.markings",
        "decorations.pathmorphing",
        "decorations.pathreplacing",
        "fit",
        "intersections",
        "matrix",
        "patterns",
        "positioning",
        "quotes",
        "shapes",
        "shapes.geometric",
        "shapes.misc",
        "shapes.multipart",
        "through",
        "angles",
    }

    # ------------------------------------------------------------------
    # Commands which are allowed but whose dangerous variants should be
    # handled carefully.
    # ------------------------------------------------------------------

    ALLOWED_EXTERNAL_COMMANDS = {
        "tikz",
        "draw",
        "path",
        "node",
        "coordinate",
        "fill",
        "filldraw",
        "clip",
        "shade",
        "shadedraw",
        "pattern",
        "matrix",
        "foreach",
        "filldraw",
        "graph",
        "usetikzlibrary",
    }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    def sanitize(cls, latex: str) -> str:
        """
        Validate and sanitize user LaTeX.

        Returns:
            Sanitized LaTeX string.

        Raises:
            LatexSecurityError:
                If the input contains a dangerous construct.
        """

        if latex is None:
            raise LatexSecurityError("LaTeX content cannot be null.")

        if not isinstance(latex, str):
            raise LatexSecurityError("LaTeX content must be a string.")

        if not latex.strip():
            raise LatexSecurityError("LaTeX content cannot be empty.")

        if len(latex) > cls.MAX_INPUT_LENGTH:
            raise LatexSecurityError(
                f"LaTeX content exceeds the maximum allowed size "
                f"of {cls.MAX_INPUT_LENGTH} characters."
            )

        # Normalize line endings.
        content = latex.replace("\r\n", "\n").replace("\r", "\n")

        cls._check_blocked_patterns(content)
        cls._check_blocked_commands(content)
        cls._validate_tikz_libraries(content)
        cls._check_document_wrappers(content)
        cls._check_balanced_basic_delimiters(content)

        # Remove BOM if supplied.
        content = content.lstrip("\ufeff")

        # Remove accidental document wrappers.
        #
        # We don't allow users to control the document class or package
        # loading. The backend renderer owns the complete LaTeX document.
        content = cls._remove_document_wrappers(content)

        return content.strip()

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @classmethod
    def _check_blocked_patterns(cls, content: str) -> None:
        for pattern in cls.BLOCKED_PATTERNS:
            if pattern.search(content):
                raise LatexSecurityError(
                    "LaTeX contains a blocked or unsafe construct."
                )

    @classmethod
    def _check_blocked_commands(cls, content: str) -> None:
        """
        Detect LaTeX command names.

        Example:
            \\input{secret.tex}
            -> command = input
        """

        commands = re.findall(
            r"\\+([A-Za-z@]+)",
            content,
            flags=re.MULTILINE,
        )

        blocked_lower = {
            command.lower()
            for command in cls.BLOCKED_COMMANDS
        }

        for command in commands:
            if command.lower() in blocked_lower:
                raise LatexSecurityError(
                    f"LaTeX command '\\{command}' is not allowed."
                )

    @classmethod
    def _validate_tikz_libraries(cls, content: str) -> None:
        """
        Validate \\usetikzlibrary{...}.

        Example:

            \\usetikzlibrary{positioning,arrows.meta}

        is allowed.

        Unknown libraries are rejected so the user cannot arbitrarily
        request additional TeX functionality.
        """

        pattern = re.compile(
            r"\\+usetikzlibrary\s*\{([^}]*)\}",
            re.IGNORECASE,
        )

        for match in pattern.finditer(content):
            libraries = match.group(1)

            for library in libraries.split(","):
                library = library.strip()

                if not library:
                    continue

                if library not in cls.ALLOWED_TIKZ_LIBRARIES:
                    raise LatexSecurityError(
                        f"TikZ library '{library}' is not allowed."
                    )

    @classmethod
    def _check_document_wrappers(cls, content: str) -> None:
        """
        Reject explicit document/package configuration.

        The backend will generate the complete document wrapper.
        """

        forbidden_wrappers = [
            r"\\+documentclass\b",
            r"\\+usepackage\b",
            r"\\+begin\s*\{\s*document\s*\}",
            r"\\+end\s*\{\s*document\s*\}",
        ]

        for pattern in forbidden_wrappers:
            if re.search(pattern, content, flags=re.IGNORECASE):
                raise LatexSecurityError(
                    "Document/package wrappers are not allowed. "
                    "Provide only the LaTeX/TikZ content."
                )

    @classmethod
    def _check_balanced_basic_delimiters(cls, content: str) -> None:
        """
        Basic delimiter validation.

        This is deliberately not a full TeX parser.

        We check the most common delimiters so obviously malformed
        content can be rejected before compilation.
        """

        pairs = [
            ("{", "}"),
            ("[", "]"),
            ("(", ")"),
        ]

        for opening, closing in pairs:
            if not cls._is_balanced(content, opening, closing):
                raise LatexSecurityError(
                    f"Unbalanced LaTeX delimiter: '{opening}' / '{closing}'."
                )

    @staticmethod
    def _is_balanced(
        content: str,
        opening: str,
        closing: str,
    ) -> bool:
        """
        Check delimiter balance while ignoring escaped delimiters.

        This is intentionally simple and should not be considered a
        complete TeX parser.
        """

        depth = 0
        escaped = False

        for char in content:
            if escaped:
                escaped = False
                continue

            if char == "\\":
                escaped = True
                continue

            if char == opening:
                depth += 1

            elif char == closing:
                depth -= 1

                if depth < 0:
                    return False

        return depth == 0

    # ------------------------------------------------------------------
    # Sanitization helpers
    # ------------------------------------------------------------------

    @classmethod
    def _remove_document_wrappers(cls, content: str) -> str:
        """
        Remove harmless accidental wrappers.

        This method is intentionally conservative.

        Since _check_document_wrappers() already rejects explicit
        document/package wrappers, this mainly removes leading/trailing
        whitespace and UTF-8 BOMs.
        """

        content = content.lstrip("\ufeff")
        return content.strip()

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    @classmethod
    def contains_tikz(cls, latex: str) -> bool:
        """
        Return True when content appears to require TikZ rendering.
        """

        if not latex:
            return False

        tikz_patterns = [
            r"\\+begin\s*\{\s*tikzpicture\s*\}",
            r"\\+tikz\b",
            r"\\+usetikzlibrary\b",
            r"\\+draw\b",
            r"\\+path\b",
            r"\\+node\b",
            r"\\+coordinate\b",
            r"\\+filldraw\b",
        ]

        return any(
            re.search(pattern, latex, flags=re.IGNORECASE)
            for pattern in tikz_patterns
        )

    @classmethod
    def contains_complex_latex(cls, latex: str) -> bool:
        """
        Detect LaTeX that is more appropriate for server-side rendering
        than ordinary browser KaTeX rendering.

        This is intentionally broader than contains_tikz().
        """

        if not latex:
            return False

        complex_patterns = [
            r"\\+begin\s*\{\s*tikzpicture\s*\}",
            r"\\+usetikzlibrary\b",
            r"\\+begin\s*\{\s*matrix\s*\}",
            r"\\+begin\s*\{\s*array\s*\}",
            r"\\+begin\s*\{\s*aligned\s*\}",
            r"\\+begin\s*\{\s*cases\s*\}",
            r"\\+begin\s*\{\s*align\s*\*?\s*\}",
            r"\\+begin\s*\{\s*gather\s*\*?\s*\}",
        ]

        return any(
            re.search(pattern, latex, flags=re.IGNORECASE)
            for pattern in complex_patterns
        )


def sanitize_latex(latex: str) -> str:
    """
    Convenience function.

    Example:

        from app.services.latex_sanitizer import sanitize_latex

        safe_latex = sanitize_latex(user_input)
    """

    return LatexSanitizer.sanitize(latex)