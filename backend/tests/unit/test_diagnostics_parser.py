from app.sandbox.diagnostics_parser import (
    classify_build_failure,
    parse_maven_output,
)


def test_parses_maven_compiler_errors_and_normalizes_paths():
    logs = """
[INFO] Compiling 5 source files
[ERROR] /app/src/main/java/com/example/User.java:[12,8] cannot find symbol
  symbol:   class Entity
  location: package javax.persistence
[ERROR] /app/src/main/java/com/example/App.java:[4,1] package javax.validation does not exist
"""
    errors, tests = parse_maven_output(logs, container_workdir="/app")
    assert len(errors) == 2
    assert tests == []
    assert errors[0].file_path == "src/main/java/com/example/User.java"
    assert errors[0].line_number == 12
    assert "cannot find symbol" in errors[0].message
    assert classify_build_failure(logs, errors, tests) == "code"


def test_parses_surefire_test_failures():
    logs = """
[ERROR] Tests run: 2, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: 0.412 s <<< FAILURE! - in com.example.UserControllerTest
[ERROR] com.example.UserControllerTest.returnsUserById  Time elapsed: 0.015 s  <<< FAILURE!
org.opentest4j.AssertionFailedError: expected: <Alice> but was: <Bob>
\tat com.example.UserControllerTest.returnsUserById(UserControllerTest.java:14)
"""
    errors, tests = parse_maven_output(logs)
    assert errors == []
    assert len(tests) == 1
    assert tests[0].test_class == "com.example.UserControllerTest"
    assert tests[0].test_method == "returnsUserById"
    assert "expected: <Alice>" in tests[0].failure_message
    assert classify_build_failure(logs, errors, tests) == "code"


def test_classifies_missing_database_as_environment_failure():
    logs = """
[ERROR] Tests run: 1, Failures: 0, Errors: 1, Time elapsed: 1.2 s <<< ERROR! - in com.example.RepoTest
[ERROR] com.example.RepoTest.savesUser  Time elapsed: 1.1 s  <<< ERROR!
org.postgresql.util.PSQLException: Connection to localhost:5432 refused.
\tat org.postgresql.core.v3.ConnectionFactoryImpl.openConnectionImpl(ConnectionFactoryImpl.java:315)
"""
    errors, tests = parse_maven_output(logs)
    assert classify_build_failure(logs, errors, tests) == "environment"


def test_fallback_attributes_dependency_resolution_errors_to_pom():
    logs = "[ERROR] Failed to execute goal on project app: Could not resolve dependencies for project com.ex:app:1.0: Could not transfer artifact"
    errors, tests = parse_maven_output(logs)
    assert len(errors) == 1
    assert errors[0].file_path == "pom.xml"
    assert classify_build_failure(logs, errors, tests) == "environment"


def test_parses_surefire_3_x_test_failures():
    logs = """
[ERROR] Tests run: 6, Failures: 0, Errors: 1, Skipped: 0, Time elapsed: 5.123 s -- in com.kazuki43zoo.jpetstore.JpetstoreApplicationTests
[ERROR] com.kazuki43zoo.jpetstore.JpetstoreApplicationTests.testNewAccount -- Time elapsed: 2.345 s <<< ERROR!
java.lang.IllegalStateException: Failed to load ApplicationContext
\tat org.springframework.test.context.cache.DefaultCacheAwareContextLoaderDelegate.loadContext(DefaultCacheAwareContextLoaderDelegate.java:180)
"""
    errors, tests = parse_maven_output(logs)
    assert errors == []
    assert len(tests) == 1
    assert tests[0].test_class == "com.kazuki43zoo.jpetstore.JpetstoreApplicationTests"
    assert tests[0].test_method == "testNewAccount"
    assert "Failed to load ApplicationContext" in tests[0].failure_message
    assert classify_build_failure(logs, errors, tests) == "code"


def test_attributes_maven_plugin_goal_failure_to_pom():
    logs = "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.11.0:compile (default-compile) on project demo: Fatal error compiling: invalid target release: 17"
    errors, tests = parse_maven_output(logs)
    assert len(errors) == 1
    assert errors[0].file_path == "pom.xml"
    assert classify_build_failure(logs, errors, tests) == "code"


def test_attributes_pom_missing_dependency_version_to_pom():
    logs = """
[ERROR] Some problems were encountered while processing the POMs:
[ERROR] 'dependencies.dependency.version' for mysql:mysql-connector-java:jar is missing. @ line 55, column 21
[ERROR]   The project net.demo:jwt-auth-service:1.0.0 (/app/pom.xml) has 1 error
[ERROR]     'dependencies.dependency.version' for mysql:mysql-connector-java:jar is missing. @ line 55, column 21
"""
    errors, tests = parse_maven_output(logs)
    assert len(errors) >= 2
    assert any("mysql:mysql-connector-java" in e.message and e.file_path == "pom.xml" for e in errors)
    assert classify_build_failure(logs, errors, tests) == "code"


def test_classifies_sandbox_timeout_and_oom_as_environment_failure():
    truncated_compile_log = (
        "[INFO] --- compiler:3.11.0:compile (default-compile) @ app ---\n"
        "[INFO] Changes detected - recompiling the module! :source"
    )
    assert classify_build_failure(truncated_compile_log, [], [], exit_code=124) == "environment"
    assert classify_build_failure(truncated_compile_log, [], [], exit_code=137) == "environment"
    assert (
        classify_build_failure(
            "[ERROR] Sandbox command timed out after 600s (exit code 124).",
            [],
            [],
        )
        == "environment"
    )
    assert (
        classify_build_failure(
            "[ERROR] java.lang.OutOfMemoryError: Java heap space",
            [],
            [],
        )
        == "environment"
    )
