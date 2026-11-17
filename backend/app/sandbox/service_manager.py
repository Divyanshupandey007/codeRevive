from contextlib import contextmanager
from dataclasses import dataclass
import subprocess
import time
from typing import Iterator, List, Optional
import uuid

from app.config import settings
from app.sandbox.service_detector import ServiceRequirement


class ServiceProvisioningError(Exception):
    """Raised when an ephemeral backing service container fails to start or become ready."""


@dataclass
class ActiveServiceContext:
    """Network mode passed to the Maven sandbox container while sidecars are alive."""

    network_mode: Optional[str]


def readiness_command_for(service: ServiceRequirement) -> List[str]:
    if service.kind == "mysql":
        cmd = [
            "mysql",
            "-h",
            "127.0.0.1",
            "-P",
            str(service.port),
            "--protocol=tcp",
            "-u",
            service.username or "root",
        ]
        if service.password:
            cmd.append(f"-p{service.password}")
        cmd += ["-e", "SELECT 1"]
        if service.database:
            cmd.append(service.database)
        return cmd

    if service.kind == "postgres":
        return [
            "pg_isready",
            "-h",
            "127.0.0.1",
            "-p",
            str(service.port),
            "-U",
            service.username or "postgres",
            "-d",
            service.database or "postgres",
        ]

    if service.kind == "redis":
        cmd = ["redis-cli", "-h", "127.0.0.1", "-p", str(service.port)]
        if service.password:
            cmd += ["-a", service.password]
        cmd.append("ping")
        return cmd

    if service.kind == "mongo":
        return [
            "mongosh",
            "--host",
            "127.0.0.1",
            "--port",
            str(service.port),
            "--quiet",
            "--eval",
            "db.adminCommand('ping').ok",
        ]

    return ["true"]


class DockerServiceManager:
    """Starts ephemeral backing service containers for sandboxed tests and tears them down cleanly."""

    def __init__(
        self,
        anchor_image: Optional[str] = None,
        ready_timeout_seconds: Optional[int] = None,
        network_disabled: Optional[bool] = None,
    ):
        self.anchor_image = anchor_image or settings.sandbox_docker_image
        self.ready_timeout_seconds = (
            settings.sandbox_service_ready_timeout_seconds
            if ready_timeout_seconds is None
            else ready_timeout_seconds
        )
        self.network_disabled = (
            settings.sandbox_network_disabled
            if network_disabled is None
            else network_disabled
        )

    @staticmethod
    def _use_bridge_network(services: List[ServiceRequirement]) -> bool:
        return any(s.host not in ("localhost", "127.0.0.1", "") for s in services)

    @contextmanager
    def start_services(
        self, services: List[ServiceRequirement], max_lifetime_seconds: int
    ) -> Iterator[ActiveServiceContext]:
        if not services:
            yield ActiveServiceContext(network_mode=None)
            return

        try:
            import docker

            client = docker.from_env()
            client.ping()
        except Exception:
            with self._start_via_cli(services, max_lifetime_seconds) as ctx:
                yield ctx
            return

        with self._start_via_sdk(client, services, max_lifetime_seconds) as ctx:
            yield ctx

    @contextmanager
    def _start_via_sdk(
        self, client, services: List[ServiceRequirement], max_lifetime_seconds: int
    ) -> Iterator[ActiveServiceContext]:
        run_id = uuid.uuid4().hex[:8]
        use_bridge = self._use_bridge_network(services)
        anchor = None
        network = None
        sidecars = []

        try:
            if use_bridge:
                net_name = f"coderevive-net-{run_id}"
                network = client.networks.create(net_name, driver="bridge")
                target_network_mode = net_name
            else:
                anchor_kwargs = {
                    "entrypoint": ["sleep"],
                    "command": [str(max_lifetime_seconds + 120)],
                    "name": f"coderevive-pod-{run_id}",
                    "detach": True,
                    "mem_limit": "64m",
                    "pids_limit": 32,
                }
                if self.network_disabled:
                    anchor_kwargs["network_mode"] = "none"
                anchor = client.containers.run(self.anchor_image, **anchor_kwargs)
                target_network_mode = f"container:{anchor.id}"

            for svc in services:
                run_kwargs = {
                    "name": f"coderevive-{svc.kind}-{run_id}",
                    "environment": svc.env,
                    "detach": True,
                    "network_mode": target_network_mode,
                    "mem_limit": "1g",
                }
                if svc.command:
                    run_kwargs["command"] = svc.command
                container = client.containers.run(svc.image, **run_kwargs)
                if use_bridge and network is not None and svc.host not in ("localhost", "127.0.0.1"):
                    network.disconnect(container)
                    network.connect(container, aliases=[svc.host])
                sidecars.append((svc, container))

            for svc, container in sidecars:
                self._wait_ready_sdk(svc, container)

            yield ActiveServiceContext(network_mode=target_network_mode)
        except ServiceProvisioningError:
            raise
        except Exception as exc:
            raise ServiceProvisioningError(
                f"Failed to start dynamic sandbox service container(s): {exc}"
            ) from exc
        finally:
            for _, container in reversed(sidecars):
                try:
                    container.remove(force=True)
                except Exception:
                    pass
            if anchor is not None:
                try:
                    anchor.remove(force=True)
                except Exception:
                    pass
            if network is not None:
                try:
                    network.remove()
                except Exception:
                    pass

    def _wait_ready_sdk(self, svc: ServiceRequirement, container) -> None:
        probe = readiness_command_for(svc)
        deadline = time.monotonic() + self.ready_timeout_seconds
        while time.monotonic() < deadline:
            container.reload()
            if container.status == "exited":
                logs = container.logs(tail=30).decode("utf-8", errors="replace")
                raise ServiceProvisioningError(
                    f"{svc.kind} container exited before becoming ready:\n{logs}"
                )
            res = container.exec_run(probe)
            if res.exit_code == 0:
                return
            time.sleep(1.0)
        logs = container.logs(tail=30).decode("utf-8", errors="replace")
        raise ServiceProvisioningError(
            f"{svc.kind} container did not become ready within {self.ready_timeout_seconds}s:\n{logs}"
        )

    @contextmanager
    def _start_via_cli(
        self, services: List[ServiceRequirement], max_lifetime_seconds: int
    ) -> Iterator[ActiveServiceContext]:
        run_id = uuid.uuid4().hex[:8]
        use_bridge = self._use_bridge_network(services)
        anchor_name: Optional[str] = None
        net_name: Optional[str] = None
        sidecar_names: List[tuple[ServiceRequirement, str]] = []

        try:
            if use_bridge:
                net_name = f"coderevive-net-{run_id}"
                proc = subprocess.run(
                    ["docker", "network", "create", net_name],
                    capture_output=True,
                    text=True,
                )
                if proc.returncode != 0:
                    raise ServiceProvisioningError(
                        f"docker network create failed: {proc.stderr}"
                    )
                target_network_mode = net_name
            else:
                anchor_name = f"coderevive-pod-{run_id}"
                cmd = [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    anchor_name,
                    "--entrypoint",
                    "sleep",
                    "--memory",
                    "64m",
                    "--pids-limit",
                    "32",
                ]
                if self.network_disabled:
                    cmd += ["--network", "none"]
                cmd += [self.anchor_image, str(max_lifetime_seconds + 120)]
                proc = subprocess.run(cmd, capture_output=True, text=True)
                if proc.returncode != 0:
                    raise ServiceProvisioningError(
                        f"Failed to start pod network anchor: {proc.stderr}"
                    )
                target_network_mode = f"container:{anchor_name}"

            for svc in services:
                cname = f"coderevive-{svc.kind}-{run_id}"
                cmd = [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    cname,
                    "--network",
                    target_network_mode,
                    "--memory",
                    "1g",
                ]
                if use_bridge and svc.host not in ("localhost", "127.0.0.1"):
                    cmd += ["--network-alias", svc.host]
                for k, v in svc.env.items():
                    cmd += ["-e", f"{k}={v}"]
                cmd.append(svc.image)
                cmd.extend(svc.command)
                proc = subprocess.run(cmd, capture_output=True, text=True)
                if proc.returncode != 0:
                    raise ServiceProvisioningError(
                        f"Failed to start {svc.kind} container ({svc.image}): {proc.stderr}"
                    )
                sidecar_names.append((svc, cname))

            for svc, cname in sidecar_names:
                self._wait_ready_cli(svc, cname)

            yield ActiveServiceContext(network_mode=target_network_mode)
        except ServiceProvisioningError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise ServiceProvisioningError(
                f"Failed to start dynamic sandbox service container(s) via Docker CLI: {exc}"
            ) from exc
        finally:
            for _, cname in reversed(sidecar_names):
                try:
                    subprocess.run(["docker", "rm", "-f", cname], capture_output=True)
                except OSError:
                    pass
            if anchor_name is not None:
                try:
                    subprocess.run(["docker", "rm", "-f", anchor_name], capture_output=True)
                except OSError:
                    pass
            if net_name is not None:
                try:
                    subprocess.run(["docker", "network", "rm", net_name], capture_output=True)
                except OSError:
                    pass

    def _wait_ready_cli(self, svc: ServiceRequirement, cname: str) -> None:
        probe = readiness_command_for(svc)
        deadline = time.monotonic() + self.ready_timeout_seconds
        while time.monotonic() < deadline:
            status_proc = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Status}}", cname],
                capture_output=True,
                text=True,
            )
            if status_proc.returncode == 0 and status_proc.stdout.strip() in ("exited", "dead"):
                logs = subprocess.run(
                    ["docker", "logs", "--tail", "30", cname],
                    capture_output=True,
                    text=True,
                )
                raise ServiceProvisioningError(
                    f"{svc.kind} container exited before becoming ready:\n"
                    f"{logs.stdout}\n{logs.stderr}"
                )
            res = subprocess.run(
                ["docker", "exec", cname, *probe],
                capture_output=True,
                text=True,
            )
            if res.returncode == 0:
                return
            time.sleep(1.0)
        logs = subprocess.run(
            ["docker", "logs", "--tail", "30", cname],
            capture_output=True,
            text=True,
        )
        raise ServiceProvisioningError(
            f"{svc.kind} container did not become ready within {self.ready_timeout_seconds}s:\n"
            f"{logs.stdout}\n{logs.stderr}"
        )
