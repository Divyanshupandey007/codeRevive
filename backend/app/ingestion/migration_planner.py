from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from app.config import settings
from app.models.manifest import (
    AdditionalArtifactSpec,
    DependencyMigrationAction,
    MigrationPlan,
    MavenDependency,
    PomManifest,
)

# Well-known Spring Boot 2 -> 3 artifact replacements & version floors
# Format: (group_id, artifact_id): (target_group_id, target_artifact_id, target_version_or_None, remove_version, notes)
_ARTIFACT_REPLACEMENTS = {
    ("org.thymeleaf.extras", "thymeleaf-extras-springsecurity5"): (
        "org.thymeleaf.extras",
        "thymeleaf-extras-springsecurity6",
        None,
        False,
        "Upgrade Thymeleaf Spring Security integration from springsecurity5 to springsecurity6",
    ),
    ("io.springfox", "springfox-boot-starter"): (
        "org.springdoc",
        "springdoc-openapi-starter-webmvc-ui",
        "2.5.0",
        False,
        "Springfox is incompatible with Spring Boot 3; migrate to springdoc-openapi-starter-webmvc-ui",
    ),
    ("io.springfox", "springfox-swagger2"): (
        "org.springdoc",
        "springdoc-openapi-starter-webmvc-ui",
        "2.5.0",
        False,
        "Springfox is incompatible with Spring Boot 3; migrate to springdoc-openapi-starter-webmvc-ui",
    ),
    ("mysql", "mysql-connector-java"): (
        "com.mysql",
        "mysql-connector-j",
        None,
        True,
        "MySQL driver moved from mysql:mysql-connector-java to BOM-managed com.mysql:mysql-connector-j in Spring Boot 3",
    ),
    ("javax.servlet", "javax.servlet-api"): (
        "jakarta.servlet",
        "jakarta.servlet-api",
        None,
        True,
        "Upgrade javax.servlet-api to BOM-managed jakarta.servlet:jakarta.servlet-api",
    ),
    ("javax.servlet", "servlet-api"): (
        "jakarta.servlet",
        "jakarta.servlet-api",
        None,
        True,
        "Upgrade legacy servlet-api to BOM-managed jakarta.servlet:jakarta.servlet-api",
    ),
    ("javax.servlet", "jstl"): (
        "jakarta.servlet.jsp.jstl",
        "jakarta.servlet.jsp.jstl-api",
        None,
        True,
        "Upgrade javax.servlet:jstl to BOM-managed jakarta.servlet.jsp.jstl:jakarta.servlet.jsp.jstl-api",
    ),
    ("jstl", "jstl"): (
        "jakarta.servlet.jsp.jstl",
        "jakarta.servlet.jsp.jstl-api",
        None,
        True,
        "Upgrade jstl to BOM-managed jakarta.servlet.jsp.jstl:jakarta.servlet.jsp.jstl-api",
    ),
    ("javax.servlet.jsp", "jsp-api"): (
        "jakarta.servlet.jsp",
        "jakarta.servlet.jsp-api",
        "3.1.1",
        False,
        "Upgrade javax.servlet.jsp:jsp-api to jakarta.servlet.jsp:jakarta.servlet.jsp-api:3.1.1 (not in Boot 3 BOM)",
    ),
    ("javax.servlet.jsp", "javax.servlet.jsp-api"): (
        "jakarta.servlet.jsp",
        "jakarta.servlet.jsp-api",
        "3.1.1",
        False,
        "Upgrade javax.servlet.jsp-api to jakarta.servlet.jsp:jakarta.servlet.jsp-api:3.1.1 (not in Boot 3 BOM)",
    ),
    ("javax.validation", "validation-api"): (
        "jakarta.validation",
        "jakarta.validation-api",
        None,
        True,
        "Upgrade javax.validation:validation-api to BOM-managed jakarta.validation:jakarta.validation-api",
    ),
    ("javax.annotation", "javax.annotation-api"): (
        "jakarta.annotation",
        "jakarta.annotation-api",
        None,
        True,
        "Upgrade javax.annotation-api to BOM-managed jakarta.annotation:jakarta.annotation-api",
    ),
    ("javax.persistence", "javax.persistence-api"): (
        "jakarta.persistence",
        "jakarta.persistence-api",
        None,
        True,
        "Upgrade javax.persistence-api to BOM-managed jakarta.persistence:jakarta.persistence-api",
    ),
    ("javax.transaction", "javax.transaction-api"): (
        "jakarta.transaction",
        "jakarta.transaction-api",
        None,
        True,
        "Upgrade javax.transaction-api to BOM-managed jakarta.transaction:jakarta.transaction-api",
    ),
    ("javax.transaction", "jta"): (
        "jakarta.transaction",
        "jakarta.transaction-api",
        None,
        True,
        "Upgrade javax.transaction:jta to BOM-managed jakarta.transaction:jakarta.transaction-api",
    ),
    ("javax.el", "javax.el-api"): (
        "jakarta.el",
        "jakarta.el-api",
        None,
        True,
        "Upgrade javax.el-api to BOM-managed jakarta.el:jakarta.el-api",
    ),
    ("javax.websocket", "javax.websocket-api"): (
        "jakarta.websocket",
        "jakarta.websocket-api",
        None,
        True,
        "Upgrade javax.websocket-api to BOM-managed jakarta.websocket:jakarta.websocket-api",
    ),
    ("javax.mail", "javax.mail-api"): (
        "jakarta.mail",
        "jakarta.mail-api",
        None,
        True,
        "Upgrade javax.mail-api to BOM-managed jakarta.mail:jakarta.mail-api",
    ),
    ("com.sun.mail", "javax.mail"): (
        "jakarta.mail",
        "jakarta.mail-api",
        None,
        True,
        "Upgrade com.sun.mail:javax.mail to BOM-managed jakarta.mail:jakarta.mail-api",
    ),
    ("javax.xml.bind", "jaxb-api"): (
        "jakarta.xml.bind",
        "jakarta.xml.bind-api",
        None,
        True,
        "Upgrade jaxb-api to BOM-managed jakarta.xml.bind:jakarta.xml.bind-api",
    ),
    ("org.hibernate", "hibernate-core"): (
        "org.hibernate.orm",
        "hibernate-core",
        None,
        True,
        "Hibernate 6 moved from org.hibernate:hibernate-core to BOM-managed org.hibernate.orm:hibernate-core",
    ),
    ("com.alibaba", "druid-spring-boot-starter"): (
        "com.alibaba",
        "druid-spring-boot-3-starter",
        "1.2.23",
        False,
        "Upgrade Alibaba Druid Spring Boot starter from druid-spring-boot-starter to druid-spring-boot-3-starter:1.2.23",
    ),
}

_VERSIONED_UPGRADES = {
    ("org.mybatis.spring.boot", "mybatis-spring-boot-starter"): (
        "3.0.3",
        "Upgrade MyBatis Spring Boot starter to 3.0.x for Spring Boot 3 compatibility",
    ),
    ("org.mybatis.spring.boot", "mybatis-spring-boot-starter-test"): (
        "3.0.3",
        "Upgrade MyBatis Spring Boot starter test to 3.0.x for Spring Boot 3 compatibility",
    ),
    ("com.codeborne", "selenide"): (
        "7.2.3",
        "Upgrade Selenide for compatibility with Spring Boot 3 managed Selenium 4.x",
    ),
}

# Spring Boot 3 BOM dropped org.apache.httpcomponents:* dependencyManagement in favor of client5.
# Pin Boot 2.7 BOM-compatible v4 versions for unversioned org.apache.httpcomponents deps when staying on v4.
_HTTPCOMPONENTS_V4_DEFAULT_VERSIONS: Dict[str, str] = {
    "httpclient": "4.5.14",
    "httpclient-cache": "4.5.14",
    "httpmime": "4.5.14",
    "fluent-hc": "4.5.14",
    "httpcore": "4.4.16",
    "httpcore-nio": "4.4.16",
    "httpasyncclient": "4.1.5",
}

# Properties that often override Boot 3 managed BOM versions and break resolution
_CONFLICTING_BOM_PROPERTIES = {
    "thymeleaf.version",
    "selenium.version",
    "hibernate.version",
    "jakarta.servlet-api.version",
    "jakarta.validation-api.version",
}

_SHIRO_JAKARTA_VERSION = "2.0.2"
_SHIRO_REQUIRED_DEPS: Dict[str, List[str]] = {
    "shiro-all": ["shiro-core", "shiro-web", "shiro-spring"],
    "shiro-core": [],
    "shiro-web": ["shiro-core"],
    "shiro-spring": ["shiro-core", "shiro-web"],
    "shiro-spring-boot-starter": ["shiro-core", "shiro-spring"],
    "shiro-spring-boot-web-starter": [
        "shiro-core",
        "shiro-web",
        "shiro-spring",
        "shiro-spring-boot-starter",
    ],
    "shiro-jakarta-ee": ["shiro-core"],
    "shiro-cdi": ["shiro-core"],
    "shiro-jaxrs": ["shiro-core"],
}


def _parse_version_tuple(version: Optional[str]) -> Optional[tuple[int, ...]]:
    if not version:
        return None
    parts: List[int] = []
    for seg in version.strip().split("."):
        digits = ""
        for ch in seg:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else None


def select_openrewrite_recipes(target_spring_boot: str) -> List[str]:
    """Select the OpenRewrite Spring Boot 3 recipe matching the configured target version."""
    parts = target_spring_boot.strip().split(".")
    minor = parts[1] if len(parts) >= 2 else "0"
    if minor in ("0", "1", "2", "3"):
        boot_recipe = f"org.openrewrite.java.spring.boot3.UpgradeSpringBoot_3_{minor}"
    else:
        boot_recipe = "org.openrewrite.java.spring.boot3.UpgradeSpringBoot_3_2"

    return [
        boot_recipe,
        "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta",
    ]


def _shiro_spec(artifact: str) -> AdditionalArtifactSpec:
    exclusions = [] if artifact == "shiro-core" else [("org.apache.shiro", "*")]
    return AdditionalArtifactSpec(
        group_id="org.apache.shiro",
        artifact_id=artifact,
        version=_SHIRO_JAKARTA_VERSION,
        classifier="jakarta",
        exclusions=exclusions,
    )


def _plan_shiro_dependency(
    dep: MavenDependency,
    pom: PomManifest,
    scheduled_coords: Set[Tuple[str, str]],
    properties_to_update: Dict[str, str],
) -> DependencyMigrationAction:
    ver_tuple = _parse_version_tuple(dep.version)
    if ver_tuple is not None and ver_tuple >= (2, 0):
        return DependencyMigrationAction(
            group_id=dep.group_id,
            artifact_id=dep.artifact_id,
            current_version=dep.version,
            target_strategy="keep_or_verify",
            notes="Apache Shiro 2.x already configured; verify jakarta classifier if web/spring modules are used",
        )

    if "shiro.version" in pom.properties:
        properties_to_update["shiro.version"] = _SHIRO_JAKARTA_VERSION

    if dep.artifact_id == "shiro-all":
        needed = [
            art
            for art in _SHIRO_REQUIRED_DEPS["shiro-all"]
            if ("org.apache.shiro", art) not in scheduled_coords
        ]
        for art in needed:
            scheduled_coords.add(("org.apache.shiro", art))
        if not needed:
            return DependencyMigrationAction(
                group_id=dep.group_id,
                artifact_id=dep.artifact_id,
                current_version=dep.version,
                target_strategy="remove_artifact",
                notes="shiro-all was removed in Shiro 2.0 and modular Jakarta artifacts are already present",
            )
        first = _shiro_spec(needed[0])
        extras = [_shiro_spec(art) for art in needed[1:]]
        return DependencyMigrationAction(
            group_id=dep.group_id,
            artifact_id=dep.artifact_id,
            current_version=dep.version,
            target_strategy="upgrade_artifact",
            target_group_id=first.group_id,
            target_artifact_id=first.artifact_id,
            target_version=first.version,
            target_classifier=first.classifier,
            exclusions=first.exclusions,
            additional_artifacts=extras,
            notes="Replace removed shiro-all (javax) with Shiro 2.0.2 Jakarta-classified modules",
        )

    primary = _shiro_spec(dep.artifact_id)
    extras: List[AdditionalArtifactSpec] = []
    for companion in _SHIRO_REQUIRED_DEPS[dep.artifact_id]:
        if ("org.apache.shiro", companion) not in scheduled_coords:
            scheduled_coords.add(("org.apache.shiro", companion))
            extras.append(_shiro_spec(companion))
    return DependencyMigrationAction(
        group_id=dep.group_id,
        artifact_id=dep.artifact_id,
        current_version=dep.version,
        target_strategy="upgrade_artifact",
        target_group_id=primary.group_id,
        target_artifact_id=primary.artifact_id,
        target_version=primary.version,
        target_classifier=primary.classifier,
        exclusions=primary.exclusions,
        additional_artifacts=extras,
        notes="Upgrade Apache Shiro to 2.0.2 with Jakarta EE classifier",
    )


def _plan_dependency_action(
    dep: MavenDependency,
    pom: PomManifest,
    resolved_boot: str,
    has_primary_springfox: bool,
    has_hibernate_core_or_jpa: bool,
    scheduled_coords: Set[Tuple[str, str]],
    properties_to_update: Dict[str, str],
) -> DependencyMigrationAction:
    key = (dep.group_id, dep.artifact_id)
    ver_tuple = _parse_version_tuple(dep.version)

    if key == ("io.springfox", "springfox-swagger-ui"):
        if has_primary_springfox:
            return DependencyMigrationAction(
                group_id=dep.group_id,
                artifact_id=dep.artifact_id,
                current_version=dep.version,
                target_strategy="remove_artifact",
                notes="Redundant when migrating Springfox to springdoc-openapi-starter-webmvc-ui",
            )
        return DependencyMigrationAction(
            group_id=dep.group_id,
            artifact_id=dep.artifact_id,
            current_version=dep.version,
            target_strategy="upgrade_artifact",
            target_group_id="org.springdoc",
            target_artifact_id="springdoc-openapi-starter-webmvc-ui",
            target_version="2.5.0",
            notes="Springfox is incompatible with Spring Boot 3; migrate to springdoc-openapi-starter-webmvc-ui",
        )

    if key == ("org.hibernate", "hibernate-entitymanager"):
        if has_hibernate_core_or_jpa:
            return DependencyMigrationAction(
                group_id=dep.group_id,
                artifact_id=dep.artifact_id,
                current_version=dep.version,
                target_strategy="remove_artifact",
                notes="hibernate-entitymanager was merged into org.hibernate.orm:hibernate-core in Hibernate 6 / Spring Boot 3",
            )
        return DependencyMigrationAction(
            group_id=dep.group_id,
            artifact_id=dep.artifact_id,
            current_version=dep.version,
            target_strategy="upgrade_artifact",
            target_group_id="org.hibernate.orm",
            target_artifact_id="hibernate-core",
            remove_version=True,
            notes="Replace removed hibernate-entitymanager with BOM-managed org.hibernate.orm:hibernate-core",
        )

    if key in _ARTIFACT_REPLACEMENTS:
        new_group, new_artifact, new_ver, remove_ver, notes = _ARTIFACT_REPLACEMENTS[key]
        return DependencyMigrationAction(
            group_id=dep.group_id,
            artifact_id=dep.artifact_id,
            current_version=dep.version,
            target_strategy="upgrade_artifact",
            target_group_id=new_group,
            target_artifact_id=new_artifact,
            target_version=new_ver,
            remove_version=remove_ver,
            notes=notes,
        )

    if key == ("ch.qos.logback", "logback-classic"):
        if ver_tuple is not None and ver_tuple < (1, 4):
            return DependencyMigrationAction(
                group_id=dep.group_id,
                artifact_id=dep.artifact_id,
                current_version=dep.version,
                target_strategy="use_boot_bom",
                remove_version=True,
                notes="Remove pinned Logback 1.2.x version so Spring Boot 3 BOM manages SLF4J 2 / Logback 1.4.x",
            )
        return DependencyMigrationAction(
            group_id=dep.group_id,
            artifact_id=dep.artifact_id,
            current_version=dep.version,
            target_strategy="use_boot_bom",
            notes="Managed by Spring Boot 3 parent BOM",
        )

    if key == ("org.hibernate.validator", "hibernate-validator"):
        if ver_tuple is not None and ver_tuple < (8, 0):
            return DependencyMigrationAction(
                group_id=dep.group_id,
                artifact_id=dep.artifact_id,
                current_version=dep.version,
                target_strategy="use_boot_bom",
                remove_version=True,
                notes="Remove pinned Hibernate Validator < 8 version so Spring Boot 3 BOM manages Jakarta Validation 3",
            )
        return DependencyMigrationAction(
            group_id=dep.group_id,
            artifact_id=dep.artifact_id,
            current_version=dep.version,
            target_strategy="use_boot_bom",
            notes="Managed by Spring Boot 3 parent BOM",
        )

    if key == ("org.projectlombok", "lombok"):
        if ver_tuple is not None and ver_tuple < (1, 18, 30):
            if "lombok.version" in pom.properties:
                properties_to_update["lombok.version"] = "1.18.32"
            return DependencyMigrationAction(
                group_id=dep.group_id,
                artifact_id=dep.artifact_id,
                current_version=dep.version,
                target_strategy="upgrade_version",
                target_version="1.18.32",
                notes="Upgrade Lombok to >= 1.18.30 for Java 17 javac compatibility",
            )
        return DependencyMigrationAction(
            group_id=dep.group_id,
            artifact_id=dep.artifact_id,
            current_version=dep.version,
            target_strategy="use_boot_bom",
            notes="Managed or compatible with Java 17 / Spring Boot 3",
        )

    if key == ("org.tuckey", "urlrewritefilter"):
        if ver_tuple is None or ver_tuple < (5, 0):
            if "urlrewritefilter.version" in pom.properties:
                properties_to_update["urlrewritefilter.version"] = "5.1.3"
            return DependencyMigrationAction(
                group_id=dep.group_id,
                artifact_id=dep.artifact_id,
                current_version=dep.version,
                target_strategy="upgrade_version",
                target_version="5.1.3",
                notes="Upgrade UrlRewriteFilter to 5.1.3 for Jakarta Servlet 6 compatibility",
            )
        return DependencyMigrationAction(
            group_id=dep.group_id,
            artifact_id=dep.artifact_id,
            current_version=dep.version,
            target_strategy="keep_or_verify",
            notes="Compatible with Jakarta Servlet",
        )

    if key == ("org.sitemesh", "sitemesh"):
        if ver_tuple is None or ver_tuple < (3, 2):
            if "sitemesh.version" in pom.properties:
                properties_to_update["sitemesh.version"] = "3.2.1"
            return DependencyMigrationAction(
                group_id=dep.group_id,
                artifact_id=dep.artifact_id,
                current_version=dep.version,
                target_strategy="upgrade_version",
                target_version="3.2.1",
                notes="Upgrade SiteMesh to 3.2.1 for Jakarta Servlet compatibility",
            )
        return DependencyMigrationAction(
            group_id=dep.group_id,
            artifact_id=dep.artifact_id,
            current_version=dep.version,
            target_strategy="keep_or_verify",
            notes="Compatible with Jakarta Servlet",
        )

    if key == ("com.fasterxml.jackson.core", "jackson-databind"):
        if pom.spring_boot_version and (ver_tuple is None or ver_tuple < (2, 15)):
            return DependencyMigrationAction(
                group_id=dep.group_id,
                artifact_id=dep.artifact_id,
                current_version=dep.version,
                target_strategy="use_boot_bom",
                remove_version=True,
                notes="Remove pinned Jackson < 2.15 version so Spring Boot 3 BOM manages Jackson 2.15+",
            )
        return DependencyMigrationAction(
            group_id=dep.group_id,
            artifact_id=dep.artifact_id,
            current_version=dep.version,
            target_strategy="use_boot_bom",
            notes="Managed by Spring Boot 3 parent BOM",
        )

    if dep.group_id == "org.apache.shiro" and dep.artifact_id in _SHIRO_REQUIRED_DEPS:
        return _plan_shiro_dependency(dep, pom, scheduled_coords, properties_to_update)

    if key in _VERSIONED_UPGRADES:
        new_ver, notes = _VERSIONED_UPGRADES[key]
        if dep.artifact_id == "selenide" and resolved_boot.startswith("3.0"):
            new_ver = "6.19.1"
        prop_key = f"{dep.artifact_id}.version"
        if prop_key in pom.properties:
            properties_to_update[prop_key] = new_ver
        return DependencyMigrationAction(
            group_id=dep.group_id,
            artifact_id=dep.artifact_id,
            current_version=dep.version,
            target_strategy="upgrade_version",
            target_version=new_ver,
            notes=notes,
        )

    if (
        dep.group_id == "org.apache.httpcomponents"
        and dep.artifact_id in _HTTPCOMPONENTS_V4_DEFAULT_VERSIONS
        and not dep.version
    ):
        pinned_ver = _HTTPCOMPONENTS_V4_DEFAULT_VERSIONS[dep.artifact_id]
        return DependencyMigrationAction(
            group_id=dep.group_id,
            artifact_id=dep.artifact_id,
            current_version=dep.version,
            target_strategy="upgrade_version",
            target_version=pinned_ver,
            notes=(
                f"Pin {dep.group_id}:{dep.artifact_id} to {pinned_ver} because "
                "Spring Boot 3 parent BOM dropped Apache HttpComponents v4 dependencyManagement"
            ),
        )

    if dep.group_id.startswith("org.springframework"):
        return DependencyMigrationAction(
            group_id=dep.group_id,
            artifact_id=dep.artifact_id,
            current_version=dep.version,
            target_strategy="use_boot_bom",
            notes="Managed by Spring Boot 3 parent BOM",
        )

    if dep.group_id.startswith("javax."):
        return DependencyMigrationAction(
            group_id=dep.group_id,
            artifact_id=dep.artifact_id,
            current_version=dep.version,
            target_strategy="migrate_to_jakarta",
            notes="Replace legacy javax.* dependency with jakarta.* BOM-managed artifact",
        )

    return DependencyMigrationAction(
        group_id=dep.group_id,
        artifact_id=dep.artifact_id,
        current_version=dep.version,
        target_strategy="keep_or_verify",
        notes="Retain unless incompatible with Jakarta EE / Java 17",
    )


def build_migration_plan(
    pom: PomManifest,
    target_spring_boot: Optional[str] = None,
    target_java: Optional[str] = None,
) -> MigrationPlan:
    """Generate a repository-independent MigrationPlan dynamically from parsed POM metadata."""
    resolved_boot = target_spring_boot or settings.target_spring_boot_version
    resolved_java = target_java or settings.target_java_version

    properties_to_update: Dict[str, str] = {}
    present_coords = {(d.group_id, d.artifact_id) for d in pom.dependencies}
    scheduled_coords = set(present_coords)
    has_primary_springfox = any(
        coord in present_coords
        for coord in (
            ("io.springfox", "springfox-boot-starter"),
            ("io.springfox", "springfox-swagger2"),
        )
    )
    has_hibernate_core_or_jpa = any(
        coord in present_coords
        for coord in (
            ("org.hibernate", "hibernate-core"),
            ("org.hibernate.orm", "hibernate-core"),
            ("org.springframework.boot", "spring-boot-starter-data-jpa"),
        )
    )

    dep_actions = [
        _plan_dependency_action(
            dep=dep,
            pom=pom,
            resolved_boot=resolved_boot,
            has_primary_springfox=has_primary_springfox,
            has_hibernate_core_or_jpa=has_hibernate_core_or_jpa,
            scheduled_coords=scheduled_coords,
            properties_to_update=properties_to_update,
        )
        for dep in pom.dependencies
    ]

    properties_to_remove: List[str] = [
        prop for prop in pom.properties if prop in _CONFLICTING_BOM_PROPERTIES
    ]

    for prop in (
        "java.version",
        "maven.compiler.source",
        "maven.compiler.target",
        "maven.compiler.release",
    ):
        if prop in pom.properties and pom.properties[prop] != resolved_java:
            properties_to_update[prop] = resolved_java
    if "java.version" not in properties_to_update and pom.java_version != resolved_java:
        properties_to_update["java.version"] = resolved_java

    repositories_to_remove = [
        url
        for url in pom.repositories
        if "repo.spring.io" in url
    ]

    return MigrationPlan(
        source_spring_boot=pom.spring_boot_version,
        target_spring_boot=resolved_boot,
        source_java=pom.java_version,
        target_java=resolved_java,
        dependencies=dep_actions,
        properties_to_remove=properties_to_remove,
        properties_to_update=properties_to_update,
        repositories_to_remove=repositories_to_remove,
        openrewrite_recipes=select_openrewrite_recipes(resolved_boot),
    )


def render_pom_migration_instruction(plan: MigrationPlan) -> str:
    """Render dynamic, repository-specific POM migration instructions from a MigrationPlan."""
    lines = [
        f"Upgrade this Maven pom.xml from Spring Boot {plan.source_spring_boot or '2.x'} "
        f"(Java {plan.source_java or '8/11'}) to Spring Boot {plan.target_spring_boot} "
        f"(Java {plan.target_java}):",
        f"1. Update the spring-boot-starter-parent <version> to {plan.target_spring_boot}.",
    ]

    if plan.properties_to_update:
        updates = ", ".join(f"<{k}>{v}</{k}>" for k, v in plan.properties_to_update.items())
        lines.append(f"2. Set Maven properties: {updates}.")

    if plan.properties_to_remove:
        removals = ", ".join(f"<{p}>" for p in plan.properties_to_remove)
        lines.append(
            f"3. Remove explicit version overrides ({removals}) so the Spring Boot 3 BOM manages compatible versions."
        )

    special_deps = [
        d
        for d in plan.dependencies
        if d.target_strategy
        in ("upgrade_artifact", "upgrade_version", "migrate_to_jakarta", "remove_artifact")
    ]
    if special_deps:
        formatted: List[str] = []
        for d in special_deps:
            if d.target_strategy == "remove_artifact":
                target_desc = "remove dependency"
            elif d.target_group_id and d.target_artifact_id:
                target_desc = f"{d.target_group_id}:{d.target_artifact_id}"
                if d.target_version:
                    target_desc += f":{d.target_version}"
                if d.target_classifier:
                    target_desc += f":{d.target_classifier}"
                if d.exclusions:
                    excl_str = ", ".join(f"{eg}:{ea}" for eg, ea in d.exclusions)
                    target_desc += f" (exclude {excl_str})"
                if d.additional_artifacts:
                    extra_str = ", ".join(
                        f"{e.group_id}:{e.artifact_id}"
                        + (f":{e.version}" if e.version else "")
                        + (f":{e.classifier}" if e.classifier else "")
                        for e in d.additional_artifacts
                    )
                    target_desc += f" + [{extra_str}]"
            else:
                target_desc = d.target_artifact_id or d.target_version or d.target_strategy
            formatted.append(f"{d.group_id}:{d.artifact_id} -> {target_desc} ({d.notes})")
        lines.append(f"4. Update incompatible dependencies: {'; '.join(formatted)}.")

    if plan.repositories_to_remove:
        repos = ", ".join(plan.repositories_to_remove)
        lines.append(
            f"5. Remove unreachable or legacy Spring milestone/snapshot repository entries ({repos}) if Maven Central provides the GA artifacts."
        )

    lines.append("Preserve all other dependencies, plugins, and project metadata untouched.")
    return "\n".join(lines)


def render_resource_migration_instruction(file_path: str, content: str) -> str:
    """Render dynamic instructions for templates, XML configs, and properties/YAML files."""
    suffix = Path(file_path).suffix.lower()
    hints: List[str] = []

    if "javax." in content or "javax/" in content:
        hints.append(
            "Migrate Jakarta EE javax.* references (e.g., javax.servlet, javax.validation, javax.persistence) "
            "to jakarta.*, leaving Java SE packages like javax.sql.* untouched."
        )
    if "#request" in content or "#session" in content or "#servletContext" in content:
        hints.append(
            "In Thymeleaf 3.1 (Spring Boot 3), #request, #session, and #servletContext were removed from "
            "Thymeleaf expressions. Replace `#request.getAttribute('javax.servlet.error.status_code') ge 400` "
            "(or similar status checks) with a null-safe Spring Boot error attribute expression like "
            "`${status != null and status ge 400}` so non-error pages do not fail SpEL evaluation on null."
        )
    if suffix in (".yml", ".yaml", ".properties"):
        hints.append(
            "Update deprecated Spring Boot 2.x configuration properties to Spring Boot 3.x equivalents "
            "(e.g., spring.redis.* -> spring.data.redis.*, spring.zipkin.* -> management.zipkin.tracing.endpoint, "
            "and remove removed properties like spring.jpa.hibernate.use-new-id-generator-mappings)."
        )

    if not hints:
        hints.append(
            "Update any Spring Boot 2.x-specific configuration keys, XML namespaces, or template expressions for Spring Boot 3 compatibility."
        )

    return " ".join(hints) + " Do not change unrelated configuration or markup."
