from collections import defaultdict
from pathlib import Path
import sys
from typing import Dict, List, Optional

backend_root = Path(__file__).resolve().parents[2]
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))

from app.models.manifest import BlastRadiusEntry, DependencyGraphManifest, JavaClassManifest, JavaFileManifest, MigrationTarget
from app.ingestion.migration_rules import SPRING_BOOT_3_RULES

import networkx as nx

class DependencyGraphBuilder:
    def __init__(self):
        self.graph=nx.DiGraph()
        self._fqcn_to_node:Dict[str,str]={}
        self._file_path_to_node:Dict[str,str]={}

    @staticmethod
    def _primary_class_name(manifest:JavaFileManifest)->Optional[str]:
        if not manifest.classes:
            return None

        file_stem=Path(manifest.file_path).stem
        for class_info in manifest.classes:
            if class_info.name==file_stem:
                return class_info.name

        return manifest.classes[0].name

    def _node_id(self,manifest:JavaFileManifest)->str:
        class_name=self._primary_class_name(manifest)
        if class_name:
            if manifest.package_name:
                return f"{manifest.package_name}.{class_name}"
            return class_name
        return manifest.file_path

    @staticmethod
    def _is_test_file(file_path:str)->bool:
        return "src/test/java" in Path(file_path).as_posix()

    def _add_nodes(self,manifests:List[JavaFileManifest]):
        seen_node_ids=set()

        for manifest in manifests:
            node_id=self._node_id(manifest)

            if node_id in seen_node_ids:
                node_id=f"{node_id} ({manifest.file_path})"
            seen_node_ids.add(node_id)

            # Run every migration-rule predicate against the full manifest
            matched_rules=[rule.id for rule in SPRING_BOOT_3_RULES if rule.predicate(manifest)]

            self.graph.add_node(
                node_id,
                file_path=manifest.file_path,
                package_name=manifest.package_name,
                has_javax=bool(manifest.javax_imports),
                is_test=self._is_test_file(manifest.file_path),
                matched_rules=matched_rules
            )
            self._file_path_to_node[manifest.file_path]=node_id

            for class_info in manifest.classes:
                if not class_info.name:
                    continue
                fqcn=(
                    f"{manifest.package_name}.{class_info.name}"
                    if manifest.package_name else class_info.name
                )

                self._fqcn_to_node.setdefault(fqcn,node_id)

    def _add_edges(self,manifests:List[JavaFileManifest])->int:
        unresolved=0
        package_to_nodes:Dict[str,List[str]]=defaultdict(list)
        for node_id,data in self.graph.nodes(data=True):
            if data.get("package_name"):
                package_to_nodes[data["package_name"]].append(node_id)

        for manifest in manifests:
            source_node=self._file_path_to_node[manifest.file_path]
            for import_name in manifest.imports:
                if import_name.endswith(".*"):
                    targets=package_to_nodes.get(import_name[:-2],[])
                    if not targets:
                        unresolved+=1
                        continue
                    for target_node in targets:
                        if target_node!=source_node:
                            self.graph.add_edge(source_node,target_node)
                    continue

                target_node=self._fqcn_to_node.get(import_name)
                if target_node is None:
                    unresolved+=1
                    continue
                if target_node!=source_node:
                    self.graph.add_edge(source_node,target_node)

        return unresolved

    def _migration_depths(self)->Dict[str,int]:
        if self.graph.number_of_nodes()==0:
            return {}

        condensed=nx.condensation(self.graph)
        reversed_condensed=condensed.reverse(copy=True)

        depth_by_scc:Dict[int,int]={}
        for depth,generation in enumerate(nx.topological_generations(reversed_condensed)):
            for scc_id in generation:
                depth_by_scc[scc_id]=depth

        mapping=condensed.graph["mapping"]
        return {node:depth_by_scc[scc_id] for node,scc_id in mapping.items()}

    def _blast_radius(self,min_affected:int=2,percentile:float=0.85,limit:int=25)->List[BlastRadiusEntry]:
        affected_by_node:Dict[str,List[str]]={}
        for node in self.graph.nodes:
            ancestors=nx.ancestors(self.graph,node)
            if ancestors:
                affected_by_node[node]=sorted(ancestors)

        if not affected_by_node:
            return []

        counts=sorted(len(v) for v in affected_by_node.values())
        idx=min(len(counts)-1,int(len(counts)*percentile))
        threshold=max(min_affected,counts[idx])

        entries=[
            BlastRadiusEntry(
                identifier=node,
                file_path=self.graph.nodes[node]["file_path"],
                in_degree=self.graph.in_degree(node),
                affected_count=len(affected),
                affected_identifiers=affected
            )
            for node, affected in affected_by_node.items()
            if len(affected)>=threshold
        ]

        entries.sort(key=lambda e:e.affected_count,reverse=True)
        return entries[:limit]

    def get_blast_radius(self,identifier:str)->List[str]:
        if identifier not in self.graph:
            raise KeyError(f"Unknown node: {identifier}")
        return sorted(nx.ancestors(self.graph,identifier))

    def get_dependencies(self,identifier:str)->List[str]:
        if identifier not in self.graph:
            raise KeyError(f"Unknown node: {identifier}")
        return sorted(nx.descendants(self.graph,identifier))

    def build_graph(self,manifests:List[JavaFileManifest])->DependencyGraphManifest:
        self.graph=nx.DiGraph()
        self._fqcn_to_node={}
        self._file_path_to_node={}

        self._add_nodes(manifests)
        unresolved_count=self._add_edges(manifests)

        depths=self._migration_depths()

        migration_order=[]
        for node,data in self.graph.nodes(data=True):
            out_degree=self.graph.out_degree(node)
            migration_order.append(
                MigrationTarget(
                    identifier=node,
                    file_path=data["file_path"],
                    package_name=data.get("package_name"),
                    migration_depth=depths.get(node,0),
                    in_degree=self.graph.in_degree(node),
                    out_degree=out_degree,
                    is_leaf=out_degree==0,
                    is_test=data["is_test"],
                    has_javax=data["has_javax"],
                    matched_rules=data.get("matched_rules",[])
                )
            )

        migration_order.sort(key=lambda t: (t.migration_depth,-t.in_degree,t.file_path))
        return DependencyGraphManifest(
            total_nodes=self.graph.number_of_nodes(),
            total_edges=self.graph.number_of_edges(),
            unresolved_import_count=unresolved_count,
            migration_order=migration_order,
            high_blast_radius=self._blast_radius()
        )

if __name__ == "__main__":
    from app.ingestion.ast_parser import JavaAstParser
 
    repo_path = (
        Path(__file__).resolve().parents[2]
        / "workspace"
        / "mybatis-spring-boot-jpetstore"
    )
    print(f"Repository path: {repo_path}")
    print(f"Repository exists: {repo_path.is_dir()}")
 
    if not repo_path.is_dir():
        print("Run git_manager.py first to clone the repo!")
    else:
        manifests = JavaAstParser(str(repo_path)).parse_repo()
 
        graph_manifest = DependencyGraphBuilder().build_graph(manifests)
 
        print("\n========== DEPENDENCY GRAPH ==========")
        print(f"Total nodes: {graph_manifest.total_nodes}")
        print(f"Total edges: {graph_manifest.total_edges}")
        print(f"Unresolved (external) imports: {graph_manifest.unresolved_import_count}")
 
        print("\n--- Migration order (first 15) ---")
        for target in graph_manifest.migration_order[:15]:
            print(
                f"depth={target.migration_depth:<3} "
                f"in={target.in_degree:<3} out={target.out_degree:<3} "
                f"leaf={str(target.is_leaf):<5} {target.identifier}"
            )
 
        print("\n--- High blast-radius files ---")
        for entry in graph_manifest.high_blast_radius:
            print(f"{entry.identifier}: affects {entry.affected_count} files -> {entry.affected_identifiers}")
 
