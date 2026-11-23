from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
import subprocess
import time
from typing import List, Optional
import uuid

from app.config import settings
from app.models.execution import BuildResult
from app.sandbox.diagnostics_parser import parse_maven_output
from app.sandbox.service_detector import ServiceRequirement, detect_required_services
from app.sandbox.service_manager import (
    DockerServiceManager,
    ServiceProvisioningError,
)


class ContainerExecutionError(Exception):
    """The sandbox itself couldn't run (daemon unreachable, image pull failed, etc.)."""


@dataclass
class _RawExecution:
    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float
    timed_out: bool


class _BuildExecutor(ABC):
    @abstractmethod
    def execute(
        self,
        repo_path: Path,
        maven_args: List[str],
        timeout_seconds: int,
        memory_limit: str,
        cpu_count: float,
        network_disabled: bool,
        cache_volume: str,
        container_workdir: str,
        network_mode: Optional[str] = None,
    ) -> _RawExecution:
        ...


class _DockerSDKExecutor(_BuildExecutor):
    def __init__(self, image: str):
        self.image = image
        try:
            import docker
        except ImportError as exc:
            raise ContainerExecutionError(
                "The docker package isn't installed. Run: pip install docker"
            ) from exc
        self._docker = docker
        try:
            self._client = docker.from_env()
            self._client.ping()
        except Exception as exc:
            raise ContainerExecutionError(
                f"Could not reach the Docker daemon: {exc}. Is Docker running, "
                "and is its socket accessible from this environment?"
            ) from exc

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
        network_mode: Optional[str] = None,
    ) -> _RawExecution:
        from docker.errors import APIError, ContainerError, ImageNotFound
        from requests import RequestException
        from requests.exceptions import ReadTimeout

        command = ["mvn", *maven_args]
        volumes = {
            str(repo_path.resolve()): {"bind": container_workdir, "mode": "rw"},
            cache_volume: {"bind": "/root/.m2", "mode": "rw"},
        }

        container = None
        start = time.monotonic()
        timed_out = False
        exit_code = 1
        stdout = stderr = ""
        try:
            run_kwargs = {
                "command": command,
                "volumes": volumes,
                "working_dir": container_workdir,
                "mem_limit": memory_limit,
                "shm_size": "2g",
                "nano_cpus": int(cpu_count * 1_000_000_000),
                "cap_drop": ["ALL"],
                "cap_add": ["DAC_OVERRIDE"],
                "security_opt": ["no-new-privileges:true"],
                "pids_limit": 1024,
                "detach": True,
                "stdout": True,
                "stderr": True,
            }
            if network_mode:
                run_kwargs["network_mode"] = network_mode
            else:
                run_kwargs["network_disabled"] = network_disabled

            container = self._client.containers.run(self.image, **run_kwargs)
            try:
                wait_result = container.wait(timeout=timeout_seconds)
                exit_code = wait_result.get("StatusCode", 1)
            except (ReadTimeout, ConnectionError, RequestException):
                timed_out = True
                exit_code = 124
                try:
                    container.kill()
                except Exception:
                    pass

            stdout = container.logs(stdout=True, stderr=False).decode(
                "utf-8", errors="replace"
            )
            stderr = container.logs(stdout=False, stderr=True).decode(
                "utf-8", errors="replace"
            )

        except (ContainerError, ImageNotFound, APIError) as exc:
            raise ContainerExecutionError(f"Docker execution failed: {exc}") from exc
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    pass

        duration = time.monotonic() - start
        return _RawExecution(stdout, stderr, exit_code, duration, timed_out)


class _DockerCLIExecutor(_BuildExecutor):
    def __init__(self, image: str):
        self.image = image

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
        network_mode: Optional[str] = None,
    ) -> _RawExecution:
        container_name = f"coderevive-build-{uuid.uuid4().hex[:10]}"
        cmd = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--cap-drop=ALL",
            "--cap-add=DAC_OVERRIDE",
            "--security-opt=no-new-privileges:true",
            "--pids-limit=1024",
            "-v",
            f"{repo_path.resolve().as_posix()}:{container_workdir}",
            "-v",
            f"{cache_volume}:/root/.m2",
            "-w",
            container_workdir,
            "--memory",
            memory_limit,
            "--shm-size",
            "2g",
            "--cpus",
            str(cpu_count),
        ]
        if network_mode:
            cmd += ["--network", network_mode]
        elif network_disabled:
            cmd += ["--network", "none"]
        cmd += [self.image, "mvn", *maven_args]
        start = time.monotonic()
        timed_out = False

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_seconds
            )
            stdout, stderr, exit_code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = 124
            stdout = (
                exc.stdout
                if isinstance(exc.stdout, str)
                else (exc.stdout or b"").decode("utf-8", errors="replace")
            )
            stderr = (
                exc.stderr
                if isinstance(exc.stderr, str)
                else (exc.stderr or b"").decode("utf-8", errors="replace")
            )
            subprocess.run(["docker", "kill", container_name], capture_output=True)
        except FileNotFoundError as exc:
            raise ContainerExecutionError(
                "The docker CLI isn't available on PATH."
            ) from exc

        duration = time.monotonic() - start
        return _RawExecution(stdout, stderr, exit_code, duration, timed_out)


class DockerRunner:
    def __init__(
        self,
        image: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        memory_limit: Optional[str] = None,
        cpu_count: Optional[float] = None,
        network_disabled: Optional[bool] = None,
        cache_volume: Optional[str] = None,
        container_workdir: Optional[str] = None,
        raw_log_char_limit: Optional[int] = None,
        executor: Optional[_BuildExecutor] = None,
        openrewrite_timeout_seconds: Optional[int] = None,
        service_manager: Optional[DockerServiceManager] = None,
        dynamic_services: Optional[bool] = None,
    ):
        self.image = settings.sandbox_docker_image if image is None else image
        self.timeout_seconds = (
            settings.sandbox_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        self.openrewrite_timeout_seconds = (
            settings.openrewrite_timeout_seconds
            if openrewrite_timeout_seconds is None
            else openrewrite_timeout_seconds
        )
        self.memory_limit = (
            settings.sandbox_memory_limit if memory_limit is None else memory_limit
        )
        self.cpu_count = settings.sandbox_cpu_count if cpu_count is None else cpu_count
        self.network_disabled = (
            settings.sandbox_network_disabled
            if network_disabled is None
            else network_disabled
        )
        self.cache_volume = (
            settings.sandbox_maven_cache_volume if cache_volume is None else cache_volume
        )
        self.container_workdir = (
            settings.sandbox_container_workdir
            if container_workdir is None
            else container_workdir
        )
        self.raw_log_char_limit = (
            settings.sandbox_raw_log_char_limit
            if raw_log_char_limit is None
            else raw_log_char_limit
        )
        self._executor = executor or self._select_executor(self.image)
        self.service_manager = service_manager or DockerServiceManager(
            anchor_image=self.image,
            network_disabled=self.network_disabled,
        )
        if dynamic_services is not None:
            self.dynamic_services = dynamic_services
        elif executor is not None and service_manager is None:
            self.dynamic_services = False
        else:
            self.dynamic_services = settings.sandbox_dynamic_services

    @staticmethod
    def _select_executor(image: str) -> _BuildExecutor:
        try:
            return _DockerSDKExecutor(image)
        except ContainerExecutionError:
            return _DockerCLIExecutor(image)

    def run_maven_rewrite(
        self, repo_path: Path, maven_args: List[str]
    ) -> tuple[int, str]:
        """Implements RecipeCommandExecutor so OpenRewriteRunner can execute inside Docker."""
        raw = self._executor.execute(
            repo_path=Path(repo_path),
            maven_args=maven_args,
            timeout_seconds=self.openrewrite_timeout_seconds,
            memory_limit=self.memory_limit,
            cpu_count=self.cpu_count,
            network_disabled=self.network_disabled,
            cache_volume=self.cache_volume,
            container_workdir=self.container_workdir,
        )
        return raw.exit_code, f"{raw.stdout}\n{raw.stderr}"

    def run_tests(
        self,
        repo_path: Path,
        maven_args: Optional[List[str]] = None,
        services: Optional[List[ServiceRequirement]] = None,
    ) -> BuildResult:
        repo_path = Path(repo_path)
        if not repo_path.is_dir():
            raise ContainerExecutionError(f"repo_path does not exist; {repo_path}")

        maven_args = (
            settings.sandbox_default_maven_args if maven_args is None else maven_args
        )

        using_embedded_override = any(
            arg.startswith("-Dspring.profiles.active=test")
            or arg.startswith("-Dspring.datasource.url=jdbc:h2:")
            for arg in maven_args
        )
        active_services: List[ServiceRequirement] = []
        if services is not None:
            active_services = list(services)
        elif self.dynamic_services and not using_embedded_override:
            active_services = detect_required_services(repo_path, maven_args=maven_args)

        raw: Optional[_RawExecution] = None
        if active_services:
            summary = ", ".join(
                f"{s.kind}({s.host}:{s.port}"
                + (f", db={s.database}" if s.database else "")
                + ")"
                for s in active_services
            )
            print(
                f" [SANDBOX SERVICES] Starting ephemeral container(s): {summary} ..."
            )
            try:
                with self.service_manager.start_services(
                    active_services, self.timeout_seconds
                ) as ctx:
                    print(
                        f" [SANDBOX SERVICES] Ready (network={ctx.network_mode}); executing Maven verification..."
                    )
                    raw = self._executor.execute(
                        repo_path=repo_path,
                        maven_args=maven_args,
                        timeout_seconds=self.timeout_seconds,
                        memory_limit=self.memory_limit,
                        cpu_count=self.cpu_count,
                        network_disabled=self.network_disabled,
                        cache_volume=self.cache_volume,
                        container_workdir=self.container_workdir,
                        network_mode=ctx.network_mode,
                    )
            except ServiceProvisioningError as exc:
                print(
                    f" [SANDBOX SERVICES] Warning: {exc}; "
                    "continuing without sidecar containers."
                )

        if raw is None:
            raw = self._executor.execute(
                repo_path=repo_path,
                maven_args=maven_args,
                timeout_seconds=self.timeout_seconds,
                memory_limit=self.memory_limit,
                cpu_count=self.cpu_count,
                network_disabled=self.network_disabled,
                cache_volume=self.cache_volume,
                container_workdir=self.container_workdir,
            )

        combined_logs = f"{raw.stdout}\n{raw.stderr}".strip()
        if raw.timed_out or raw.exit_code == 124:
            combined_logs = (
                f"{combined_logs}\n"
                f"[ERROR] Sandbox command timed out after {self.timeout_seconds}s "
                f"(exit code {raw.exit_code}). Increase CODEREVIVE_SANDBOX_TIMEOUT_SECONDS "
                "for large repositories or slow Maven mirrors."
            ).strip()
        elif raw.exit_code == 137:
            combined_logs = (
                f"{combined_logs}\n"
                "[ERROR] Sandbox container was killed (SIGKILL / exit code 137 — likely out of memory). "
                f"Increase CODEREVIVE_SANDBOX_MEMORY_LIMIT (currently {self.memory_limit})."
            ).strip()
        elif raw.exit_code == 143:
            combined_logs = (
                f"{combined_logs}\n"
                "[ERROR] Sandbox container received SIGTERM (exit code 143) before Maven completed."
            ).strip()

        compiler_errors, failed_tests = parse_maven_output(
            combined_logs, self.container_workdir
        )

        return BuildResult(
            success=(raw.exit_code == 0) and not raw.timed_out,
            exit_code=raw.exit_code,
            duration_seconds=round(raw.duration_seconds, 2),
            compiler_errors=compiler_errors,
            failed_tests=failed_tests,
            raw_logs=_truncate_logs(combined_logs, self.raw_log_char_limit),
        )


def _truncate_logs(logs: str, char_limit: int) -> str:
    noise_free = "\n".join(
        line
        for line in logs.splitlines()
        if "Downloading from" not in line and "Download from" not in line
    )
    if len(noise_free) <= char_limit:
        return noise_free

    head_budget = char_limit // 4
    tail_budget = char_limit - head_budget
    head = noise_free[:head_budget]
    tail = noise_free[-tail_budget:]
    omitted = len(noise_free) - head_budget - tail_budget
    return f"{head}\n\n... [{omitted} characters omitted] ...\n\n{tail}"