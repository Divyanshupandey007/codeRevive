from pathlib import Path
import sys
from typing import List

from tree_sitter_language_pack import get_parser

backend_root = Path(__file__).resolve().parents[2]
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))


from app.models.manifest import JavaClassManifest, JavaFileManifest, JavaMethodManifest

class JavaAstParser:
    def __init__(self,repo_path:str):
        self.repo_path=Path(repo_path)
        self.parser=get_parser("java")

    def find_java_files(self):
        java_files=[]

        src_directories=[
            self.repo_path / "src" / "main" / "java",
            self.repo_path / "src" / "test" / "java"
        ]

        for src_dir in src_directories:
            if src_dir.is_dir():
                java_files.extend(src_dir.rglob("*.java"))

        return java_files

    def get_node_text(self,node,source_code):
        return source_code[node.start_byte:node.end_byte].decode("utf-8",errors="replace")

    def parse_file(self,file_path:Path)->JavaFileManifest:
        source_code=file_path.read_bytes()
        tree=self.parser.parse(source_code)
        root_node=tree.root_node

        result={
            "file":str(file_path),
            "package":None,
            "imports":[],
            "javax_imports":[],
            "classes":[],
            "methods":[],
            "test_count":0
        }

        def traverse(node):
            if node.type=="package_declaration":
                result["package"]=self.get_node_text(node,source_code).removeprefix("package").strip().removesuffix(";").strip()
            elif node.type=="import_declaration":
                import_name=self.get_node_text(node,source_code).removeprefix("import").strip().removeprefix("static").strip().removesuffix(";").strip()
                if import_name.startswith("javax."):
                    result["javax_imports"].append(import_name)
                result["imports"].append(import_name)
            elif node.type in ("class_declaration", "interface_declaration", "annotation_type_declaration", "enum_declaration", "record_declaration"):
                is_interface=node.type=="interface_declaration"
                class_info={
                    "name":None,
                    "extends":[],
                    "implements":[]
                }

                for child in node.children:
                    if child.type=="identifier":
                        class_info["name"]=self.get_node_text(child,source_code)
                    elif child.type=="superclass":
                        class_info["extends"]=[self.get_node_text(type_node,source_code) for type_node in child.named_children]
                    elif child.type in ("super_interfaces","extends_interfaces"):
                        type_nodes=[]
                        for named_child in child.named_children:
                            if named_child.type=="type_list":
                                type_nodes.extend(named_child.named_children)
                            else:
                                type_nodes.append(named_child)

                        extracted_types=[self.get_node_text(type_node,source_code) for type_node in type_nodes]

                        if is_interface:
                            class_info["extends"]=extracted_types
                        else:
                            class_info["implements"]=extracted_types
                result["classes"].append(class_info)
            elif node.type=="method_declaration":
                method_name=None
                for child in node.children:
                    if child.type=="identifier":
                        method_name=self.get_node_text(child,source_code)
                        break

                has_test_annotation=False
                def check_annotation(parent):
                    nonlocal has_test_annotation
                    for child in parent.children:
                        if child.type=="marker_annotation":
                            annotation=self.get_node_text(child,source_code)
                            annotation_name=annotation.split("(",1)[0]

                            if annotation_name in ("@Test","@org.junit.Test","@org.junit.jupiter.api.Test","@RepeatedTest","@ParameterizedTest"):
                                has_test_annotation=True

                        elif child.type=="annotation":
                            annotation=self.get_node_text(child,source_code)
                            annotation_name=annotation.split("(",1)[0]
                            if annotation_name in ("@Test","@org.junit.Test","@org.junit.jupiter.api.Test","@RepeatedTest","@ParameterizedTest"):
                                has_test_annotation=True

                        check_annotation(child)

                has_public_modifier=False
                for child in node.children:
                    if child.type=="modifiers":
                        check_annotation(child)
                        has_public_modifier=any(
                            modifier.type=="public" for modifier in child.children
                        )

                if has_test_annotation:
                    result["test_count"]+=1

                result["methods"].append({
                    "name":method_name,
                    "is_test":has_test_annotation,
                    "is_public":has_public_modifier
                })

            for child in node.children:
                traverse(child)

        traverse(root_node)
        return JavaFileManifest(
            file_path=file_path.relative_to(self.repo_path).as_posix(),
            package_name=result["package"],
            imports=result["imports"],
            javax_imports=result["javax_imports"],
            classes=[
                JavaClassManifest(**class_info)
                for class_info in result["classes"]
            ],
            methods=[
                JavaMethodManifest(**method_info)
                for method_info in result["methods"]
            ],
            test_count=result["test_count"]
        )

    def parse_repo(self)->List[JavaFileManifest]:
        all_results=[]
        java_files=self.find_java_files()
        for file in java_files:
            parsed_data=self.parse_file(file)
            all_results.append(parsed_data)

        return all_results

if __name__ == "__main__":

    print("Starting Java AST Parser...")

    repo_path = (
    Path(__file__).resolve().parents[2]
    / "workspace"
    / "mybatis-spring-boot-jpetstore"
)
    print(f"Repository path: {repo_path}")
    print(f"Repository exists: {repo_path.is_dir()}")

    parser = JavaAstParser(str(repo_path))

    results = parser.parse_repo()

    print("\n========== PARSING RESULTS ==========")

    total_tests = sum(result.test_count for result in results)

    files_with_javax = [
        result for result in results
        if result.javax_imports
    ]

    print(f"Total Java Files: {len(results)}")
    print(f"Total Test Methods: {total_tests}")

    print("\nFiles with javax imports:")

    for result in files_with_javax:
        print(f"\nFile: {result.file_path}")
        print(f"Imports: {result.javax_imports}")
        print(f"Classes: {result.classes}")

    print("\n========== PARSING COMPLETED ==========")