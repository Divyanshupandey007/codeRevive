from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from app.sandbox.docker_runner import DockerRunner, _RawExecution
from app.sandbox.service_detector import ServiceRequirement, detect_required_services
from app.sandbox.service_manager import (
    ActiveServiceContext,
    DockerServiceManager,
    ServiceProvisioningError,
    readiness_command_for,
)


def test_detects_mysql_from_application_properties_and_pom(tmp_path: Path):
    (tmp_path / "pom.xml").write_text(
        """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.shop</groupId><artifactId>shop</artifactId><version>1.0</version>
  <dependencies>
    <dependency><groupId>com.h2database</groupId><artifactId>h2</artifactId><scope>runtime</scope></dependency>
    <dependency><groupId>com.mysql</groupId><artifactId>mysql-connector-j</artifactId><scope>runtime</scope></dependency>
  </dependencies>
</project>""",
        encoding="utf-8",
    )
    res_dir = tmp_path / "src/main/resources"
    res_dir.mkdir(parents=True)
    (res_dir / "application.properties").write_text(
        "spring.datasource.driver-class-name=com.mysql.cj.jdbc.Driver\n"
        "spring.datasource.url=jdbc:mysql://localhost:3306/shop?serverTimezone=UTC\n"
        "spring.datasource.username=root\n"
        "spring.datasource.password=1234\n",
        encoding="utf-8",
    )

    services = detect_required_services(tmp_path)
    assert len(services) == 1
    mysql = services[0]
    assert mysql.kind == "mysql"
    assert mysql.host == "localhost"
    assert mysql.port == 3306
    assert mysql.database == "shop"
    assert mysql.username == "root"
    assert mysql.password == "1234"
    assert mysql.env["MYSQL_DATABASE"] == "shop"
    assert mysql.env["MYSQL_ROOT_PASSWORD"] == "1234"


def test_detects_postgres_and_redis_from_application_yml(tmp_path: Path):
    (tmp_path / "pom.xml").write_text(
        """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.ex</groupId><artifactId>app</artifactId><version>1.0</version>
  <dependencies>
    <dependency><groupId>org.postgresql</groupId><artifactId>postgresql</artifactId></dependency>
    <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-data-redis</artifactId></dependency>
  </dependencies>
</project>""",
        encoding="utf-8",
    )
    res_dir = tmp_path / "src/main/resources"
    res_dir.mkdir(parents=True)
    (res_dir / "application.yml").write_text(
        """spring:
  datasource:
    url: ${DB_URL:jdbc:postgresql://localhost:5433/orders}
    username: ${DB_USER:appuser}
    password: ${DB_PASS:secret}
  data:
    redis:
      host: localhost
      port: 6380
""",
        encoding="utf-8",
    )

    services = detect_required_services(tmp_path)
    by_kind = {s.kind: s for s in services}
    assert set(by_kind) == {"postgres", "redis"}
    assert by_kind["postgres"].port == 5433
    assert by_kind["postgres"].database == "orders"
    assert by_kind["postgres"].username == "appuser"
    assert by_kind["postgres"].command == ["-p", "5433"]
    assert by_kind["redis"].port == 6380
    assert by_kind["redis"].command == ["redis-server", "--port", "6380"]


def test_skips_sidecar_for_embedded_h2_only_repo(tmp_path: Path):
    (tmp_path / "pom.xml").write_text(
        """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.ex</groupId><artifactId>h2app</artifactId><version>1.0</version>
  <dependencies>
    <dependency><groupId>com.h2database</groupId><artifactId>h2</artifactId></dependency>
  </dependencies>
</project>""",
        encoding="utf-8",
    )
    assert detect_required_services(tmp_path) == []


def test_readiness_commands_match_service_protocols():
    mysql_cmd = readiness_command_for(
        ServiceRequirement(
            kind="mysql",
            image="mysql:8.0",
            port=3306,
            database="shop",
            username="root",
            password="1234",
        )
    )
    assert mysql_cmd[:5] == ["mysql", "-h", "127.0.0.1", "-P", "3306"]
    assert "-p1234" in mysql_cmd
    assert mysql_cmd[-2:] == ["SELECT 1", "shop"]

    redis_cmd = readiness_command_for(
        ServiceRequirement(kind="redis", image="redis:7-alpine", port=6379)
    )
    assert redis_cmd == ["redis-cli", "-h", "127.0.0.1", "-p", "6379", "ping"]


class _RecordingExecutor:
    def __init__(self):
        self.network_modes: List[Optional[str]] = []

    def execute(
        self,
        repo_path,
        maven_args,
        timeout_seconds,
        memory_limit,
        cpu_count,
        network_disabled,
        cache_volume,
        container_workdir,
        network_mode=None,
    ):
        self.network_modes.append(network_mode)
        return _RawExecution(
            stdout="[INFO] BUILD SUCCESS",
            stderr="",
            exit_code=0,
            duration_seconds=0.5,
            timed_out=False,
        )


class _FakeServiceManager(DockerServiceManager):
    def __init__(self, should_fail: bool = False):
        super().__init__(anchor_image="coderevive-maven-chrome:17", ready_timeout_seconds=5)
        self.should_fail = should_fail
        self.started_services: List[List[ServiceRequirement]] = []

    @contextmanager
    def start_services(self, services, max_lifetime_seconds):
        self.started_services.append(list(services))
        if self.should_fail:
            raise ServiceProvisioningError("simulated image pull failure")
        yield ActiveServiceContext(network_mode="container:coderevive-pod-test")


def test_docker_runner_attaches_maven_container_to_dynamic_service_network(tmp_path: Path):
    res_dir = tmp_path / "src/main/resources"
    res_dir.mkdir(parents=True)
    (res_dir / "application.properties").write_text(
        "spring.datasource.url=jdbc:mysql://localhost:3306/shop\n"
        "spring.datasource.username=root\n"
        "spring.datasource.password=1234\n",
        encoding="utf-8",
    )

    executor = _RecordingExecutor()
    svc_mgr = _FakeServiceManager()
    runner = DockerRunner(executor=executor, service_manager=svc_mgr, dynamic_services=True)

    result = runner.run_tests(tmp_path)
    assert result.success is True
    assert len(svc_mgr.started_services) == 1
    assert svc_mgr.started_services[0][0].kind == "mysql"
    assert executor.network_modes == ["container:coderevive-pod-test"]


def test_docker_runner_falls_back_gracefully_if_sidecar_provisioning_fails(tmp_path: Path):
    res_dir = tmp_path / "src/main/resources"
    res_dir.mkdir(parents=True)
    (res_dir / "application.properties").write_text(
        "spring.datasource.url=jdbc:mysql://localhost:3306/shop\n",
        encoding="utf-8",
    )

    executor = _RecordingExecutor()
    svc_mgr = _FakeServiceManager(should_fail=True)
    runner = DockerRunner(executor=executor, service_manager=svc_mgr, dynamic_services=True)

    result = runner.run_tests(tmp_path)
    assert result.success is True
    assert executor.network_modes == [None]


def test_docker_runner_records_explicit_error_on_timeout_and_oom(tmp_path: Path):
    class _FixedExecutor:
        def __init__(self, raw: _RawExecution):
            self._raw = raw

        def execute(self, **kwargs) -> _RawExecution:
            return self._raw

    timeout_runner = DockerRunner(
        executor=_FixedExecutor(
            _RawExecution(
                stdout="[INFO] Changes detected - recompiling the module! :source",
                stderr="",
                exit_code=124,
                duration_seconds=600.0,
                timed_out=True,
            )
        ),
        timeout_seconds=600,
    )
    timeout_result = timeout_runner.run_tests(tmp_path)
    assert timeout_result.success is False
    assert timeout_result.exit_code == 124
    assert "timed out after 600s" in timeout_result.raw_logs
    assert any("timed out" in e.message.lower() for e in timeout_result.compiler_errors)

    oom_runner = DockerRunner(
        executor=_FixedExecutor(
            _RawExecution(
                stdout="[INFO] Compiling 430 source files",
                stderr="",
                exit_code=137,
                duration_seconds=120.0,
                timed_out=False,
            )
        ),
        memory_limit="2g",
    )
    oom_result = oom_runner.run_tests(tmp_path)
    assert oom_result.success is False
    assert oom_result.exit_code == 137
    assert "exit code 137" in oom_result.raw_logs
    assert any("exit code 137" in e.message.lower() for e in oom_result.compiler_errors)


def test_detects_services_from_active_spring_profile_and_cli_override(tmp_path: Path):
    res_dir = tmp_path / "src/main/resources"
    res_dir.mkdir(parents=True)
    (res_dir / "application.yml").write_text(
        """spring:
  profiles:
    active: dev
""",
        encoding="utf-8",
    )
    (res_dir / "application-dev.yml").write_text(
        """spring:
  datasource:
    driver-class-name: com.mysql.cj.jdbc.Driver
    url: jdbc:mysql://127.0.0.1:3306/dwsurvey?useUnicode=true
    username: dwuser
    password: secret#dev
  redis:
    host: 127.0.0.1
    port: 6379
""",
        encoding="utf-8",
    )
    (res_dir / "application-staging.yml").write_text(
        """spring:
  datasource:
    url: jdbc:postgresql://127.0.0.1:5435/staging_db
    username: stg
    password: stg_pw
""",
        encoding="utf-8",
    )

    dev_services = {s.kind: s for s in detect_required_services(tmp_path)}
    assert set(dev_services) == {"mysql", "redis"}
    assert dev_services["mysql"].database == "dwsurvey"
    assert dev_services["mysql"].username == "dwuser"
    assert dev_services["mysql"].password == "secret#dev"
    assert dev_services["mysql"].env == {
        "MYSQL_DATABASE": "dwsurvey",
        "MYSQL_ROOT_PASSWORD": "secret#dev",
        "MYSQL_USER": "dwuser",
        "MYSQL_PASSWORD": "secret#dev",
    }

    stg_services = {
        s.kind: s
        for s in detect_required_services(
            tmp_path, maven_args=["-Dspring.profiles.active=staging"]
        )
    }
    assert set(stg_services) == {"postgres"}
    assert stg_services["postgres"].database == "staging_db"
    assert stg_services["postgres"].port == 5435
    assert stg_services["postgres"].username == "stg"


def test_detects_services_from_docker_compose_and_dotenv_without_hardcoded_defaults(tmp_path: Path):
    (tmp_path / ".env").write_text(
        "MYSQL_IMG=mysql:8.4\nCUSTOM_DB=portal_db\nCUSTOM_PW=s3cr3t\n",
        encoding="utf-8",
    )
    (tmp_path / "docker-compose.yml").write_text(
        """services:
  db:
    image: ${MYSQL_IMG}
    ports:
      - "3307:3306"
    environment:
      MYSQL_DATABASE: ${CUSTOM_DB}
      MYSQL_ROOT_PASSWORD: ${CUSTOM_PW}
  cache:
    image: redis:7.2-alpine
    ports:
      - "6381:6379"
""",
        encoding="utf-8",
    )

    services = {s.kind: s for s in detect_required_services(tmp_path)}
    assert set(services) == {"mysql", "redis"}
    assert services["mysql"].image == "mysql:8.4"
    assert services["mysql"].port == 3307
    assert services["mysql"].database == "portal_db"
    assert services["mysql"].password == "s3cr3t"
    assert services["redis"].image == "redis:7.2-alpine"
    assert services["redis"].port == 6381

    # POM-only mysql without explicit DB name or password must NOT invent 'app' or 'root'
    pom_only = tmp_path / "pom-only"
    pom_only.mkdir()
    (pom_only / "pom.xml").write_text(
        """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.ex</groupId><artifactId>bare</artifactId><version>1.0</version>
  <dependencies>
    <dependency><groupId>com.mysql</groupId><artifactId>mysql-connector-j</artifactId></dependency>
  </dependencies>
</project>""",
        encoding="utf-8",
    )
    bare_services = detect_required_services(pom_only)
    assert len(bare_services) == 1
    assert bare_services[0].database is None
    assert bare_services[0].password == ""
    assert "MYSQL_DATABASE" not in bare_services[0].env
    assert bare_services[0].env.get("MYSQL_ALLOW_EMPTY_PASSWORD") == "yes"


