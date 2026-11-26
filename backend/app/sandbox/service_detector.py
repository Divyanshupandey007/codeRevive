from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from app.config import settings
from app.ingestion.pom_parser import PomParser
from app.models.manifest import PomManifest

_DEFAULT_PORTS = {
    "mysql": 3306,
    "postgres": 5432,
    "redis": 6379,
    "mongo": 27017,
}

_PLACEHOLDER_RE = re.compile(r"\$\{([^:}]+)(?::([^}]*))?\}")
_HOST_PORT_RE = re.compile(r"(?:://|to\s+)([A-Za-z0-9_.-]+):(\d{2,5})")


@dataclass(frozen=True)
class ServiceRequirement:
    kind: str  # "mysql" | "postgres" | "redis" | "mongo"
    image: str
    host: str = "localhost"
    port: int = 0
    database: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    command: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.port == 0:
            object.__setattr__(self, "port", _DEFAULT_PORTS.get(self.kind, 0))


@dataclass
class _ComposeServiceInfo:
    name: str
    kind: str
    image: str
    port: int
    env: Dict[str, str] = field(default_factory=dict)
    command: List[str] = field(default_factory=list)


def _strip_quotes(val: str) -> str:
    s = val.strip()
    if len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        return s[1:-1]
    return s


def _strip_yaml_comment(line: str) -> str:
    """Strip `# comment` outside single/double quotes when preceded by start-of-line or whitespace."""
    in_single = False
    in_double = False
    for idx, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if idx == 0 or line[idx - 1].isspace():
                return line[:idx].rstrip()
    return line.rstrip()


def _resolve_placeholders(value: str, context: Optional[Dict[str, str]] = None) -> str:
    """Resolve `${VAR}` or `${VAR:default}` using repo `.env` / config context before falling back to defaults."""
    ctx = context or {}

    def _replace(match: re.Match) -> str:
        key = match.group(1).strip()
        default = match.group(2)
        if key in ctx and ctx[key] != "":
            return ctx[key]
        return "" if default is None else default

    resolved = value
    for _ in range(4):
        next_val = _PLACEHOLDER_RE.sub(_replace, resolved).strip()
        if next_val == resolved:
            break
        resolved = next_val
    return resolved


def _load_dotenv_files(repo_path: Path) -> Dict[str, str]:
    env_vars: Dict[str, str] = {}
    for name in (".env", ".env.local", ".env.dev", ".env.test"):
        env_file = repo_path / name
        if not env_file.is_file():
            continue
        for raw_line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            env_vars[key.strip()] = _strip_quotes(val)
    return env_vars


def _parse_properties_file(path: Path, env_vars: Dict[str, str]) -> Dict[str, str]:
    props: Dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if "=" in line:
            key, val = line.split("=", 1)
        elif ":" in line:
            key, val = line.split(":", 1)
        else:
            continue
        props[key.strip()] = _resolve_placeholders(_strip_quotes(val), env_vars)
    return props


def _parse_simple_yaml_file(path: Path, env_vars: Dict[str, str]) -> Dict[str, str]:
    """Dependency-free indentation parser for Spring Boot YAML configs."""
    props: Dict[str, str] = {}
    stack: List[Tuple[int, str]] = []
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        cleaned = _strip_yaml_comment(raw_line)
        stripped = cleaned.strip()
        if not stripped:
            continue
        if stripped in ("---", "..."):
            stack.clear()
            continue
        if stripped.startswith("- "):
            continue
        indent = len(cleaned) - len(cleaned.lstrip(" "))
        if ":" not in stripped:
            continue
        key_part, val_part = stripped.split(":", 1)
        key = _strip_quotes(key_part)
        val = _strip_quotes(val_part)

        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))

        if val:
            dotted = ".".join(k for _, k in stack)
            props[dotted] = _resolve_placeholders(val, env_vars)
    return props


def _extract_profiles_from_maven_args(maven_args: Optional[List[str]]) -> List[str]:
    profiles: List[str] = []
    if not maven_args:
        return profiles
    for arg in maven_args:
        for prefix in ("-Dspring.profiles.active=", "-Dspring-boot.run.profiles="):
            if arg.startswith(prefix):
                raw = arg[len(prefix) :]
                for item in raw.split(","):
                    p = item.strip()
                    if p and p not in profiles:
                        profiles.append(p)
    return profiles


def _load_spring_configs(
    repo_path: Path,
    env_vars: Dict[str, str],
    maven_args: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Load base Spring configs and resolve active profile files (`application-{profile}.*`)."""
    merged: Dict[str, str] = {}
    resource_roots = [
        repo_path / "src/main/resources",
        repo_path / "src/test/resources",
    ]
    base_names = ("bootstrap", "application")
    exts = (".properties", ".yml", ".yaml")

    def _merge_file(cfg: Path) -> None:
        if not cfg.is_file():
            return
        if cfg.suffix == ".properties":
            merged.update(_parse_properties_file(cfg, env_vars))
        else:
            merged.update(_parse_simple_yaml_file(cfg, env_vars))

    for root in resource_roots:
        for base in base_names:
            for ext in exts:
                _merge_file(root / f"{base}{ext}")

    active_profiles = _extract_profiles_from_maven_args(maven_args)
    if not active_profiles:
        raw_active = merged.get("spring.profiles.active", "")
        for item in raw_active.split(","):
            p = item.strip()
            if p and p not in active_profiles:
                active_profiles.append(p)

    for profile in active_profiles:
        for root in resource_roots:
            for base in base_names:
                for ext in exts:
                    _merge_file(root / f"{base}-{profile}{ext}")

    # Resolve any intra-config `${spring...}` references once merged
    for key, val in list(merged.items()):
        if "${" in val:
            merged[key] = _resolve_placeholders(val, {**env_vars, **merged})
    return merged


def _infer_kind_from_image(image: str, service_name: str = "") -> Optional[str]:
    lower = f"{image} {service_name}".lower()
    if "mysql" in lower or "mariadb" in lower:
        return "mysql"
    if "postgres" in lower:
        return "postgres"
    if "redis" in lower:
        return "redis"
    if "mongo" in lower:
        return "mongo"
    return None


def _parse_docker_compose(
    repo_path: Path, env_vars: Dict[str, str]
) -> Dict[str, _ComposeServiceInfo]:
    """Extract service definitions (`image`, `ports`, `environment`) from repo Compose files."""
    compose_file: Optional[Path] = None
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        candidate = repo_path / name
        if candidate.is_file():
            compose_file = candidate
            break
    if compose_file is None:
        return {}

    lines = compose_file.read_text(encoding="utf-8", errors="ignore").splitlines()
    raw_services: Dict[str, Dict[str, object]] = {}
    in_services = False
    services_indent: Optional[int] = None
    cur_service: Optional[str] = None
    cur_service_indent: Optional[int] = None
    cur_section: Optional[str] = None
    cur_section_indent: Optional[int] = None

    for raw_line in lines:
        cleaned = _strip_yaml_comment(raw_line)
        stripped = cleaned.strip()
        if not stripped:
            continue
        indent = len(cleaned) - len(cleaned.lstrip(" "))

        if not in_services:
            if stripped == "services:" or stripped.startswith("services:"):
                in_services = True
                services_indent = indent
            continue

        if services_indent is not None and indent <= services_indent and not stripped.startswith("-"):
            # Left top-level `services:` block
            break

        if not stripped.startswith("-") and ":" in stripped:
            if cur_service_indent is None or indent <= cur_service_indent:
                if cur_section is not None and cur_section_indent is not None and indent <= cur_service_indent:
                    cur_service = stripped.split(":", 1)[0].strip()
                    cur_service_indent = indent
                    cur_section = None
                    cur_section_indent = None
                    raw_services.setdefault(cur_service, {"ports": [], "env": {}, "command": []})
                    continue

        if cur_service is None:
            if not stripped.startswith("-") and ":" in stripped:
                cur_service = stripped.split(":", 1)[0].strip()
                cur_service_indent = indent
                raw_services.setdefault(cur_service, {"ports": [], "env": {}, "command": []})
            continue

        svc_data = raw_services[cur_service]

        if stripped.startswith("- "):
            item = _resolve_placeholders(_strip_quotes(stripped[2:].strip()), env_vars)
            if cur_section == "ports":
                svc_data["ports"].append(item)  # type: ignore[union-attr]
            elif cur_section == "environment" and "=" in item:
                ek, ev = item.split("=", 1)
                svc_data["env"][ek.strip()] = _strip_quotes(ev)  # type: ignore[index]
            elif cur_section == "command":
                svc_data["command"].append(item)  # type: ignore[union-attr]
            continue

        if ":" not in stripped:
            continue

        key_part, val_part = stripped.split(":", 1)
        key = _strip_quotes(key_part)
        val = _resolve_placeholders(_strip_quotes(val_part), env_vars)

        if cur_section == "environment" and cur_section_indent is not None and indent > cur_section_indent:
            svc_data["env"][key] = val  # type: ignore[index]
            continue

        cur_section = None
        cur_section_indent = None

        if key == "image" and val:
            svc_data["image"] = val
        elif key in ("ports", "environment", "command"):
            cur_section = key
            cur_section_indent = indent
            if key == "command" and val:
                svc_data["command"].extend(val.split())

    by_kind: Dict[str, _ComposeServiceInfo] = {}
    for svc_name, data in raw_services.items():
        image = str(data.get("image") or "")
        kind = _infer_kind_from_image(image, svc_name)
        if not kind or kind in by_kind:
            continue
        port = _DEFAULT_PORTS[kind]
        ports_list: List[str] = data.get("ports", [])  # type: ignore[assignment]
        if ports_list:
            # Parse "host_port:container_port" or "ip:host_port:container_port" or "port"
            first_port = str(ports_list[0]).split("/")[0]
            parts = first_port.split(":")
            candidate_port = parts[-2] if len(parts) >= 2 else parts[0]
            if candidate_port.isdigit():
                port = int(candidate_port)
        by_kind[kind] = _ComposeServiceInfo(
            name=svc_name,
            kind=kind,
            image=image,
            port=port,
            env=dict(data.get("env", {})),  # type: ignore[arg-type]
            command=list(data.get("command", [])),  # type: ignore[arg-type]
        )
    return by_kind


def _parse_jdbc_or_uri(raw_url: str, default_port: int) -> Tuple[str, int, Optional[str]]:
    cleaned = raw_url.strip()
    if cleaned.startswith("jdbc:"):
        cleaned = cleaned[len("jdbc:") :]
    parsed = urlparse(cleaned)
    host = parsed.hostname or "localhost"
    port = parsed.port or default_port
    db_name = parsed.path.lstrip("/") if parsed.path else None
    return host, port, (db_name or None)


def _extract_host_port_from_logs(raw_logs: str, default_port: int) -> Tuple[str, int]:
    for match in _HOST_PORT_RE.finditer(raw_logs or ""):
        host = match.group(1)
        port = int(match.group(2))
        if port == default_port:
            return host, port
    return "localhost", default_port


def _mysql_spec(
    host: str = "localhost",
    port: int = 3306,
    database: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    image: Optional[str] = None,
) -> ServiceRequirement:
    user = username or "root"
    pw = "" if password is None else password

    env: Dict[str, str] = {}
    if database:
        env["MYSQL_DATABASE"] = database
    if user == "root":
        if pw:
            env["MYSQL_ROOT_PASSWORD"] = pw
        else:
            env["MYSQL_ALLOW_EMPTY_PASSWORD"] = "yes"
    else:
        if pw:
            env["MYSQL_ROOT_PASSWORD"] = pw
        else:
            env["MYSQL_ALLOW_EMPTY_PASSWORD"] = "yes"
        env["MYSQL_USER"] = user
        env["MYSQL_PASSWORD"] = pw

    cmd = [f"--port={port}"] if port != 3306 else []
    return ServiceRequirement(
        kind="mysql",
        image=image or settings.sandbox_mysql_image,
        host=host,
        port=port,
        database=database,
        username=user,
        password=pw,
        env=env,
        command=cmd,
    )


def _postgres_spec(
    host: str = "localhost",
    port: int = 5432,
    database: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    image: Optional[str] = None,
) -> ServiceRequirement:
    env: Dict[str, str] = {}
    if database:
        env["POSTGRES_DB"] = database
    if username:
        env["POSTGRES_USER"] = username
    if password:
        env["POSTGRES_PASSWORD"] = password
    else:
        env["POSTGRES_HOST_AUTH_METHOD"] = "trust"
    cmd = ["-p", str(port)] if port != 5432 else []
    return ServiceRequirement(
        kind="postgres",
        image=image or settings.sandbox_postgres_image,
        host=host,
        port=port,
        database=database,
        username=username,
        password=password or "",
        env=env,
        command=cmd,
    )


def _redis_spec(
    host: str = "localhost",
    port: int = 6379,
    password: Optional[str] = None,
    image: Optional[str] = None,
) -> ServiceRequirement:
    cmd: List[str] = ["redis-server"]
    if port != 6379:
        cmd += ["--port", str(port)]
    if password:
        cmd += ["--requirepass", password]
    if len(cmd) == 1:
        cmd = []
    return ServiceRequirement(
        kind="redis",
        image=image or settings.sandbox_redis_image,
        host=host,
        port=port,
        password=password,
        command=cmd,
    )


def _mongo_spec(
    host: str = "localhost",
    port: int = 27017,
    database: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    image: Optional[str] = None,
) -> ServiceRequirement:
    env: Dict[str, str] = {}
    if database:
        env["MONGO_INITDB_DATABASE"] = database
    if username and password:
        env["MONGO_INITDB_ROOT_USERNAME"] = username
        env["MONGO_INITDB_ROOT_PASSWORD"] = password
    cmd = ["--port", str(port)] if port != 27017 else []
    return ServiceRequirement(
        kind="mongo",
        image=image or settings.sandbox_mongo_image,
        host=host,
        port=port,
        database=database,
        username=username,
        password=password,
        env=env,
        command=cmd,
    )


def detect_required_services(
    repo_path: Path,
    pom: Optional[PomManifest] = None,
    raw_logs: str = "",
    maven_args: Optional[List[str]] = None,
) -> List[ServiceRequirement]:
    """Dynamically extract required ephemeral Docker backing services from the target repository."""
    repo_path = Path(repo_path)
    if pom is None and (repo_path / "pom.xml").is_file():
        try:
            pom = PomParser(repo_path / "pom.xml").parse()
        except Exception:
            pom = None

    artifacts = {d.artifact_id for d in pom.dependencies} if pom else set()
    has_embedded_db = any(a in artifacts for a in ("h2", "hsqldb", "derby"))
    has_embedded_redis = any("embedded-redis" in a for a in artifacts)
    has_embedded_mongo = any("embed.mongo" in a or "flapdoodle" in a for a in artifacts)

    env_vars = _load_dotenv_files(repo_path)
    compose = _parse_docker_compose(repo_path, env_vars)
    cfg = _load_spring_configs(repo_path, env_vars, maven_args=maven_args)

    ds_url = cfg.get("spring.datasource.url", "")
    ds_driver = cfg.get("spring.datasource.driver-class-name", "")
    ds_user = cfg.get("spring.datasource.username")
    ds_pass = cfg.get("spring.datasource.password")
    lower_logs = raw_logs.lower()
    ds_uses_embedded = any(
        ds_url.lower().startswith(prefix)
        for prefix in ("jdbc:h2:", "jdbc:hsqldb:", "jdbc:derby:")
    )

    services: Dict[str, ServiceRequirement] = {}

    # 1. MySQL / MariaDB
    mysql_compose = compose.get("mysql")
    if (
        "jdbc:mysql:" in ds_url.lower()
        or "jdbc:mariadb:" in ds_url.lower()
        or "mysql" in ds_driver.lower()
        or "mariadb" in ds_driver.lower()
        or (mysql_compose is not None and not ds_uses_embedded)
        or "communications link failure" in lower_logs
        or ":3306" in lower_logs
        or (
            not ds_url
            and not has_embedded_db
            and any(a in artifacts for a in ("mysql-connector-j", "mysql-connector-java", "mariadb-java-client"))
        )
    ):
        if "jdbc:mysql:" in ds_url.lower() or "jdbc:mariadb:" in ds_url.lower():
            host, port, db_name = _parse_jdbc_or_uri(ds_url, 3306)
        elif mysql_compose is not None:
            host = "localhost"
            port = mysql_compose.port
            db_name = mysql_compose.env.get("MYSQL_DATABASE")
        else:
            host, port = _extract_host_port_from_logs(raw_logs, 3306)
            db_name = None

        c_env = mysql_compose.env if mysql_compose else {}
        eff_user = ds_user or c_env.get("MYSQL_USER")
        eff_pass = (
            ds_pass
            if ds_pass is not None
            else (c_env.get("MYSQL_PASSWORD") or c_env.get("MYSQL_ROOT_PASSWORD"))
        )
        services["mysql"] = _mysql_spec(
            host=host,
            port=port,
            database=db_name,
            username=eff_user,
            password=eff_pass,
            image=mysql_compose.image if mysql_compose else None,
        )

    # 2. PostgreSQL
    pg_compose = compose.get("postgres")
    if (
        "jdbc:postgresql:" in ds_url.lower()
        or "postgresql" in ds_driver.lower()
        or (pg_compose is not None and not ds_uses_embedded)
        or ":5432" in lower_logs
        or "psqlexception" in lower_logs
        or (not ds_url and not has_embedded_db and "postgresql" in artifacts)
    ):
        if "jdbc:postgresql:" in ds_url.lower():
            host, port, db_name = _parse_jdbc_or_uri(ds_url, 5432)
        elif pg_compose is not None:
            host = "localhost"
            port = pg_compose.port
            db_name = pg_compose.env.get("POSTGRES_DB")
        else:
            host, port = _extract_host_port_from_logs(raw_logs, 5432)
            db_name = None

        c_env = pg_compose.env if pg_compose else {}
        eff_user = ds_user or c_env.get("POSTGRES_USER")
        eff_pass = ds_pass if ds_pass is not None else c_env.get("POSTGRES_PASSWORD")
        services["postgres"] = _postgres_spec(
            host=host,
            port=port,
            database=db_name,
            username=eff_user,
            password=eff_pass,
            image=pg_compose.image if pg_compose else None,
        )

    # 3. Redis
    redis_compose = compose.get("redis")
    redis_host = cfg.get("spring.data.redis.host") or cfg.get("spring.redis.host")
    redis_port_str = cfg.get("spring.data.redis.port") or cfg.get("spring.redis.port")
    redis_pw = cfg.get("spring.data.redis.password") or cfg.get("spring.redis.password")
    redis_url = cfg.get("spring.data.redis.url") or cfg.get("spring.redis.url")
    if (
        redis_host
        or redis_port_str
        or redis_url
        or redis_compose is not None
        or (
            not has_embedded_redis
            and any(
                a in artifacts
                for a in (
                    "spring-boot-starter-data-redis",
                    "spring-boot-starter-data-redis-reactive",
                    "jedis",
                    "lettuce-core",
                )
            )
        )
        or "redisconnectionfailureexception" in lower_logs
        or ":6379" in lower_logs
    ):
        if redis_url:
            host, port, _ = _parse_jdbc_or_uri(redis_url, 6379)
        elif redis_host or redis_port_str:
            host = redis_host or "localhost"
            try:
                port = int(redis_port_str) if redis_port_str else 6379
            except ValueError:
                port = 6379
        elif redis_compose is not None:
            host, port = "localhost", redis_compose.port
        else:
            host, port = _extract_host_port_from_logs(raw_logs, 6379)

        c_env = redis_compose.env if redis_compose else {}
        eff_pw = redis_pw if redis_pw is not None else c_env.get("REDIS_PASSWORD")
        services["redis"] = _redis_spec(
            host=host,
            port=port,
            password=eff_pw,
            image=redis_compose.image if redis_compose else None,
        )

    # 4. MongoDB
    mongo_compose = compose.get("mongo")
    mongo_uri = cfg.get("spring.data.mongodb.uri", "")
    mongo_host = cfg.get("spring.data.mongodb.host")
    mongo_port_str = cfg.get("spring.data.mongodb.port")
    mongo_db = cfg.get("spring.data.mongodb.database")
    mongo_user = cfg.get("spring.data.mongodb.username")
    mongo_pass = cfg.get("spring.data.mongodb.password")
    if (
        mongo_uri
        or mongo_host
        or mongo_db
        or mongo_compose is not None
        or (
            not has_embedded_mongo
            and any(
                a in artifacts
                for a in (
                    "spring-boot-starter-data-mongodb",
                    "spring-boot-starter-data-mongodb-reactive",
                )
            )
        )
        or "mongosocketopenexception" in lower_logs
        or ":27017" in lower_logs
    ):
        if mongo_uri:
            host, port, db_name = _parse_jdbc_or_uri(mongo_uri, 27017)
        elif mongo_host or mongo_port_str or mongo_db:
            host = mongo_host or "localhost"
            try:
                port = int(mongo_port_str) if mongo_port_str else 27017
            except ValueError:
                port = 27017
            db_name = mongo_db
        elif mongo_compose is not None:
            host = "localhost"
            port = mongo_compose.port
            db_name = mongo_compose.env.get("MONGO_INITDB_DATABASE")
        else:
            host, port = _extract_host_port_from_logs(raw_logs, 27017)
            db_name = None

        c_env = mongo_compose.env if mongo_compose else {}
        services["mongo"] = _mongo_spec(
            host=host,
            port=port,
            database=db_name,
            username=mongo_user or c_env.get("MONGO_INITDB_ROOT_USERNAME"),
            password=mongo_pass or c_env.get("MONGO_INITDB_ROOT_PASSWORD"),
            image=mongo_compose.image if mongo_compose else None,
        )

    return list(services.values())
