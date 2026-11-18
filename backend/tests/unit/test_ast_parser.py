from pathlib import Path

from app.ingestion.ast_parser import JavaAstParser


def test_ast_parser_captures_full_method_signatures(tmp_path: Path):
    java_dir = tmp_path / "src" / "main" / "java" / "com" / "example"
    java_dir.mkdir(parents=True)
    (java_dir / "UserService.java").write_text(
        """package com.example;

import javax.persistence.Entity;
import javax.sql.DataSource;
import javax.crypto.Cipher;

public class UserService {
    public String getUser(Long id, boolean active) {
        return "ok";
    }

    private void helper() {}
}
""",
        encoding="utf-8",
    )

    manifests = JavaAstParser(str(tmp_path)).parse_repo()
    assert len(manifests) == 1
    m = manifests[0]

    assert m.package_name == "com.example"
    # All javax imports captured, but only Jakarta EE ones marked migratable
    assert set(m.javax_imports) == {
        "javax.persistence.Entity",
        "javax.sql.DataSource",
        "javax.crypto.Cipher",
    }
    assert m.jakarta_migratable_imports == ["javax.persistence.Entity"]

    get_user = next(meth for meth in m.methods if meth.name == "getUser")
    assert get_user.is_public is True
    assert get_user.return_type == "String"
    assert get_user.parameters == ["Long", "boolean"]
    assert get_user.signature_key(m.file_path) == (
        "src/main/java/com/example/UserService.java#String getUser(Long,boolean)"
    )


def test_ast_parser_detects_test_annotations_and_empty_bodies(tmp_path: Path):
    test_dir = tmp_path / "src" / "test" / "java" / "com" / "example"
    test_dir.mkdir(parents=True)
    (test_dir / "SampleTest.java").write_text(
        """package com.example;

import org.junit.jupiter.api.Test;

public class SampleTest {
    @Test
    void nonEmptyTest() {
        int x = 1 + 1;
    }

    @Test
    void emptyTest() {
    }
}
""",
        encoding="utf-8",
    )

    manifests = JavaAstParser(str(tmp_path)).parse_repo()
    assert manifests[0].test_count == 2
    by_name = {meth.name: meth for meth in manifests[0].methods}
    assert by_name["nonEmptyTest"].has_assertions_or_body is True
    assert by_name["emptyTest"].has_assertions_or_body is False


def test_ast_parser_detects_deprecated_pbkdf2_constructor(tmp_path: Path):
    java_dir = tmp_path / "src" / "main" / "java" / "com" / "example"
    java_dir.mkdir(parents=True)
    legacy = java_dir / "LegacyAppConfig.java"
    legacy.write_text(
        """package com.example;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.crypto.password.Pbkdf2PasswordEncoder;

public class LegacyAppConfig {
    public PasswordEncoder passwordEncoder() {
        return new Pbkdf2PasswordEncoder();
    }
}
""",
        encoding="utf-8",
    )
    migrated = java_dir / "MigratedAppConfig.java"
    migrated.write_text(
        """package com.example;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.crypto.password.Pbkdf2PasswordEncoder;

public class MigratedAppConfig {
    public PasswordEncoder passwordEncoder() {
        return Pbkdf2PasswordEncoder.defaultsForSpringSecurity_v5_5();
    }
}
""",
        encoding="utf-8",
    )

    by_path = {m.file_path: m for m in JavaAstParser(str(tmp_path)).parse_repo()}
    assert by_path["src/main/java/com/example/LegacyAppConfig.java"].has_deprecated_pbkdf2_constructor is True
    assert by_path["src/main/java/com/example/MigratedAppConfig.java"].has_deprecated_pbkdf2_constructor is False


def test_ast_parser_detects_deprecated_spring_assert_only_for_spring_assert(tmp_path: Path):
    java_dir = tmp_path / "src" / "main" / "java" / "com" / "example"
    java_dir.mkdir(parents=True)
    (java_dir / "LegacyAssert.java").write_text(
        """package com.example;
import org.springframework.util.Assert;
public class LegacyAssert {
    void check(String s) {
        Assert.hasText(s);
    }
}
""",
        encoding="utf-8",
    )
    (java_dir / "ModernAssert.java").write_text(
        """package com.example;
import org.springframework.util.Assert;
public class ModernAssert {
    void check(String s) {
        Assert.notNull(s, "s must not be null");
        Assert.hasText(s, "s must have text");
    }
}
""",
        encoding="utf-8",
    )
    (java_dir / "ShiroAssert.java").write_text(
        """package com.example;
import org.apache.shiro.lang.util.Assert;
public class ShiroAssert {
    void check(String s) {
        Assert.hasText(s);
    }
}
""",
        encoding="utf-8",
    )

    by_path = {m.file_path: m for m in JavaAstParser(str(tmp_path)).parse_repo()}
    assert by_path["src/main/java/com/example/LegacyAssert.java"].has_deprecated_spring_assert is True
    assert by_path["src/main/java/com/example/ModernAssert.java"].has_deprecated_spring_assert is False
    assert by_path["src/main/java/com/example/ShiroAssert.java"].has_deprecated_spring_assert is False
