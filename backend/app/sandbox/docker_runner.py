from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
import time
from typing import List, Optional
import uuid

backend_root = Path(__file__).resolve().parents[2]
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))


from app.models.execution import BuildResult
from app.sandbox.diagnostics_parser import parse_maven_output
from app.config import settings

class ContainerExecutionError(Exception):
    """The sandbox itself couldn't run (daemon unreachable, image pull
    failed, docker not installed, repo_path missing, ...). Distinct from
    the build/tests failing *inside* a container that ran fine - that's a
    normal, successfully-captured BuildResult(success=False)."""


@dataclass
class _RawExecution:
    stdout:str
    stderr:str
    exit_code:int
    duration_seconds:float
    timed_out:bool

class _BuildExecutor(ABC):
    @abstractmethod
    def execute(self,repo_path:Path,maven_args:List[str],timeout_seconds:int,memory_limit:str,cpu_count:float,network_disabled:bool,cache_volume:str,container_workdir:str)->_RawExecution:
        ...

class _DockerSDKExecutor(_BuildExecutor):
    def __init__(self,image:str):
        self.image=image
        try:
            import docker
        except ImportError as exc:
            raise ContainerExecutionError("The docker package isn't installed. Run: pip install docker") from exc
        self._docker=docker
        try:
            self._client=docker.from_env()
            self._client.ping()
        except Exception as exc:
            raise ContainerExecutionError(f"Could not reach the Docker daemon: {exc}. Is Docker running, "
                "and is its socket accessible from this environment?"
                ) from exc

    def execute(self, repo_path, maven_args, timeout_seconds, memory_limit, cpu_count, network_disabled, cache_volume, container_workdir)->_RawExecution:
        from docker.errors import APIError,ContainerError,ImageNotFound
        from requests.exceptions import ReadTimeout
        from requests import RequestException

        command=["mvn",*maven_args]
        volumes={
            str(repo_path.resolve()):{"bind":container_workdir,"mode":"rw"},
            cache_volume:{"bind":"/root/.m2","mode":"rw"}
        }

        container=None
        start=time.monotonic()
        timed_out=False
        exit_code=1
        stdout=stderr=""
        try:
            container=self._client.containers.run(
                self.image,
                command=command,
                volumes=volumes,
                working_dir=container_workdir,
                mem_limit=memory_limit,
                nano_cpus=int(cpu_count*1_000_000_000),
                network_disabled=network_disabled,
                detach=True,
                stdout=True,
                stderr=True
            )
            try:
                wait_result=container.wait(timeout=timeout_seconds)
                exit_code=wait_result.get("StatusCode",1)
            except (ReadTimeout,ConnectionError,RequestException):
                timed_out=True
                exit_code=124
                try:
                    container.kill()
                except Exception:
                    pass

            stdout=container.logs(stdout=True,stderr=False).decode("utf-8",errors="replace")
            stderr=container.logs(stdout=False,stderr=True).decode("utf-8",errors="replace")

        except(ContainerError,ImageNotFound,APIError) as exc:
            raise ContainerExecutionError(f"Docker execution failed: {exc}") from exc
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    pass

        duration=time.monotonic()-start
        return _RawExecution(stdout,stderr,exit_code,duration,timed_out)

class _DockerCLIExecutor(_BuildExecutor):
    def __init__(self,image:str):
        self.image=image

    def execute(self, repo_path, maven_args, timeout_seconds, memory_limit, cpu_count, network_disabled, cache_volume, container_workdir)->_RawExecution:
        container_name=f"coderevive-build-{uuid.uuid4().hex[:10]}"
        cmd=[
            "docker","run","--rm","--name",container_name,
            "-v",f"{repo_path.resolve().as_posix()}:{container_workdir}",
            "-v",f"{cache_volume}:/root/.m2",
            "-w",container_workdir,
            "--memory",memory_limit,
            "--cpus",str(cpu_count)
        ]
        if network_disabled:
            cmd+=["--network","none"]
        cmd+=[self.image,"mvn",*maven_args]
        start=time.monotonic()
        timed_out=False

        try:
            proc=subprocess.run(cmd,capture_output=True,text=True,timeout=timeout_seconds)
            stdout,stderr,exit_code=proc.stdout,proc.stderr,proc.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out=True
            exit_code=124
            stdout=exc.stdout if isinstance(exc.stdout,str) else (exc.stdout or b"").decode("utf-8",errors="replace")
            stderr=exc.stderr if isinstance(exc.stderr,str) else (exc.stderr or b"").decode("utf-8",errors="replace")
            subprocess.run(["docker","kill",container_name],capture_output=True)
        except FileNotFoundError as exc:
            raise ContainerExecutionError("The docker CLI isn't available on PATH.") from exc

        duration=time.monotonic()-start
        return _RawExecution(
            stdout,stderr,exit_code,duration,timed_out
        )

class DockerRunner:
    def __init__(
            self,
            image:Optional[str]=None,
            timeout_seconds:Optional[int]=None,
            memory_limit:Optional[str]=None,
            cpu_count:Optional[float]=None,
            network_disabled:Optional[bool]=None,
            cache_volume:Optional[str]=None,
            container_workdir:Optional[str]=None,
            raw_log_char_limit:Optional[int]=None,
            executor:Optional[_BuildExecutor]=None
    ):
        self.image=settings.sandbox_docker_image if image is None else image
        self.timeout_seconds=settings.sandbox_timeout_seconds if timeout_seconds is None else timeout_seconds
        self.memory_limit=settings.sandbox_memory_limit if memory_limit is None else memory_limit
        self.cpu_count=settings.sandbox_cpu_count if cpu_count is None else cpu_count
        self.network_disabled=settings.sandbox_network_disabled if network_disabled is None else network_disabled
        self.cache_volume=settings.sandbox_maven_cache_volume if cache_volume is None else cache_volume
        self.container_workdir=settings.sandbox_container_workdir if container_workdir is None else container_workdir
        self.raw_log_char_limit=settings.sandbox_raw_log_char_limit if raw_log_char_limit is None else raw_log_char_limit
        self._executor=executor or self._select_executor(self.image)

    @staticmethod
    def _select_executor(image:str)->_BuildExecutor:
        try:
            return _DockerSDKExecutor(image)
        except ContainerExecutionError:
            return _DockerCLIExecutor(image)

    def run_tests(self,repo_path:Path,maven_args:Optional[List[str]]=None)->BuildResult:
        repo_path=Path(repo_path)
        if not repo_path.is_dir():
            raise ContainerExecutionError(f"repo_path does not exist; {repo_path}")

        maven_args=settings.sandbox_default_maven_args if maven_args is None else maven_args

        raw=self._executor.execute(
           repo_path=repo_path,
           maven_args=maven_args,
           timeout_seconds=self.timeout_seconds,
           memory_limit=self.memory_limit,
           cpu_count=self.cpu_count,
           network_disabled=self.network_disabled,
           cache_volume=self.cache_volume,
           container_workdir=self.container_workdir 
        )

        combined_logs=f"{raw.stdout}\n{raw.stderr}"
        compiler_errors,failed_tests=parse_maven_output(combined_logs,self.container_workdir)

        return BuildResult(
            success=(raw.exit_code==0) and not raw.timed_out,
            exit_code=raw.exit_code,
            duration_seconds=round(raw.duration_seconds,2),
            compiler_errors=compiler_errors,
            failed_tests=failed_tests,
            raw_logs=_truncate_logs(combined_logs,self.raw_log_char_limit)
        )

def _truncate_logs(logs:str,char_limit:int)->str:
    noise_free="\n".join(line for line in logs.splitlines()
                         if "Downloading from" not in line and "Download from" not in line
                         )
    if len(noise_free)<=char_limit:
        return noise_free

    head_budget=char_limit//4
    tail_budget=char_limit-head_budget
    head=noise_free[:head_budget]
    tail=noise_free[-tail_budget:]
    omitted=len(noise_free)-head_budget-tail_budget
    return f"{head}\n\n... [{omitted} characters omitted] ...\n\n{tail}"
    
if __name__=="__main__":
    repo_path=Path(__file__).resolve().parents[2]/"workspace"/"mybatis-spring-boot-jpetstore"
    print(f"Repository path: {repo_path}")
    print(f"Repository exists: {repo_path.is_dir()}")
 
    if not repo_path.is_dir():
        print("Run git_manager.py (or pipeline.py) first to clone the repo!")
    else:
        try:
            result=DockerRunner().run_tests(repo_path)
            
        except ContainerExecutionError as exc:
            print(f"\nCould not run the sandbox here: {exc}")
            sys.exit(1)

        if not result.success:
            print("\n--- RAW LOGS (STDERR / STDOUT) ---")
            print(result.raw_logs)
        print("\n========== BUILD RESULT ==========")
        print(f"success: {result.success}")
        print(f"exit_code: {result.exit_code}")
        print(f"duration_seconds: {result.duration_seconds}")
        print(f"compiler_errors: {len(result.compiler_errors)}")
        for e in result.compiler_errors:
            print(f"  {e.file_path}:{e.line_number}:{e.column}  {e.message}")
        print(f"failed_tests: {len(result.failed_tests)}")
        for t in result.failed_tests:
            print(f"  {t.test_class}.{t.test_method}: {t.failure_message}")