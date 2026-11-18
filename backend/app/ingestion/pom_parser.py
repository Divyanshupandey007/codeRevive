from pathlib import Path
from typing import Dict, List, Optional

from lxml import etree as ET

from app.models.manifest import MavenDependency, PomManifest


class PomParser:
    def __init__(self, pom_path: Path):
        self.pom_path = Path(pom_path)
        if not self.pom_path.exists():
            raise FileNotFoundError(f"pom.xml not found at: {self.pom_path}")

        try:
            parser = ET.XMLParser(remove_comments=True, recover=False)
            self.tree = ET.parse(str(self.pom_path), parser=parser)
            self.root = self.tree.getroot()
        except ET.XMLSyntaxError as e:
            raise ValueError(f"Malformed pom.xml at {self.pom_path}: {e}") from e

        if self.root.tag.startswith("{"):
            uri = self.root.tag.split("}")[0].strip("{")
            self.ns = {"m": uri}
        else:
            self.ns = {}

    def _tag(self, name: str) -> str:
        return f"m:{name}" if self.ns else name

    def _resolve_property(
        self, value: Optional[str], properties: Dict[str, str], depth: int = 0
    ) -> Optional[str]:
        if not value:
            return None
        if depth > 5:
            return value
        if value.startswith("${") and value.endswith("}"):
            key = value[2:-1].strip()
            if key in properties:
                resolved = properties[key]
                if resolved.startswith("${") and resolved.endswith("}"):
                    return self._resolve_property(resolved, properties, depth + 1)
                return resolved
            return value
        return value

    def _find_text(self, element: ET._Element, path: str) -> Optional[str]:
        parts = [self._tag(p) for p in path.split("/")]
        full_path = "/".join(parts)
        target = element.find(full_path, namespaces=self.ns if self.ns else None)
        return target.text.strip() if target is not None and target.text else None

    def _get_properties(self) -> Dict[str, str]:
        props: Dict[str, str] = {}
        props_elem = self.root.find(
            self._tag("properties"), namespaces=self.ns if self.ns else None
        )
        if props_elem is not None:
            for child in props_elem:
                if not isinstance(child.tag, str):
                    continue
                tag_name = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if child.text:
                    props[tag_name] = child.text.strip()
        return props

    def _get_modules(self) -> List[str]:
        modules: List[str] = []
        modules_elem = self.root.find(
            self._tag("modules"), namespaces=self.ns if self.ns else None
        )
        if modules_elem is not None:
            for mod in modules_elem.findall(
                self._tag("module"), namespaces=self.ns if self.ns else None
            ):
                if mod.text and mod.text.strip():
                    modules.append(mod.text.strip())
        return modules

    def _get_repositories(self) -> List[str]:
        urls: List[str] = []
        for container_tag, item_tag in (
            ("repositories", "repository"),
            ("pluginRepositories", "pluginRepository"),
        ):
            container = self.root.find(
                self._tag(container_tag), namespaces=self.ns if self.ns else None
            )
            if container is not None:
                for repo_elem in container.findall(
                    self._tag(item_tag), namespaces=self.ns if self.ns else None
                ):
                    url = self._find_text(repo_elem, "url")
                    if url and url not in urls:
                        urls.append(url)
        return urls

    def parse(self) -> PomManifest:
        properties = self._get_properties()
        packaging = self._find_text(self.root, "packaging")
        modules = self._get_modules()

        spring_boot_version: Optional[str] = None
        parent_group_id: Optional[str] = None
        parent_artifact_id: Optional[str] = None
        parent_elem = self.root.find(
            self._tag("parent"), namespaces=self.ns if self.ns else None
        )
        if parent_elem is not None:
            parent_group_id = self._find_text(parent_elem, "groupId")
            parent_artifact_id = self._find_text(parent_elem, "artifactId")
            if parent_group_id == "org.springframework.boot":
                raw_version = self._find_text(parent_elem, "version")
                spring_boot_version = self._resolve_property(raw_version, properties)

        java_version = (
            properties.get("java.version")
            or properties.get("maven.compiler.release")
            or properties.get("maven.compiler.target")
            or properties.get("maven.compiler.source")
        )

        dependencies: List[MavenDependency] = []
        deps_parent = self.root.find(
            self._tag("dependencies"), namespaces=self.ns if self.ns else None
        )
        if deps_parent is not None:
            for dep_elem in deps_parent.findall(
                self._tag("dependency"), namespaces=self.ns if self.ns else None
            ):
                group_id = self._find_text(dep_elem, "groupId")
                artifact_id = self._find_text(dep_elem, "artifactId")
                raw_version = self._find_text(dep_elem, "version")
                scope = self._find_text(dep_elem, "scope")

                if group_id and artifact_id:
                    resolved_version = self._resolve_property(raw_version, properties)
                    dependencies.append(
                        MavenDependency(
                            group_id=group_id,
                            artifact_id=artifact_id,
                            version=resolved_version,
                            scope=scope,
                        )
                    )

        return PomManifest(
            spring_boot_version=spring_boot_version,
            parent_group_id=parent_group_id,
            parent_artifact_id=parent_artifact_id,
            java_version=java_version,
            packaging=packaging,
            is_multi_module=bool(modules),
            modules=modules,
            properties=properties,
            repositories=self._get_repositories(),
            dependencies=dependencies,
        )