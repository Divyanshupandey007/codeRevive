from pathlib import Path
import re
from typing import Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from app.ingestion.ast_parser import JavaAstParser
from app.ingestion.migration_rules import (
    SHIRO_PACKAGE_MOVES,
    is_jakarta_migratable_import,
    uses_hibernate_5_removed_imports,
)
from app.models.manifest import JavaFileManifest, JavaMethodManifest


class MethodSignatureDiffResult(BaseModel):
    missing_methods: List[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.missing_methods


class PostExecutionIntegrityResult(BaseModel):
    baseline_test_count: int
    final_test_count: int
    test_count_passed: bool
    missing_public_methods: List[str] = Field(default_factory=list)
    method_signatures_passed: bool
    emptied_test_methods: List[str] = Field(default_factory=list)
    test_bodies_passed: bool = True
    passed: bool
    violations: List[str] = Field(default_factory=list)


_SECURITY_ADAPTER_EXEMPT_METHODS = {
    ("configure", ("HttpSecurity",)),
    ("configure", ("WebSecurity",)),
    ("configure", ("AuthenticationManagerBuilder",)),
    ("authenticationManagerBean", ()),
    ("userDetailsServiceBean", ()),
}


def _short_type(type_name: Optional[str]) -> str:
    if not type_name:
        return ""
    return type_name.split("<", 1)[0].rsplit(".", 1)[-1].rstrip(".").strip()


def _normalized_short_type(type_name: Optional[str]) -> str:
    if not type_name:
        return ""
    base = _short_type(type_name)
    base = base.replace("...", " ...").strip()
    if base.endswith(" ..."):
        base = base[:-4].strip() + "..."
    if base.endswith("[]"):
        base = base[:-2].strip() + "[]"
    return base


def _canonical_type(type_name: Optional[str]) -> str:
    """Normalize cosmetic Java type formatting (spaces inside generics/arrays/varargs)."""
    if not type_name:
        return ""
    s = " ".join(type_name.split())
    s = re.sub(r"\s*([<>,\[\]])\s*", r"\1", s)
    s = re.sub(r"\s*\.\.\.\s*", "...", s)
    return s


def _canonical_method_signature(file_path: str, method: JavaMethodManifest) -> str:
    ret = _canonical_type(method.return_type or "void")
    params = ",".join(_canonical_type(p) for p in method.parameters)
    return f"{file_path}#{ret} {method.name}({params})"


def _strip_known_packages(
    type_name: Optional[str],
    imports: List[str],
) -> str:
    """Normalize package-qualified types (e.g. `org.hibernate.Session` vs `Session`,
    `java.util.Map<String, Object>` vs `Map<String,Object>`) without losing generic arguments."""
    canon = _canonical_type(type_name)
    if not canon:
        return ""

    imported_pkgs = {imp.rsplit(".", 1)[0] for imp in imports if "." in imp}

    def _replace_fqcn(match: re.Match) -> str:
        pkg = match.group(1)
        cls = match.group(2)
        if (
            pkg in imported_pkgs
            or pkg.startswith(("java.lang", "java.util", "org.hibernate", "jakarta.", "javax."))
        ):
            return cls
        return match.group(0)

    return re.sub(r"\b((?:[a-z_]\w*\.)*[a-z_]\w*)\.([A-Z]\w*)", _replace_fqcn, canon)


def _rewrite_jakarta_type(type_str: str, allow_all_javax: bool) -> str:
    if allow_all_javax:
        return type_str.replace("javax.", "jakarta.")

    def _repl(match: re.Match) -> str:
        fqcn = match.group(0)
        if is_jakarta_migratable_import(fqcn):
            return "jakarta." + fqcn[len("javax."):]
        return fqcn

    return re.sub(r"\bjavax(?:\.[a-z_]\w*)+\.[A-Z]\w*", _repl, type_str)


_REMOVED_HIBERNATE_5_TYPES = {
    "Criterion",
    "Criteria",
    "DetachedCriteria",
    "SQLQuery",
    "Projection",
    "ResultTransformer",
    "CriteriaImpl",
}

_SPRINGFOX_SPI_TYPES = {
    "Docket",
    "DocumentationType",
    "OpenApiTransformationContext",
    "WebMvcOpenApiTransformationFilter",
    "OperationContext",
    "ParameterContext",
    "ModelContext",
    "ModelPropertyContext",
    "RequestMappingContext",
    "OperationBuilderPlugin",
    "ParameterBuilderPlugin",
    "ApiListingBuilderPlugin",
    "ModelBuilderPlugin",
    "ModelPropertyBuilderPlugin",
}


def _type_mentions_any(type_str: Optional[str], names: Set[str]) -> bool:
    if not type_str:
        return False
    tokens = (
        type_str.replace("<", " ")
        .replace(">", " ")
        .replace(",", " ")
        .replace("...", " ")
        .replace("[]", " ")
        .split()
    )
    for tok in tokens:
        short = tok.rsplit(".", 1)[-1].strip()
        if short in names:
            return True
    return False


def _types_compatible_with_normalization(
    base_t: Optional[str],
    cur_t: Optional[str],
    baseline_manifest: JavaFileManifest,
    current_manifest: JavaFileManifest,
    allow_raw_parameterization: bool = False,
) -> bool:
    b_canon = _canonical_type(base_t)
    c_canon = _canonical_type(cur_t)
    if b_canon == c_canon:
        return True

    # Jakarta EE javax.* -> jakarta.* equivalence (including inside generics)
    jk_b = _rewrite_jakarta_type(
        b_canon, allow_all_javax=bool(baseline_manifest.jakarta_migratable_imports)
    )
    if jk_b == c_canon:
        return True

    # Package-qualified vs imported short type (preserving exact generic type arguments)
    combined_imports = list(baseline_manifest.imports) + list(current_manifest.imports)
    if _strip_known_packages(jk_b, combined_imports) == _strip_known_packages(c_canon, combined_imports):
        return True

    # Allow raw (unparameterized) Query / CriteriaQuery to gain generic type arguments
    # (e.g. Query -> Query<T>/Query<?>/TypedQuery<T>, CriteriaQuery -> CriteriaQuery<T>/CriteriaQuery<?>)
    if allow_raw_parameterization and "<" not in b_canon:
        b_short = _normalized_short_type(b_canon)
        c_short = _normalized_short_type(c_canon)
        if b_short == "Query" and c_short in ("Query", "TypedQuery"):
            return True
        if b_short == "CriteriaQuery" and c_short == "CriteriaQuery":
            return True

    return False


def _matches_normalized_or_parameterized_signature(
    baseline_manifest: JavaFileManifest,
    method: JavaMethodManifest,
    current_manifest: Optional[JavaFileManifest],
) -> bool:
    """Match methods whose signatures are semantically identical after whitespace/package
    normalization, or where raw Hibernate/JPA types (`Query`, `CriteriaQuery`) gained
    generic type arguments (e.g. `Query<X>`, `CriteriaQuery<T>`) during Hibernate 6 migration."""
    if current_manifest is None:
        return False

    has_query_or_criteria_context = any(
        i
        in (
            "org.hibernate.Query",
            "org.hibernate.query.Query",
            "org.hibernate.query.*",
            "org.hibernate.*",
            "javax.persistence.criteria.CriteriaQuery",
            "javax.persistence.criteria.*",
            "jakarta.persistence.criteria.CriteriaQuery",
            "jakarta.persistence.criteria.*",
            "org.hibernate.criterion.CriteriaQuery",
            "org.hibernate.criterion.*",
        )
        or i.startswith("org.hibernate.criterion.")
        for i in (baseline_manifest.imports + current_manifest.imports)
    ) or uses_hibernate_5_removed_imports(baseline_manifest.imports)

    mentions_raw_query_or_criteria = (
        _type_mentions_any(method.return_type, {"Query", "CriteriaQuery"})
        or any(_type_mentions_any(p, {"Query", "CriteriaQuery"}) for p in method.parameters)
    )
    allow_raw_param = has_query_or_criteria_context and mentions_raw_query_or_criteria

    for cur_m in current_manifest.methods:
        if not (cur_m.is_public and not cur_m.is_test and cur_m.name == method.name):
            continue
        if len(cur_m.parameters) != len(method.parameters):
            continue
        if not _types_compatible_with_normalization(
            method.return_type or "void",
            cur_m.return_type or "void",
            baseline_manifest,
            current_manifest,
            allow_raw_parameterization=allow_raw_param,
        ):
            continue
        if all(
            _types_compatible_with_normalization(
                bp,
                cp,
                baseline_manifest,
                current_manifest,
                allow_raw_parameterization=allow_raw_param,
            )
            for bp, cp in zip(method.parameters, cur_m.parameters)
        ):
            return True
    return False


def _is_exempt_framework_migration_method(
    baseline_manifest: JavaFileManifest,
    method: JavaMethodManifest,
    current_by_path: Dict[str, JavaFileManifest],
    canonical_current_signatures: Set[str],
) -> bool:
    current_manifest = current_by_path.get(baseline_manifest.file_path)

    # 1. WebSecurityConfigurerAdapter overrides replaced by Spring Security 6 @Beans
    has_web_sec_adapter = any(
        "WebSecurityConfigurerAdapter" in i for i in baseline_manifest.imports
    ) or any(
        "WebSecurityConfigurerAdapter" in ext
        for cls in baseline_manifest.classes
        for ext in cls.extends
    )
    short_params = tuple(_short_type(p) for p in method.parameters)
    if has_web_sec_adapter and (method.name, short_params) in _SECURITY_ADAPTER_EXEMPT_METHODS:
        return True

    # 2. Springfox Docket @Bean and SPI filter/plugin methods replaced by SpringDoc OpenAPI 3
    has_springfox = any(i.startswith("springfox.") for i in baseline_manifest.imports)
    if (
        has_springfox
        and (
            current_manifest is None
            or not any(i.startswith("springfox.") for i in current_manifest.imports)
        )
    ):
        springfox_types = set(_SPRINGFOX_SPI_TYPES)
        for imp in baseline_manifest.imports:
            if imp.startswith("springfox.") and not imp.endswith(".*"):
                springfox_types.add(imp.rsplit(".", 1)[-1].strip())
        if _type_mentions_any(method.return_type, springfox_types) or any(
            _type_mentions_any(p, springfox_types) for p in method.parameters
        ):
            return True

    # 3. ResponseEntityExceptionHandler overrides migrating HttpStatus -> HttpStatusCode
    has_exc_handler = any(
        "ResponseEntityExceptionHandler" in i for i in baseline_manifest.imports
    ) or any(
        "ResponseEntityExceptionHandler" in ext
        for cls in baseline_manifest.classes
        for ext in cls.extends
    )
    if (
        has_exc_handler
        and method.name
        and method.name.startswith("handle")
        and any(_short_type(p) == "HttpStatus" for p in method.parameters)
    ):
        upgraded_params = [
            "HttpStatusCode" if _short_type(p) == "HttpStatus" else _canonical_type(p)
            for p in method.parameters
        ]
        upgraded_sig = (
            f"{baseline_manifest.file_path}#{_canonical_type(method.return_type or 'void')} "
            f"{method.name}({','.join(upgraded_params)})"
        )
        if upgraded_sig in canonical_current_signatures:
            return True

    # 4. Removed Hibernate 5 Criteria / Criterion / CriteriaQuery / SQLQuery / ResultTransformer public signatures
    if uses_hibernate_5_removed_imports(baseline_manifest.imports) and (
        current_manifest is None
        or not uses_hibernate_5_removed_imports(current_manifest.imports)
    ):
        removed_types = set(_REMOVED_HIBERNATE_5_TYPES)
        if any(
            i in ("org.hibernate.criterion.Order", "org.hibernate.criterion.*")
            for i in baseline_manifest.imports
        ):
            removed_types.add("Order")
        has_jpa_criteria_query = any(
            i in ("javax.persistence.criteria.*", "jakarta.persistence.criteria.*")
            or (i.endswith(".CriteriaQuery") and "persistence.criteria" in i)
            for i in baseline_manifest.imports
        )
        if not has_jpa_criteria_query:
            removed_types.add("CriteriaQuery")
        if any(i in ("org.hibernate.Query", "org.hibernate.*") for i in baseline_manifest.imports):
            removed_types.add("Query")
        if _type_mentions_any(method.return_type, removed_types) or any(
            _type_mentions_any(p, removed_types) for p in method.parameters
        ):
            return True
        # Even if JPA CriteriaQuery was imported alongside removed Hibernate 5 Criteria APIs,
        # allow raw CriteriaQuery signatures replaced by Specification/Predicate/CriteriaQuery<T>
        # on the same method name.
        if (
            current_manifest is not None
            and any(_normalized_short_type(p) == "CriteriaQuery" for p in method.parameters)
            and any(
                cur_m.is_public
                and not cur_m.is_test
                and cur_m.name == method.name
                and any(
                    _type_mentions_any(cp, {"CriteriaQuery", "Predicate", "Specification"})
                    for cp in cur_m.parameters
                )
                for cur_m in current_manifest.methods
            )
        ):
            return True

    # 4b. Normalized whitespace/package-qualified match & raw Query/CriteriaQuery -> parameterized generics
    if _matches_normalized_or_parameterized_signature(
        baseline_manifest, method, current_manifest
    ):
        return True

    # 5. Fully-qualified Apache Shiro 1.x -> 2.0 relocated parameter/return type substitution
    if any(i.startswith("org.apache.shiro") for i in baseline_manifest.imports) or (
        "org.apache.shiro" in (method.return_type or "")
        or any("org.apache.shiro" in p for p in method.parameters)
    ):
        def _rewrite_shiro(type_str: str) -> str:
            res = type_str
            for old_fqcn, new_fqcn in SHIRO_PACKAGE_MOVES.items():
                if old_fqcn in res:
                    res = res.replace(old_fqcn, new_fqcn)
            return _canonical_type(res)

        sh_ret = _rewrite_shiro(method.return_type or "void")
        sh_params = [_rewrite_shiro(p) for p in method.parameters]
        sh_sig = f"{baseline_manifest.file_path}#{sh_ret} {method.name}({','.join(sh_params)})"
        if sh_sig in canonical_current_signatures:
            return True

    return False


def _public_method_signatures(manifests: List[JavaFileManifest]) -> Set[str]:
    signatures: Set[str] = set()
    for manifest in manifests:
        for method in manifest.methods:
            if method.is_public and not method.is_test and method.name:
                signatures.add(_canonical_method_signature(manifest.file_path, method))
    return signatures


def _test_methods_with_bodies(manifests: List[JavaFileManifest]) -> Dict[str, bool]:
    test_bodies: Dict[str, bool] = {}
    for manifest in manifests:
        for method in manifest.methods:
            if method.is_test and method.name:
                test_bodies[method.signature_key(manifest.file_path)] = (
                    method.has_assertions_or_body
                )
    return test_bodies


def check_test_count(
    manifests: List[JavaFileManifest], baseline_test_count: int
) -> Tuple[int, bool]:
    final_test_count = sum(m.test_count for m in manifests)
    return final_test_count, final_test_count >= baseline_test_count


def check_method_signatures(
    baseline_manifests: List[JavaFileManifest],
    current_manifests: List[JavaFileManifest],
) -> MethodSignatureDiffResult:
    canonical_current_methods = _public_method_signatures(current_manifests)
    current_by_path = {m.file_path: m for m in current_manifests}
    missing: Set[str] = set()
    for manifest in baseline_manifests:
        for method in manifest.methods:
            if not (method.is_public and not method.is_test and method.name):
                continue
            canon_sig = _canonical_method_signature(manifest.file_path, method)
            if canon_sig in canonical_current_methods:
                continue
            if _is_exempt_framework_migration_method(
                manifest, method, current_by_path, canonical_current_methods
            ):
                continue
            missing.add(method.signature_key(manifest.file_path))
    return MethodSignatureDiffResult(missing_methods=sorted(missing))


def check_test_bodies(
    baseline_manifests: List[JavaFileManifest],
    current_manifests: List[JavaFileManifest],
) -> List[str]:
    baseline_tests = _test_methods_with_bodies(baseline_manifests)
    current_tests = _test_methods_with_bodies(current_manifests)
    emptied: List[str] = []
    for sig, had_body in baseline_tests.items():
        if had_body and sig in current_tests and not current_tests[sig]:
            emptied.append(sig)
    return sorted(emptied)


def run_post_execution_integrity(
    repo_path: Path,
    baseline_manifests: List[JavaFileManifest],
    baseline_test_count: int,
) -> PostExecutionIntegrityResult:
    current_manifests = JavaAstParser(str(repo_path)).parse_repo()
    final_test_count, test_count_passed = check_test_count(
        current_manifests, baseline_test_count
    )
    signature_diff = check_method_signatures(baseline_manifests, current_manifests)
    emptied_tests = check_test_bodies(baseline_manifests, current_manifests)
    test_bodies_passed = not emptied_tests

    violations: List[str] = []
    if not test_count_passed:
        violations.append(
            "Agent deleted unit test methods to force build pass. "
            f"baseline_test_count={baseline_test_count}, final_test_count={final_test_count}"
        )

    if not signature_diff.passed:
        violations.append(
            "Agent removed or changed signature of public method(s) that existed before "
            f"migration: {', '.join(signature_diff.missing_methods)}"
        )

    if not test_bodies_passed:
        violations.append(
            "Agent emptied body of existing @Test method(s): "
            f"{', '.join(emptied_tests)}"
        )

    return PostExecutionIntegrityResult(
        baseline_test_count=baseline_test_count,
        final_test_count=final_test_count,
        test_count_passed=test_count_passed,
        missing_public_methods=signature_diff.missing_methods,
        method_signatures_passed=signature_diff.passed,
        emptied_test_methods=emptied_tests,
        test_bodies_passed=test_bodies_passed,
        passed=test_count_passed and signature_diff.passed and test_bodies_passed,
        violations=violations,
    )