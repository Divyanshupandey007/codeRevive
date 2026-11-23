from http import HTTPStatus
import http.client
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.ingestion.git_manager import GitWorkspaceManager, _safe_repo_name
from app.ingestion.openrewrite_runner import DeterministicRewritePass
from app.llm.base import LLMProviderError
from app.llm.gemini_provider import GeminiProvider
from app.llm.nvidia_nim_provider import NvidiaNimProvider
from app.main import _CodeReviveHTTPServer, _StdlibApiHandler
from app.models.agent import FilePatchResult
from app.models.execution import CompilerError
from app.models.manifest import JavaClassManifest, JavaFileManifest
from app.orchestrator.dependency_context import extract_error_line_local_paths
from app.sandbox.docker_runner import _DockerCLIExecutor, _DockerSDKExecutor
from app.sandbox.service_detector import ServiceRequirement
from app.sandbox.service_manager import DockerServiceManager, ServiceProvisioningError


def test_nvidia_nim_provider_strips_fences_and_wraps_sdk_errors(monkeypatch):
    fake_client = MagicMock()
    fake_openai_mod = SimpleNamespace(OpenAI=MagicMock(return_value=fake_client))
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_openai_mod)

    provider = NvidiaNimProvider(api_key="test-key", max_retries=1)

    # Fenced JSON response should parse cleanly
    fenced_payload = (
        '```json\n{"file_path": "A.java", "new_content": "class A {}", "explanation": "ok"}\n```'
    )
    fake_client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=fenced_payload))]
    )
    result = provider.generate_structured("prompt", FilePatchResult)
    assert result.file_path == "A.java"
    assert result.new_content == "class A {}"

    # Empty choices raises LLMProviderError
    fake_client.chat.completions.create.return_value = SimpleNamespace(choices=[])
    with pytest.raises(LLMProviderError, match="no completion choices"):
        provider.generate_text("hello")

    # Non-retryable SDK error wraps in LLMProviderError
    fake_client.chat.completions.create.side_effect = RuntimeError("401 unauthorized")
    with pytest.raises(LLMProviderError, match="NVIDIA NIM API call failed"):
        provider.generate_structured("prompt", FilePatchResult)


def test_gemini_provider_wraps_sdk_errors_into_llm_provider_error(monkeypatch):
    fake_client = MagicMock()
    fake_genai_mod = SimpleNamespace(
        Client=MagicMock(return_value=fake_client),
        types=SimpleNamespace(GenerateContentConfig=MagicMock(side_effect=lambda **kw: kw)),
    )
    fake_google_mod = SimpleNamespace(genai=fake_genai_mod)
    monkeypatch.setitem(__import__("sys").modules, "google", fake_google_mod)
    monkeypatch.setitem(__import__("sys").modules, "google.genai", fake_genai_mod)

    provider = GeminiProvider(api_key="test-key", max_retries=1)
    fake_client.models.generate_content.side_effect = RuntimeError("invalid argument 400")

    with pytest.raises(LLMProviderError, match="Gemini API call failed"):
        provider.generate_structured("prompt", FilePatchResult)


def test_git_manager_safe_repo_name_rejects_traversal(tmp_path: Path):
    manager = GitWorkspaceManager(workspace_root=tmp_path)
    assert _safe_repo_name("https://github.com/org/my-app.git") == "my-app"
    with pytest.raises(ValueError):
        _safe_repo_name("https://example.com/..")
    with pytest.raises(ValueError):
        manager.prepare_workspace("")


def test_http_server_validates_headers_json_and_payload(monkeypatch):
    server = _CodeReviveHTTPServer(("127.0.0.1", 0), _StdlibApiHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)

        conn.request("GET", "/api/health")
        resp = conn.getresponse()
        resp.read()
        assert resp.status == HTTPStatus.OK

        # Malformed JSON -> 400
        conn.request(
            "POST",
            "/api/migrations",
            body="{not-json",
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        resp.read()
        assert resp.status == HTTPStatus.BAD_REQUEST

        # Non-object JSON -> 400
        conn.request(
            "POST",
            "/api/migrations",
            body="[]",
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        resp.read()
        assert resp.status == HTTPStatus.BAD_REQUEST

        # Oversized Content-Length -> 413
        conn.request(
            "POST",
            "/api/migrations",
            body="{}",
            headers={"Content-Length": str(2 * 1024 * 1024)},
        )
        resp = conn.getresponse()
        resp.read()
        assert resp.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE

        # Invalid field type -> 422
        conn.request(
            "POST",
            "/api/migrations",
            body='{"retry_budget": "not-an-int"}',
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        resp.read()
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_service_manager_wait_ready_cli_fails_fast_on_exited_container(monkeypatch):
    mgr = DockerServiceManager(ready_timeout_seconds=30)
    svc = ServiceRequirement(kind="postgres", image="postgres:16-alpine")

    calls = []

    def fake_run(cmd, capture_output=True, text=True):
        calls.append(cmd)
        if cmd[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=0, stdout="exited\n", stderr="")
        if cmd[:2] == ["docker", "logs"]:
            return SimpleNamespace(returncode=0, stdout="fatal startup error", stderr="")
        raise AssertionError(f"Unexpected command after container exited: {cmd}")

    monkeypatch.setattr("app.sandbox.service_manager.subprocess.run", fake_run)

    with pytest.raises(ServiceProvisioningError, match="exited before becoming ready"):
        mgr._wait_ready_cli(svc, "coderevive-postgres-test")

    assert any(c[:2] == ["docker", "inspect"] for c in calls)
    assert not any(c[:2] == ["docker", "exec"] for c in calls)


def test_service_manager_start_via_cli_wraps_missing_docker_binary(monkeypatch):
    mgr = DockerServiceManager(ready_timeout_seconds=5)
    svc = ServiceRequirement(kind="redis", image="redis:7-alpine")

    def missing_docker(*args, **kwargs):
        raise FileNotFoundError("docker not found")

    monkeypatch.setattr("app.sandbox.service_manager.subprocess.run", missing_docker)

    with pytest.raises(ServiceProvisioningError, match="Docker CLI"):
        with mgr._start_via_cli([svc], max_lifetime_seconds=60):
            pass


def test_docker_executors_enforce_cap_drop_all(tmp_path: Path, monkeypatch):
    captured_cli_cmd = []

    def fake_cli_run(cmd, capture_output=True, text=True, timeout=None):
        captured_cli_cmd.extend(cmd)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr("app.sandbox.docker_runner.subprocess.run", fake_cli_run)
    cli_exec = _DockerCLIExecutor("coderevive-maven-chrome:17")
    cli_exec.execute(tmp_path, ["test"], 30, "2g", 1.0, True, "vol", "/app")
    assert "--cap-drop=ALL" in captured_cli_cmd
    assert "--cap-add=DAC_OVERRIDE" in captured_cli_cmd
    assert "--security-opt=no-new-privileges:true" in captured_cli_cmd

    captured_sdk_kwargs = {}
    fake_container = MagicMock()
    fake_container.wait.return_value = {"StatusCode": 0}
    fake_container.logs.return_value = b""
    fake_client = MagicMock()
    fake_client.containers.run.side_effect = lambda image, **kw: (
        captured_sdk_kwargs.update(kw) or fake_container
    )
    fake_docker_mod = SimpleNamespace(
        from_env=MagicMock(return_value=fake_client),
        errors=SimpleNamespace(APIError=Exception, ContainerError=Exception, ImageNotFound=Exception),
    )
    monkeypatch.setitem(__import__("sys").modules, "docker", fake_docker_mod)
    monkeypatch.setitem(__import__("sys").modules, "docker.errors", fake_docker_mod.errors)

    sdk_exec = _DockerSDKExecutor("coderevive-maven-chrome:17")
    sdk_exec.execute(tmp_path, ["test"], 30, "2g", 1.0, True, "vol", "/app")
    assert captured_sdk_kwargs["cap_drop"] == ["ALL"]
    assert captured_sdk_kwargs["cap_add"] == ["DAC_OVERRIDE"]
    assert captured_sdk_kwargs["security_opt"] == ["no-new-privileges:true"]


def test_deterministic_exception_handler_inserts_http_status_code_in_default_package():
    src = (
        "import org.springframework.http.HttpHeaders;\n"
        "import org.springframework.http.HttpStatus;\n"
        "import org.springframework.http.ResponseEntity;\n"
        "import org.springframework.web.servlet.mvc.method.annotation.ResponseEntityExceptionHandler;\n\n"
        "public class GlobalHandler extends ResponseEntityExceptionHandler {\n"
        "    @Override\n"
        "    protected ResponseEntity<Object> handleExceptionInternal(\n"
        "            Exception ex, Object body, HttpHeaders headers, HttpStatus status, Object req) {\n"
        "        return null;\n"
        "    }\n"
        "}\n"
    )
    # Without HttpStatus import line, fallback should still insert HttpStatusCode at top
    src_fqcn_import_less = src.replace(
        "import org.springframework.http.HttpStatus;\n", ""
    )
    transformed = DeterministicRewritePass.transform_java_source(src_fqcn_import_less)
    assert "import org.springframework.http.HttpStatusCode;" in transformed
    assert "HttpStatusCode status" in transformed


def test_extract_error_line_local_paths_resolves_receivers_in_single_pass():
    source = (
        "package com.example.service;\n"
        "import com.example.dao.UserDao;\n"
        "import com.example.model.User;\n"
        "public class UserService {\n"
        "    private UserDao userDao;\n"
        "    public User load(Long id) {\n"
        "        return userDao.findOne(id);\n"
        "    }\n"
        "}\n"
    )
    manifest = JavaFileManifest(
        file_path="src/main/java/com/example/service/UserService.java",
        package_name="com.example.service",
        imports=["com.example.dao.UserDao", "com.example.model.User"],
        classes=[JavaClassManifest(name="UserService")],
    )
    fqcn_to_file = {
        "com.example.dao.UserDao": "src/main/java/com/example/dao/UserDao.java",
        "com.example.model.User": "src/main/java/com/example/model/User.java",
    }
    errors = [
        CompilerError(
            file_path=manifest.file_path,
            line_number=7,
            column=23,
            message="cannot find symbol: method findOne(Long)",
        )
    ]
    paths = extract_error_line_local_paths(source, manifest, errors, fqcn_to_file)
    assert "src/main/java/com/example/dao/UserDao.java" in paths


def test_collect_dependency_context_prioritizes_dao_ancestors_over_entities_and_multiline_calls(
    tmp_path: Path,
):
    from app.ingestion.ast_parser import JavaAstParser
    from app.orchestrator.dependency_context import (
        build_local_dep_lookup,
        collect_dependency_context,
    )

    (tmp_path / "src/main/java/com/example/entity").mkdir(parents=True)
    (tmp_path / "src/main/java/com/example/dao").mkdir(parents=True)
    (tmp_path / "src/main/java/com/example/service").mkdir(parents=True)

    (tmp_path / "src/main/java/com/example/entity/BigEntity.java").write_text(
        "package com.example.entity;\n"
        "import jakarta.persistence.Entity;\n"
        "@Entity\n"
        "public class BigEntity {\n"
        "    private String id;\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "src/main/java/com/example/dao/ISimpleDao.java").write_text(
        "package com.example.dao;\n"
        "import java.util.List;\n"
        "public interface ISimpleDao<T> {\n"
        "    <X> List<X> find(String hql, Object... values);\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "src/main/java/com/example/dao/CustomDao.java").write_text(
        "package com.example.dao;\n"
        "public interface CustomDao<T> extends ISimpleDao<T> {}\n",
        encoding="utf-8",
    )
    service_path = "src/main/java/com/example/service/AnswerService.java"
    (tmp_path / service_path).write_text(
        "package com.example.service;\n"
        "import com.example.dao.CustomDao;\n"
        "import com.example.entity.BigEntity;\n"
        "import java.util.List;\n"
        "public class AnswerService {\n"
        "    private CustomDao<BigEntity> answerDao;\n"
        "    public List<BigEntity> findAnswers(Object p1, Object p2) {\n"
        "        return answerDao.find(\n"
        "            p1,\n"
        "            p2);\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    parser = JavaAstParser(str(tmp_path))
    manifests = parser.parse_repo()
    fqcn_to_file, pkg_to_files = build_local_dep_lookup(manifests)

    # Compiler error points to line 10 ("p2);"), 2 lines below "answerDao.find(" on line 8.
    err = [
        CompilerError(
            file_path=service_path,
            line_number=10,
            column=13,
            message="incompatible types",
        )
    ]
    ctx = collect_dependency_context(
        repo_path=tmp_path,
        target_file_path=service_path,
        file_records={},
        modified_files=set(),
        fqcn_to_file=fqcn_to_file,
        package_to_files=pkg_to_files,
        is_repair=True,
        include_unmodified_locals=True,
        compiler_errors=err,
        parser=parser,
        manifest_cache={m.file_path: m for m in manifests},
    )
    assert ctx is not None
    assert "ISimpleDao.java" in ctx
    assert ctx.index("ISimpleDao.java") < ctx.index("BigEntity.java")


def test_generate_patch_retries_when_repair_returns_unchanged_code(tmp_path: Path):
    from app.models.execution import CompilerError
    from app.orchestrator.workflow import MigrationOrchestrator

    src = (
        "package com.example;\n\n"
        "public class SampleService {\n"
        "    public String run() { return \"old\"; }\n"
        "}\n"
    )
    fixed = (
        "package com.example;\n\n"
        "public class SampleService {\n"
        "    public String run() { return \"fixed\"; }\n"
        "}\n"
    )
    prompts: list[str] = []

    class FakeLLM:
        def generate_text(self, prompt, system_instruction=None, temperature=0.2):
            return ""

        def generate_structured(
            self, prompt, response_schema, system_instruction=None, temperature=0.1
        ):
            prompts.append(prompt)
            if len(prompts) == 1:
                return FilePatchResult(
                    file_path="src/main/java/com/example/SampleService.java",
                    new_content=src,
                    explanation="no-op",
                )
            return FilePatchResult(
                file_path="src/main/java/com/example/SampleService.java",
                new_content=fixed,
                explanation="fixed",
            )

    orch = MigrationOrchestrator(repo_path=tmp_path, llm_provider=FakeLLM())
    patch = orch.generate_patch(
        target_file_path="src/main/java/com/example/SampleService.java",
        source_code=src,
        migration_rule="Spring Boot 3 migration",
        compiler_errors=[
            CompilerError(
                file_path="src/main/java/com/example/SampleService.java",
                line_number=4,
                column=20,
                message="incompatible types",
            )
        ],
        failed_tests=[],
    )
    assert len(prompts) == 2
    assert "CRITICAL NO-OP CORRECTION" in prompts[1]
    assert patch.new_content == fixed

