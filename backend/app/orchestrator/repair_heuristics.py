from pathlib import Path
import re
from typing import Any, List, Optional, Set, Tuple

from app.config import settings
from app.models.execution import CompilerError, TestFailure

_APP_RUNTIME_FAILURE_HINTS = (
    "element not found",
    "nosuchelementexception",
    "assertionerror",
    "assertionfailederror",
    "comparisonfailure",
    "org.opentest4j",
    "expected:",
    "status expected:",
    "dataintegrityviolation",
    "badcredentials",
    "credentials is invalid",
    "accessdenied",
    "sqldataexception",
    "templateinputexception",
    "templateprocessingexception",
    "thymeleaf",
    "spel",
    "failed to load applicationcontext",
    "unsatisfieddependencyexception",
    "nosuchbeandefinitionexception",
    "beancreationexception",
    "error creating bean with name",
    "beandefinitionoverrideexception",
    "no qualifying bean of type",
    "required a bean of type",
    "could not autowire",
)

_TEST_HARNESS_FAILURE_HINTS = (
    "webdriverexception",
    "sessionnotcreatedexception",
)

_CLASSPATH_FAILURE_HINTS = (
    "nosuchmethoderror",
    "noclassdeffounderror",
    "classnotfoundexception",
)

_APP_CONTEXT_FAILURE_HINTS = (
    "failed to load applicationcontext",
    "error creating bean with name",
    "beancreationexception",
    "unsatisfieddependencyexception",
    "invalid value type for attribute 'factorybeanobjecttype'",
)

_DEPENDENCY_CLASSPATH_MARKERS = (
    "javax/servlet",
    "javax.servlet",
    "javax/validation",
    "javax.validation",
    "javax/persistence",
    "javax.persistence",
    "javax/xml",
    "javax.xml",
    "javax/annotation",
    "javax.annotation",
    "springdoc",
    "springfox",
    "factorybeanobjecttype",
    "com/baomidou",
    "com.baomidou",
    "mybatis",
    "rocketmq",
    "org/mockito",
    "org.mockito",
    "org/jooq",
    "org.jooq",
    "org/hibernate",
    "org.hibernate",
)

_MISSING_PACKAGE_RE = re.compile(
    r"package\s+([a-zA-Z_][a-zA-Z0-9_.]*)\s+does\s+not\s+exist"
)

_MISSING_PACKAGE_TO_POM_DEPENDENCY: Tuple[Tuple[str, str, str], ...] = (
    (
        "jakarta.validation",
        "org.springframework.boot:spring-boot-starter-validation",
        "Spring Boot 3 BOM-managed Jakarta Validation starter (omit <version>)",
    ),
    (
        "org.hibernate.validator",
        "org.springframework.boot:spring-boot-starter-validation",
        "Spring Boot 3 BOM-managed Hibernate Validator / Jakarta Validation starter (omit <version>)",
    ),
    (
        "jakarta.xml.bind",
        "jakarta.xml.bind:jakarta.xml.bind-api",
        "Spring Boot 3 BOM-managed Jakarta XML Binding API (omit <version>)",
    ),
    (
        "jakarta.activation",
        "jakarta.activation:jakarta.activation-api",
        "Spring Boot 3 BOM-managed Jakarta Activation API (omit <version>)",
    ),
    (
        "jakarta.annotation",
        "jakarta.annotation:jakarta.annotation-api",
        "Spring Boot 3 BOM-managed Jakarta Annotations API (omit <version>)",
    ),
    (
        "jakarta.mail",
        "org.springframework.boot:spring-boot-starter-mail",
        "Spring Boot 3 BOM-managed Jakarta Mail starter (omit <version>)",
    ),
    (
        "jakarta.servlet",
        "jakarta.servlet:jakarta.servlet-api",
        "Spring Boot 3 BOM-managed Jakarta Servlet API (omit <version>)",
    ),
    (
        "jakarta.persistence",
        "jakarta.persistence:jakarta.persistence-api",
        "Spring Boot 3 BOM-managed Jakarta Persistence API (omit <version>)",
    ),
    (
        "jakarta.transaction",
        "jakarta.transaction:jakarta.transaction-api",
        "Spring Boot 3 BOM-managed Jakarta Transaction API (omit <version>)",
    ),
    (
        "jakarta.el",
        "jakarta.el:jakarta.el-api",
        "Spring Boot 3 BOM-managed Jakarta Expression Language API (omit <version>)",
    ),
    (
        "io.swagger.v3",
        "org.springdoc:springdoc-openapi-starter-webmvc-ui:2.5.0",
        "SpringDoc OpenAPI 3 starter for Spring Boot 3",
    ),
    (
        "org.springdoc",
        "org.springdoc:springdoc-openapi-starter-webmvc-ui:2.5.0",
        "SpringDoc OpenAPI 3 starter for Spring Boot 3",
    ),
)

_BEAN_NAME_RE = re.compile(r"Error creating bean with name '([A-Za-z0-9_$.]+)'")

_TEST_PROFILE_FILENAMES = (
    "application-test.properties",
    "application-test.yml",
    "application-test.yaml",
)
_MAVEN_LIFECYCLE_GOALS = {"clean", "compile", "test-compile", "test", "package", "verify", "install"}


def is_dependency_classpath_runtime_failure(
    failed_tests: List[TestFailure], raw_logs: str = ""
) -> bool:
    """Return True when runtime test failures point to an incompatible Boot 2 dependency in pom.xml."""
    if not failed_tests:
        return False
    combined_parts = [
        f"{t.failure_message} {t.stack_trace or ''}" for t in failed_tests
    ]
    if raw_logs:
        combined_parts.append(raw_logs)
    combined = " ".join(combined_parts).lower()
    if any(hint in combined for hint in _TEST_HARNESS_FAILURE_HINTS):
        return False
    if "invalid value type for attribute 'factorybeanobjecttype'" in combined:
        return True
    has_classpath_err = any(hint in combined for hint in _CLASSPATH_FAILURE_HINTS)
    has_app_context = any(hint in combined for hint in _APP_CONTEXT_FAILURE_HINTS)
    has_dep_marker = any(marker in combined for marker in _DEPENDENCY_CLASSPATH_MARKERS)
    return has_classpath_err and (has_app_context or has_dep_marker)


def looks_like_app_runtime_failure(
    failed_tests: List[TestFailure], raw_logs: str = ""
) -> bool:
    if not failed_tests:
        return False
    combined_parts = [
        f"{t.failure_message} {t.stack_trace or ''}" for t in failed_tests
    ]
    if raw_logs:
        combined_parts.append(raw_logs)
    combined = " ".join(combined_parts).lower()
    if any(hint in combined for hint in _TEST_HARNESS_FAILURE_HINTS):
        return False
    if is_dependency_classpath_runtime_failure(failed_tests, raw_logs):
        return True
    if any(hint in combined for hint in _CLASSPATH_FAILURE_HINTS):
        return False
    return any(hint in combined for hint in _APP_RUNTIME_FAILURE_HINTS)


def detect_missing_pom_dependencies(
    compiler_errors: List[CompilerError],
) -> List[Tuple[str, str, str]]:
    """Return (missing_pkg, maven_coord, reason) tuples for missing-package javac errors."""
    results: List[Tuple[str, str, str]] = []
    seen_coords: Set[str] = set()
    for err in compiler_errors or []:
        msg = err.message or ""
        for match in _MISSING_PACKAGE_RE.finditer(msg):
            pkg = match.group(1).strip()
            for prefix, coord, reason in _MISSING_PACKAGE_TO_POM_DEPENDENCY:
                if pkg == prefix or pkg.startswith(f"{prefix}."):
                    if coord not in seen_coords:
                        seen_coords.add(coord)
                        results.append((pkg, coord, reason))
                    break
    return results


def java_errors_only_missing_packages(
    errors: List[CompilerError], missing_packages: Set[str]
) -> bool:
    """Return True when every compiler error on a Java file is caused by missing_packages."""
    if not errors or not missing_packages:
        return False
    saw_missing_pkg = False
    for err in errors:
        msg = err.message or ""
        pkg_match = _MISSING_PACKAGE_RE.search(msg)
        if pkg_match:
            pkg = pkg_match.group(1).strip()
            if any(pkg == mp or pkg.startswith(f"{mp}.") for mp in missing_packages):
                saw_missing_pkg = True
                continue
            return False
        if "cannot find symbol" in msg.lower():
            continue
        return False
    return saw_missing_pkg


def detect_jooq_codegen_skip_defines(
    repo_path: Path, require_existing_sources: bool = True
) -> List[str]:
    """Return Maven -D flags to skip DB-dependent jooq-codegen-maven when safe/needed."""
    pom_path = Path(repo_path) / "pom.xml"
    if not pom_path.is_file():
        return []
    try:
        from lxml import etree as ET

        parser = ET.XMLParser(remove_comments=True, recover=True)
        root = ET.fromstring(pom_path.read_bytes(), parser=parser)
    except Exception:
        return []
    if root is None:
        return []

    ns_uri = root.tag.split("}")[0].strip("{") if root.tag.startswith("{") else None
    ns = {"m": ns_uri} if ns_uri else None
    q = (lambda n: f"m:{n}") if ns_uri else (lambda n: n)

    jooq_plugin = None
    for plugin in root.findall(f".//{q('plugin')}", namespaces=ns):
        aid = plugin.find(q("artifactId"), namespaces=ns)
        if aid is not None and aid.text and aid.text.strip() == "jooq-codegen-maven":
            jooq_plugin = plugin
            break
    if jooq_plugin is None:
        return []

    skip_prop: Optional[str] = None
    skip_el = jooq_plugin.find(f".//{q('skip')}", namespaces=ns)
    if skip_el is not None and skip_el.text:
        raw_skip = skip_el.text.strip()
        if raw_skip.startswith("${") and raw_skip.endswith("}"):
            skip_prop = raw_skip[2:-1].strip()

    has_existing_generated_sources = False
    target_dir_el = jooq_plugin.find(f".//{q('target')}/{q('directory')}", namespaces=ns)
    target_pkg_el = jooq_plugin.find(f".//{q('target')}/{q('packageName')}", namespaces=ns)
    if target_dir_el is not None and target_dir_el.text:
        base_dir = target_dir_el.text.strip().lstrip("./")
        pkg_rel = (
            target_pkg_el.text.strip().replace(".", "/")
            if target_pkg_el is not None and target_pkg_el.text
            else ""
        )
        candidate_dir = Path(repo_path) / base_dir / pkg_rel if pkg_rel else Path(repo_path) / base_dir
        if candidate_dir.is_dir() and any(candidate_dir.rglob("*.java")):
            has_existing_generated_sources = True

    if require_existing_sources and not has_existing_generated_sources:
        return []

    defines = ["-Djooq.codegen.skip=true"]
    if skip_prop and skip_prop != "jooq.codegen.skip":
        defines.append(f"-D{skip_prop}=true")
    return defines


def extract_implicated_bean_stems(raw_logs: str) -> List[str]:
    stems: List[str] = []
    for match in _BEAN_NAME_RE.finditer(raw_logs or ""):
        bean = match.group(1).rsplit(".", 1)[-1].lower()
        if bean and bean not in stems:
            stems.append(bean)
    return stems


def extract_runtime_log_excerpt(raw_logs: str, max_chars: int = 2000) -> str:
    interesting: List[str] = []
    for line in raw_logs.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if (
            "caused by:" in lower
            or "exception" in lower
            or "error" in lower
            or "thymeleaf" in lower
            or "sql" in lower
            or "security" in lower
        ):
            if not stripped.startswith("Downloading from"):
                interesting.append(stripped)
    if not interesting:
        return raw_logs[-max_chars:]
    excerpt = "\n".join(interesting[-30:])
    return excerpt[-max_chars:]


def app_candidate_priority(file_path: str, record: Any) -> Tuple[int, int, str]:
    lower = file_path.lower()
    via_rank = 0 if record.applied_by == "llm" else 1
    if "security" in lower or "auth" in lower:
        kind_rank = 0
    elif "config" in lower:
        kind_rank = 1
    elif "controller" in lower:
        kind_rank = 2
    elif lower.endswith((".html", ".xhtml", ".yml", ".yaml", ".properties")):
        kind_rank = 3
    elif lower == "pom.xml":
        kind_rank = 4
    else:
        kind_rank = 5
    return (via_rank, kind_rank, file_path)


def is_pure_cascading_override_failure(
    compiler_errors: List[CompilerError], failed_tests: List[TestFailure]
) -> bool:
    if failed_tests or not compiler_errors:
        return False
    return all(
        "is not abstract and does not override abstract method" in (e.message or "")
        for e in compiler_errors
    )


def has_spring_test_profile(repo_path: Path) -> bool:
    for rel_dir in ("src/main/resources", "src/test/resources"):
        res_dir = repo_path / rel_dir
        if not res_dir.is_dir():
            continue
        for filename in _TEST_PROFILE_FILENAMES:
            if (res_dir / filename).is_file():
                return True
    return False


def insert_maven_defines(base_args: List[str], defines: List[str]) -> List[str]:
    result = list(base_args)
    insert_at = len(result)
    for idx, arg in enumerate(result):
        if arg in _MAVEN_LIFECYCLE_GOALS:
            insert_at = idx
            break
    return result[:insert_at] + list(defines) + result[insert_at:]


def resolve_sandbox_db_fallback_args(
    repo_path: Path,
    pom_has_h2: bool,
    initial_maven_args: Optional[List[str]],
    current_maven_args: Optional[List[str]],
    tried_fallbacks: Set[str],
    raw_logs: str = "",
) -> Optional[Tuple[str, str, List[str]]]:
    effective_current = (
        list(current_maven_args)
        if current_maven_args is not None
        else list(settings.sandbox_default_maven_args)
    )
    if (
        "jooq_skip" not in tried_fallbacks
        and "jooq-codegen-maven" in (raw_logs or "").lower()
    ):
        skip_defines = detect_jooq_codegen_skip_defines(
            repo_path, require_existing_sources=False
        ) or ["-Djooq.codegen.skip=true", "-Djooq.skip=true"]
        missing_defines = [d for d in skip_defines if d not in effective_current]
        if missing_defines:
            return (
                "jooq_skip",
                f"Skipping DB-dependent jOOQ codegen plugin ({' '.join(missing_defines)})",
                insert_maven_defines(effective_current, missing_defines),
            )

    if (
        "profile_test" not in tried_fallbacks
        and has_spring_test_profile(repo_path)
        and not any(a.startswith("-Dspring.profiles.active=") for a in effective_current)
    ):
        return (
            "profile_test",
            "Activating repository's existing 'test' Spring profile (-Dspring.profiles.active=test)",
            insert_maven_defines(effective_current, ["-Dspring.profiles.active=test"]),
        )

    clean_base = (
        list(initial_maven_args)
        if initial_maven_args is not None
        else list(settings.sandbox_default_maven_args)
    )
    if (
        "h2_override" not in tried_fallbacks
        and pom_has_h2
        and not any(a.startswith("-Dspring.datasource.url=") for a in clean_base)
    ):
        h2_defines = [
            "-Dspring.datasource.url=jdbc:h2:mem:coderevive;DB_CLOSE_DELAY=-1",
            "-Dspring.datasource.driver-class-name=org.h2.Driver",
            "-Dspring.datasource.username=sa",
            "-Dspring.datasource.password=",
            "-Dspring.jpa.database-platform=org.hibernate.dialect.H2Dialect",
            "-Dspring.jpa.hibernate.ddl-auto=create-drop",
        ]
        return (
            "h2_override",
            "Applying in-memory H2 Spring Boot overrides (H2 dependency detected in pom.xml)",
            insert_maven_defines(clean_base, h2_defines),
        )

    return None
