from pathlib import Path
import pytest

from app.guardrails.post_execution import (
    check_method_signatures,
    check_test_bodies,
    check_test_count,
)
from app.guardrails.pre_execution import SecurityViolationError, validate_patch_safety
from app.ingestion.patch_applicator import apply_patch_with_diff
from app.models.agent import FilePatchResult
from app.models.manifest import JavaClassManifest, JavaFileManifest, JavaMethodManifest


def test_pre_execution_blocks_unexpected_target_path(tmp_path: Path):
    patch = FilePatchResult(
        file_path="src/main/java/SecurityConfig.java",
        new_content="class SecurityConfig {}",
        explanation="Sneaky redirect to another valid file",
    )
    with pytest.raises(SecurityViolationError, match="unexpected file"):
        validate_patch_safety(
            tmp_path,
            patch,
            expected_target_path="src/main/java/UserService.java",
        )


def test_pre_execution_blocks_traversal_and_forbidden_patterns(tmp_path: Path):
    traversal = FilePatchResult(
        file_path="../../evil.sh",
        new_content="echo hi",
        explanation="traversal",
    )
    with pytest.raises(SecurityViolationError):
        validate_patch_safety(tmp_path, traversal)

    fence = FilePatchResult(
        file_path="A.java",
        new_content="```java\nclass A {}\n```",
        explanation="leaked fence",
    )
    with pytest.raises(SecurityViolationError, match="markdown code fences"):
        validate_patch_safety(tmp_path, fence, expected_target_path="A.java")

    exec_call = FilePatchResult(
        file_path="A.java",
        new_content='class A { void x() { Runtime.getRuntime().exec("id"); } }',
        explanation="forbidden exec",
    )
    with pytest.raises(SecurityViolationError, match="forbidden pattern"):
        validate_patch_safety(tmp_path, exec_call, expected_target_path="A.java")


def test_apply_patch_with_diff_captures_unified_diff(tmp_path: Path):
    target = tmp_path / "A.java"
    target.write_text("import javax.validation.Valid;\nclass A {}\n", encoding="utf-8")

    patch = FilePatchResult(
        file_path="A.java",
        new_content="import jakarta.validation.Valid;\nclass A {}\n",
        explanation="jakarta migration",
    )
    written, diff = apply_patch_with_diff(tmp_path, patch, expected_target_path="A.java")
    assert written == target.resolve()
    assert "-import javax.validation.Valid;" in diff
    assert "+import jakarta.validation.Valid;" in diff


def test_post_execution_catches_parameter_and_return_type_changes():
    baseline = [
        JavaFileManifest(
            file_path="UserService.java",
            classes=[JavaClassManifest(name="UserService")],
            methods=[
                JavaMethodManifest(
                    name="getUser",
                    return_type="User",
                    parameters=["Long"],
                    is_public=True,
                ),
                JavaMethodManifest(
                    name="testFind",
                    is_test=True,
                    has_assertions_or_body=True,
                ),
            ],
            test_count=1,
        )
    ]

    # Parameter type silently changed from Long -> String (same method name!)
    changed_param = [
        JavaFileManifest(
            file_path="UserService.java",
            classes=[JavaClassManifest(name="UserService")],
            methods=[
                JavaMethodManifest(
                    name="getUser",
                    return_type="User",
                    parameters=["String"],
                    is_public=True,
                ),
                JavaMethodManifest(
                    name="testFind",
                    is_test=True,
                    has_assertions_or_body=True,
                ),
            ],
            test_count=1,
        )
    ]

    diff = check_method_signatures(baseline, changed_param)
    assert diff.passed is False
    assert "UserService.java#User getUser(Long)" in diff.missing_methods


def test_post_execution_catches_emptied_test_body_and_dropped_tests():
    baseline = [
        JavaFileManifest(
            file_path="UserTest.java",
            classes=[JavaClassManifest(name="UserTest")],
            methods=[
                JavaMethodManifest(
                    name="works",
                    return_type="void",
                    is_test=True,
                    has_assertions_or_body=True,
                )
            ],
            test_count=1,
        )
    ]
    emptied = [
        JavaFileManifest(
            file_path="UserTest.java",
            classes=[JavaClassManifest(name="UserTest")],
            methods=[
                JavaMethodManifest(
                    name="works",
                    return_type="void",
                    is_test=True,
                    has_assertions_or_body=False,
                )
            ],
            test_count=1,
        )
    ]
    assert check_test_bodies(baseline, emptied) == ["UserTest.java#void works()"]
    _, count_ok = check_test_count(emptied, baseline_test_count=2)
    assert count_ok is False


def test_post_execution_allows_spring_boot_3_framework_adapter_replacements():
    baseline = [
        JavaFileManifest(
            file_path="src/main/java/net/demo/security/WebSecurityConfig.java",
            imports=[
                "org.springframework.security.config.annotation.web.configuration.WebSecurityConfigurerAdapter"
            ],
            classes=[
                JavaClassManifest(
                    name="WebSecurityConfig",
                    extends=["WebSecurityConfigurerAdapter"],
                )
            ],
            methods=[
                JavaMethodManifest(
                    name="configure",
                    return_type="void",
                    parameters=["WebSecurity"],
                    is_public=True,
                ),
                JavaMethodManifest(
                    name="authenticationManagerBean",
                    return_type="AuthenticationManager",
                    parameters=[],
                    is_public=True,
                ),
                JavaMethodManifest(
                    name="passwordEncoder",
                    return_type="PasswordEncoder",
                    parameters=[],
                    is_public=True,
                ),
            ],
        ),
        JavaFileManifest(
            file_path="src/main/java/net/demo/configuration/SwaggerConfig.java",
            imports=["springfox.documentation.spring.web.plugins.Docket"],
            classes=[JavaClassManifest(name="SwaggerConfig")],
            methods=[
                JavaMethodManifest(
                    name="api",
                    return_type="Docket",
                    parameters=[],
                    is_public=True,
                )
            ],
        ),
        JavaFileManifest(
            file_path="src/main/java/net/demo/exception/GlobalExHandler.java",
            imports=["org.springframework.web.servlet.mvc.method.annotation.ResponseEntityExceptionHandler"],
            classes=[
                JavaClassManifest(
                    name="GlobalExHandler",
                    extends=["ResponseEntityExceptionHandler"],
                )
            ],
            methods=[
                JavaMethodManifest(
                    name="handleMethodArgumentNotValid",
                    return_type="ResponseEntity<Object>",
                    parameters=["MethodArgumentNotValidException", "HttpHeaders", "HttpStatus", "WebRequest"],
                    is_public=True,
                )
            ],
        ),
    ]

    migrated = [
        JavaFileManifest(
            file_path="src/main/java/net/demo/security/WebSecurityConfig.java",
            imports=["org.springframework.security.web.SecurityFilterChain"],
            classes=[JavaClassManifest(name="WebSecurityConfig")],
            methods=[
                JavaMethodManifest(
                    name="securityFilterChain",
                    return_type="SecurityFilterChain",
                    parameters=["HttpSecurity"],
                    is_public=True,
                ),
                JavaMethodManifest(
                    name="webSecurityCustomizer",
                    return_type="WebSecurityCustomizer",
                    parameters=[],
                    is_public=True,
                ),
                JavaMethodManifest(
                    name="authenticationManager",
                    return_type="AuthenticationManager",
                    parameters=["AuthenticationConfiguration"],
                    is_public=True,
                ),
                JavaMethodManifest(
                    name="passwordEncoder",
                    return_type="PasswordEncoder",
                    parameters=[],
                    is_public=True,
                ),
            ],
        ),
        JavaFileManifest(
            file_path="src/main/java/net/demo/configuration/SwaggerConfig.java",
            imports=["io.swagger.v3.oas.models.OpenAPI"],
            classes=[JavaClassManifest(name="SwaggerConfig")],
            methods=[
                JavaMethodManifest(
                    name="customOpenAPI",
                    return_type="OpenAPI",
                    parameters=[],
                    is_public=True,
                )
            ],
        ),
        JavaFileManifest(
            file_path="src/main/java/net/demo/exception/GlobalExHandler.java",
            imports=["org.springframework.web.servlet.mvc.method.annotation.ResponseEntityExceptionHandler"],
            classes=[
                JavaClassManifest(
                    name="GlobalExHandler",
                    extends=["ResponseEntityExceptionHandler"],
                )
            ],
            methods=[
                JavaMethodManifest(
                    name="handleMethodArgumentNotValid",
                    return_type="ResponseEntity<Object>",
                    parameters=["MethodArgumentNotValidException", "HttpHeaders", "HttpStatusCode", "WebRequest"],
                    is_public=True,
                )
            ],
        ),
    ]

    diff = check_method_signatures(baseline, migrated)
    assert diff.passed is True
    assert diff.missing_methods == []

    # Removing non-adapter public bean method (passwordEncoder) must still fail!
    migrated_missing_encoder = [
        JavaFileManifest(
            file_path="src/main/java/net/demo/security/WebSecurityConfig.java",
            imports=["org.springframework.security.web.SecurityFilterChain"],
            classes=[JavaClassManifest(name="WebSecurityConfig")],
            methods=[m for m in migrated[0].methods if m.name != "passwordEncoder"],
        ),
        migrated[1],
        migrated[2],
    ]
    diff2 = check_method_signatures(baseline, migrated_missing_encoder)
    assert diff2.passed is False
    assert (
        "src/main/java/net/demo/security/WebSecurityConfig.java#PasswordEncoder passwordEncoder()"
        in diff2.missing_methods
    )


def test_post_execution_allows_hibernate_5_criteria_signature_migration():
    baseline = [
        JavaFileManifest(
            file_path="src/main/java/com/ex/dao/IHibernateDao.java",
            imports=["org.hibernate.Criteria", "org.hibernate.criterion.Criterion"],
            classes=[JavaClassManifest(name="IHibernateDao")],
            methods=[
                JavaMethodManifest(
                    name="find",
                    return_type="List<T>",
                    parameters=["Criterion..."],
                    is_public=True,
                ),
                JavaMethodManifest(
                    name="createCriteria",
                    return_type="Criteria",
                    parameters=["Criterion..."],
                    is_public=True,
                ),
                JavaMethodManifest(
                    name="getById",
                    return_type="T",
                    parameters=["Long"],
                    is_public=True,
                ),
            ],
        )
    ]

    migrated = [
        JavaFileManifest(
            file_path="src/main/java/com/ex/dao/IHibernateDao.java",
            imports=["jakarta.persistence.criteria.CriteriaQuery", "jakarta.persistence.criteria.Predicate"],
            classes=[JavaClassManifest(name="IHibernateDao")],
            methods=[
                JavaMethodManifest(
                    name="find",
                    return_type="List<T>",
                    parameters=["Specification<T>"],
                    is_public=True,
                ),
                JavaMethodManifest(
                    name="getById",
                    return_type="T",
                    parameters=["Long"],
                    is_public=True,
                ),
            ],
        )
    ]

    diff = check_method_signatures(baseline, migrated)
    assert diff.passed is True
    assert diff.missing_methods == []

    # Removing non-Hibernate-5 public method getById must still fail!
    migrated_missing_get = [
        JavaFileManifest(
            file_path="src/main/java/com/ex/dao/IHibernateDao.java",
            imports=["jakarta.persistence.criteria.CriteriaQuery"],
            classes=[JavaClassManifest(name="IHibernateDao")],
            methods=[m for m in migrated[0].methods if m.name != "getById"],
        )
    ]
    diff2 = check_method_signatures(baseline, migrated_missing_get)
    assert diff2.passed is False
    assert "src/main/java/com/ex/dao/IHibernateDao.java#T getById(Long)" in diff2.missing_methods


def test_post_execution_allows_shiro_2_fqcn_signature_migration():
    baseline = [
        JavaFileManifest(
            file_path="src/main/java/com/ex/security/SaltHelper.java",
            imports=["org.apache.shiro.util.ByteSource"],
            classes=[JavaClassManifest(name="SaltHelper")],
            methods=[
                JavaMethodManifest(
                    name="makeSalt",
                    return_type="org.apache.shiro.util.ByteSource",
                    parameters=["String"],
                    is_public=True,
                )
            ],
        )
    ]
    migrated = [
        JavaFileManifest(
            file_path="src/main/java/com/ex/security/SaltHelper.java",
            imports=["org.apache.shiro.lang.util.ByteSource"],
            classes=[JavaClassManifest(name="SaltHelper")],
            methods=[
                JavaMethodManifest(
                    name="makeSalt",
                    return_type="org.apache.shiro.lang.util.ByteSource",
                    parameters=["String"],
                    is_public=True,
                )
            ],
        )
    ]
    diff = check_method_signatures(baseline, migrated)
    assert diff.passed is True
    assert diff.missing_methods == []


def test_post_execution_allows_hibernate_criteria_query_raw_query_and_springfox_spi():
    baseline = [
        JavaFileManifest(
            file_path="src/main/java/com/ex/dao/IHibernateDao.java",
            imports=["org.hibernate.criterion.CriteriaQuery", "org.hibernate.criterion.Criterion"],
            classes=[JavaClassManifest(name="IHibernateDao")],
            methods=[
                JavaMethodManifest(
                    name="findAll",
                    return_type="List<T>",
                    parameters=["CriteriaQuery"],
                    is_public=True,
                ),
            ],
        ),
        JavaFileManifest(
            file_path="src/main/java/com/ex/dao/ISimpleHibernateDao.java",
            imports=["org.hibernate.query.Query", "java.util.Map"],
            classes=[JavaClassManifest(name="ISimpleHibernateDao")],
            methods=[
                JavaMethodManifest(
                    name="createQuery",
                    return_type="Query",
                    parameters=["String", "Map<String, ?>"],
                    is_public=True,
                ),
                JavaMethodManifest(
                    name="createQuery",
                    return_type="Query",
                    parameters=["String", "Object ..."],
                    is_public=True,
                ),
                JavaMethodManifest(
                    name="distinct",
                    return_type="Query",
                    parameters=["Query"],
                    is_public=True,
                ),
            ],
        ),
        JavaFileManifest(
            file_path="src/main/java/com/ex/config/SwaggerHostResolver.java",
            imports=[
                "springfox.documentation.oas.web.OpenApiTransformationContext",
                "springfox.documentation.oas.web.WebMvcOpenApiTransformationFilter",
                "springfox.documentation.spi.DocumentationType",
                "io.swagger.v3.oas.models.OpenAPI",
            ],
            classes=[
                JavaClassManifest(
                    name="SwaggerHostResolver",
                    implements=["WebMvcOpenApiTransformationFilter"],
                )
            ],
            methods=[
                JavaMethodManifest(
                    name="transform",
                    return_type="OpenAPI",
                    parameters=["OpenApiTransformationContext<HttpServletRequest>"],
                    is_public=True,
                ),
                JavaMethodManifest(
                    name="supports",
                    return_type="boolean",
                    parameters=["DocumentationType"],
                    is_public=True,
                ),
                JavaMethodManifest(
                    name="getHostHeader",
                    return_type="String",
                    parameters=[],
                    is_public=True,
                ),
            ],
        ),
    ]

    migrated = [
        JavaFileManifest(
            file_path="src/main/java/com/ex/dao/IHibernateDao.java",
            imports=["jakarta.persistence.criteria.CriteriaQuery"],
            classes=[JavaClassManifest(name="IHibernateDao")],
            methods=[
                JavaMethodManifest(
                    name="findAll",
                    return_type="List<T>",
                    parameters=["CriteriaQuery<T>"],
                    is_public=True,
                ),
            ],
        ),
        JavaFileManifest(
            file_path="src/main/java/com/ex/dao/ISimpleHibernateDao.java",
            imports=["org.hibernate.query.Query", "java.util.Map"],
            classes=[JavaClassManifest(name="ISimpleHibernateDao")],
            methods=[
                JavaMethodManifest(
                    name="createQuery",
                    return_type="Query<T>",
                    parameters=["String", "Map<String, ?>"],
                    is_public=True,
                ),
                JavaMethodManifest(
                    name="createQuery",
                    return_type="Query<T>",
                    parameters=["String", "Object..."],
                    is_public=True,
                ),
                JavaMethodManifest(
                    name="distinct",
                    return_type="Query<X>",
                    parameters=["Query<X>"],
                    is_public=True,
                ),
            ],
        ),
        JavaFileManifest(
            file_path="src/main/java/com/ex/config/SwaggerHostResolver.java",
            imports=[
                "org.springdoc.core.customizers.OpenApiCustomizer",
                "io.swagger.v3.oas.models.OpenAPI",
            ],
            classes=[
                JavaClassManifest(
                    name="SwaggerHostResolver",
                    implements=["OpenApiCustomizer"],
                )
            ],
            methods=[
                JavaMethodManifest(
                    name="customise",
                    return_type="void",
                    parameters=["OpenAPI"],
                    is_public=True,
                ),
                JavaMethodManifest(
                    name="getHostHeader",
                    return_type="String",
                    parameters=[],
                    is_public=True,
                ),
            ],
        ),
    ]

    diff = check_method_signatures(baseline, migrated)
    assert diff.passed is True
    assert diff.missing_methods == []

    # Deleting distinct(Query) or getHostHeader() must still fail integrity!
    migrated_broken = [
        migrated[0],
        JavaFileManifest(
            file_path=migrated[1].file_path,
            imports=migrated[1].imports,
            classes=migrated[1].classes,
            methods=[m for m in migrated[1].methods if m.name != "distinct"],
        ),
        JavaFileManifest(
            file_path=migrated[2].file_path,
            imports=migrated[2].imports,
            classes=migrated[2].classes,
            methods=[m for m in migrated[2].methods if m.name != "getHostHeader"],
        ),
    ]
    diff_broken = check_method_signatures(baseline, migrated_broken)
    assert diff_broken.passed is False
    assert "src/main/java/com/ex/dao/ISimpleHibernateDao.java#Query distinct(Query)" in diff_broken.missing_methods
    assert "src/main/java/com/ex/config/SwaggerHostResolver.java#String getHostHeader()" in diff_broken.missing_methods


def test_post_execution_normalizes_generic_whitespace_and_raw_jpa_criteria_query():
    baseline = [
        JavaFileManifest(
            file_path="src/main/java/com/ex/dao/impl/AnswerDaoImpl.java",
            imports=["org.hibernate.Session", "java.util.Map"],
            classes=[JavaClassManifest(name="AnswerDaoImpl")],
            methods=[
                JavaMethodManifest(
                    name="saveMaps",
                    return_type="int",
                    parameters=["SurveyAnswer", "Map<String,Object>", "Session"],
                    is_public=True,
                ),
            ],
        ),
        JavaFileManifest(
            file_path="src/main/java/com/ex/dao/HibernateDao.java",
            imports=[
                "org.hibernate.Criteria",
                "org.hibernate.criterion.Criterion",
                "javax.persistence.criteria.CriteriaQuery",
            ],
            classes=[JavaClassManifest(name="HibernateDao")],
            methods=[
                JavaMethodManifest(
                    name="findAll",
                    return_type="List<T>",
                    parameters=["CriteriaQuery"],
                    is_public=True,
                ),
                JavaMethodManifest(
                    name="getById",
                    return_type="T",
                    parameters=["Long"],
                    is_public=True,
                ),
            ],
        ),
        JavaFileManifest(
            file_path="src/main/java/com/ex/dao/CustomHelperDao.java",
            imports=["org.hibernate.Criteria", "org.hibernate.criterion.Criterion"],
            classes=[JavaClassManifest(name="CustomHelperDao")],
            methods=[
                JavaMethodManifest(
                    name="findAll",
                    return_type="List<T>",
                    parameters=["CriteriaQuery"],
                    is_public=True,
                ),
            ],
        ),
    ]

    migrated = [
        JavaFileManifest(
            file_path="src/main/java/com/ex/dao/impl/AnswerDaoImpl.java",
            imports=["org.hibernate.Session", "java.util.Map"],
            classes=[JavaClassManifest(name="AnswerDaoImpl")],
            methods=[
                JavaMethodManifest(
                    name="saveMaps",
                    return_type="int",
                    parameters=["SurveyAnswer", "Map<String, Object>", "org.hibernate.Session"],
                    is_public=True,
                ),
            ],
        ),
        JavaFileManifest(
            file_path="src/main/java/com/ex/dao/HibernateDao.java",
            imports=["jakarta.persistence.criteria.CriteriaQuery"],
            classes=[JavaClassManifest(name="HibernateDao")],
            methods=[
                JavaMethodManifest(
                    name="findAll",
                    return_type="List<T>",
                    parameters=["CriteriaQuery<T>"],
                    is_public=True,
                ),
                JavaMethodManifest(
                    name="getById",
                    return_type="T",
                    parameters=["Long"],
                    is_public=True,
                ),
            ],
        ),
        JavaFileManifest(
            file_path="src/main/java/com/ex/dao/CustomHelperDao.java",
            imports=["jakarta.persistence.criteria.CriteriaBuilder"],
            classes=[JavaClassManifest(name="CustomHelperDao")],
            methods=[],
        ),
    ]

    diff = check_method_signatures(baseline, migrated)
    assert diff.passed is True
    assert diff.missing_methods == []

    # Mutating a parameterized generic argument (Map<String,Object> -> Map<String,Long>) MUST fail!
    mutated_generic = [
        JavaFileManifest(
            file_path="src/main/java/com/ex/dao/impl/AnswerDaoImpl.java",
            imports=["org.hibernate.Session", "java.util.Map"],
            classes=[JavaClassManifest(name="AnswerDaoImpl")],
            methods=[
                JavaMethodManifest(
                    name="saveMaps",
                    return_type="int",
                    parameters=["SurveyAnswer", "Map<String, Long>", "Session"],
                    is_public=True,
                ),
            ],
        ),
        migrated[1],
        migrated[2],
    ]
    diff_mutated = check_method_signatures(baseline, mutated_generic)
    assert diff_mutated.passed is False
    assert (
        "src/main/java/com/ex/dao/impl/AnswerDaoImpl.java#int saveMaps(SurveyAnswer,Map<String,Object>,Session)"
        in diff_mutated.missing_methods
    )

