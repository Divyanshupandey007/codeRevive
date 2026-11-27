# Real-World Migration Evaluation

This document records end-to-end Spring Boot 2.x → 3.x migration results produced by CodeRevive against **11 public GitHub Maven repositories** spanning Spring Boot `2.1.6` through `2.7.18` (Java 8, 11, and 17). Every metric below is extracted directly from CodeRevive's exported `MigrationRunSummary` JSON audit reports.

## Methodology

| Parameter | Configuration |
| --- | --- |
| **Target Versions** | Spring Boot `3.2.5`, Java `17` |
| **Pipeline Mode** | `hybrid` (OpenRewrite Pass 1 → Hardened Docker Maven/Chrome Sandbox → Diagnostic-driven LLM repair) |
| **Verification Gate** | Full Maven compile & test pass inside `coderevive-maven-chrome:17` + pre/post integrity guardrails |
| **Commit Policy** | All-or-nothing: any unresolved compilation failure, test regression, or guardrail violation triggers a full rollback to `baseline_commit` |
| **Evaluation Window** | November 26–27, 2026 |

## Headline Results

- **10 / 11 repositories (90.9%) migrated end-to-end** with passing sandbox builds and zero integrity violations (`COMPLETED`).
- **1 / 11 repositories (9.1%) safely rolled back** to its baseline commit with full diagnostics and zero partial commits (`FAILED`).
- **261 files migrated across the 10 completed repositories**:
  - **202 files (77.4%)** transformed purely deterministically by OpenRewrite (`openrewrite`).
  - **24 files (9.2%)** transformed by OpenRewrite and refined by diagnostic-driven AI repair (`openrewrite+llm`).
  - **35 files (13.4%)** authored by AI for complex framework rewrites (`llm`).
- **5 of 10 passing repositories (50%) required zero LLM retries**, completing purely via deterministic OpenRewrite transforms.
- **Median completion time (passing runs):** `1m 41s` (`100.9s`), ranging from `59s` (`otp-redis`) to `29m 10s` (`DWSurvey`, 176 files).

## Per-Repository Results

| # | Repository | Source → Target | Key Stack / Frameworks | Migrated Files (`OR` / `OR+AI` / `AI`) | Retries | Duration | Baseline → Final | Outcome |
| ---: | --- | --- | --- | --- | ---: | ---: | --- | --- |
| 1 | [`wkeyuan/DWSurvey`](https://github.com/wkeyuan/DWSurvey) | Boot `2.7.18` (Java 8) → `3.2.5` (Java 17) | Hibernate 5→6, Apache Shiro, SpringFox→SpringDoc, Druid, Redis, Elasticsearch | **176** (`127` / `16` / `33`) | 25 | 29m 10s | `6a7b10e` → `a45e8f1` | **COMPLETED** |
| 2 | [`Vinceli-9527/snap-meal`](https://github.com/Vinceli-9527/snap-meal) | Boot `2.7.18` (Java 8) → `3.2.5` (Java 17) | JDBC, H2/MySQL, Redis, Bucket4j, SpringDoc OpenAPI, Apache POI | **19** (`18` / `1` / `0`) | 1 | 1m 56s | `739c672` → `0278716` | **COMPLETED** |
| 3 | [`melardev/JavaSpringBootOAuth2AsymmetricJwt_Separate_Crud`](https://github.com/melardev/JavaSpringBootOAuth2AsymmetricJwt_Separate_Crud) | Boot `2.1.6` (Java 8) → `3.2.5` (Java 17) | OAuth2 Resource Server, Asymmetric JWT, Spring Data JPA, H2, Actuator | **16** (`16` / `0` / `0`) | 0 | 1m 19s | `a1e8a39` → `9f05512` | **COMPLETED** |
| 4 | [`kazuki43zoo/mybatis-spring-boot-jpetstore`](https://github.com/kazuki43zoo/mybatis-spring-boot-jpetstore) | Boot `2.7.0` (Java 8) → `3.2.5` (Java 17) | MyBatis 3, Spring Security 6, Thymeleaf 3.1, Flyway, HSQLDB, Chrome/Selenide E2E | **14** (`12` / `1` / `1`) | 2 | 6m 22s | `20d013a` → `ff92c50` | **COMPLETED** |
| 5 | [`datevid/3.spring-boot-2.5.4-java8-securityJWT-jpa`](https://github.com/datevid/3.spring-boot-2.5.4-java8-securityJWT-jpa) | Boot `2.5.4` (Java 8) → `3.2.5` (Java 17) | Spring Security JWT, SpringFox→SpringDoc, JPA, ModelMapper, H2/MySQL | **11** (`5` / `5` / `1`) | 7 | 3m 57s | `9661d8d` → `9eef4f1` | **COMPLETED** |
| 6 | [`douglasdjf/security-basic-auth`](https://github.com/douglasdjf/security-basic-auth) | Boot `2.5.0` (Java 11) → `3.2.5` (Java 17) | Spring Security Basic Auth, Flyway, JPA, Bean Validation, H2 | **10** (`9` / `1` / `0`) | 1 | 1m 41s | `788083a` → `edb99b6` | **COMPLETED** |
| 7 | [`alura-cursos/rotalog-api-frotas`](https://github.com/alura-cursos/rotalog-api-frotas) | Boot `2.7.14` (Java 11) → `3.2.5` (Java 17) | Spring Data JPA, PostgreSQL, Flyway, Bean Validation, Lombok | **5** (`5` / `0` / `0`) | 0 | 1m 17s | `dbb659c` → `2d656d8` | **COMPLETED** |
| 8 | [`ayus66/otp-redis`](https://github.com/ayus66/otp-redis) | Boot `2.7.18` (Java 8) → `3.2.5` (Java 17) | Spring Data Redis (`spring.redis.*` → `spring.data.redis.*`), Twilio SDK | **4** (`4` / `0` / `0`) | 0 | 59s | `d73cda8` → `5749eec` | **COMPLETED** |
| 9 | [`AnderssonProgramming/spring-boot-postgres-codelab`](https://github.com/AnderssonProgramming/spring-boot-postgres-codelab) | Boot `2.7.18` (Java 17) → `3.2.5` (Java 17) | Spring Data JPA, PostgreSQL | **3** (`3` / `0` / `0`) | 0 | 1m 18s | `3b7c3da` → `f1c2135` | **COMPLETED** |
| 10 | [`sinsangkug/SpringBoot_Jpa_Shopping`](https://github.com/sinsangkug/SpringBoot_Jpa_Shopping) | Boot `2.7.11` (Java 11) → `3.2.5` (Java 17) | Spring Data JPA, QueryDSL, Thymeleaf, H2/MySQL | **3** (`3` / `0` / `0`) | 0 | 1m 41s | `1e65788` → `3741fbb` | **COMPLETED** |
| 11 | [`geekidea/spring-boot-plus`](https://github.com/geekidea/spring-boot-plus) | Boot `2.7.18` (Java 8) → `3.2.5` (Java 17) | MyBatis-Plus, custom JSqlParser interceptor, SpringDoc, Redis, AOP | **0** *(80 touched, reverted)* | 4 | 18m 16s | `23b0271` → `23b0271` | **FAILED (Rolled Back)** |

*Column key: `OR` = OpenRewrite only, `OR+AI` = OpenRewrite pre-pass followed by LLM repair, `AI` = LLM-only transformation.*

## Notable Passing Runs

- **`wkeyuan/DWSurvey` (176 files, 29m 10s)**: Largest repository in the evaluation set. OpenRewrite deterministically migrated 127 files (Jakarta EE namespaces, `pom.xml` alignment, standard Servlet/JPA updates), while diagnostic-driven LLM repair handled 49 files spanning Hibernate 5 → 6 Criteria/Query rewrites, Apache Shiro Jakarta compatibility, `CommonsMultipartResolver` → `StandardServletMultipartResolver`, and SpringFox → SpringDoc migration—converging cleanly with `integrity_passed: true`.
- **`kazuki43zoo/mybatis-spring-boot-jpetstore` (14 files, 6m 22s)**: Upgraded `mybatis-spring-boot-starter` (`2.2.2` → `3.0.3`), refactored `WebSecurityConfig.java` (`openrewrite+llm`), updated Thymeleaf 3.1 syntax in `templates/layout.html` (`llm`), and passed the full headless Chrome/Selenide browser E2E test suite inside the sandbox.
- **`melardev/JavaSpringBootOAuth2AsymmetricJwt_Separate_Crud` (16 files, 1m 19s)**: Oldest baseline in the set (Spring Boot `2.1.6.RELEASE`, Java 8). Migrated 16 files across both `oauth-server` and `resource-server` source trees with **0 retries** purely through deterministic OpenRewrite transforms.
- **`Vinceli-9527/snap-meal` (19 files, 1m 56s)**: OpenRewrite handled 18 Java files deterministically; a single AI repair pass updated `pom.xml` to SpringDoc OpenAPI 3 (`2.5.0`), while 4 existing integration/unit test files remained untouched (`SKIPPED`) and passed verification as-is.
- **`datevid/3.spring-boot-2.5.4-java8-securityJWT-jpa` (11 files, 3m 57s)**: Replaced `WebSecurityConfigurerAdapter` with `SecurityFilterChain` (`WebSecurityConfig.java`), migrated SpringFox Swagger 2 (`SwaggerConfig.java`) to SpringDoc OpenAPI 3, and updated Jakarta validation annotations across controllers and DTOs.

## Rollback Analysis & Known Sandbox Boundaries

### 1. Why `geekidea/spring-boot-plus` Rolled Back

CodeRevive intentionally aborted the commit on `geekidea/spring-boot-plus` and reverted all 80 workspace edits back to `23b0271` rather than producing a broken branch. The JSON audit report captured two root causes:

1. **Breaking JSqlParser 4.x API change in custom SQL interceptor**: `MerchantLineInnerInterceptor.java` subclasses a MyBatis-Plus SQL interceptor referencing `net.sf.jsqlparser.statement.select.SelectBody` and `WithItem` methods whose return types changed to `Select` in JSqlParser 4.x (bundled with MyBatis-Plus 3.5.x).
2. **SpringDoc annotation mismatch & degenerate LLM output guardrail**: `GeneratorUtil.java` failed to resolve `org.springdoc.core.annotations.ParameterObject` during SpringDoc upgrade, and the LLM repair attempt triggered CodeRevive's repetition-loop detector (`Repeated token loop detected ('oo')`). The post-execution integrity guardrail rejected the incomplete state (`"One or more target files failed transformation or were rejected by guardrails; aborting commit to prevent false-success migration."`).

### 2. Sandbox Isolation Boundary (Testcontainers / Docker-in-Docker)

Because the verification sandbox enforces `--cap-drop=ALL`, `no-new-privileges`, and does not mount the host Docker socket inside the container, repositories whose integration tests rely on **Testcontainers** to spawn sibling containers from inside `mvn test` (for example, [`IlyaIvlev/balance-service`](https://github.com/IlyaIvlev/balance-service)) cannot boot their `ApplicationContext` inside the isolated sandbox unless external services are supplied via CodeRevive's dynamic sidecars or a non-Testcontainers test profile. CodeRevive detects the `ApplicationContext` startup failure, reports the exact test exception in the Audit Report, and cleanly rolls back the branch.
