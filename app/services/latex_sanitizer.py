"""
Accqudo LaTeX Sanitizer
-----------------------

Security layer for server-side LaTeX/TikZ rendering.

The renderer accepts either:

1. LaTeX body content

   \\begin{tikzpicture}
       ...
   \\end{tikzpicture}

or:

2. A complete LaTeX document

   \\documentclass{standalone}
   \\usepackage{tikz}

   \\begin{document}
       ...
   \\end{document}

Complete-document wrappers are normalized into an Accqudo-controlled
document before compilation.

IMPORTANT:
- User-controlled LaTeX is never compiled directly.
- Arbitrary packages are not allowed.
- File access and shell execution are blocked.
- The renderer still runs inside a restricted temporary directory.
"""

from __future__ import annotations

import re


class LatexSecurityError(ValueError):
    """Raised when LaTeX content contains a blocked construct."""


class LatexSanitizer:
    # ------------------------------------------------------------------
    # Limits
    # ------------------------------------------------------------------

    MAX_INPUT_LENGTH = 50_000

    # ------------------------------------------------------------------
    # Commands that must never be supplied by the user.
    # ------------------------------------------------------------------

    BLOCKED_COMMANDS = {
        # Shell / OS execution
        "write18",
        "openin",
        "openout",
        "closein",
        "closeout",
        "read",
        "ifeof",

        # Dangerous TeX manipulation
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

        # Dynamic loading
        "loadclass",
        "requirepackage",
        "passoptionstopackage",
        "passoptionstoclass",

        # File inclusion
        "input",
        "include",
        "verbatiminput",
        "lstinputlisting",

        # Shell escape related
        "shellescape",
        "pdfshellescape",

        # Dangerous environment manipulation
        "catcode",
        "endlinechar",
        "newlinechar",

        # URL/file access
        "href",
        "url",
    }

    # ------------------------------------------------------------------
    # Raw patterns
    # ------------------------------------------------------------------

    BLOCKED_PATTERNS = [
        re.compile(
            r"\\+write18\b",
            re.IGNORECASE,
        ),

        re.compile(
            r"\\+immediate\s*\\+write18\b",
            re.IGNORECASE,
        ),

        re.compile(
            r"--shell-escape\b",
            re.IGNORECASE,
        ),

        re.compile(
            r"-shell-escape\b",
            re.IGNORECASE,
        ),

        re.compile(
            r"shell_escape",
            re.IGNORECASE,
        ),

        re.compile(
            r"\\+openout\b",
            re.IGNORECASE,
        ),

        re.compile(
            r"\\+openin\b",
            re.IGNORECASE,
        ),

        re.compile(
            r"\\+input\b",
            re.IGNORECASE,
        ),

        re.compile(
            r"\\+include\b",
            re.IGNORECASE,
        ),

        re.compile(
            r"\\+verbatiminput\b",
            re.IGNORECASE,
        ),

        re.compile(
            r"\\+catcode\b",
            re.IGNORECASE,
        ),

        re.compile(
            r"\\+sys_get_shell\b",
            re.IGNORECASE,
        ),

        re.compile(
            r"\\+@@input\b",
            re.IGNORECASE,
        ),

        re.compile(
            r"file://",
            re.IGNORECASE,
        ),

        re.compile(
            r"file:",
            re.IGNORECASE,
        ),
    ]

    # ------------------------------------------------------------------
    # Allowed TikZ libraries.
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
    # Packages that may be explicitly requested by the author.
    #
    # Accqudo still controls the actual document preamble.
    # ------------------------------------------------------------------

    ALLOWED_PACKAGES = {
        "amsmath",
        "amssymb",
        "amsfonts",
        "mathtools",
        "xcolor",
        "graphicx",
        "tikz",
        "standalone",
        "inputenc",
        "fontenc",
    }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    def sanitize(cls, latex: str) -> str:
        """
        Validate and normalize LaTeX.

        Returns only the body that Accqudo should compile.

        Complete document wrappers are removed.
        """

        if latex is None:
            raise LatexSecurityError(
                "LaTeX content cannot be null."
            )

        if not isinstance(latex, str):
            raise LatexSecurityError(
                "LaTeX content must be a string."
            )

        if not latex.strip():
            raise LatexSecurityError(
                "LaTeX content cannot be empty."
            )

        if len(latex) > cls.MAX_INPUT_LENGTH:
            raise LatexSecurityError(
                f"LaTeX content exceeds the maximum allowed size "
                f"of {cls.MAX_INPUT_LENGTH} characters."
            )

        content = latex.replace(
            "\r\n",
            "\n",
        ).replace(
            "\r",
            "\n",
        )

        content = content.lstrip("\ufeff")

        # --------------------------------------------------------------
        # Security checks BEFORE normalization.
        # --------------------------------------------------------------

        cls._check_blocked_patterns(content)
        cls._check_blocked_commands(content)

        # --------------------------------------------------------------
        # Validate explicit package requests.
        # --------------------------------------------------------------

        cls._validate_packages(content)

        # --------------------------------------------------------------
        # Validate TikZ libraries.
        # --------------------------------------------------------------

        cls._validate_tikz_libraries(content)

        # --------------------------------------------------------------
        # Normalize complete LaTeX document into its body.
        # --------------------------------------------------------------

        content = cls._extract_document_body(content)

        # --------------------------------------------------------------
        # Validate the final body.
        # --------------------------------------------------------------

        if not content.strip():
            raise LatexSecurityError(
                "LaTeX document does not contain any renderable content."
            )

        cls._check_balanced_basic_delimiters(content)

        return content.strip()

    # ------------------------------------------------------------------
    # Blocked constructs
    # ------------------------------------------------------------------

    @classmethod
    def _check_blocked_patterns(
        cls,
        content: str,
    ) -> None:

        for pattern in cls.BLOCKED_PATTERNS:
            if pattern.search(content):
                raise LatexSecurityError(
                    "LaTeX contains a blocked or unsafe construct."
                )

    @classmethod
    def _check_blocked_commands(
        cls,
        content: str,
    ) -> None:

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

    # ------------------------------------------------------------------
    # Package validation
    # ------------------------------------------------------------------

    @classmethod
    def _validate_packages(
        cls,
        content: str,
    ) -> None:

        package_pattern = re.compile(
            r"\\+usepackage"
            r"(?:\s*\[[^\]]*\])?"
            r"\s*\{([^}]*)\}",
            flags=re.IGNORECASE,
        )

        for match in package_pattern.finditer(content):
            package_list = match.group(1)

            for package in package_list.split(","):
                package = package.strip()

                if not package:
                    continue

                if package not in cls.ALLOWED_PACKAGES:
                    raise LatexSecurityError(
                        f"LaTeX package '{package}' is not allowed."
                    )

    # ------------------------------------------------------------------
    # TikZ library validation
    # ------------------------------------------------------------------

    @classmethod
    def _validate_tikz_libraries(
        cls,
        content: str,
    ) -> None:

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

    # ------------------------------------------------------------------
    # Complete document handling
    # ------------------------------------------------------------------

    @classmethod
    def _extract_document_body(
        cls,
        content: str,
    ) -> str:
        """
        Convert either a complete LaTeX document or a LaTeX body into
        body-only content.

        Example:

            \\documentclass{standalone}

            \\usepackage{tikz}

            \\begin{document}

            HELLO

            \\end{document}

        becomes:

            HELLO
        """

        # --------------------------------------------------------------
        # Remove documentclass.
        #
        # We do not trust or use the user's document class.
        # --------------------------------------------------------------

        content = re.sub(
            r"\\+documentclass"
            r"(?:\s*\[[^\]]*\])?"
            r"\s*\{[^}]*\}",
            "",
            content,
            flags=re.IGNORECASE,
        )

        # --------------------------------------------------------------
        # Remove allowed usepackage declarations.
        #
        # Accqudo controls the actual preamble in the renderer.
        # --------------------------------------------------------------

        content = re.sub(
            r"\\+usepackage"
            r"(?:\s*\[[^\]]*\])?"
            r"\s*\{[^}]*\}",
            "",
            content,
            flags=re.IGNORECASE,
        )

        # --------------------------------------------------------------
        # Remove TikZ library declarations.
        #
        # The renderer owns the required TikZ libraries.
        # --------------------------------------------------------------

        content = re.sub(
            r"\\+usetikzlibrary\s*\{[^}]*\}",
            "",
            content,
            flags=re.IGNORECASE,
        )

        # --------------------------------------------------------------
        # If a document environment exists, extract its contents.
        # --------------------------------------------------------------

        begin_match = re.search(
            r"\\+begin\s*\{\s*document\s*\}",
            content,
            flags=re.IGNORECASE,
        )

        end_matches = list(
            re.finditer(
                r"\\+end\s*\{\s*document\s*\}",
                content,
                flags=re.IGNORECASE,
            )
        )

        if begin_match:
            if not end_matches:
                raise LatexSecurityError(
                    "LaTeX contains \\begin{document} "
                    "but no matching \\end{document}."
                )

            end_match = end_matches[-1]

            if end_match.start() < begin_match.end():
                raise LatexSecurityError(
                    "Invalid LaTeX document wrapper."
                )

            content = content[
                begin_match.end():end_match.start()
            ]

        else:
            # If there is no begin{document}, there must not be an
            # end{document} floating around.
            if end_matches:
                raise LatexSecurityError(
                    "LaTeX contains \\end{document} "
                    "without \\begin{document}."
                )

        return content.strip()

    # ------------------------------------------------------------------
    # Basic delimiter validation
    # ------------------------------------------------------------------

    @classmethod
    def _check_balanced_basic_delimiters(
        cls,
        content: str,
    ) -> None:

        pairs = [
            ("{", "}"),
            ("[", "]"),
            ("(", ")"),
        ]

        for opening, closing in pairs:
            if not cls._is_balanced(
                content,
                opening,
                closing,
            ):
                raise LatexSecurityError(
                    f"Unbalanced LaTeX delimiter: "
                    f"'{opening}' / '{closing}'."
                )

    @staticmethod
    def _is_balanced(
        content: str,
        opening: str,
        closing: str,
    ) -> bool:

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
    # Detection helpers
    # ------------------------------------------------------------------

    @classmethod
    def contains_tikz(
        cls,
        latex: str,
    ) -> bool:

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
            r"\\+fill\b",
            r"\\+clip\b",
            r"\\+shade\b",
            r"\\+shadedraw\b",
            r"\\+foreach\b",
        ]

        return any(
            re.search(
                pattern,
                latex,
                flags=re.IGNORECASE,
            )
            for pattern in tikz_patterns
        )

    @classmethod
    def contains_complex_latex(
        cls,
        latex: str,
    ) -> bool:

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
            re.search(
                pattern,
                latex,
                flags=re.IGNORECASE,
            )
            for pattern in complex_patterns
        )


def sanitize_latex(
    latex: str,
) -> str:
    """
    Convenience function.
    """

    return LatexSanitizer.sanitize(latex)