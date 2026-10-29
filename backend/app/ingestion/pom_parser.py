import sys

from lxml import etree as ET
from pathlib import Path
from typing import Optional,Dict,List

backend_root = Path(__file__).resolve().parents[2]
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))

from app.models.manifest import MavenDependency, PomManifest

class PomParser:
    def __init__(self,pom_path:Path):
        self.pom_path=Path(pom_path)
        if not self.pom_path.exists():
            raise FileNotFoundError(f"pom.xml not found at: {self.pom_path}")

        try:
            parser=ET.XMLParser(remove_comments=True,recover=False)
            self.tree=ET.parse(str(self.pom_path),parser=parser)
            self.root=self.tree.getroot()
        except ET.XMLSyntaxError as e:
            raise ValueError(f"Malformed pom.xml at {self.pom_path}: {e}") from e
        
        if self.root.tag.startswith("{"):
            uri=self.root.tag.split("}")[0].strip("{")
            self.ns={"m":uri}
        else:
            self.ns={}

    def _tag(self,name:str):
        return f"m:{name}" if self.ns else name

    def _resolve_property(self,value:Optional[str],properties:Dict[str,str])->Optional[str]:
        if not value:
            return None
        if value.startswith("${") and value.endswith("}"):
            key=value[2:-1].strip()
            if key in properties:
                resolved=properties[key]
                if resolved.startswith("${") and resolved.endswith("}"):
                    return self._resolve_property(resolved,properties)
                return resolved
            return value
        return value

    def _find_text(self,element:ET._Element,path:str)->Optional[str]:
        parts=[self._tag(p) for p in path.split("/")]
        full_path="/".join(parts)
        target=element.find(full_path,namespaces=self.ns if self.ns else None)
        return target.text.strip() if target is not None and target.text else None

    def _get_properties(self)->Dict[str,str]:
        props={}
        props_elem=self.root.find(self._tag("properties"),namespaces=self.ns if self.ns else None)
        if props_elem is not None:
            for child in props_elem:
                tag_name=child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if child.text:
                    props[tag_name]=child.text.strip()
        return props


    def parse(self)->PomManifest:
        properties=self._get_properties()

        spring_boot_version=None
        parent_elem=self.root.find(self._tag("parent"),namespaces=self.ns if self.ns else None)
        if parent_elem is not None:
            group_id=self._find_text(parent_elem,"groupId")
            if group_id=="org.springframework.boot":
                raw_version=self._find_text(parent_elem,"version")
                spring_boot_version=self._resolve_property(raw_version,properties)

        java_version=(
            properties.get("java.version") or
            properties.get("maven.compiler.release") or
            properties.get("maven.compiler.target") or
            properties.get("maven.compiler.source")
        )

        dependencies: List[MavenDependency]=[]
        deps_parent=self.root.find(self._tag("dependencies"),namespaces=self.ns if self.ns else None)
        if deps_parent is not None:
            for dep_elem in deps_parent.findall(self._tag("dependency"),namespaces=self.ns if self.ns else None):
                group_id=self._find_text(dep_elem,"groupId")
                artifactId=self._find_text(dep_elem,"artifactId")
                raw_version=self._find_text(dep_elem,"version")

                if group_id and artifactId:
                    resolved_version=self._resolve_property(raw_version,properties)
                    dependencies.append(MavenDependency(
                        group_id=group_id,
                        artifact_id=artifactId,
                        version=resolved_version
                    ))

        return PomManifest(
            spring_boot_version=spring_boot_version,
            java_version=java_version,
            dependencies=dependencies
        )

if __name__ == "__main__":
    # Test against the cloned jpetstore repository in workspace
    test_pom = (
        Path(__file__).resolve().parents[2]
        / "workspace"
        / "mybatis-spring-boot-jpetstore"
        / "pom.xml"
    )

    if test_pom.exists():
        parser = PomParser(test_pom)
        manifest = parser.parse()

        print("=== POM MANIFEST PARSED SUCCESSFULLY ===")
        print(f"Spring Boot Version : {manifest.spring_boot_version}")
        print(f"Java Version        : {manifest.java_version}")
        print(f"Total Dependencies  : {len(manifest.dependencies)}")
        print("\nDependencies Sample:")
        for dep in manifest.dependencies[:5]:
            print(f" - {dep.group_id}:{dep.artifact_id} -> {dep.version}")
    else:
        print(
            f"Test pom not found at {test_pom}. Run git_manager.py first to clone the repo!"
        )