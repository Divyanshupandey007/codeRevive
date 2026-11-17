from collections.abc import Callable
from dataclasses import dataclass
from typing import Dict, List, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.manifest import JavaFileManifest

# Explicit Jakarta EE namespaces that migrated from javax.* -> jakarta.* in Jakarta EE 9/10 (Spring Boot 3).
# Java SE javax.* packages (javax.sql, javax.crypto, javax.net, javax.naming, javax.swing, javax.xml.parsers, etc.)
# MUST NOT be migrated to jakarta.*.
JAKARTA_MIGRATABLE_PREFIXES: Tuple[str, ...] = (
    "javax.activation",
    "javax.annotation",
    "javax.batch",
    "javax.decorator",
    "javax.ejb",
    "javax.el",
    "javax.enterprise",
    "javax.faces",
    "javax.inject",
    "javax.interceptor",
    "javax.jms",
    "javax.json",
    "javax.jws",
    "javax.mail",
    "javax.persistence",
    "javax.resource",
    "javax.security.auth.message",
    "javax.security.enterprise",
    "javax.security.jacc",
    "javax.servlet",
    "javax.transaction",
    "javax.validation",
    "javax.websocket",
    "javax.ws.rs",
    "javax.xml.bind",
    "javax.xml.soap",
    "javax.xml.ws",
)

# Standard Java SE packages under javax.* that remain javax.* in Java 17 / Spring Boot 3.
JAVAX_JAVA_SE_EXCLUSIONS: Tuple[str, ...] = (
    "javax.accessibility",
    "javax.crypto",
    "javax.imageio",
    "javax.lang.model",
    "javax.management",
    "javax.naming",
    "javax.net",
    "javax.print",
    "javax.rmi",
    "javax.script",
    "javax.security.auth.callback",
    "javax.security.auth.kerberos",
    "javax.security.auth.login",
    "javax.security.auth.spi",
    "javax.security.auth.Subject",
    "javax.security.auth.x500",
    "javax.security.cert",
    "javax.security.sasl",
    "javax.sound",
    "javax.sql",
    "javax.swing",
    "javax.tools",
    "javax.xml.catalog",
    "javax.xml.crypto",
    "javax.xml.datatype",
    "javax.xml.namespace",
    "javax.xml.parsers",
    "javax.xml.stream",
    "javax.xml.transform",
    "javax.xml.validation",
    "javax.xml.xpath",
)


def is_jakarta_migratable_import(import_name: str) -> bool:
    """Return True iff `import_name` belongs to a Jakarta EE javax.* namespace.

    Explicitly preserves Java SE packages such as javax.sql.* and javax.crypto.*.
    """
    clean = import_name.strip()
    if not clean.startswith("javax."):
        return False

    # Wildcard package import like `javax.sql.*` or exact subpackage check
    base = clean[:-2] if clean.endswith(".*") else clean

    # Check Java SE exclusions first (handles e.g. javax.security.auth.Subject vs javax.security.auth.message)
    for excluded in JAVAX_JAVA_SE_EXCLUSIONS:
        if base == excluded or base.startswith(excluded + "."):
            return False

    # javax.transaction.xa is Java SE (JTA XA stays in JDK javax.transaction.xa)
    if base == "javax.transaction.xa" or base.startswith("javax.transaction.xa."):
        return False

    # javax.annotation.processing is Java SE APT
    if base == "javax.annotation.processing" or base.startswith("javax.annotation.processing."):
        return False

    for prefix in JAKARTA_MIGRATABLE_PREFIXES:
        if base == prefix or base.startswith(prefix + "."):
            return True

    return False


# Exact class-level package relocations verified between Apache Shiro 1.13.0 and 2.0.2
# (shiro-lang, shiro-crypto-cipher, shiro-ini, shiro-config-ogdl). Classes that stayed in
# org.apache.shiro.util.* (e.g., CollectionUtils, ThreadContext, AntPathMatcher) are excluded.
SHIRO_PACKAGE_MOVES: Dict[str, str] = {
    "org.apache.shiro.ShiroException": "org.apache.shiro.lang.ShiroException",
    "org.apache.shiro.codec.Base64": "org.apache.shiro.lang.codec.Base64",
    "org.apache.shiro.codec.CodecException": "org.apache.shiro.lang.codec.CodecException",
    "org.apache.shiro.codec.CodecSupport": "org.apache.shiro.lang.codec.CodecSupport",
    "org.apache.shiro.codec.H64": "org.apache.shiro.lang.codec.H64",
    "org.apache.shiro.codec.Hex": "org.apache.shiro.lang.codec.Hex",
    "org.apache.shiro.io.ClassResolvingObjectInputStream": "org.apache.shiro.lang.io.ClassResolvingObjectInputStream",
    "org.apache.shiro.io.DefaultSerializer": "org.apache.shiro.lang.io.DefaultSerializer",
    "org.apache.shiro.io.ResourceUtils": "org.apache.shiro.lang.io.ResourceUtils",
    "org.apache.shiro.io.SerializationException": "org.apache.shiro.lang.io.SerializationException",
    "org.apache.shiro.io.Serializer": "org.apache.shiro.lang.io.Serializer",
    "org.apache.shiro.io.XmlSerializer": "org.apache.shiro.lang.io.XmlSerializer",
    "org.apache.shiro.util.Assert": "org.apache.shiro.lang.util.Assert",
    "org.apache.shiro.util.ByteSource": "org.apache.shiro.lang.util.ByteSource",
    "org.apache.shiro.util.ClassUtils": "org.apache.shiro.lang.util.ClassUtils",
    "org.apache.shiro.util.Destroyable": "org.apache.shiro.lang.util.Destroyable",
    "org.apache.shiro.util.Factory": "org.apache.shiro.lang.util.Factory",
    "org.apache.shiro.util.Initializable": "org.apache.shiro.lang.util.Initializable",
    "org.apache.shiro.util.InstantiationException": "org.apache.shiro.lang.util.InstantiationException",
    "org.apache.shiro.util.LifecycleUtils": "org.apache.shiro.lang.util.LifecycleUtils",
    "org.apache.shiro.util.Nameable": "org.apache.shiro.lang.util.Nameable",
    "org.apache.shiro.util.SimpleByteSource": "org.apache.shiro.lang.util.SimpleByteSource",
    "org.apache.shiro.util.SoftHashMap": "org.apache.shiro.lang.util.SoftHashMap",
    "org.apache.shiro.util.StringUtils": "org.apache.shiro.lang.util.StringUtils",
    "org.apache.shiro.util.UnknownClassException": "org.apache.shiro.lang.util.UnknownClassException",
    "org.apache.shiro.config.IniFactorySupport": "org.apache.shiro.ini.IniFactorySupport",
    "org.apache.shiro.config.IniSecurityManagerFactory": "org.apache.shiro.ini.IniSecurityManagerFactory",
    "org.apache.shiro.config.CommonsInterpolator": "org.apache.shiro.config.ogdl.CommonsInterpolator",
    "org.apache.shiro.config.DefaultInterpolator": "org.apache.shiro.config.ogdl.DefaultInterpolator",
    "org.apache.shiro.config.Interpolator": "org.apache.shiro.config.ogdl.Interpolator",
    "org.apache.shiro.config.ReflectionBuilder": "org.apache.shiro.config.ogdl.ReflectionBuilder",
    "org.apache.shiro.config.UnresolveableReferenceException": "org.apache.shiro.config.ogdl.UnresolveableReferenceException",
    "org.apache.shiro.config.event.BeanEvent": "org.apache.shiro.config.ogdl.event.BeanEvent",
    "org.apache.shiro.config.event.ConfiguredBeanEvent": "org.apache.shiro.config.ogdl.event.ConfiguredBeanEvent",
    "org.apache.shiro.config.event.DestroyedBeanEvent": "org.apache.shiro.config.ogdl.event.DestroyedBeanEvent",
    "org.apache.shiro.config.event.InitializedBeanEvent": "org.apache.shiro.config.ogdl.event.InitializedBeanEvent",
    "org.apache.shiro.config.event.InstantiatedBeanEvent": "org.apache.shiro.config.ogdl.event.InstantiatedBeanEvent",
    "org.apache.shiro.config.event.LoggingBeanEventListener": "org.apache.shiro.config.ogdl.event.LoggingBeanEventListener",
    "org.apache.shiro.crypto.AbstractSymmetricCipherService": "org.apache.shiro.crypto.cipher.AbstractSymmetricCipherService",
    "org.apache.shiro.crypto.AesCipherService": "org.apache.shiro.crypto.cipher.AesCipherService",
    "org.apache.shiro.crypto.BlowfishCipherService": "org.apache.shiro.crypto.cipher.BlowfishCipherService",
    "org.apache.shiro.crypto.CipherService": "org.apache.shiro.crypto.cipher.CipherService",
    "org.apache.shiro.crypto.DefaultBlockCipherService": "org.apache.shiro.crypto.cipher.DefaultBlockCipherService",
    "org.apache.shiro.crypto.JcaCipherService": "org.apache.shiro.crypto.cipher.JcaCipherService",
    "org.apache.shiro.crypto.OperationMode": "org.apache.shiro.crypto.cipher.OperationMode",
    "org.apache.shiro.crypto.PaddingScheme": "org.apache.shiro.crypto.cipher.PaddingScheme",
}

SHIRO_REMOVED_HASH_AND_MATCHER_IMPORTS: Tuple[str, ...] = (
    "org.apache.shiro.crypto.hash.Md2Hash",
    "org.apache.shiro.crypto.hash.Md5Hash",
    "org.apache.shiro.crypto.hash.Sha1Hash",
    "org.apache.shiro.authc.credential.Md2CredentialsMatcher",
    "org.apache.shiro.authc.credential.Md5CredentialsMatcher",
    "org.apache.shiro.authc.credential.Sha1CredentialsMatcher",
)

SHIRO_WILDCARD_AFFECTED_IMPORTS: Tuple[str, ...] = (
    "org.apache.shiro.*",
    "org.apache.shiro.util.*",
    "org.apache.shiro.codec.*",
    "org.apache.shiro.io.*",
    "org.apache.shiro.config.*",
    "org.apache.shiro.config.event.*",
    "org.apache.shiro.crypto.*",
)


def is_shiro_1_to_2_import(import_name: str) -> bool:
    """Return True if `import_name` is a Shiro 1.x import moved or removed in Shiro 2.0."""
    clean = import_name.strip()
    if clean in SHIRO_PACKAGE_MOVES or clean in SHIRO_REMOVED_HASH_AND_MATCHER_IMPORTS:
        return True
    if clean in SHIRO_WILDCARD_AFFECTED_IMPORTS:
        return True
    for old_fqcn in SHIRO_PACKAGE_MOVES:
        if clean.startswith(old_fqcn + "."):
            return True
    return False


_HIBERNATE_5_REMOVED_EXACT_IMPORTS = frozenset(
    {
        "org.hibernate.Criteria",
        "org.hibernate.SQLQuery",
        "org.hibernate.Query",
        "org.hibernate.criterion.*",
    }
)
_HIBERNATE_5_REMOVED_PREFIXES = (
    "org.hibernate.criterion.",
    "org.hibernate.internal.CriteriaImpl",
    "org.hibernate.transform.",
    "org.hibernate.metadata.",
)


def uses_hibernate_5_removed_imports(imports: List[str]) -> bool:
    """Return True if any import references a Hibernate 5 API removed in Hibernate 6."""
    return any(
        imp in _HIBERNATE_5_REMOVED_EXACT_IMPORTS
        or imp.startswith(_HIBERNATE_5_REMOVED_PREFIXES)
        for imp in imports
    )


# Spring Framework 6 removed deprecated message-less Assert methods (deprecated since 4.3.7).
SPRING_6_ASSERT_ONE_ARG_MESSAGES: Dict[str, str] = {
    "isTrue": "Expression must be true",
    "state": "Invariant state must be true",
    "isNull": "Object must be null",
    "notNull": "Argument must not be null",
    "hasLength": "String must have length; it must not be null or empty",
    "hasText": "String must have text; it must not be null, empty, or blank",
    "notEmpty": "Collection, map, or array must not be empty",
    "noNullElements": "Collection or array must not contain null elements",
}

SPRING_6_ASSERT_TWO_ARG_MESSAGES: Dict[str, str] = {
    "doesNotContain": "String must not contain the forbidden substring",
    "isInstanceOf": "Object must be an instance of the required type",
    "isAssignable": "Type must be assignable to supertype",
}


@dataclass
class MigrationRule:
    """One category of Spring Boot 2 -> 3 breaking change."""

    id: str
    description: str
    predicate: Callable[["JavaFileManifest"], bool]
    migration_instruction: str
    commit_summary: str
    deterministic_capable: bool = False


SPRING_BOOT_3_RULES: List[MigrationRule] = [
    MigrationRule(
        id="javax-to-jakarta",
        description="Jakarta EE javax.* namespace -> jakarta.* (excluding Java SE javax.*)",
        predicate=lambda m: bool(m.jakarta_migratable_imports),
        migration_instruction=(
            "Migrate ONLY Jakarta EE javax.* imports and references in this file to "
            "their jakarta.* equivalents (e.g. javax.persistence -> jakarta.persistence, "
            "javax.validation -> jakarta.validation, javax.servlet -> jakarta.servlet, "
            "javax.annotation -> jakarta.annotation, javax.transaction -> jakarta.transaction). "
            "NEVER modify Java SE javax.* imports such as javax.sql.*, javax.crypto.*, "
            "javax.net.*, javax.naming.*, or javax.xml.parsers.*. Do not change anything else."
        ),
        commit_summary="jakarta-ee javax.* -> jakarta.*",
        deterministic_capable=True,
    ),
    MigrationRule(
        id="thymeleaf-spring5-to-spring6",
        description="org.thymeleaf.spring5 -> org.thymeleaf.spring6",
        predicate=lambda m: any(i.startswith("org.thymeleaf.spring5") for i in m.imports),
        migration_instruction=(
            "Replace all org.thymeleaf.spring5.* imports with their "
            "org.thymeleaf.spring6.* equivalents. The class names are identical; "
            "only the package segment changes (spring5 -> spring6). "
            "Do not change anything else."
        ),
        commit_summary="thymeleaf spring5 -> spring6",
        deterministic_capable=True,
    ),
    MigrationRule(
        id="security-configurer-adapter-removal",
        description="WebSecurityConfigurerAdapter removal",
        predicate=lambda m: (
            any("WebSecurityConfigurerAdapter" in i for i in m.imports)
            or any("WebSecurityConfigurerAdapter" in ext for c in m.classes for ext in c.extends)
        ),
        migration_instruction=(
            "Spring Security 6.0 removed WebSecurityConfigurerAdapter. Refactor "
            "this class to use component-based security configuration:\n"
            "1. Ensure @Configuration and @EnableWebSecurity are present on the class, and remove 'extends WebSecurityConfigurerAdapter' and its import. Also replace deprecated @EnableGlobalMethodSecurity with @EnableMethodSecurity if present.\n"
            "2. Convert configure(HttpSecurity http) into a @Bean method returning SecurityFilterChain using Spring Security 6 lambda DSL (authorizeHttpRequests(auth -> ...), requestMatchers(...) instead of antMatchers(...), httpBasic(Customizer.withDefaults()), sessionManagement(sm -> ...), exceptionHandling(ex -> ...), csrf(csrf -> ...)).\n"
            "3. When injecting a PasswordEncoder field/bean, use the `org.springframework.security.crypto.password.PasswordEncoder` interface (not concrete `BCryptPasswordEncoder`) unless a concrete `BCryptPasswordEncoder` bean is explicitly defined, so Spring DI matches `@Bean PasswordEncoder` configurations cleanly.\n"
            "4. If configure(AuthenticationManagerBuilder) exists only to wire UserDetailsService and PasswordEncoder, Spring Security 6 auto-wires existing UserDetailsService and PasswordEncoder beans; remove unused @Autowired fields if no longer needed, or expose an AuthenticationManager @Bean via AuthenticationConfiguration (instead of authenticationManagerBean()).\n"
            "5. If configure(WebSecurity) exists, expose a @Bean WebSecurityCustomizer using `web.ignoring().requestMatchers(...)` (not `antMatchers`).\n"
            "6. Add any necessary new imports and preserve all security rules and proper multi-line formatting."
        ),
        commit_summary="remove WebSecurityConfigurerAdapter (Spring Security 6)",
        deterministic_capable=False,
    ),
    MigrationRule(
        id="springfox-to-springdoc",
        description="Springfox Swagger 2 / io.swagger.annotations -> SpringDoc OpenAPI 3",
        predicate=lambda m: any(
            i.startswith("springfox.") or i.startswith("io.swagger.annotations")
            for i in m.imports
        ),
        migration_instruction=(
            "Springfox (`springfox.documentation.*`) and Swagger 2 annotations (`io.swagger.annotations.*`) "
            "are incompatible with Spring Boot 3 and replaced by `springdoc-openapi-starter-webmvc-ui` (OpenAPI 3):\n"
            "- For configuration classes defining a Springfox `Docket` bean with `@EnableSwagger2`: remove `@EnableSwagger2` "
            "and `springfox.*` / `com.google.common.base.Predicates` imports, and convert the `Docket` bean to an "
            "`io.swagger.v3.oas.models.OpenAPI` `@Bean` (or `org.springdoc.core.models.GroupedOpenApi` `@Bean`), preserving title, "
            "description, version, license, contact, and Bearer/JWT `SecurityScheme` if present.\n"
            "- For controller/DTO classes importing `io.swagger.annotations.*` or `springfox.documentation.annotations.*`: migrate to `io.swagger.v3.oas.annotations.*` "
            "(`@Api(tags = \"...\")` -> `@Tag(name = \"...\")` — `io.swagger.v3.oas.annotations.tags.Tag` uses `name`, NEVER `tags` or `value`! — "
            "`@ApiOperation` -> `@Operation`, `@ApiParam` -> `@Parameter`, `@ApiModel` / `@ApiModelProperty` -> "
            "`io.swagger.v3.oas.annotations.media.Schema` without `position` since `@Schema` has no `position()` attribute, "
            "`@ApiResponses`/`@ApiResponse` -> `io.swagger.v3.oas.annotations.responses.ApiResponses`/`ApiResponse` with `responseCode` string, "
            "`@Authorization` -> `@SecurityRequirement`). "
            "For `@ApiIgnore`: on method PARAMETERS, replace with `@io.swagger.v3.oas.annotations.Parameter(hidden = true)` (or omit it — NEVER put `@Hidden` on a parameter because `@Hidden` does not target `PARAMETER` and causes `annotation type not applicable to this kind of declaration`!); on methods or classes, replace with `@io.swagger.v3.oas.annotations.Hidden`. "
            "Preserve all controller request mappings, parameters, and business logic.\n"
            "- For Springfox SPI filters/plugins (such as `WebMvcOpenApiTransformationFilter`, `OperationBuilderPlugin`, or "
            "`ParameterBuilderPlugin`): migrate to the corresponding SpringDoc customizer interface (such as "
            "`org.springdoc.core.customizers.OpenApiCustomizer` implementing `public void customise(OpenAPI openApi)` "
            "— note British spelling `customise` — or `OperationCustomizer`), removing `supports(DocumentationType)` and all `springfox.*` imports."
        ),
        commit_summary="Springfox Swagger 2 -> SpringDoc OpenAPI 3",
        deterministic_capable=False,
    ),
    MigrationRule(
        id="response-entity-exception-handler-statuscode",
        description="ResponseEntityExceptionHandler HttpStatus -> HttpStatusCode (Spring 6 / Boot 3)",
        predicate=lambda m: (
            (
                any("ResponseEntityExceptionHandler" in ext for c in m.classes for ext in c.extends)
                or any("ResponseEntityExceptionHandler" in i for i in m.imports)
            )
            and any(
                param == "HttpStatus" or param.endswith(".HttpStatus")
                for method in m.methods
                for param in method.parameters
            )
        ),
        migration_instruction=(
            "In Spring Framework 6 / Spring Boot 3, protected @Override methods in subclasses of "
            "`ResponseEntityExceptionHandler` (such as `handleMethodArgumentNotValid`, "
            "`handleHttpMessageNotReadable`, `handleExceptionInternal`, `handleMissingServletRequestParameter`, etc.) "
            "changed their status parameter type from `org.springframework.http.HttpStatus` to "
            "`org.springframework.http.HttpStatusCode`. Import `org.springframework.http.HttpStatusCode` and "
            "update the `HttpStatus` parameter type in those overridden handler method signatures to `HttpStatusCode` "
            "(leave `HttpStatus.BAD_REQUEST` / `HttpStatus.*` usages inside method bodies untouched)."
        ),
        commit_summary="ResponseEntityExceptionHandler HttpStatus -> HttpStatusCode",
        deterministic_capable=True,
    ),
    MigrationRule(
        id="mvc-configurer-adapter-removal",
        description="WebMvcConfigurerAdapter removal",
        predicate=lambda m: (
            any("WebMvcConfigurerAdapter" in i for i in m.imports)
            or any("WebMvcConfigurerAdapter" in ext for c in m.classes for ext in c.extends)
        ),
        migration_instruction=(
            "WebMvcConfigurerAdapter was removed in Spring 6. Change the class "
            "to implement WebMvcConfigurer directly instead of extending the "
            "adapter. Remove the WebMvcConfigurerAdapter import and keep everything else identical."
        ),
        commit_summary="WebMvcConfigurerAdapter -> implements WebMvcConfigurer",
        deterministic_capable=False,
    ),
    MigrationRule(
        id="pbkdf2-password-encoder-upgrade",
        description="Pbkdf2PasswordEncoder constructor upgrade (Spring Security 6)",
        predicate=lambda m: (
            m.has_deprecated_pbkdf2_constructor
            if m.has_deprecated_pbkdf2_constructor is not None
            else any("Pbkdf2PasswordEncoder" in i for i in m.imports)
        ),
        migration_instruction=(
            "In Spring Security 6, the no-argument constructor `new Pbkdf2PasswordEncoder()` "
            "(and legacy single-argument constructors) were removed. Replace `new Pbkdf2PasswordEncoder()` "
            "with `Pbkdf2PasswordEncoder.defaultsForSpringSecurity_v5_5()` so existing Spring Boot 2.x / "
            "Spring Security 5.x password hashes (8-byte salt, SHA-1, 80-character hex output) continue to "
            "verify and fit legacy VARCHAR(80) password columns. Do NOT use "
            "`defaultsForSpringSecurity_v5_8()`, which changes the salt length to 16 bytes (96-char hex "
            "output) and algorithm to SHA-256 and breaks existing database hashes. "
            "Do not change anything else in the class."
        ),
        commit_summary="Pbkdf2PasswordEncoder -> defaultsForSpringSecurity_v5_5 (Spring Security 6)",
        deterministic_capable=True,
    ),
    MigrationRule(
        id="local-server-port-package-move",
        description="LocalServerPort moved to org.springframework.boot.test.web.server in Spring Boot 3",
        predicate=lambda m: any(
            i == "org.springframework.boot.web.server.LocalServerPort" for i in m.imports
        ),
        migration_instruction=(
            "In Spring Boot 3, @LocalServerPort moved from "
            "org.springframework.boot.web.server.LocalServerPort to "
            "org.springframework.boot.test.web.server.LocalServerPort. "
            "Update the import statement and leave everything else untouched."
        ),
        commit_summary="LocalServerPort -> org.springframework.boot.test.web.server.LocalServerPort",
        deterministic_capable=True,
    ),
    MigrationRule(
        id="commons-multipart-resolver-removal",
        description="CommonsMultipartResolver removal (Spring Framework 6 / Boot 3)",
        predicate=lambda m: (
            any("CommonsMultipartResolver" in i for i in m.imports)
            or any("CommonsMultipartResolver" in ext for c in m.classes for ext in c.extends)
        ),
        migration_instruction=(
            "Spring Framework 6 / Spring Boot 3 removed Apache Commons FileUpload support "
            "(`org.springframework.web.multipart.commons.CommonsMultipartResolver`). "
            "Migrate to Jakarta Servlet standard multipart handling using "
            "`org.springframework.web.multipart.support.StandardServletMultipartResolver` "
            "(or `MultipartResolver` / `StandardMultipartHttpServletRequest` / `HttpServletRequest.getParts()`), "
            "preserving any custom `isMultipart(HttpServletRequest)` or upload-handling logic."
        ),
        commit_summary="CommonsMultipartResolver -> StandardServletMultipartResolver (Spring Boot 3)",
        deterministic_capable=False,
    ),
    MigrationRule(
        id="hibernate-5-to-6",
        description="Hibernate 5 removed Criteria/Criterion/SQLQuery/Query/ClassMetadata -> Hibernate 6 / JPA Criteria",
        predicate=lambda m: uses_hibernate_5_removed_imports(m.imports),
        migration_instruction=(
            "Hibernate 6 (Spring Boot 3) removed Hibernate 5 legacy Criteria/Query/Metadata APIs "
            "(`org.hibernate.Criteria`, `org.hibernate.criterion.*`, `org.hibernate.SQLQuery`, "
            "`org.hibernate.Query`, `org.hibernate.internal.CriteriaImpl`, "
            "`org.hibernate.transform.ResultTransformer`, and `org.hibernate.metadata.ClassMetadata`). "
            "Migrate ONLY these removed Hibernate 5 APIs in this file:\n"
            "1. Replace `org.hibernate.SQLQuery` / `session.createSQLQuery(...)` with `org.hibernate.query.NativeQuery` / `session.createNativeQuery(...)`, "
            "and `org.hibernate.Query` with `org.hibernate.query.Query`. Note that in Hibernate 6, 1-arg `session.createQuery(String)` returns `Query<Object>` rather than raw `Query` or `Query<X>`; pass `entityClass` when available (`session.createQuery(hql, clazz)`) or cast explicitly (`(Query<X>)`, `(List<X>) query.list()`, `(X) query.uniqueResult()`) with `@SuppressWarnings(\"unchecked\")` if a helper returns `<X> Query<X>`, `<X> List<X>`, or `<X> X`. "
            "NEVER call `query.setParameterList(values)` with a single argument (`setParameterList` always requires a parameter name or index first); to bind positional `Object... values` onto a Hibernate `Query`, iterate with a loop: `for (int i = 0; i < values.length; i++) { query.setParameter(i, values[i]); }` (or `i + 1` if queries use JPA `?1`/`?2` placeholders).\n"
            "2. Replace `org.hibernate.Criteria` / `org.hibernate.criterion.*` (`Restrictions`, `Order`, `Projections`, `Criterion`, `CriteriaSpecification`, `DetachedCriteria`, `CriteriaImpl`) "
            "with Jakarta Persistence Criteria API (`jakarta.persistence.criteria.CriteriaBuilder`, `CriteriaQuery`, `Predicate`, `Root`, `Order`):\n"
            "   ```java\n"
            "   CriteriaBuilder cb = session.getCriteriaBuilder();\n"
            "   CriteriaQuery<Entity> cq = cb.createQuery(Entity.class);\n"
            "   Root<Entity> root = cq.from(Entity.class);\n"
            "   cq.select(root).where(...);\n"
            "   ```\n"
            "   NEVER call `cb.root(...)` — neither `CriteriaBuilder` nor `HibernateCriteriaBuilder` has a `.root()` method; `Root<T>` MUST come from `cq.from(...)`. Always add the required `jakarta.persistence.criteria.*` imports (`CriteriaBuilder`, `CriteriaQuery`, `Predicate`, `Root`, `Order`) and preserve all existing non-Hibernate imports (including enums/entities).\n"
            "3. NEVER invent methods on DAOs or helpers (such as `dao.getEntityManager()`, `dao.getCriteriaBuilder()`, `dao.getRoot()`) unless those exact method signatures appear in `Updated local project dependencies`. Use ONLY methods actually declared on the DAO interface / superinterfaces shown in dependency context (for example `getSession()` or HQL `find(hql, ...)`).\n"
            "4. NEVER pass `Predicate[]` / `Predicate...` to an HQL method taking `(String hql, Object...)` or `(Page, String hql, Object...)`, and NEVER pass varargs `(p1, p2, ...)` to a method taking `List<Predicate>` (use `Arrays.asList(...)` / `List.of(...)` and exact parameter types such as `PageRequest` vs `Page`). If a DAO has no compatible `Predicate` / `List<Predicate>` overload for a service method (or if a base service preserves a `Predicate...` signature while the DAO expects `List<Predicate>` or HQL), either convert via `Arrays.asList(predicates)` to the matching DAO method or execute the JPA `CriteriaQuery` directly using `getSession().getCriteriaBuilder()`.\n"
            "5. Replace `sessionFactory.getClassMetadata(entityClass).getIdentifierPropertyName()` with Jakarta Metamodel lookup via `sessionFactory.getMetamodel().entity(entityClass)` "
            "(e.g. finding the singular attribute where `attr.isId()` is true or using `entityType.getId(entityType.getIdType().getJavaType()).getName()`).\n"
            "6. Replace `CriteriaSpecification.DISTINCT_ROOT_ENTITY` / `ResultTransformer` with `cq.distinct(true)` / `SELECT DISTINCT` or `setTupleTransformer(...)`. NEVER call `query.setDistinct(...)` on `org.hibernate.query.Query` (no such method exists in Hibernate 6 — use `SELECT DISTINCT`, `cq.distinct(true)`, or `LinkedHashSet` / `.stream().distinct()` in Java).\n"
            "7. If this class/interface declares or overrides shared DAO/service methods, match raw vs generic parameter and return types strictly to the superinterface/superclass in dependency context (for example, keep raw `Query distinct(Query query)` if the interface declares raw `Query`, so erasure matches without name clashes)."
        ),
        commit_summary="Hibernate 5 Criteria/Query -> Hibernate 6 / JPA Criteria",
        deterministic_capable=False,
    ),
    MigrationRule(
        id="shiro-1-to-2",
        description="Apache Shiro 1.x -> 2.0 package relocations (shiro-lang, crypto-cipher, ini, ogdl)",
        predicate=lambda m: any(is_shiro_1_to_2_import(i) for i in m.imports),
        migration_instruction=(
            "Apache Shiro 2.0 (required for Jakarta EE / Spring Boot 3) reorganized utility, "
            "codec, I/O, config, and cipher classes into new packages:\n"
            "1. Moved to `org.apache.shiro.lang.util.*`: `StringUtils`, `ByteSource`, "
            "`SimpleByteSource`, `Assert`, `ClassUtils`, `Destroyable`, `Factory`, "
            "`Initializable`, `InstantiationException`, `LifecycleUtils`, `Nameable`, "
            "`SoftHashMap`, `UnknownClassException`. (Classes like `CollectionUtils`, "
            "`ThreadContext`, `AntPathMatcher`, and `PatternMatcher` remain in `org.apache.shiro.util.*`.)\n"
            "2. Moved to `org.apache.shiro.lang.codec.*`: `Base64`, `Hex`, `H64`, "
            "`CodecSupport`, `CodecException`.\n"
            "3. Moved to `org.apache.shiro.lang.io.*`: `ResourceUtils`, `Serializer`, "
            "`DefaultSerializer`, `XmlSerializer`, `SerializationException`, `ClassResolvingObjectInputStream`.\n"
            "4. `org.apache.shiro.ShiroException` -> `org.apache.shiro.lang.ShiroException`.\n"
            "5. Symmetric ciphers (`AesCipherService`, `BlowfishCipherService`, `CipherService`, "
            "`OperationMode`, `PaddingScheme`) moved from `org.apache.shiro.crypto.*` to "
            "`org.apache.shiro.crypto.cipher.*`; INI factories moved to `org.apache.shiro.ini.*`; "
            "OGDL/ReflectionBuilder/BeanEvent classes moved to `org.apache.shiro.config.ogdl.*`.\n"
            "6. Removed weak hash classes (`Md2Hash`, `Md5Hash`, `Sha1Hash`, `Md5CredentialsMatcher`, "
            "`Sha1CredentialsMatcher`) -> replace with `SimpleHash` or `Sha256Hash` / `HashedCredentialsMatcher`."
        ),
        commit_summary="Apache Shiro 1.x -> 2.0 package migration",
        deterministic_capable=True,
    ),
    MigrationRule(
        id="spring-6-assert",
        description="Spring Framework 6 removed message-less org.springframework.util.Assert methods",
        predicate=lambda m: bool(m.has_deprecated_spring_assert),
        migration_instruction=(
            "Spring Framework 6 removed the deprecated message-less methods on "
            "`org.springframework.util.Assert` (1-argument `notNull`, `hasText`, `hasLength`, "
            "`isTrue`, `state`, `isNull`, `notEmpty`, `noNullElements`, and 2-argument "
            "`doesNotContain`, `isInstanceOf`, `isAssignable`). Supply an explicit `String` "
            "message as the final argument."
        ),
        commit_summary="Spring 6 Assert message argument",
        deterministic_capable=True,
    ),
    MigrationRule(
        id="guava-transitive-removal",
        description="Replace transitive com.google.common.* Guava usages with Java 17 standard APIs",
        predicate=lambda m: False,
        migration_instruction=(
            "This file imports `com.google.common.*` (Guava), which was previously pulled in transitively "
            "by a removed Spring Boot 2 dependency (such as Springfox) and is no longer on the classpath. "
            "Replace all `com.google.common.*` imports and usages with standard JDK 17 equivalents "
            "(e.g. `Lists.newArrayList(...)` -> `new ArrayList<>(...)`, `Maps.newHashMap()` -> `new HashMap<>()`, "
            "`Sets.newHashSet(...)` -> `new HashSet<>(...)`, `ImmutableList.of(...)` -> `List.of(...)`, "
            "`ImmutableMap.of(...)` -> `Map.of(...)`, `Preconditions.checkNotNull(...)` -> `Objects.requireNonNull(...)`, "
            "`CaseFormat` -> standard camelCase/snake_case string logic). Preserve all public method signatures and behavior."
        ),
        commit_summary="replace transitive Guava imports with JDK 17 standard APIs",
        deterministic_capable=False,
    ),
]