from dataclasses import dataclass, field
import difflib
from pathlib import Path
import re
from typing import Dict, List, Optional, Protocol

from git import Repo
from lxml import etree as ET

from app.config import settings
from app.ingestion.ast_parser import get_java_parser
from app.ingestion.migration_rules import (
    SHIRO_PACKAGE_MOVES,
    SPRING_6_ASSERT_ONE_ARG_MESSAGES,
    SPRING_6_ASSERT_TWO_ARG_MESSAGES,
    is_jakarta_migratable_import,
)
from app.models.manifest import MigrationPlan
from app.orchestrator.repair_heuristics import detect_jooq_codegen_skip_defines


@dataclass
class OpenRewriteExecutionResult:
    executed: bool
    mode: str  # "maven_plugin" | "deterministic_fallback" | "skipped"
    recipes: List[str] = field(default_factory=list)
    modified_files: List[str] = field(default_factory=list)
    diffs: Dict[str, str] = field(default_factory=dict)
    logs: str = ""
    error: Optional[str] = None


class RecipeCommandExecutor(Protocol):
    def run_maven_rewrite(
        self, repo_path: Path, maven_args: List[str]
    ) -> tuple[int, str]:
        ...


class DeterministicRewritePass:
    """Applies safe, deterministic Spring Boot 2 -> 3 mechanical transformations.

    Used either standalone (when Maven/Docker OpenRewrite plugin is unavailable)
    or alongside OpenRewrite recipes to guarantee allowlisted Jakarta EE and
    POM/Thymeleaf package moves without burning LLM tokens.
    """

    _IMPORT_RE = re.compile(
        r"^(\s*import\s+(?:static\s+)?)(javax\.[A-Za-z0-9_.*]+)(\s*;)",
        re.MULTILINE,
    )
    _THYMELEAF_RE = re.compile(r"\borg\.thymeleaf\.spring5\b")
    _LOCAL_SERVER_PORT_RE = re.compile(
        r"\borg\.springframework\.boot\.web\.server\.LocalServerPort\b"
    )
    _PBKDF2_NO_ARG_RE = re.compile(r"\bnew\s+Pbkdf2PasswordEncoder\s*\(\s*\)")
    _EXCEPTION_HANDLER_EXTENDS_RE = re.compile(
        r"\bextends\s+ResponseEntityExceptionHandler\b"
    )
    _HANDLER_METHOD_SIG_RE = re.compile(
        r"((?:protected|public)\s+ResponseEntity\s*<(?:[^<>]|<[^>]*>)+>\s+handle[A-Za-z0-9_]*\s*\()([^)]*)(\))"
    )
    _HTTP_STATUS_PARAM_RE = re.compile(r"\bHttpStatus\b(?=\s+[A-Za-z_][A-Za-z0-9_]*)")
    _SHIRO_FQCN_RE = re.compile(
        r"\b("
        + "|".join(re.escape(k) for k in sorted(SHIRO_PACKAGE_MOVES, key=len, reverse=True))
        + r")\b"
    )
    _CREATE_SQL_QUERY_RE = re.compile(r"\.createSQLQuery\s*\(")
    _OPENAPI_TAG_RE = re.compile(r"@Tag\s*\(([^)]*)\)")

    @classmethod
    def _normalize_openapi_tag_annotations(cls, source: str) -> str:
        if "io.swagger.v3.oas.annotations.tags." not in source or "@Tag" not in source:
            return source

        def _rewrite_tag(match: re.Match) -> str:
            inner = match.group(1)
            if not inner or not inner.strip():
                return match.group(0)
            has_name = bool(re.search(r"\bname\s*=", inner))

            # @Tag(tags = {"a", "b"}) or @Tag(tags = "a")
            brace_m = re.search(r"\btags\s*=\s*\{([^}]*)\}", inner)
            if brace_m:
                items = [
                    m.group(1)
                    for m in re.finditer(r'"((?:\\.|[^"\\])*)"', brace_m.group(1))
                ]
                if items:
                    replacement = "" if has_name else f'name = "{", ".join(items)}"'
                    new_inner = inner[: brace_m.start()] + replacement + inner[brace_m.end() :]
                    new_inner = re.sub(r"\s*,\s*,", ",", new_inner).strip(" ,")
                    return f"@Tag({new_inner})"

            str_m = re.search(r'\b(?:tags|value)\s*=\s*("(?:\\.|[^"\\])*")', inner)
            if str_m:
                replacement = "" if has_name else f"name = {str_m.group(1)}"
                new_inner = inner[: str_m.start()] + replacement + inner[str_m.end() :]
                new_inner = re.sub(r"\s*,\s*,", ",", new_inner).strip(" ,")
                return f"@Tag({new_inner})"

            bare_m = re.fullmatch(r'\s*("(?:\\.|[^"\\])*")\s*', inner)
            if bare_m and not has_name:
                return f"@Tag(name = {bare_m.group(1)})"

            return match.group(0)

        return cls._OPENAPI_TAG_RE.sub(_rewrite_tag, source)

    @classmethod
    def _rewrite_spring_6_assert_calls(cls, source: str) -> str:
        if "Assert." not in source:
            return source
        if (
            "org.springframework.util.Assert" not in source
            and not re.search(r"\bAssert\s*\.", source)
        ):
            return source

        src_bytes = source.encode("utf-8")
        tree = get_java_parser().parse(src_bytes)
        imports: List[str] = []
        edits: List[tuple[int, bytes]] = []

        def collect_imports(node):
            if node.type == "import_declaration":
                imp_text = (
                    src_bytes[node.start_byte : node.end_byte]
                    .decode("utf-8", errors="replace")
                    .removeprefix("import")
                    .strip()
                    .removeprefix("static")
                    .strip()
                    .removesuffix(";")
                    .strip()
                )
                imports.append(imp_text)
            for child in node.children:
                collect_imports(child)

        collect_imports(tree.root_node)
        has_spring_assert_import = (
            "org.springframework.util.Assert" in imports
            and not any(
                imp.endswith(".Assert") and imp != "org.springframework.util.Assert"
                for imp in imports
            )
        )

        def visit(node):
            if node.type == "method_invocation":
                non_comment = [
                    c
                    for c in node.children
                    if c.type not in ("line_comment", "block_comment")
                ]
                if (
                    len(non_comment) >= 3
                    and non_comment[-2].type == "identifier"
                    and non_comment[-1].type == "argument_list"
                ):
                    target_expr = (
                        src_bytes[non_comment[0].start_byte : non_comment[0].end_byte]
                        .decode("utf-8", errors="replace")
                        .strip()
                    )
                    if target_expr == "org.springframework.util.Assert" or (
                        target_expr == "Assert" and has_spring_assert_import
                    ):
                        method_name = (
                            src_bytes[
                                non_comment[-2].start_byte : non_comment[-2].end_byte
                            ]
                            .decode("utf-8", errors="replace")
                            .strip()
                        )
                        args = [
                            c
                            for c in non_comment[-1].named_children
                            if c.type not in ("line_comment", "block_comment")
                        ]
                        msg: Optional[str] = None
                        if (
                            method_name in SPRING_6_ASSERT_ONE_ARG_MESSAGES
                            and len(args) == 1
                        ):
                            msg = SPRING_6_ASSERT_ONE_ARG_MESSAGES[method_name]
                        elif (
                            method_name in SPRING_6_ASSERT_TWO_ARG_MESSAGES
                            and len(args) == 2
                        ):
                            msg = SPRING_6_ASSERT_TWO_ARG_MESSAGES[method_name]
                        if msg is not None:
                            edits.append(
                                (args[-1].end_byte, f', "{msg}"'.encode("utf-8"))
                            )
            for child in node.children:
                visit(child)

        visit(tree.root_node)
        if not edits:
            return source

        buf = bytearray(src_bytes)
        for pos, insertion in sorted(edits, key=lambda item: item[0], reverse=True):
            buf[pos:pos] = insertion
        return buf.decode("utf-8")

    @classmethod
    def transform_java_source(cls, source: str) -> str:
        def _replace_import(match: re.Match) -> str:
            prefix, imp, suffix = match.group(1), match.group(2), match.group(3)
            if is_jakarta_migratable_import(imp):
                return f"{prefix}jakarta.{imp[len('javax.'):]}{suffix}"
            return match.group(0)

        updated = cls._IMPORT_RE.sub(_replace_import, source)
        updated = cls._THYMELEAF_RE.sub("org.thymeleaf.spring6", updated)
        updated = cls._LOCAL_SERVER_PORT_RE.sub(
            "org.springframework.boot.test.web.server.LocalServerPort", updated
        )
        if "Pbkdf2PasswordEncoder" in updated:
            updated = cls._PBKDF2_NO_ARG_RE.sub(
                "Pbkdf2PasswordEncoder.defaultsForSpringSecurity_v5_5()", updated
            )
        if cls._EXCEPTION_HANDLER_EXTENDS_RE.search(updated):
            sig_changed = False

            def _upgrade_handler_sig(match: re.Match) -> str:
                nonlocal sig_changed
                prefix, params, suffix = match.group(1), match.group(2), match.group(3)
                new_params, count = cls._HTTP_STATUS_PARAM_RE.subn(
                    "HttpStatusCode", params
                )
                if count > 0:
                    sig_changed = True
                return f"{prefix}{new_params}{suffix}"

            updated = cls._HANDLER_METHOD_SIG_RE.sub(_upgrade_handler_sig, updated)
            if sig_changed and "org.springframework.http.HttpStatusCode" not in updated:
                if "import org.springframework.http.HttpStatus;" in updated:
                    updated = updated.replace(
                        "import org.springframework.http.HttpStatus;",
                        "import org.springframework.http.HttpStatus;\nimport org.springframework.http.HttpStatusCode;",
                        1,
                    )
                else:
                    pkg_match = re.search(r"^(package\s+[^;]+;\s*\n)", updated)
                    if pkg_match:
                        updated = (
                            updated[: pkg_match.end()]
                            + "import org.springframework.http.HttpStatusCode;\n"
                            + updated[pkg_match.end() :]
                        )
                    else:
                        updated = "import org.springframework.http.HttpStatusCode;\n" + updated
        if "org.hibernate.SQLQuery" in updated:
            updated = re.sub(
                r"^(\s*import\s+)org\.hibernate\.SQLQuery(\s*;)",
                r"\1org.hibernate.query.NativeQuery\2",
                updated,
                flags=re.MULTILINE,
            )
            updated = re.sub(r"\bSQLQuery\b", "NativeQuery", updated)
            updated = cls._CREATE_SQL_QUERY_RE.sub(".createNativeQuery(", updated)
        elif cls._CREATE_SQL_QUERY_RE.search(updated):
            updated = cls._CREATE_SQL_QUERY_RE.sub(".createNativeQuery(", updated)
        if "org.apache.shiro" in updated:
            updated = cls._SHIRO_FQCN_RE.sub(
                lambda m: SHIRO_PACKAGE_MOVES[m.group(1)], updated
            )
            wildcard_matches = list(
                re.finditer(
                    r"^(\s*import\s+org\.apache\.shiro(?:\.[A-Za-z0-9_]+)*\.\*\s*;)",
                    updated,
                    flags=re.MULTILINE,
                )
            )
            if wildcard_matches:
                code_without_imports = re.sub(
                    r"^\s*(?:package|import)\s+[^;]+;\s*$",
                    "",
                    updated,
                    flags=re.MULTILINE,
                )
                for wm in reversed(wildcard_matches):
                    wildcard_stmt = wm.group(1)
                    pkg_prefix = re.sub(
                        r"^\s*import\s+|\.\*\s*;$", "", wildcard_stmt
                    )
                    additions: List[str] = []
                    for old_fqcn, new_fqcn in SHIRO_PACKAGE_MOVES.items():
                        old_pkg, simple = old_fqcn.rsplit(".", 1)
                        if old_pkg != pkg_prefix:
                            continue
                        if not re.search(rf"\b{re.escape(simple)}\b", code_without_imports):
                            continue
                        if re.search(
                            rf"^\s*import\s+[A-Za-z0-9_.]+\.{re.escape(simple)}\s*;",
                            updated,
                            flags=re.MULTILINE,
                        ):
                            continue
                        additions.append(f"import {new_fqcn};")
                    if additions:
                        insert_text = "\n" + "\n".join(sorted(set(additions)))
                        updated = (
                            updated[: wm.end()] + insert_text + updated[wm.end() :]
                        )
        updated = cls._rewrite_spring_6_assert_calls(updated)
        updated = cls._normalize_openapi_tag_annotations(updated)
        return updated

    @staticmethod
    def transform_pom_xml(pom_text: str, plan: MigrationPlan) -> str:
        parser = ET.XMLParser(remove_comments=False, recover=False)
        root = ET.fromstring(pom_text.encode("utf-8"), parser=parser)
        ns_uri = root.tag.split("}")[0].strip("{") if root.tag.startswith("{") else None
        ns = {"m": ns_uri} if ns_uri else None

        def tag(name: str) -> str:
            return f"{{{ns_uri}}}{name}" if ns_uri else name

        def qname(name: str) -> str:
            return f"m:{name}" if ns_uri else name

        # 1. Parent Spring Boot version
        parent = root.find(qname("parent"), namespaces=ns)
        if parent is not None:
            pg = parent.find(qname("groupId"), namespaces=ns)
            pv = parent.find(qname("version"), namespaces=ns)
            if pg is not None and pg.text and pg.text.strip() == "org.springframework.boot" and pv is not None:
                pv.text = plan.target_spring_boot

        # 2. Properties updates & removals
        props = root.find(qname("properties"), namespaces=ns)
        if props is not None:
            for prop_name in plan.properties_to_remove:
                elem = props.find(qname(prop_name), namespaces=ns)
                if elem is not None:
                    props.remove(elem)
            for prop_name, target_val in plan.properties_to_update.items():
                elem = props.find(qname(prop_name), namespaces=ns)
                if elem is not None:
                    elem.text = target_val
                else:
                    new_elem = ET.SubElement(props, tag(prop_name))
                    new_elem.text = target_val

        # 3. Dependency artifact/version actions
        def _set_classifier(dep_elem: ET._Element, classifier: str) -> None:
            cls_el = dep_elem.find(qname("classifier"), namespaces=ns)
            if cls_el is None:
                cls_el = ET.SubElement(dep_elem, tag("classifier"))
            cls_el.text = classifier

        def _apply_exclusions(
            dep_elem: ET._Element, exclusions: list[tuple[str, str]]
        ) -> None:
            if not exclusions:
                return
            excls_el = dep_elem.find(qname("exclusions"), namespaces=ns)
            if excls_el is None:
                excls_el = ET.SubElement(dep_elem, tag("exclusions"))
            existing_excls = set()
            for ex_el in excls_el.findall(qname("exclusion"), namespaces=ns):
                eg = ex_el.find(qname("groupId"), namespaces=ns)
                ea = ex_el.find(qname("artifactId"), namespaces=ns)
                if eg is not None and ea is not None and eg.text and ea.text:
                    existing_excls.add((eg.text.strip(), ea.text.strip()))
            for eg_val, ea_val in exclusions:
                if (eg_val, ea_val) in existing_excls:
                    continue
                ex_el = ET.SubElement(excls_el, tag("exclusion"))
                ET.SubElement(ex_el, tag("groupId")).text = eg_val
                ET.SubElement(ex_el, tag("artifactId")).text = ea_val
                existing_excls.add((eg_val, ea_val))

        deps = root.find(qname("dependencies"), namespaces=ns)
        if deps is not None:
            action_map = {
                (a.group_id, a.artifact_id): a for a in plan.dependencies
            }
            existing_dep_coords = set()
            for existing_dep in deps.findall(qname("dependency"), namespaces=ns):
                eg = existing_dep.find(qname("groupId"), namespaces=ns)
                ea = existing_dep.find(qname("artifactId"), namespaces=ns)
                if eg is not None and ea is not None and eg.text and ea.text:
                    existing_dep_coords.add((eg.text.strip(), ea.text.strip()))

            for dep in list(deps.findall(qname("dependency"), namespaces=ns)):
                gid_el = dep.find(qname("groupId"), namespaces=ns)
                aid_el = dep.find(qname("artifactId"), namespaces=ns)
                ver_el = dep.find(qname("version"), namespaces=ns)
                if gid_el is None or aid_el is None or not gid_el.text or not aid_el.text:
                    continue
                action = action_map.get((gid_el.text.strip(), aid_el.text.strip()))
                if not action:
                    continue
                if action.target_strategy == "remove_artifact":
                    deps.remove(dep)
                elif action.target_strategy == "use_boot_bom":
                    if action.remove_version and ver_el is not None:
                        dep.remove(ver_el)
                elif action.target_strategy == "upgrade_artifact" and action.target_artifact_id:
                    aid_el.text = action.target_artifact_id
                    if action.target_group_id:
                        gid_el.text = action.target_group_id
                    existing_dep_coords.add((gid_el.text.strip(), aid_el.text.strip()))
                    if action.target_version:
                        if ver_el is not None and ver_el.text and ver_el.text.strip().startswith("${"):
                            prop_ref = ver_el.text.strip()[2:-1].strip()
                            prop_el = props.find(qname(prop_ref), namespaces=ns) if props is not None else None
                            if prop_el is not None:
                                prop_el.text = action.target_version
                            else:
                                ver_el.text = action.target_version
                        else:
                            if ver_el is None:
                                ver_el = ET.SubElement(dep, tag("version"))
                            ver_el.text = action.target_version
                    elif action.remove_version and ver_el is not None:
                        dep.remove(ver_el)
                    if action.target_classifier:
                        _set_classifier(dep, action.target_classifier)
                    if action.exclusions:
                        _apply_exclusions(dep, action.exclusions)
                    if action.additional_artifacts:
                        insert_idx = list(deps).index(dep) + 1
                        for extra in action.additional_artifacts:
                            coord = (extra.group_id, extra.artifact_id)
                            if coord in existing_dep_coords:
                                continue
                            new_dep = ET.Element(tag("dependency"))
                            ET.SubElement(new_dep, tag("groupId")).text = extra.group_id
                            ET.SubElement(new_dep, tag("artifactId")).text = extra.artifact_id
                            if extra.version:
                                ET.SubElement(new_dep, tag("version")).text = extra.version
                            if extra.classifier:
                                ET.SubElement(new_dep, tag("classifier")).text = extra.classifier
                            if extra.exclusions:
                                _apply_exclusions(new_dep, extra.exclusions)
                            deps.insert(insert_idx, new_dep)
                            insert_idx += 1
                            existing_dep_coords.add(coord)
                elif action.target_strategy == "upgrade_version" and action.target_version:
                    if ver_el is not None and ver_el.text and ver_el.text.strip().startswith("${"):
                        prop_ref = ver_el.text.strip()[2:-1].strip()
                        if props is not None:
                            prop_el = props.find(qname(prop_ref), namespaces=ns)
                            if prop_el is not None:
                                prop_el.text = action.target_version
                    else:
                        if ver_el is None:
                            ver_el = ET.SubElement(dep, tag("version"))
                        ver_el.text = action.target_version
                    if action.target_classifier:
                        _set_classifier(dep, action.target_classifier)
                    if action.exclusions:
                        _apply_exclusions(dep, action.exclusions)

        # 4. Remove unreachable Spring milestone/snapshot repositories
        if plan.repositories_to_remove:
            for container_name, item_name in (
                ("repositories", "repository"),
                ("pluginRepositories", "pluginRepository"),
            ):
                container = root.find(qname(container_name), namespaces=ns)
                if container is not None:
                    for item in list(container.findall(qname(item_name), namespaces=ns)):
                        url_el = item.find(qname("url"), namespaces=ns)
                        if url_el is not None and url_el.text and url_el.text.strip() in plan.repositories_to_remove:
                            container.remove(item)
                    if len(container) == 0:
                        root.remove(container)

        ET.indent(root, space="    ")
        return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8") + "\n"

    @staticmethod
    def transform_config_resource(
        rel_path: str, content: str, upgrade_mysql_driver: bool = False
    ) -> str:
        """Apply safe Spring Boot 3 config cleanups to application properties/YAML files."""
        updated = content
        if upgrade_mysql_driver and "com.mysql.jdbc.Driver" in updated:
            updated = updated.replace("com.mysql.jdbc.Driver", "com.mysql.cj.jdbc.Driver")

        suffix = Path(rel_path).suffix.lower()
        if suffix == ".properties":
            updated = re.sub(
                r"(?m)^(\s*)spring\.redis\.",
                r"\1spring.data.redis.",
                updated,
            )
            return updated

        if suffix in (".yml", ".yaml"):
            lines = updated.splitlines(keepends=True)
            # Only migrate a direct `spring: -> redis:` block when no sibling `data:` key exists
            spring_indent: Optional[int] = None
            has_sibling_data = False
            redis_start: Optional[int] = None
            redis_indent: Optional[int] = None
            redis_end: Optional[int] = None

            for idx, raw_line in enumerate(lines):
                stripped = raw_line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped in ("---", "..."):
                    if redis_start is not None and redis_end is None:
                        redis_end = idx
                    break
                indent = len(raw_line) - len(raw_line.lstrip(" "))
                if spring_indent is None:
                    if re.match(r"^spring\s*:\s*(?:#.*)?$", stripped):
                        spring_indent = indent
                    continue
                if indent <= spring_indent:
                    if redis_start is not None and redis_end is None:
                        redis_end = idx
                    break
                if redis_start is not None and redis_end is None and redis_indent is not None:
                    if indent <= redis_indent:
                        redis_end = idx
                if redis_indent is None or indent == redis_indent:
                    if re.match(r"^data\s*:\s*(?:#.*)?$", stripped):
                        has_sibling_data = True
                    elif redis_start is None and re.match(r"^redis\s*:\s*(?:#.*)?$", stripped):
                        redis_start = idx
                        redis_indent = indent
                elif indent < (redis_indent or 0):
                    if re.match(r"^data\s*:\s*(?:#.*)?$", stripped):
                        has_sibling_data = True

            if redis_start is not None and redis_indent is not None and not has_sibling_data:
                if redis_end is None:
                    redis_end = len(lines)
                pad = " " * redis_indent
                newline = "\r\n" if lines[redis_start].endswith("\r\n") else "\n"
                nested: List[str] = [f"{pad}data:{newline}"]
                for line in lines[redis_start:redis_end]:
                    if line.strip():
                        nested.append("  " + line)
                    else:
                        nested.append(line)
                lines = lines[:redis_start] + nested + lines[redis_end:]
                updated = "".join(lines)

        return updated

    @classmethod
    def apply_to_repo(cls, repo_path: Path, plan: MigrationPlan) -> OpenRewriteExecutionResult:
        modified_files: List[str] = []
        diffs: Dict[str, str] = {}

        # Transform pom.xml
        pom_file = repo_path / "pom.xml"
        if pom_file.is_file():
            original_pom = pom_file.read_text(encoding="utf-8")
            updated_pom = cls.transform_pom_xml(original_pom, plan)
            if updated_pom != original_pom:
                pom_file.write_text(updated_pom, encoding="utf-8")
                modified_files.append("pom.xml")
                diffs["pom.xml"] = "".join(
                    difflib.unified_diff(
                        original_pom.splitlines(keepends=True),
                        updated_pom.splitlines(keepends=True),
                        fromfile="a/pom.xml",
                        tofile="b/pom.xml",
                    )
                )

        # Transform Java sources deterministically
        for src_root in (repo_path / "src" / "main" / "java", repo_path / "src" / "test" / "java"):
            if not src_root.is_dir():
                continue
            for java_file in sorted(src_root.rglob("*.java")):
                rel = java_file.relative_to(repo_path).as_posix()
                original = java_file.read_text(encoding="utf-8")
                updated = cls.transform_java_source(original)
                if updated != original:
                    java_file.write_text(updated, encoding="utf-8")
                    modified_files.append(rel)
                    diffs[rel] = "".join(
                        difflib.unified_diff(
                            original.splitlines(keepends=True),
                            updated.splitlines(keepends=True),
                            fromfile=f"a/{rel}",
                            tofile=f"b/{rel}",
                        )
                    )

        upgrade_mysql_driver = any(
            d.group_id == "mysql" and d.artifact_id == "mysql-connector-java"
            for d in plan.dependencies
        ) or any(
            d.group_id == "com.mysql" and d.artifact_id == "mysql-connector-j"
            for d in plan.dependencies
        )
        for res_root in (repo_path / "src" / "main" / "resources", repo_path / "src" / "test" / "resources"):
            if not res_root.is_dir():
                continue
            for cfg_file in sorted(res_root.iterdir()):
                if not cfg_file.is_file():
                    continue
                if not cfg_file.name.startswith("application") or cfg_file.suffix.lower() not in (
                    ".yml",
                    ".yaml",
                    ".properties",
                ):
                    continue
                rel = cfg_file.relative_to(repo_path).as_posix()
                original = cfg_file.read_text(encoding="utf-8", errors="ignore")
                updated = cls.transform_config_resource(
                    rel, original, upgrade_mysql_driver=upgrade_mysql_driver
                )
                if updated != original:
                    cfg_file.write_text(updated, encoding="utf-8")
                    modified_files.append(rel)
                    diffs[rel] = "".join(
                        difflib.unified_diff(
                            original.splitlines(keepends=True),
                            updated.splitlines(keepends=True),
                            fromfile=f"a/{rel}",
                            tofile=f"b/{rel}",
                        )
                    )

        return OpenRewriteExecutionResult(
            executed=True,
            mode="deterministic_fallback",
            recipes=list(plan.openrewrite_recipes),
            modified_files=modified_files,
            diffs=diffs,
            logs=f"Deterministic OpenRewrite-equivalent pass transformed {len(modified_files)} file(s).",
        )


class OpenRewriteRunner:
    """Runs OpenRewrite Maven recipes in the sandbox, with safe deterministic pre-pass support."""

    def __init__(
        self,
        plugin_version: Optional[str] = None,
        spring_recipe_version: Optional[str] = None,
        executor: Optional[RecipeCommandExecutor] = None,
        allow_deterministic_fallback: bool = True,
        mode: Optional[str] = None,
    ):
        self.plugin_version = plugin_version or settings.openrewrite_plugin_version
        self.spring_recipe_version = (
            spring_recipe_version or settings.openrewrite_spring_recipe_version
        )
        self.executor = executor
        self.mode = mode or settings.openrewrite_mode
        if self.mode == "maven_only":
            self.allow_deterministic_fallback = False
        else:
            self.allow_deterministic_fallback = allow_deterministic_fallback

    def build_maven_args(self, recipes: List[str], dry_run: bool = False) -> List[str]:
        goal = "dryRun" if dry_run else "run"
        active = ",".join(recipes)
        coords = f"org.openrewrite.recipe:rewrite-spring:{self.spring_recipe_version}"
        return [
            "-B",
            "-Dmaven.artifact.threads=5",
            f"org.openrewrite.maven:rewrite-maven-plugin:{self.plugin_version}:{goal}",
            f"-Drewrite.recipeArtifactCoordinates={coords}",
            f"-Drewrite.activeRecipes={active}",
            "-Drewrite.pomCacheEnabled=false",
            "-Drewrite.exportDatatables=false",
        ]

    @staticmethod
    def _git_modified_files(repo_path: Path) -> tuple[List[str], Dict[str, str]]:
        try:
            repo = Repo(repo_path)
            changed = sorted({item.a_path for item in repo.index.diff(None)})
            diffs: Dict[str, str] = {}
            for rel in changed:
                diffs[rel] = repo.git.diff("--", rel)
            return changed, diffs
        except Exception:
            return [], {}

    @staticmethod
    def _normalize_pbkdf2_if_legacy_in_head(repo_path: Path, changed_files: List[str]) -> None:
        try:
            repo = Repo(repo_path)
        except Exception:
            return
        for rel in changed_files:
            if not rel.endswith(".java"):
                continue
            target = repo_path / rel
            if not target.is_file():
                continue
            current = target.read_text(encoding="utf-8", errors="ignore")
            if "defaultsForSpringSecurity_v5_8" not in current:
                continue
            try:
                baseline = repo.git.show(f"HEAD:{rel}")
            except Exception:
                continue
            if "new Pbkdf2PasswordEncoder" in baseline or "defaultsForSpringSecurity_v5_5" in baseline:
                updated = current.replace(
                    "defaultsForSpringSecurity_v5_8()",
                    "defaultsForSpringSecurity_v5_5()",
                )
                if updated != current:
                    target.write_text(updated, encoding="utf-8")

    _HC4_INCOMPATIBLE_ARTIFACTS = frozenset(
        {
            ("org.apache.httpcomponents", "httpclient-cache"),
            ("org.apache.httpcomponents", "httpmime"),
            ("org.apache.httpcomponents", "httpasyncclient"),
            ("org.apache.httpcomponents", "fluent-hc"),
            ("org.elasticsearch.client", "elasticsearch-rest-client"),
            ("org.elasticsearch.client", "elasticsearch-rest-high-level-client"),
        }
    )

    @classmethod
    def _pom_requires_httpclient4(cls, pom_text: str) -> bool:
        if not pom_text:
            return False
        try:
            parser = ET.XMLParser(remove_comments=True, recover=True)
            root = ET.fromstring(pom_text.encode("utf-8"), parser=parser)
        except Exception:
            return False
        if root is None:
            return False
        ns_uri = root.tag.split("}")[0].strip("{") if root.tag.startswith("{") else None
        ns = {"m": ns_uri} if ns_uri else None
        q = (lambda n: f"m:{n}") if ns_uri else (lambda n: n)
        for dep in root.findall(f".//{q('dependency')}", namespaces=ns):
            gid = dep.find(q("groupId"), namespaces=ns)
            aid = dep.find(q("artifactId"), namespaces=ns)
            if gid is not None and aid is not None and gid.text and aid.text:
                if (gid.text.strip(), aid.text.strip()) in cls._HC4_INCOMPATIBLE_ARTIFACTS:
                    return True
        return False

    @classmethod
    def _restore_httpclient4_if_incompatible(
        cls,
        repo_path: Path,
        pre_pom_text: Optional[str],
        changed_files: List[str],
    ) -> None:
        if not pre_pom_text or not cls._pom_requires_httpclient4(pre_pom_text):
            return

        pom_file = repo_path / "pom.xml"
        if pom_file.is_file():
            cur_pom = pom_file.read_text(encoding="utf-8", errors="ignore")
            should_restore_pom = (
                "org.apache.httpcomponents.client5" in cur_pom
                and "org.apache.httpcomponents.client5" not in pre_pom_text
            )
            if not should_restore_pom:
                try:
                    cur_root = ET.fromstring(
                        cur_pom.encode("utf-8"),
                        parser=ET.XMLParser(remove_comments=True, recover=True),
                    )
                    if cur_root is not None:
                        ns_uri = (
                            cur_root.tag.split("}")[0].strip("{")
                            if cur_root.tag.startswith("{")
                            else None
                        )
                        ns = {"m": ns_uri} if ns_uri else None
                        q = (lambda n: f"m:{n}") if ns_uri else (lambda n: n)
                        props_el = cur_root.find(q("properties"), namespaces=ns)
                        defined_props = (
                            {el.tag.split("}")[-1] for el in props_el if el.tag}
                            if props_el is not None
                            else set()
                        )
                        for dep in cur_root.findall(f".//{q('dependency')}", namespaces=ns):
                            gid = dep.find(q("groupId"), namespaces=ns)
                            ver = dep.find(q("version"), namespaces=ns)
                            if (
                                gid is not None
                                and gid.text
                                and gid.text.strip() == "org.apache.httpcomponents"
                            ):
                                ver_text = ver.text.strip() if ver is not None and ver.text else ""
                                unresolved_prop = (
                                    ver_text.startswith("${")
                                    and ver_text.endswith("}")
                                    and not ver_text[2:-1].strip().startswith("project.")
                                    and ver_text[2:-1].strip() not in defined_props
                                )
                                if not ver_text or unresolved_prop:
                                    should_restore_pom = True
                                    break
                except Exception:
                    pass
            if should_restore_pom:
                pom_file.write_text(pre_pom_text, encoding="utf-8")

        try:
            repo = Repo(repo_path)
        except Exception:
            return

        for rel in changed_files:
            if not rel.endswith(".java"):
                continue
            target = repo_path / rel
            if not target.is_file():
                continue
            current = target.read_text(encoding="utf-8", errors="ignore")
            if "org.apache.hc." not in current:
                continue
            try:
                baseline = repo.git.show(f"HEAD:{rel}")
            except Exception:
                continue
            if "org.apache.hc." not in baseline and "org.apache.http." in baseline:
                target.write_text(baseline, encoding="utf-8")

    @staticmethod
    def _summarize_failure_logs(logs: str) -> str:
        ignored = (
            "for more information",
            "-> [help",
            "[help 1]",
            "re-run maven",
            "to see the full stack trace",
        )
        preferred: List[str] = []
        fallback: List[str] = []
        for raw_line in logs.splitlines():
            line = raw_line.strip()
            if not line or line in ("[ERROR]", "[FATAL]"):
                continue
            lower = line.lower()
            if any(frag in lower for frag in ignored):
                continue
            if "caused by:" in lower or "exception" in lower or "timed out" in lower:
                if "pluginexecutionexception" in lower or "failed to execute goal" in lower:
                    fallback.append(line)
                else:
                    preferred.append(line)
            elif "[ERROR]" in line:
                fallback.append(line)
        chosen = preferred[-3:] if preferred else fallback[-3:]
        return " | ".join(chosen)[:300] if chosen else ""

    def run(
        self,
        repo_path: Path,
        plan: MigrationPlan,
        dry_run: bool = False,
    ) -> OpenRewriteExecutionResult:
        repo_path = Path(repo_path)
        recipes = plan.openrewrite_recipes

        if self.mode == "deterministic":
            print(" [OPENREWRITE] Running deterministic AST/POM pass (mode=deterministic)")
            return DeterministicRewritePass.apply_to_repo(repo_path, plan)

        maven_error: Optional[str] = None
        if self.executor is not None:
            pom_file = repo_path / "pom.xml"
            pre_pom_text = (
                pom_file.read_text(encoding="utf-8", errors="ignore")
                if pom_file.is_file()
                else None
            )
            args = self.build_maven_args(recipes, dry_run=dry_run)
            for define in detect_jooq_codegen_skip_defines(repo_path, require_existing_sources=False):
                if define not in args:
                    args.append(define)
            timeout_s = getattr(
                self.executor, "openrewrite_timeout_seconds", settings.openrewrite_timeout_seconds
            )
            print(f" [OPENREWRITE] Running Maven OpenRewrite plugin (timeout={timeout_s}s)...")
            try:
                exit_code, logs = self.executor.run_maven_rewrite(repo_path, args)
                if exit_code == 0:
                    # Normalize BOM properties, third-party Boot 3 version floors, HttpClient 4 preservation, and Pbkdf2 v5_5 defaults
                    if self.allow_deterministic_fallback and not dry_run:
                        pre_changed, _ = self._git_modified_files(repo_path)
                        self._normalize_pbkdf2_if_legacy_in_head(repo_path, pre_changed)
                        self._restore_httpclient4_if_incompatible(
                            repo_path, pre_pom_text, pre_changed
                        )
                        DeterministicRewritePass.apply_to_repo(repo_path, plan)
                    changed, diffs = self._git_modified_files(repo_path)
                    return OpenRewriteExecutionResult(
                        executed=True,
                        mode="maven_plugin",
                        recipes=recipes,
                        modified_files=changed,
                        diffs=diffs,
                        logs=logs,
                    )
                if exit_code == 124:
                    maven_error = f"OpenRewrite Maven plugin timed out after {timeout_s}s (exit code 124)"
                    print(
                        f" [OPENREWRITE] Maven plugin timed out after {timeout_s}s (exit code 124); "
                        "falling back to deterministic AST/POM pass "
                        "(pass --openrewrite-mode deterministic to skip Maven plugin download)"
                    )
                else:
                    hint = self._summarize_failure_logs(logs)
                    suffix = f" ({hint})" if hint else ""
                    maven_error = f"OpenRewrite Maven plugin exited with code {exit_code}{suffix}"
                    print(
                        f" [OPENREWRITE] Maven plugin exited with code {exit_code}{suffix}; "
                        "falling back to deterministic AST/POM pass"
                    )
                if not self.allow_deterministic_fallback:
                    return OpenRewriteExecutionResult(
                        executed=False,
                        mode="maven_plugin",
                        recipes=recipes,
                        logs=logs,
                        error=maven_error,
                    )
            except Exception as exc:
                maven_error = str(exc)
                print(
                    f" [OPENREWRITE] Maven plugin error ({exc}); "
                    "falling back to deterministic AST/POM pass"
                )
                if not self.allow_deterministic_fallback:
                    return OpenRewriteExecutionResult(
                        executed=False,
                        mode="maven_plugin",
                        recipes=recipes,
                        error=maven_error,
                    )

        if self.allow_deterministic_fallback:
            fallback_result = DeterministicRewritePass.apply_to_repo(repo_path, plan)
            if maven_error:
                fallback_result.error = f"{maven_error}; deterministic AST/POM fallback applied"
            return fallback_result

        return OpenRewriteExecutionResult(
            executed=False,
            mode="skipped",
            recipes=recipes,
            logs="OpenRewrite executor not configured and deterministic fallback disabled.",
        )
