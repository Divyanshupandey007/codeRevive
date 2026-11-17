from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from app.ingestion.ast_parser import JavaAstParser
from app.models.execution import CompilerError
from app.models.manifest import JavaFileManifest
from app.orchestrator.java_patch_utils import summarize_java_api

_FQCN_TOKEN_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)\b"
)
_ENTITY_ANNOTATION_RE = re.compile(
    r"^\s*@(?:jakarta|javax)\.persistence\.Entity\b|^\s*@Entity\b",
    re.MULTILINE,
)


def build_local_dep_lookup(
    manifests: List[JavaFileManifest],
) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    fqcn_to_file: Dict[str, str] = {}
    package_to_files: Dict[str, List[str]] = {}
    for manifest in manifests:
        if manifest.package_name:
            package_to_files.setdefault(manifest.package_name, []).append(
                manifest.file_path
            )
        for cls in manifest.classes:
            if not cls.name:
                continue
            fqcn = (
                f"{manifest.package_name}.{cls.name}"
                if manifest.package_name
                else cls.name
            )
            fqcn_to_file.setdefault(fqcn, manifest.file_path)
    return fqcn_to_file, package_to_files


def resolve_local_type_path(
    raw_type: str,
    imports: List[str],
    package_name: Optional[str],
    fqcn_to_file: Dict[str, str],
) -> Optional[str]:
    base = raw_type.split("<", 1)[0].strip()
    if not base:
        return None
    if "." in base and base in fqcn_to_file:
        return fqcn_to_file[base]
    for imp in imports:
        if imp.endswith(f".{base}") and imp in fqcn_to_file:
            return fqcn_to_file[imp]
    if package_name:
        same_pkg = f"{package_name}.{base}"
        if same_pkg in fqcn_to_file:
            return fqcn_to_file[same_pkg]
    for imp in imports:
        if imp.endswith(".*"):
            candidate = f"{imp[:-2]}.{base}"
            if candidate in fqcn_to_file:
                return fqcn_to_file[candidate]
    return None


def extract_error_implicated_local_paths(
    compiler_errors: Optional[List[CompilerError]],
    fqcn_to_file: Dict[str, str],
) -> List[str]:
    if not compiler_errors or not fqcn_to_file:
        return []
    implicated: List[str] = []
    seen: Set[str] = set()
    for err in compiler_errors:
        if not err.message:
            continue
        for match in _FQCN_TOKEN_RE.finditer(err.message):
            dep_path = fqcn_to_file.get(match.group(1))
            if dep_path and dep_path not in seen:
                seen.add(dep_path)
                implicated.append(dep_path)
    return implicated


def extract_error_line_local_paths(
    target_text: str,
    manifest: JavaFileManifest,
    compiler_errors: Optional[List[CompilerError]],
    fqcn_to_file: Dict[str, str],
) -> List[str]:
    """Resolve local types referenced directly or via receivers/parameters on failing source line(s)."""
    if not target_text or not compiler_errors or not fqcn_to_file:
        return []
    src_lines = target_text.splitlines()
    tokens: Set[str] = set()
    for err in compiler_errors:
        if err.line_number is None or err.line_number < 1 or err.line_number > len(src_lines):
            continue
        idx = err.line_number - 1
        window = "\n".join(src_lines[max(0, idx - 2) : min(len(src_lines), idx + 2)])
        tokens.update(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", window))
    if not tokens:
        return []

    resolved: List[str] = []
    seen: Set[str] = set()

    def _try_add(raw_type: str) -> None:
        dep_path = resolve_local_type_path(
            raw_type, manifest.imports, manifest.package_name, fqcn_to_file
        )
        if dep_path and dep_path not in seen:
            seen.add(dep_path)
            resolved.append(dep_path)

    lower_tokens: List[str] = []
    for tok in sorted(tokens):
        if tok[0].isupper():
            _try_add(tok)
        else:
            lower_tokens.append(tok)

    if lower_tokens:
        tok_alternation = "|".join(re.escape(tok) for tok in lower_tokens)
        combined_decl_re = re.compile(
            rf"\b([A-Z][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
            rf"(?:\s*<[^;{{}}()]{{0,80}}>)?"
            rf"(?:\s*\[\s*\])*"
            rf"\s+(?:{tok_alternation})\b(?:\s*\(|\s*[=;,):])"
        )
        for line in src_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith(("//", "/*", "*", "import ", "package ")):
                continue
            for m in combined_decl_re.finditer(stripped):
                _try_add(m.group(1))

    return resolved


def parse_manifest_cached(
    parser: JavaAstParser,
    repo_path: Path,
    rel_path: str,
    cache: Dict[str, Optional[JavaFileManifest]],
) -> Optional[JavaFileManifest]:
    if rel_path in cache:
        return cache[rel_path]
    abs_path = repo_path / rel_path
    if not abs_path.is_file():
        cache[rel_path] = None
        return None
    try:
        manifest = parser.parse_file(abs_path)
    except Exception:
        manifest = None
    cache[rel_path] = manifest
    return manifest


def expand_type_ancestors(
    seed_paths: List[str],
    repo_path: Path,
    parser: JavaAstParser,
    fqcn_to_file: Dict[str, str],
    manifest_cache: Dict[str, Optional[JavaFileManifest]],
    max_depth: int = 4,
    include_seeds: bool = True,
) -> Dict[str, int]:
    """BFS walk through extends/implements relationships up to `max_depth` hops."""
    distances: Dict[str, int] = {}
    queue: List[Tuple[str, int]] = []
    for sp in seed_paths:
        if include_seeds and sp not in distances:
            distances[sp] = 0
        queue.append((sp, 0))

    idx = 0
    while idx < len(queue):
        cur_path, depth = queue[idx]
        idx += 1
        if depth >= max_depth:
            continue
        cur_manifest = parse_manifest_cached(parser, repo_path, cur_path, manifest_cache)
        if cur_manifest is None:
            continue
        for cls in cur_manifest.classes:
            for base in cls.extends + cls.implements:
                parent_path = resolve_local_type_path(
                    base, cur_manifest.imports, cur_manifest.package_name, fqcn_to_file
                )
                if not parent_path or parent_path == cur_path:
                    continue
                next_depth = depth + 1
                if parent_path not in distances or next_depth < distances[parent_path]:
                    distances[parent_path] = next_depth
                    queue.append((parent_path, next_depth))
    return distances


def collect_dependency_context(
    repo_path: Path,
    target_file_path: str,
    file_records: Dict[str, Any],
    modified_files: Set[str],
    fqcn_to_file: Dict[str, str],
    package_to_files: Dict[str, List[str]],
    is_repair: bool = False,
    include_unmodified_locals: bool = False,
    compiler_errors: Optional[List[CompilerError]] = None,
    max_files: int = 10,
    max_total_chars: int = 18000,
    parser: Optional[JavaAstParser] = None,
    manifest_cache: Optional[Dict[str, Optional[JavaFileManifest]]] = None,
) -> Optional[str]:
    if not target_file_path.endswith(".java") or not fqcn_to_file:
        return None
    target_abs = repo_path / target_file_path
    if not target_abs.is_file():
        return None

    active_parser = parser or JavaAstParser(str(repo_path))
    cache = manifest_cache if manifest_cache is not None else {}
    manifest = parse_manifest_cached(active_parser, repo_path, target_file_path, cache)
    if manifest is None:
        return None

    direct_parents: List[str] = []
    for cls in manifest.classes:
        for base in cls.extends + cls.implements:
            dep_path = resolve_local_type_path(
                base, manifest.imports, manifest.package_name, fqcn_to_file
            )
            if dep_path and dep_path not in direct_parents:
                direct_parents.append(dep_path)

    direct_ancestors = expand_type_ancestors(
        direct_parents,
        repo_path,
        active_parser,
        fqcn_to_file,
        cache,
        max_depth=4,
        include_seeds=True,
    )

    try:
        target_text = target_abs.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        target_text = ""

    error_implicated = extract_error_implicated_local_paths(
        compiler_errors, fqcn_to_file
    )
    for line_dep in extract_error_line_local_paths(
        target_text, manifest, compiler_errors, fqcn_to_file
    ):
        if line_dep not in error_implicated:
            error_implicated.append(line_dep)

    error_ancestors = expand_type_ancestors(
        error_implicated,
        repo_path,
        active_parser,
        fqcn_to_file,
        cache,
        max_depth=4,
        include_seeds=True,
    )

    imported_locals: List[str] = []
    for imp in manifest.imports:
        if imp in fqcn_to_file:
            dep_path = fqcn_to_file[imp]
            if dep_path not in imported_locals:
                imported_locals.append(dep_path)
        elif imp.endswith(".*"):
            for dep_path in package_to_files.get(imp[:-2], []):
                if dep_path not in imported_locals:
                    imported_locals.append(dep_path)

    if manifest.package_name and (is_repair or include_unmodified_locals) and target_text:
        for same_pkg_path in package_to_files.get(manifest.package_name, []):
            if same_pkg_path == target_file_path or same_pkg_path in imported_locals:
                continue
            same_manifest = parse_manifest_cached(
                active_parser, repo_path, same_pkg_path, cache
            )
            if not same_manifest:
                continue
            if any(
                cls.name and re.search(rf"\b{re.escape(cls.name)}\b", target_text)
                for cls in same_manifest.classes
            ):
                imported_locals.append(same_pkg_path)

    allow_unmodified = is_repair or include_unmodified_locals
    imported_ancestors: Dict[str, int] = {}
    if allow_unmodified:
        imported_ancestors = expand_type_ancestors(
            imported_locals,
            repo_path,
            active_parser,
            fqcn_to_file,
            cache,
            max_depth=4,
            include_seeds=True,
        )
    else:
        modified_imported = [
            dp
            for dp in imported_locals
            if dp in modified_files
            or (dp in file_records and file_records[dp].applied_by is not None)
        ]
        if modified_imported:
            imported_ancestors = expand_type_ancestors(
                modified_imported,
                repo_path,
                active_parser,
                fqcn_to_file,
                cache,
                max_depth=4,
                include_seeds=True,
            )

    candidate_pool: List[str] = (
        list(direct_ancestors.keys())
        + list(error_ancestors.keys())
        + imported_locals
        + list(imported_ancestors.keys())
    )
    seen: Set[str] = set()
    candidates: List[str] = []
    dep_texts: Dict[str, str] = {}
    is_entity_map: Dict[str, bool] = {}

    for dep_path in candidate_pool:
        if dep_path == target_file_path or dep_path in seen:
            continue
        seen.add(dep_path)
        dep_abs = repo_path / dep_path
        if not dep_abs.is_file():
            continue
        is_tracked_modified = dep_path in modified_files or (
            dep_path in file_records and file_records[dep_path].applied_by is not None
        )
        in_priority_chain = (
            dep_path in direct_ancestors
            or dep_path in error_ancestors
            or dep_path in imported_ancestors
        )
        if not (
            is_tracked_modified
            or (allow_unmodified and (in_priority_chain or dep_path in imported_locals or dep_path in file_records))
        ):
            continue
        try:
            dep_text = dep_abs.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        dep_texts[dep_path] = dep_text
        is_entity_map[dep_path] = bool(_ENTITY_ANNOTATION_RE.search(dep_text))
        candidates.append(dep_path)

    if not candidates:
        return None

    def _priority_key(dp: str) -> Tuple[int, int, int, int, str]:
        if dp in direct_ancestors:
            return (0, direct_ancestors[dp], 0 if dp in modified_files else 1, 0, dp)
        if dp in error_ancestors:
            return (
                1,
                1 if is_entity_map.get(dp, False) else 0,
                error_ancestors[dp],
                0 if dp in modified_files else 1,
                dp,
            )
        if dp in modified_files or (
            dp in file_records and file_records[dp].applied_by is not None
        ):
            return (2, 0 if not is_entity_map.get(dp, False) else 1, 0, 0, dp)
        if not is_entity_map.get(dp, False):
            hop = imported_ancestors.get(dp, 0)
            return (3, 0, hop, 0, dp)
        return (4, 0, 0, 0, dp)

    candidates.sort(key=_priority_key)

    chunks: List[str] = []
    total_chars = 0
    for dep_path in candidates[:max_files]:
        dep_text = dep_texts[dep_path]
        stub = summarize_java_api(dep_text, max_chars=5000)
        entry = f"// {dep_path}\n{stub}"
        if total_chars + len(entry) > max_total_chars:
            if not chunks:
                chunks.append(entry[:max_total_chars])
            break
        chunks.append(entry)
        total_chars += len(entry) + 2

    return "\n\n".join(chunks) if chunks else None
