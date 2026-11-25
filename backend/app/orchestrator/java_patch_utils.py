import re
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

_SYNTAX_PARSE_LOCK = threading.Lock()

_GLUED_ANNOTATION_MODIFIER_RE = re.compile(
    r"@(?:Override|Autowired|Bean|Service|Configuration|Component|Repository|RestController|Controller|ControllerAdvice|Profile|Test|Transactional|PreAuthorize)"
    r"(?:public|private|protected)\b"
)
_CB_ROOT_HALLUCINATION_RE = re.compile(
    r"(?:\b(?:cb|criteriaBuilder|builder|hibernateCriteriaBuilder)|getCriteriaBuilder\s*\([^)]*\))\.root\s*\("
)
_RESIDUAL_HIBERNATE_5_IMPORT_RE = re.compile(
    r"^\s*import\s+org\.hibernate\.(?:Criteria\b|SQLQuery\b|Query\s*;|criterion\.|internal\.CriteriaImpl\b|transform\.|metadata\.)",
    re.MULTILINE,
)
_RESIDUAL_HIBERNATE_5_FQCN_RE = re.compile(
    r"\borg\.hibernate\.(?:Criteria\b|SQLQuery\b|criterion\.|internal\.CriteriaImpl\b|transform\.|metadata\.)"
)
_JAVA_COMMENTS_AND_STRINGS_RE = re.compile(
    r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"',
    re.DOTALL,
)
_VALID_JAVA_STRING_ESCAPES = frozenset("btnfrs\"'\\u01234567")


def validate_multiline_java_formatting(
    file_path: str, original: str, patched: str
) -> Optional[str]:
    if not file_path.endswith(".java"):
        return None
    if _GLUED_ANNOTATION_MODIFIER_RE.search(patched):
        return (
            "Patch concatenated a Java annotation directly onto a visibility modifier "
            "(e.g. '@Autowiredprivate' or '@Beanpublic')."
        )
    orig_nonempty = [ln for ln in original.splitlines() if ln.strip()]
    new_nonempty = [ln for ln in patched.splitlines() if ln.strip()]
    if len(orig_nonempty) >= 4 and len(new_nonempty) <= 2 and patched.count(";") >= 2:
        return "Patch collapsed a multi-line Java file onto a single line."
    return None


def strip_java_comments_and_strings(source: str) -> str:
    return _JAVA_COMMENTS_AND_STRINGS_RE.sub(" ", source)


def validate_java_api_sanity(
    file_path: str, migration_rule: str, patched: str
) -> Optional[str]:
    if not file_path.endswith(".java"):
        return None
    stripped = strip_java_comments_and_strings(patched)
    if (
        ("CriteriaBuilder" in stripped or "CriteriaQuery" in stripped or "getCriteriaBuilder" in stripped)
        and _CB_ROOT_HALLUCINATION_RE.search(stripped)
    ):
        return (
            "CriteriaBuilder / HibernateCriteriaBuilder does NOT have a `.root(Class)` method. "
            "Replace every `cb.root(Entity.class)` call with `CriteriaQuery<Entity> cq = cb.createQuery(Entity.class); "
            "Root<Entity> root = cq.from(Entity.class);` (or use the existing `Root` variable from `cq.from(...)`)."
        )
    if (
        "Hibernate 6 (Spring Boot 3) removed Hibernate 5" in migration_rule
        and (
            _RESIDUAL_HIBERNATE_5_IMPORT_RE.search(stripped)
            or _RESIDUAL_HIBERNATE_5_FQCN_RE.search(stripped)
        )
    ):
        return (
            "The patch still leaves removed Hibernate 5 imports or inline references "
            "(`org.hibernate.Criteria`, `org.hibernate.Query`, `org.hibernate.SQLQuery`, "
            "`org.hibernate.criterion.*`, or `org.hibernate.metadata.*`). "
            "Remove EVERY Hibernate 5 reference—including inline fully-qualified names—and migrate them "
            "to Hibernate 6 / Jakarta Persistence Criteria."
        )
    return None


def _check_java_delimiter_balance(source: str) -> Optional[str]:
    """Scan Java source outside comments/strings/chars for unclosed strings or unbalanced delimiters."""
    n = len(source)
    i = 0
    state = "code"
    braces = 0
    parens = 0
    brackets = 0

    while i < n:
        ch = source[i]
        if state == "code":
            if source.startswith("//", i):
                state = "line_comment"
                i += 2
                continue
            if source.startswith("/*", i):
                state = "block_comment"
                i += 2
                continue
            if source.startswith('"""', i):
                state = "text_block"
                i += 3
                continue
            if ch == '"':
                state = "string"
                i += 1
                continue
            if ch == "'":
                state = "char"
                i += 1
                continue
            if ch == "{":
                braces += 1
            elif ch == "}":
                braces -= 1
            elif ch == "(":
                parens += 1
            elif ch == ")":
                parens -= 1
            elif ch == "[":
                brackets += 1
            elif ch == "]":
                brackets -= 1
            i += 1
        elif state == "line_comment":
            if ch == "\n":
                state = "code"
            i += 1
        elif state == "block_comment":
            if source.startswith("*/", i):
                state = "code"
                i += 2
            else:
                i += 1
        elif state == "text_block":
            if source.startswith('"""', i):
                state = "code"
                i += 3
            elif ch == "\\" and i + 1 < n:
                i += 2
            else:
                i += 1
        elif state == "string":
            if ch == "\\" and i + 1 < n:
                i += 2
            elif ch == '"':
                state = "code"
                i += 1
            elif ch == "\n":
                return "unterminated string literal"
            else:
                i += 1
        elif state == "char":
            if ch == "\\" and i + 1 < n:
                i += 2
            elif ch == "'":
                state = "code"
                i += 1
            elif ch == "\n":
                return "unterminated character literal"
            else:
                i += 1

    if state in ("string", "text_block", "block_comment", "char"):
        return f"unclosed {state.replace('_', ' ')} at end of file (file appears truncated)"
    if braces != 0 or parens != 0 or brackets != 0:
        parts: List[str] = []
        if braces != 0:
            parts.append(f"braces off by {braces:+d}")
        if parens != 0:
            parts.append(f"parentheses off by {parens:+d}")
        if brackets != 0:
            parts.append(f"brackets off by {brackets:+d}")
        return f"unbalanced delimiters ({', '.join(parts)}) — file appears truncated or missing closing syntax"
    return None


def _collect_treesitter_syntax_errors(
    root_node: Any, source_lines: List[str], limit: int = 5
) -> List[str]:
    """Walk a Tree-sitter AST and return concise 1-based line diagnostics for ERROR/MISSING nodes."""
    errors: List[str] = []
    seen_lines: Set[int] = set()
    stack = [root_node]

    while stack and len(errors) < limit:
        node = stack.pop()
        is_err = getattr(node, "type", "") == "ERROR"
        is_missing = bool(getattr(node, "is_missing", False))
        if is_err or is_missing:
            line_no = node.start_point[0] + 1
            if line_no not in seen_lines:
                seen_lines.add(line_no)
                snippet = (
                    source_lines[line_no - 1].strip()[:90]
                    if 0 <= line_no - 1 < len(source_lines)
                    else ""
                )
                kind = f"missing `{node.type}`" if is_missing else "syntax error"
                errors.append(
                    f"line {line_no}: {kind}"
                    + (f" near `{snippet}`" if snippet else "")
                )
            continue
        if getattr(node, "has_error", False):
            children = getattr(node, "children", ()) or ()
            for child in reversed(children):
                if getattr(child, "has_error", False) or getattr(child, "is_missing", False):
                    stack.append(child)

    return errors


def validate_java_syntax(
    file_path: str,
    original: str,
    patched: str,
    parser: Any = None,
) -> Optional[str]:
    """Catch Java syntax errors & EOF truncation before writing a patch to disk."""
    if not file_path.endswith(".java") or not patched or not patched.strip():
        return None

    # 1. Fast comment/string-aware delimiter & EOF truncation check
    patched_balance_err = _check_java_delimiter_balance(patched)
    if patched_balance_err is not None:
        orig_balance_err = _check_java_delimiter_balance(original)
        if orig_balance_err is None:
            return f"Java syntax check failed: {patched_balance_err}."

    # 2. Full Tree-sitter Java AST parse check (thread-safe via lock)
    if parser is not None and hasattr(parser, "parse"):
        with _SYNTAX_PARSE_LOCK:
            patched_tree = parser.parse(patched.encode("utf-8"))
            patched_has_error = bool(getattr(patched_tree.root_node, "has_error", False))
            if patched_has_error:
                orig_tree = parser.parse(original.encode("utf-8"))
                orig_has_error = bool(getattr(orig_tree.root_node, "has_error", False))
                if not orig_has_error:
                    details = _collect_treesitter_syntax_errors(
                        patched_tree.root_node, patched.splitlines()
                    )
                    summary = "; ".join(details) if details else "malformed Java syntax / unexpected EOF"
                    return (
                        f"Java syntax check failed before compilation ({summary}). "
                        "Fix every missing semicolon, parenthesis, brace, and truncated method."
                    )

    return None


def restore_stripped_java_backslashes(
    file_path: str, original: str, patched: str
) -> str:
    """Restore double-backslashes (e.g. \\\\s, \\\\S, \\\\/) stripped during LLM JSON decoding."""
    if not file_path.endswith(".java") or "\\\\" not in original:
        return patched

    orig_lines = original.splitlines()
    orig_stripped = {ln.strip() for ln in orig_lines}
    collapsed_map: Dict[str, Optional[str]] = {}
    for ln in orig_lines:
        s = ln.strip()
        if "\\\\" not in s:
            continue
        collapsed = s.replace("\\\\", "\\")
        if collapsed == s or collapsed in orig_stripped:
            continue
        if collapsed in collapsed_map and collapsed_map[collapsed] != s:
            collapsed_map[collapsed] = None
        else:
            collapsed_map[collapsed] = s

    if not any(v is not None for v in collapsed_map.values()):
        return patched

    trailing_nl = "\n" if patched.endswith("\n") else ""
    out_lines: List[str] = []
    changed = False
    for ln in patched.splitlines():
        s = ln.strip()
        if s not in orig_stripped:
            restored = collapsed_map.get(s)
            if restored:
                leading = ln[: len(ln) - len(ln.lstrip())]
                out_lines.append(f"{leading}{restored}")
                changed = True
                continue
        out_lines.append(ln)

    if not changed:
        return patched
    return "\n".join(out_lines) + trailing_nl


def repair_illegal_java_string_escapes(
    java_src: str, target_lines: Optional[Set[int]] = None
) -> str:
    """Double invalid backslashes inside standard Java string literals on target lines."""
    out: List[str] = []
    n = len(java_src)
    i = 0
    line_num = 1
    state = "code"  # code | line_comment | block_comment | string | text_block | char

    while i < n:
        ch = java_src[i]
        if state == "code":
            if java_src.startswith("//", i):
                out.append("//")
                i += 2
                state = "line_comment"
            elif java_src.startswith("/*", i):
                out.append("/*")
                i += 2
                state = "block_comment"
            elif java_src.startswith('"""', i):
                out.append('"""')
                i += 3
                state = "text_block"
            elif ch == '"':
                out.append(ch)
                i += 1
                state = "string"
            elif ch == "'":
                out.append(ch)
                i += 1
                state = "char"
            else:
                out.append(ch)
                if ch == "\n":
                    line_num += 1
                i += 1
        elif state == "line_comment":
            out.append(ch)
            if ch == "\n":
                line_num += 1
                state = "code"
            i += 1
        elif state == "block_comment":
            if java_src.startswith("*/", i):
                out.append("*/")
                i += 2
                state = "code"
            else:
                out.append(ch)
                if ch == "\n":
                    line_num += 1
                i += 1
        elif state == "text_block":
            if java_src.startswith('"""', i):
                out.append('"""')
                i += 3
                state = "code"
            elif ch == "\\" and i + 1 < n:
                out.append(java_src[i : i + 2])
                if java_src[i + 1] == "\n":
                    line_num += 1
                i += 2
            else:
                out.append(ch)
                if ch == "\n":
                    line_num += 1
                i += 1
        elif state == "char":
            if ch == "\\" and i + 1 < n:
                out.append(java_src[i : i + 2])
                if java_src[i + 1] == "\n":
                    line_num += 1
                i += 2
            else:
                out.append(ch)
                if ch == "'":
                    state = "code"
                elif ch == "\n":
                    line_num += 1
                    state = "code"
                i += 1
        elif state == "string":
            if ch == '"':
                out.append(ch)
                i += 1
                state = "code"
            elif ch == "\n":
                out.append(ch)
                line_num += 1
                state = "code"
                i += 1
            elif ch == "\\" and i + 1 < n:
                nxt = java_src[i + 1]
                if nxt == "\n":
                    out.append(ch)
                    i += 1
                elif nxt in _VALID_JAVA_STRING_ESCAPES:
                    out.append("\\" + nxt)
                    i += 2
                elif target_lines is None or line_num in target_lines:
                    out.append("\\\\" + nxt)
                    i += 2
                else:
                    out.append("\\" + nxt)
                    i += 2
            else:
                out.append(ch)
                i += 1

    return "".join(out)


def summarize_java_api(source: str, max_chars: int = 5000) -> str:
    """Return a compact package + imports + class/interface/method signatures view of Java code."""
    out: List[str] = []
    n = len(source)
    i = 0
    depth = 0
    in_line_comment = False
    in_block_comment = False
    in_string = False
    in_char = False
    skipping_body = False
    body_start_depth = 0

    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                if not skipping_body:
                    out.append(ch)
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
            else:
                if ch == "\n" and not skipping_body:
                    out.append(ch)
                i += 1
            continue

        if in_string:
            if not skipping_body:
                out.append(ch)
            if ch == "\\" and i + 1 < n:
                if not skipping_body:
                    out.append(nxt)
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue

        if in_char:
            if not skipping_body:
                out.append(ch)
            if ch == "\\" and i + 1 < n:
                if not skipping_body:
                    out.append(nxt)
                i += 2
                continue
            if ch == "'":
                in_char = False
            i += 1
            continue

        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch == '"':
            in_string = True
            if not skipping_body:
                out.append(ch)
            i += 1
            continue
        if ch == "'":
            in_char = True
            if not skipping_body:
                out.append(ch)
            i += 1
            continue

        if ch == "{":
            if skipping_body:
                depth += 1
            elif depth == 1:
                out.append("{ /* ... */ }")
                skipping_body = True
                body_start_depth = depth
                depth += 1
            else:
                depth += 1
                out.append(ch)
            i += 1
            continue

        if ch == "}":
            depth = max(depth - 1, 0)
            if skipping_body:
                if depth == body_start_depth:
                    skipping_body = False
            else:
                out.append(ch)
            i += 1
            continue

        if not skipping_body:
            out.append(ch)
        i += 1

    lines = [ln.rstrip() for ln in "".join(out).splitlines() if ln.strip()]
    collapsed = "\n".join(lines)
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[: max_chars - 18].rsplit("\n", 1)[0] + "\n// ... (truncated)"
