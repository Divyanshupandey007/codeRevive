from pathlib import Path

from git import Repo

from app.ingestion.migration_planner import build_migration_plan
from app.ingestion.openrewrite_runner import (
    DeterministicRewritePass,
    OpenRewriteRunner,
)
from app.ingestion.pom_parser import PomParser


def test_deterministic_java_transform_preserves_java_se_javax():
    source = """package com.example;

import javax.persistence.Entity;
import javax.validation.constraints.NotNull;
import javax.sql.DataSource;
import javax.crypto.Cipher;
import org.thymeleaf.spring5.SpringTemplateEngine;
import org.springframework.boot.web.server.LocalServerPort;

@Entity
public class Demo {
    private DataSource ds;
    private Cipher cipher;
}
"""
    transformed = DeterministicRewritePass.transform_java_source(source)

    assert "import jakarta.persistence.Entity;" in transformed
    assert "import jakarta.validation.constraints.NotNull;" in transformed
    assert "import org.thymeleaf.spring6.SpringTemplateEngine;" in transformed
    assert "import org.springframework.boot.test.web.server.LocalServerPort;" in transformed
    # Java SE APIs MUST stay javax.*
    assert "import javax.sql.DataSource;" in transformed
    assert "import javax.crypto.Cipher;" in transformed


def test_openrewrite_runner_builds_maven_args_and_uses_injected_executor(tmp_path: Path):
    pom_path = tmp_path / "pom.xml"
    pom_path.write_text(
        """<project>
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>2.7.14</version>
    </parent>
    <groupId>com.example</groupId>
    <artifactId>demo</artifactId>
    <version>1.0</version>
</project>""",
        encoding="utf-8",
    )
    repo = Repo.init(tmp_path)
    with repo.config_writer() as cfg:
        cfg.set_value("user", "name", "Test")
        cfg.set_value("user", "email", "test@local")
    repo.git.add(A=True)
    repo.index.commit("init")

    captured = {}

    class FakeRewriteExecutor:
        def run_maven_rewrite(self, repo_path: Path, maven_args: list[str]) -> tuple[int, str]:
            captured["args"] = maven_args
            (repo_path / "pom.xml").write_text(
                pom_path.read_text(encoding="utf-8").replace("2.7.14", "3.2.5"),
                encoding="utf-8",
            )
            return 0, "[INFO] OpenRewrite recipe completed"

    plan = build_migration_plan(PomParser(pom_path).parse(), target_spring_boot="3.2.5")
    runner = OpenRewriteRunner(executor=FakeRewriteExecutor())
    result = runner.run(tmp_path, plan)

    assert result.executed is True
    assert result.mode == "maven_plugin"
    assert "pom.xml" in result.modified_files
    assert any("rewrite-maven-plugin" in a for a in captured["args"])
    assert any("UpgradeSpringBoot_3_2" in a for a in captured["args"])


def test_openrewrite_runner_falls_back_to_deterministic_pass_when_executor_fails(tmp_path: Path):
    pom_path = tmp_path / "pom.xml"
    pom_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>2.7.10</version>
    </parent>
    <properties>
        <java.version>11</java.version>
        <thymeleaf.version>3.0.14.RELEASE</thymeleaf.version>
    </properties>
</project>""",
        encoding="utf-8",
    )

    class FailingExecutor:
        def run_maven_rewrite(self, repo_path: Path, maven_args: list[str]):
            return 1, "[ERROR] plugin failed"

    plan = build_migration_plan(PomParser(pom_path).parse(), target_spring_boot="3.2.5")
    runner = OpenRewriteRunner(executor=FailingExecutor(), allow_deterministic_fallback=True)
    result = runner.run(tmp_path, plan)

    assert result.executed is True
    assert result.mode == "deterministic_fallback"
    assert "pom.xml" in result.modified_files
    updated_pom = pom_path.read_text(encoding="utf-8")
    assert "<version>3.2.5</version>" in updated_pom
    assert "<java.version>17</java.version>" in updated_pom
    assert "thymeleaf.version" not in updated_pom


def test_deterministic_java_transform_upgrades_pbkdf2_to_v5_5():
    source = """package com.example;

import org.springframework.context.annotation.Bean;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.crypto.password.Pbkdf2PasswordEncoder;

public class ApplicationConfig {
    @Bean
    PasswordEncoder passwordEncoder() {
        return new Pbkdf2PasswordEncoder();
    }
}
"""
    transformed = DeterministicRewritePass.transform_java_source(source)
    assert "Pbkdf2PasswordEncoder.defaultsForSpringSecurity_v5_5()" in transformed
    assert "new Pbkdf2PasswordEncoder()" not in transformed
    assert "defaultsForSpringSecurity_v5_8" not in transformed


def test_openrewrite_runner_deterministic_mode_skips_maven_executor(tmp_path: Path):
    pom_path = tmp_path / "pom.xml"
    pom_path.write_text(
        """<project>
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>2.7.0</version>
    </parent>
</project>""",
        encoding="utf-8",
    )

    class ExplodingExecutor:
        def run_maven_rewrite(self, repo_path: Path, maven_args: list[str]):
            raise AssertionError("Executor must not be called in deterministic mode")

    plan = build_migration_plan(PomParser(pom_path).parse(), target_spring_boot="3.2.5")
    runner = OpenRewriteRunner(executor=ExplodingExecutor(), mode="deterministic")
    result = runner.run(tmp_path, plan)

    assert result.executed is True
    assert result.mode == "deterministic_fallback"
    assert "<version>3.2.5</version>" in pom_path.read_text(encoding="utf-8")


def test_openrewrite_runner_handles_exit_124_timeout_gracefully(tmp_path: Path):
    pom_path = tmp_path / "pom.xml"
    pom_path.write_text(
        """<project>
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>2.7.0</version>
    </parent>
</project>""",
        encoding="utf-8",
    )

    class TimeoutExecutor:
        openrewrite_timeout_seconds = 300

        def run_maven_rewrite(self, repo_path: Path, maven_args: list[str]):
            return 124, ""

    plan = build_migration_plan(PomParser(pom_path).parse(), target_spring_boot="3.2.5")
    runner = OpenRewriteRunner(executor=TimeoutExecutor(), mode="hybrid")
    result = runner.run(tmp_path, plan)

    assert result.executed is True
    assert result.mode == "deterministic_fallback"
    assert "<version>3.2.5</version>" in pom_path.read_text(encoding="utf-8")


def test_openrewrite_runner_normalizes_maven_v5_8_back_to_v5_5(tmp_path: Path):
    pom_path = tmp_path / "pom.xml"
    pom_path.write_text(
        """<project>
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>2.7.0</version>
    </parent>
</project>""",
        encoding="utf-8",
    )
    app_cfg = tmp_path / "src" / "main" / "java" / "com" / "ex" / "ApplicationConfig.java"
    app_cfg.parent.mkdir(parents=True)
    app_cfg.write_text(
        """package com.ex;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.crypto.password.Pbkdf2PasswordEncoder;

public class ApplicationConfig {
    PasswordEncoder passwordEncoder() {
        return new Pbkdf2PasswordEncoder();
    }
}
""",
        encoding="utf-8",
    )
    repo = Repo.init(tmp_path)
    with repo.config_writer() as cfg:
        cfg.set_value("user", "name", "Test")
        cfg.set_value("user", "email", "test@local")
    repo.git.add(A=True)
    repo.index.commit("init")

    class OpenRewriteV58Executor:
        def run_maven_rewrite(self, repo_path: Path, maven_args: list[str]):
            cfg_file = repo_path / "src/main/java/com/ex/ApplicationConfig.java"
            cfg_file.write_text(
                cfg_file.read_text(encoding="utf-8").replace(
                    "new Pbkdf2PasswordEncoder()",
                    "Pbkdf2PasswordEncoder.defaultsForSpringSecurity_v5_8()",
                ),
                encoding="utf-8",
            )
            return 0, "[INFO] OpenRewrite recipe completed"

    plan = build_migration_plan(PomParser(pom_path).parse(), target_spring_boot="3.2.5")
    runner = OpenRewriteRunner(executor=OpenRewriteV58Executor(), mode="hybrid")
    result = runner.run(tmp_path, plan)

    assert result.executed is True
    assert result.mode == "maven_plugin"
    updated = app_cfg.read_text(encoding="utf-8")
    assert "Pbkdf2PasswordEncoder.defaultsForSpringSecurity_v5_5()" in updated
    assert "defaultsForSpringSecurity_v5_8" not in updated


def test_deterministic_java_transform_upgrades_exception_handler_http_status_code():
    source = """package com.ex.exception;

import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ControllerAdvice;
import org.springframework.web.context.request.WebRequest;
import org.springframework.web.servlet.mvc.method.annotation.ResponseEntityExceptionHandler;

@ControllerAdvice
public class GlobalExceptionHandler extends ResponseEntityExceptionHandler {
    @Override
    protected ResponseEntity<Object> handleMethodArgumentNotValid(
            MethodArgumentNotValidException ex,
            HttpHeaders headers,
            HttpStatus status,
            WebRequest request) {
        return new ResponseEntity<>(ex.getMessage(), HttpStatus.BAD_REQUEST);
    }

    public ResponseEntity<String> helper(HttpStatus status) {
        return ResponseEntity.status(status).body("err");
    }
}
"""
    transformed = DeterministicRewritePass.transform_java_source(source)

    assert "import org.springframework.http.HttpStatus;" in transformed
    assert "import org.springframework.http.HttpStatusCode;" in transformed
    assert "HttpStatusCode status," in transformed
    assert "return new ResponseEntity<>(ex.getMessage(), HttpStatus.BAD_REQUEST);" in transformed
    assert "public ResponseEntity<String> helper(HttpStatus status)" in transformed


def test_deterministic_pom_transform_upgrades_mysql_springfox_and_lombok(tmp_path: Path):
    pom_path = tmp_path / "pom.xml"
    pom_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>2.5.4</version>
    </parent>
    <properties>
        <java.version>1.8</java.version>
    </properties>
    <dependencies>
        <dependency>
            <groupId>mysql</groupId>
            <artifactId>mysql-connector-java</artifactId>
        </dependency>
        <dependency>
            <groupId>org.projectlombok</groupId>
            <artifactId>lombok</artifactId>
            <version>1.18.20</version>
            <scope>provided</scope>
        </dependency>
        <dependency>
            <groupId>io.springfox</groupId>
            <artifactId>springfox-swagger2</artifactId>
            <version>2.9.2</version>
        </dependency>
        <dependency>
            <groupId>io.springfox</groupId>
            <artifactId>springfox-swagger-ui</artifactId>
            <version>2.9.2</version>
        </dependency>
    </dependencies>
</project>""",
        encoding="utf-8",
    )

    plan = build_migration_plan(PomParser(pom_path).parse(), target_spring_boot="3.2.5")
    result = OpenRewriteRunner(mode="deterministic").run(tmp_path, plan)

    assert result.executed is True
    updated = pom_path.read_text(encoding="utf-8")
    assert "<version>3.2.5</version>" in updated
    assert "<java.version>17</java.version>" in updated
    assert "mysql-connector-java" not in updated
    assert "<groupId>com.mysql</groupId>" in updated
    assert "<artifactId>mysql-connector-j</artifactId>" in updated
    assert "<version>1.18.32</version>" in updated
    assert updated.count("springdoc-openapi-starter-webmvc-ui") == 1
    assert "springfox-swagger-ui" not in updated


def test_deterministic_pom_transform_upgrades_servlet_hibernate6_and_logback(tmp_path: Path):
    pom_path = tmp_path / "pom.xml"
    pom_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>2.7.18</version>
    </parent>
    <properties>
        <java.version>1.8</java.version>
    </properties>
    <dependencies>
        <dependency>
            <groupId>javax.servlet</groupId>
            <artifactId>javax.servlet-api</artifactId>
        </dependency>
        <dependency>
            <groupId>javax.servlet</groupId>
            <artifactId>jstl</artifactId>
        </dependency>
        <dependency>
            <groupId>javax.servlet.jsp</groupId>
            <artifactId>jsp-api</artifactId>
            <version>2.1</version>
            <scope>provided</scope>
        </dependency>
        <dependency>
            <groupId>org.hibernate</groupId>
            <artifactId>hibernate-entitymanager</artifactId>
        </dependency>
        <dependency>
            <groupId>org.hibernate</groupId>
            <artifactId>hibernate-core</artifactId>
        </dependency>
        <dependency>
            <groupId>ch.qos.logback</groupId>
            <artifactId>logback-classic</artifactId>
            <version>1.2.3</version>
        </dependency>
    </dependencies>
</project>""",
        encoding="utf-8",
    )

    plan = build_migration_plan(PomParser(pom_path).parse(), target_spring_boot="3.2.5")
    result = OpenRewriteRunner(mode="deterministic").run(tmp_path, plan)

    assert result.executed is True
    updated = pom_path.read_text(encoding="utf-8")
    assert "javax.servlet-api" not in updated
    assert "<artifactId>jakarta.servlet-api</artifactId>" in updated
    assert "<artifactId>jakarta.servlet.jsp.jstl-api</artifactId>" in updated
    assert "<artifactId>jakarta.servlet.jsp-api</artifactId>" in updated
    assert "<version>3.1.1</version>" in updated
    assert "hibernate-entitymanager" not in updated
    assert "<groupId>org.hibernate.orm</groupId>" in updated
    assert "<version>1.2.3</version>" not in updated


def test_deterministic_java_transform_rewrites_hibernate_sql_query_to_native_query():
    source = """package com.ex.dao;

import org.hibernate.SQLQuery;
import org.hibernate.Session;

public class QuestionDaoImpl {
    public int countRows(Session session) {
        SQLQuery query = session.createSQLQuery("SELECT count(*) FROM t_question");
        return ((Number) query.uniqueResult()).intValue();
    }
}
"""
    transformed = DeterministicRewritePass.transform_java_source(source)

    assert "import org.hibernate.query.NativeQuery;" in transformed
    assert "import org.hibernate.SQLQuery;" not in transformed
    assert "NativeQuery query = session.createNativeQuery(" in transformed
    assert "createSQLQuery" not in transformed


def test_deterministic_pom_transform_upgrades_shiro_all_to_jakarta_modules(tmp_path: Path):
    pom_path = tmp_path / "pom.xml"
    pom_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>2.7.18</version>
    </parent>
    <properties>
        <java.version>1.8</java.version>
    </properties>
    <dependencies>
        <dependency>
            <groupId>org.apache.shiro</groupId>
            <artifactId>shiro-all</artifactId>
            <version>1.10.0</version>
        </dependency>
        <dependency>
            <groupId>org.tuckey</groupId>
            <artifactId>urlrewritefilter</artifactId>
            <version>4.0.3</version>
        </dependency>
        <dependency>
            <groupId>org.sitemesh</groupId>
            <artifactId>sitemesh</artifactId>
            <version>3.0.1</version>
        </dependency>
        <dependency>
            <groupId>com.alibaba</groupId>
            <artifactId>druid-spring-boot-starter</artifactId>
            <version>1.2.16</version>
        </dependency>
        <dependency>
            <groupId>com.fasterxml.jackson.core</groupId>
            <artifactId>jackson-databind</artifactId>
            <version>2.13.4.1</version>
        </dependency>
    </dependencies>
</project>""",
        encoding="utf-8",
    )

    plan = build_migration_plan(PomParser(pom_path).parse(), target_spring_boot="3.2.5")
    result = OpenRewriteRunner(mode="deterministic").run(tmp_path, plan)

    assert result.executed is True
    parsed_after = PomParser(pom_path).parse()
    coords = {(d.group_id, d.artifact_id): d for d in parsed_after.dependencies}
    assert ("org.apache.shiro", "shiro-all") not in coords
    assert coords[("org.apache.shiro", "shiro-core")].version == "2.0.2"
    assert coords[("org.apache.shiro", "shiro-web")].version == "2.0.2"
    assert coords[("org.apache.shiro", "shiro-spring")].version == "2.0.2"
    assert coords[("org.tuckey", "urlrewritefilter")].version == "5.1.3"
    assert coords[("org.sitemesh", "sitemesh")].version == "3.2.1"
    assert coords[("com.alibaba", "druid-spring-boot-3-starter")].version == "1.2.23"
    assert coords[("com.fasterxml.jackson.core", "jackson-databind")].version is None

    updated_xml = pom_path.read_text(encoding="utf-8")
    assert updated_xml.count("<classifier>jakarta</classifier>") == 3
    assert updated_xml.count("<artifactId>*</artifactId>") == 2


def test_deterministic_java_transform_rewrites_shiro_2_moved_classes_and_preserves_core_util():
    source = """package com.ex.security;

import javax.servlet.Filter;
import org.apache.shiro.ShiroException;
import org.apache.shiro.codec.Base64;
import org.apache.shiro.codec.Hex;
import org.apache.shiro.config.IniSecurityManagerFactory;
import org.apache.shiro.crypto.AesCipherService;
import org.apache.shiro.io.ResourceUtils;
import org.apache.shiro.util.ByteSource;
import org.apache.shiro.util.CollectionUtils;
import org.apache.shiro.util.SimpleByteSource;
import org.apache.shiro.util.StringUtils;
import org.apache.shiro.util.ThreadContext;

public class CustomAuthFilter {
    public boolean check(String val) throws ShiroException {
        org.apache.shiro.util.ByteSource src = org.apache.shiro.util.ByteSource.Util.bytes(val);
        return StringUtils.hasText(val) && !CollectionUtils.isEmpty(ThreadContext.getResources());
    }
}
"""
    transformed = DeterministicRewritePass.transform_java_source(source)

    assert "import jakarta.servlet.Filter;" in transformed
    assert "import org.apache.shiro.lang.ShiroException;" in transformed
    assert "import org.apache.shiro.lang.codec.Base64;" in transformed
    assert "import org.apache.shiro.lang.codec.Hex;" in transformed
    assert "import org.apache.shiro.ini.IniSecurityManagerFactory;" in transformed
    assert "import org.apache.shiro.crypto.cipher.AesCipherService;" in transformed
    assert "import org.apache.shiro.lang.io.ResourceUtils;" in transformed
    assert "import org.apache.shiro.lang.util.ByteSource;" in transformed
    assert "import org.apache.shiro.lang.util.SimpleByteSource;" in transformed
    assert "import org.apache.shiro.lang.util.StringUtils;" in transformed
    assert "org.apache.shiro.lang.util.ByteSource src = org.apache.shiro.lang.util.ByteSource.Util.bytes(val);" in transformed
    # Classes that remained in org.apache.shiro.util in Shiro 2.0.2 MUST be preserved!
    assert "import org.apache.shiro.util.CollectionUtils;" in transformed
    assert "import org.apache.shiro.util.ThreadContext;" in transformed


def test_deterministic_java_transform_handles_shiro_wildcard_imports():
    source = """package com.ex.security;

import org.apache.shiro.util.*;

public class WildcardFilter {
    public boolean has(String v) {
        return StringUtils.hasText(v) && ThreadContext.getResources() != null;
    }
}
"""
    transformed = DeterministicRewritePass.transform_java_source(source)
    assert "import org.apache.shiro.util.*;" in transformed
    assert "import org.apache.shiro.lang.util.StringUtils;" in transformed

    # Should NOT insert an ambiguous import if Spring's StringUtils is explicitly imported
    spring_imported = """package com.ex.security;

import org.apache.shiro.util.*;
import org.springframework.util.StringUtils;

public class SpringUtilFilter {
    public boolean has(String v) {
        return StringUtils.hasText(v) && ThreadContext.getResources() != null;
    }
}
"""
    transformed_spring = DeterministicRewritePass.transform_java_source(spring_imported)
    assert "org.apache.shiro.lang.util.StringUtils" not in transformed_spring


def test_deterministic_java_transform_rewrites_inherited_create_sql_query_without_session_import():
    source = """package com.ex.dao.impl;

import java.util.List;

public class SubDaoImpl extends BaseDaoImpl {
    public int countBySurvey(String id) {
        Object val = getSession().createSQLQuery("SELECT count(*) FROM t_item WHERE id = ?1")
            .setParameter(1, id)
            .uniqueResult();
        return ((Number) val).intValue();
    }
}
"""
    transformed = DeterministicRewritePass.transform_java_source(source)
    assert "getSession().createNativeQuery(\"SELECT count(*) FROM t_item WHERE id = ?1\")" in transformed
    assert "createSQLQuery" not in transformed


def test_deterministic_java_transform_rewrites_spring_6_assert_and_preserves_other_asserts():
    source = """package com.ex.util;

import java.util.Map;
import org.springframework.util.Assert;

public class IPv4Locator {
    public void validate(Map<String, String> map, String key, Object item) {
        // Assert.notNull(item);
        Assert.notNull(item);
        Assert.hasText(map.get(key));
        Assert.notNull(
            map.get("a,b)" /* inner */)
        );
        Assert.isInstanceOf(String.class, key);
        Assert.notNull(key, "key must be set");
        org.springframework.util.Assert.isTrue(item != null);
    }
}
"""
    transformed = DeterministicRewritePass.transform_java_source(source)
    assert 'Assert.notNull(item, "Argument must not be null");' in transformed
    assert 'Assert.hasText(map.get(key), "String must have text; it must not be null, empty, or blank");' in transformed
    assert 'map.get("a,b)" /* inner */), "Argument must not be null"' in transformed
    assert 'Assert.isInstanceOf(String.class, key, "Object must be an instance of the required type");' in transformed
    assert 'Assert.notNull(key, "key must be set");' in transformed
    assert 'org.springframework.util.Assert.isTrue(item != null, "Expression must be true");' in transformed
    # Comment untouched
    assert "// Assert.notNull(item);" in transformed

    shiro_source = """package com.ex.util;

import org.apache.shiro.util.Assert;

public class ShiroAssertCaller {
    public void check(String val) {
        Assert.hasText(val);
    }
}
"""
    shiro_transformed = DeterministicRewritePass.transform_java_source(shiro_source)
    assert "import org.apache.shiro.lang.util.Assert;" in shiro_transformed
    assert "Assert.hasText(val);" in shiro_transformed


def test_openrewrite_build_maven_args_and_failure_log_summarization(tmp_path: Path):
    runner = OpenRewriteRunner()
    args = runner.build_maven_args(["org.openrewrite.java.spring.boot3.UpgradeSpringBoot_3_2"])
    coords_arg = next(a for a in args if a.startswith("-Drewrite.recipeArtifactCoordinates="))
    assert "rewrite-spring:" in coords_arg
    assert "rewrite-migrate-java" not in coords_arg
    assert "-Drewrite.pomCacheEnabled=false" in args
    assert "-Drewrite.exportDatatables=false" in args

    sample_logs = (
        "[ERROR] Failed to execute goal org.openrewrite.maven:rewrite-maven-plugin:5.43.1:run "
        "(default-cli) on project demo: Execution default-cli failed\n"
        "Caused by: java.lang.IllegalStateException: Missing Java 17 toolchain\n"
        "[ERROR] -> [Help 1]\n"
        "[ERROR] For more information about the errors and possible solutions, please read the following articles:\n"
    )
    summary = OpenRewriteRunner._summarize_failure_logs(sample_logs)
    assert "Missing Java 17 toolchain" in summary
    assert "[Help 1]" not in summary
    assert "For more information" not in summary

    pom_path = tmp_path / "pom.xml"
    pom_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>2.7.14</version>
    </parent>
</project>""",
        encoding="utf-8",
    )

    class PluginErrorExecutor:
        def run_maven_rewrite(self, repo_path: Path, maven_args: list[str]):
            return 1, sample_logs

    plan = build_migration_plan(PomParser(pom_path).parse(), target_spring_boot="3.2.5")
    res = OpenRewriteRunner(executor=PluginErrorExecutor(), allow_deterministic_fallback=True).run(
        tmp_path, plan
    )
    assert res.executed is True
    assert res.mode == "deterministic_fallback"
    assert res.error is not None
    assert "Missing Java 17 toolchain" in res.error


def test_deterministic_java_transform_normalizes_openapi_tag_annotations_and_preserves_junit_tag():
    swagger_ctrl = """package com.ex.web;

import io.swagger.v3.oas.annotations.tags.Tag;
import org.springframework.web.bind.annotation.RestController;

@Tag(tags = "Survey API")
@RestController
public class SurveyController {
    @Tag(tags = {"Design", "Export"}, description = "Design ops")
    public void op1() {}

    @Tag(value = "Legacy")
    public void op2() {}

    @Tag("Bare")
    public void op3() {}
}
"""
    transformed = DeterministicRewritePass.transform_java_source(swagger_ctrl)
    assert '@Tag(name = "Survey API")' in transformed
    assert '@Tag(name = "Design, Export", description = "Design ops")' in transformed
    assert '@Tag(name = "Legacy")' in transformed
    assert '@Tag(name = "Bare")' in transformed
    assert "tags =" not in transformed

    junit_test = """package com.ex.web;

import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;

@Tag("fast")
public class FastTest {
    @Test
    void runs() {}
}
"""
    assert DeterministicRewritePass.transform_java_source(junit_test) == junit_test


def test_openrewrite_runner_preserves_httpclient4_when_httpmime_or_es_rest_client_present(
    tmp_path: Path,
):
    pom_path = tmp_path / "pom.xml"
    pom_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>2.7.18</version>
    </parent>
    <dependencies>
        <dependency>
            <groupId>org.apache.httpcomponents</groupId>
            <artifactId>httpclient</artifactId>
        </dependency>
        <dependency>
            <groupId>org.apache.httpcomponents</groupId>
            <artifactId>httpclient-cache</artifactId>
        </dependency>
        <dependency>
            <groupId>org.apache.httpcomponents</groupId>
            <artifactId>httpmime</artifactId>
        </dependency>
        <dependency>
            <groupId>org.elasticsearch.client</groupId>
            <artifactId>elasticsearch-rest-high-level-client</artifactId>
            <version>7.17.10</version>
        </dependency>
    </dependencies>
</project>""",
        encoding="utf-8",
    )
    es_cfg = tmp_path / "src/main/java/com/ex/config/ESClientConfig.java"
    es_cfg.parent.mkdir(parents=True)
    es_cfg.write_text(
        """package com.ex.config;

import javax.annotation.PostConstruct;
import org.apache.http.HttpHost;
import org.apache.http.client.CredentialsProvider;
import org.apache.http.impl.client.BasicCredentialsProvider;

public class ESClientConfig {
    @PostConstruct
    public HttpHost host() {
        CredentialsProvider provider = new BasicCredentialsProvider();
        return new HttpHost("localhost", 9200, "http");
    }
}
""",
        encoding="utf-8",
    )

    repo = Repo.init(tmp_path)
    with repo.config_writer() as cfg:
        cfg.set_value("user", "name", "Test")
        cfg.set_value("user", "email", "test@local")
    repo.git.add(A=True)
    repo.index.commit("init")

    class BrokenHttpClient5Executor:
        def run_maven_rewrite(self, repo_path: Path, maven_args: list[str]):
            # Simulate OpenRewrite UpgradeApacheHttpClient_5 breaking pom.xml and ESClientConfig
            (repo_path / "pom.xml").write_text(
                pom_path.read_text(encoding="utf-8")
                .replace("2.7.18", "3.2.5")
                .replace("<groupId>org.apache.httpcomponents</groupId>\n            <artifactId>httpclient</artifactId>", "<groupId>org.apache.httpcomponents.client5</groupId>\n            <artifactId>httpclient5</artifactId>"),
                encoding="utf-8",
            )
            es_cfg.write_text(
                """package com.ex.config;

import jakarta.annotation.PostConstruct;
import org.apache.hc.core5.http.HttpHost;
import org.apache.hc.client5.http.auth.CredentialsProvider;
import org.apache.hc.client5.http.impl.auth.BasicCredentialsProvider;

public class ESClientConfig {
    @PostConstruct
    public HttpHost host() {
        CredentialsProvider provider = new BasicCredentialsProvider();
        return new HttpHost("http", "localhost", 9200);
    }
}
""",
                encoding="utf-8",
            )
            return 0, "[INFO] OpenRewrite recipe completed"

    plan = build_migration_plan(PomParser(pom_path).parse(), target_spring_boot="3.2.5")
    result = OpenRewriteRunner(executor=BrokenHttpClient5Executor(), mode="hybrid").run(
        tmp_path, plan
    )

    assert result.executed is True
    assert result.mode == "maven_plugin"

    updated_pom = pom_path.read_text(encoding="utf-8")
    assert "org.apache.httpcomponents.client5" not in updated_pom
    parsed_after = PomParser(pom_path).parse()
    coords = {(d.group_id, d.artifact_id): d for d in parsed_after.dependencies}
    assert coords[("org.apache.httpcomponents", "httpclient")].version == "4.5.14"
    assert coords[("org.apache.httpcomponents", "httpclient-cache")].version == "4.5.14"
    assert coords[("org.apache.httpcomponents", "httpmime")].version == "4.5.14"

    updated_java = es_cfg.read_text(encoding="utf-8")
    assert "import jakarta.annotation.PostConstruct;" in updated_java
    assert "import org.apache.http.HttpHost;" in updated_java
    assert "org.apache.hc." not in updated_java



