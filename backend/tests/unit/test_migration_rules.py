import pytest

from app.ingestion.migration_rules import (
    SPRING_BOOT_3_RULES,
    is_jakarta_migratable_import,
)
from app.models.manifest import JavaClassManifest, JavaFileManifest, JavaMethodManifest


@pytest.mark.parametrize(
    "import_name",
    [
        "javax.persistence.Entity",
        "javax.persistence.*",
        "javax.validation.Valid",
        "javax.validation.constraints.NotBlank",
        "javax.servlet.http.HttpServletRequest",
        "javax.annotation.PostConstruct",
        "javax.transaction.Transactional",
        "javax.ws.rs.GET",
        "javax.xml.bind.annotation.XmlRootElement",
        "javax.security.auth.message.AuthStatus",
    ],
)
def test_jakarta_allowlist_matches_jakarta_ee_packages(import_name: str):
    assert is_jakarta_migratable_import(import_name) is True


@pytest.mark.parametrize(
    "import_name",
    [
        "javax.sql.DataSource",
        "javax.crypto.Cipher",
        "javax.net.ssl.SSLContext",
        "javax.naming.InitialContext",
        "javax.swing.JFrame",
        "javax.xml.parsers.DocumentBuilderFactory",
        "javax.xml.transform.Transformer",
        "javax.transaction.xa.XAResource",
        "javax.annotation.processing.Processor",
        "javax.security.auth.Subject",
        "java.util.List",
    ],
)
def test_java_se_javax_packages_are_excluded(import_name: str):
    assert is_jakarta_migratable_import(import_name) is False


def test_javax_rule_does_not_trigger_on_java_se_only_manifest():
    manifest = JavaFileManifest(
        file_path="src/main/java/com/ex/DbHelper.java",
        package_name="com.ex",
        imports=["javax.sql.DataSource", "javax.crypto.Cipher"],
        javax_imports=["javax.sql.DataSource", "javax.crypto.Cipher"],
        jakarta_migratable_imports=[],
        classes=[JavaClassManifest(name="DbHelper")],
    )
    javax_rule = next(r for r in SPRING_BOOT_3_RULES if r.id == "javax-to-jakarta")
    assert javax_rule.predicate(manifest) is False


def test_pbkdf2_rule_requires_v5_5_and_respects_ast_flag():
    rule = next(r for r in SPRING_BOOT_3_RULES if r.id == "pbkdf2-password-encoder-upgrade")
    assert "defaultsForSpringSecurity_v5_5()" in rule.migration_instruction
    assert "Do NOT use `defaultsForSpringSecurity_v5_8()`" in rule.migration_instruction

    already_migrated = JavaFileManifest(
        file_path="src/main/java/com/ex/AppConfig.java",
        imports=["org.springframework.security.crypto.password.Pbkdf2PasswordEncoder"],
        has_deprecated_pbkdf2_constructor=False,
    )
    assert rule.predicate(already_migrated) is False

    needs_migration = JavaFileManifest(
        file_path="src/main/java/com/ex/AppConfig.java",
        imports=["org.springframework.security.crypto.password.Pbkdf2PasswordEncoder"],
        has_deprecated_pbkdf2_constructor=True,
    )
    assert rule.predicate(needs_migration) is True


def test_response_entity_exception_handler_rule_triggers_on_http_status_param():
    rule = next(
        r for r in SPRING_BOOT_3_RULES if r.id == "response-entity-exception-handler-statuscode"
    )
    assert rule.deterministic_capable is True

    needs_migration = JavaFileManifest(
        file_path="src/main/java/com/ex/GlobalExceptionHandler.java",
        imports=[
            "org.springframework.http.HttpStatus",
            "org.springframework.web.servlet.mvc.method.annotation.ResponseEntityExceptionHandler",
        ],
        classes=[
            JavaClassManifest(
                name="GlobalExceptionHandler",
                extends=["ResponseEntityExceptionHandler"],
            )
        ],
        methods=[
            JavaMethodManifest(
                name="handleMethodArgumentNotValid",
                parameters=[
                    "MethodArgumentNotValidException",
                    "HttpHeaders",
                    "HttpStatus",
                    "WebRequest",
                ],
            )
        ],
    )
    assert rule.predicate(needs_migration) is True

    already_migrated = JavaFileManifest(
        file_path="src/main/java/com/ex/GlobalExceptionHandler.java",
        imports=[
            "org.springframework.http.HttpStatus",
            "org.springframework.http.HttpStatusCode",
            "org.springframework.web.servlet.mvc.method.annotation.ResponseEntityExceptionHandler",
        ],
        classes=[
            JavaClassManifest(
                name="GlobalExceptionHandler",
                extends=["ResponseEntityExceptionHandler"],
            )
        ],
        methods=[
            JavaMethodManifest(
                name="handleMethodArgumentNotValid",
                parameters=[
                    "MethodArgumentNotValidException",
                    "HttpHeaders",
                    "HttpStatusCode",
                    "WebRequest",
                ],
            )
        ],
    )
    assert rule.predicate(already_migrated) is False


def test_security_configurer_adapter_rule_guides_password_encoder_interface_and_lambda_dsl():
    rule = next(
        r for r in SPRING_BOOT_3_RULES if r.id == "security-configurer-adapter-removal"
    )
    assert "@Configuration" in rule.migration_instruction
    assert "@EnableWebSecurity" in rule.migration_instruction
    assert "SecurityFilterChain" in rule.migration_instruction
    assert "PasswordEncoder" in rule.migration_instruction
    assert "BCryptPasswordEncoder" in rule.migration_instruction


def test_springfox_to_springdoc_rule_matches_swagger_imports():
    rule = next(r for r in SPRING_BOOT_3_RULES if r.id == "springfox-to-springdoc")
    swagger_cfg = JavaFileManifest(
        file_path="src/main/java/com/ex/SwaggerConfig.java",
        imports=["springfox.documentation.spring.web.plugins.Docket"],
    )
    controller = JavaFileManifest(
        file_path="src/main/java/com/ex/UserController.java",
        imports=["io.swagger.annotations.ApiOperation"],
    )
    plain = JavaFileManifest(
        file_path="src/main/java/com/ex/PlainService.java",
        imports=["org.springframework.stereotype.Service"],
    )
    assert rule.predicate(swagger_cfg) is True
    assert rule.predicate(controller) is True
    assert rule.predicate(plain) is False


def test_commons_multipart_resolver_rule_matches_imports_and_superclass():
    rule = next(
        r for r in SPRING_BOOT_3_RULES if r.id == "commons-multipart-resolver-removal"
    )
    resolver = JavaFileManifest(
        file_path="src/main/java/com/ex/MyMultipartResolver.java",
        imports=["org.springframework.web.multipart.commons.CommonsMultipartResolver"],
        classes=[JavaClassManifest(name="MyMultipartResolver", extends=["CommonsMultipartResolver"])],
    )
    assert rule.predicate(resolver) is True
    assert "StandardServletMultipartResolver" in rule.migration_instruction


def test_hibernate_5_to_6_rule_matches_removed_criteria_and_query_apis():
    rule = next(r for r in SPRING_BOOT_3_RULES if r.id == "hibernate-5-to-6")

    criteria_dao = JavaFileManifest(
        file_path="src/main/java/com/ex/dao/SimpleHibernateDao.java",
        imports=["org.hibernate.Criteria", "org.hibernate.criterion.Restrictions"],
    )
    sql_dao = JavaFileManifest(
        file_path="src/main/java/com/ex/dao/QuestionDaoImpl.java",
        imports=["org.hibernate.SQLQuery"],
    )
    migrated_dao = JavaFileManifest(
        file_path="src/main/java/com/ex/dao/ModernDao.java",
        imports=[
            "jakarta.persistence.criteria.CriteriaBuilder",
            "org.hibernate.query.NativeQuery",
            "org.hibernate.query.Query",
        ],
    )

    assert rule.predicate(criteria_dao) is True
    assert rule.predicate(sql_dao) is True
    assert rule.predicate(migrated_dao) is False
    assert "CriteriaBuilder" in rule.migration_instruction
    assert "NativeQuery" in rule.migration_instruction


def test_shiro_1_to_2_rule_matches_moved_and_removed_imports_only():
    rule = next(r for r in SPRING_BOOT_3_RULES if r.id == "shiro-1-to-2")
    assert rule.deterministic_capable is True

    moved_util = JavaFileManifest(
        file_path="src/main/java/com/ex/security/RolesOrAuthorizationFilter.java",
        imports=["org.apache.shiro.util.StringUtils"],
    )
    wildcard_util = JavaFileManifest(
        file_path="src/main/java/com/ex/security/WildcardFilter.java",
        imports=["org.apache.shiro.util.*"],
    )
    removed_hash = JavaFileManifest(
        file_path="src/main/java/com/ex/security/LegacyHash.java",
        imports=["org.apache.shiro.crypto.hash.Md5Hash"],
    )
    migrated_or_core = JavaFileManifest(
        file_path="src/main/java/com/ex/security/ModernFilter.java",
        imports=[
            "org.apache.shiro.lang.util.StringUtils",
            "org.apache.shiro.util.CollectionUtils",
            "org.apache.shiro.util.ThreadContext",
            "org.apache.shiro.web.filter.authz.RolesAuthorizationFilter",
        ],
    )

    assert rule.predicate(moved_util) is True
    assert rule.predicate(wildcard_util) is True
    assert rule.predicate(removed_hash) is True
    assert rule.predicate(migrated_or_core) is False
    assert "org.apache.shiro.lang.util" in rule.migration_instruction


def test_spring_6_assert_rule_and_hibernate_6_criteria_guidance():
    assert_rule = next(r for r in SPRING_BOOT_3_RULES if r.id == "spring-6-assert")
    assert assert_rule.deterministic_capable is True
    assert assert_rule.predicate(
        JavaFileManifest(
            file_path="src/main/java/com/ex/Locator.java",
            has_deprecated_spring_assert=True,
        )
    ) is True
    assert assert_rule.predicate(
        JavaFileManifest(
            file_path="src/main/java/com/ex/Locator.java",
            has_deprecated_spring_assert=False,
        )
    ) is False

    hib_rule = next(r for r in SPRING_BOOT_3_RULES if r.id == "hibernate-5-to-6")
    assert "cq.from" in hib_rule.migration_instruction
    assert "NEVER call `cb.root(...)`" in hib_rule.migration_instruction
    assert "dao.getEntityManager()" in hib_rule.migration_instruction

