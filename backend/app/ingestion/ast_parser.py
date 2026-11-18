from pathlib import Path
from typing import List, Optional

from tree_sitter_language_pack import get_parser

from app.ingestion.migration_rules import (
    SPRING_6_ASSERT_ONE_ARG_MESSAGES,
    SPRING_6_ASSERT_TWO_ARG_MESSAGES,
    is_jakarta_migratable_import,
)
from app.models.manifest import JavaClassManifest, JavaFileManifest, JavaMethodManifest

_TEST_ANNOTATIONS = {
    "@Test",
    "@org.junit.Test",
    "@org.junit.jupiter.api.Test",
    "@RepeatedTest",
    "@ParameterizedTest",
}

_JAVA_PARSER = None


def get_java_parser():
    """Return a cached Tree-sitter Java parser instance."""
    global _JAVA_PARSER
    if _JAVA_PARSER is None:
        _JAVA_PARSER = get_parser("java")
    return _JAVA_PARSER


class JavaAstParser:
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path)
        self.parser = get_java_parser()

    def find_java_files(self) -> List[Path]:
        java_files: List[Path] = []
        src_directories = [
            self.repo_path / "src" / "main" / "java",
            self.repo_path / "src" / "test" / "java",
        ]
        for src_dir in src_directories:
            if src_dir.is_dir():
                java_files.extend(sorted(src_dir.rglob("*.java")))
        return java_files

    def get_node_text(self, node, source_code: bytes) -> str:
        return source_code[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    def _extract_annotations_and_modifiers(self, modifiers_node, source_code: bytes):
        annotations: List[str] = []
        modifiers: List[str] = []

        for child in modifiers_node.children:
            if child.type in ("marker_annotation", "annotation"):
                annotations.append(self.get_node_text(child, source_code).strip())
            elif child.is_named or child.type in (
                "public",
                "private",
                "protected",
                "static",
                "final",
                "abstract",
                "synchronized",
                "default",
                "native",
                "strictfp",
            ):
                modifiers.append(self.get_node_text(child, source_code).strip())

        return annotations, modifiers

    def _extract_parameters(self, params_node, source_code: bytes) -> List[str]:
        params: List[str] = []
        for child in params_node.children:
            if child.type in ("formal_parameter", "spread_parameter", "receiver_parameter"):
                type_parts: List[str] = []
                for sub in child.children:
                    if sub.type in ("modifiers", "identifier", "variable_declarator"):
                        continue
                    type_parts.append(self.get_node_text(sub, source_code).strip())
                param_type = " ".join(p for p in type_parts if p).strip()
                if child.type == "spread_parameter" and "..." not in param_type:
                    param_type = f"{param_type}..."
                if param_type:
                    params.append(param_type)
        return params

    def _extract_return_type(self, method_node, source_code: bytes) -> Optional[str]:
        for child in method_node.children:
            if child.type in ("modifiers", "type_parameters"):
                continue
            if child.type == "identifier":
                break
            if child.type in (
                "void_type",
                "integral_type",
                "floating_point_type",
                "boolean_type",
                "type_identifier",
                "generic_type",
                "array_type",
                "scoped_type_identifier",
            ):
                return self.get_node_text(child, source_code).strip()
        return None

    @staticmethod
    def _method_has_nonempty_body(method_node) -> bool:
        for child in method_node.children:
            if child.type == "block":
                return any(
                    sub.type not in ("{", "}", "line_comment", "block_comment")
                    for sub in child.children
                )
        return True

    def parse_file(self, file_path: Path) -> JavaFileManifest:
        source_code = file_path.read_bytes()
        tree = self.parser.parse(source_code)
        root_node = tree.root_node

        package_name: Optional[str] = None
        imports: List[str] = []
        javax_imports: List[str] = []
        jakarta_migratable_imports: List[str] = []
        classes: List[JavaClassManifest] = []
        methods: List[JavaMethodManifest] = []
        test_count = 0
        has_deprecated_pbkdf2_constructor = False
        spring_assert_calls: List[tuple[str, str, int]] = []

        def traverse(node):
            nonlocal package_name, test_count, has_deprecated_pbkdf2_constructor

            if node.type == "package_declaration":
                package_name = (
                    self.get_node_text(node, source_code)
                    .removeprefix("package")
                    .strip()
                    .removesuffix(";")
                    .strip()
                )
            elif node.type == "import_declaration":
                import_name = (
                    self.get_node_text(node, source_code)
                    .removeprefix("import")
                    .strip()
                    .removeprefix("static")
                    .strip()
                    .removesuffix(";")
                    .strip()
                )
                if import_name.startswith("javax."):
                    javax_imports.append(import_name)
                    if is_jakarta_migratable_import(import_name):
                        jakarta_migratable_imports.append(import_name)
                imports.append(import_name)
            elif node.type in (
                "class_declaration",
                "interface_declaration",
                "annotation_type_declaration",
                "enum_declaration",
                "record_declaration",
            ):
                is_interface = node.type == "interface_declaration"
                class_name: Optional[str] = None
                extends: List[str] = []
                implements: List[str] = []

                for child in node.children:
                    if child.type == "identifier":
                        class_name = self.get_node_text(child, source_code)
                    elif child.type == "superclass":
                        extends = [
                            self.get_node_text(type_node, source_code)
                            for type_node in child.named_children
                        ]
                    elif child.type in ("super_interfaces", "extends_interfaces"):
                        type_nodes = []
                        for named_child in child.named_children:
                            if named_child.type == "type_list":
                                type_nodes.extend(named_child.named_children)
                            else:
                                type_nodes.append(named_child)
                        extracted_types = [
                            self.get_node_text(type_node, source_code)
                            for type_node in type_nodes
                        ]
                        if is_interface:
                            extends = extracted_types
                        else:
                            implements = extracted_types

                classes.append(
                    JavaClassManifest(
                        name=class_name,
                        extends=extends,
                        implements=implements,
                    )
                )
            elif node.type == "method_declaration":
                method_name: Optional[str] = None
                parameters: List[str] = []
                annotations: List[str] = []
                modifiers: List[str] = []
                return_type = self._extract_return_type(node, source_code)

                for child in node.children:
                    if child.type == "identifier":
                        method_name = self.get_node_text(child, source_code)
                    elif child.type == "formal_parameters":
                        parameters = self._extract_parameters(child, source_code)
                    elif child.type == "modifiers":
                        annotations, modifiers = self._extract_annotations_and_modifiers(
                            child, source_code
                        )

                has_test_annotation = any(
                    ann.split("(", 1)[0] in _TEST_ANNOTATIONS for ann in annotations
                )
                has_public_modifier = "public" in modifiers
                if has_test_annotation:
                    test_count += 1

                methods.append(
                    JavaMethodManifest(
                        name=method_name,
                        return_type=return_type,
                        parameters=parameters,
                        modifiers=modifiers,
                        annotations=annotations,
                        is_test=has_test_annotation,
                        is_public=has_public_modifier,
                        has_assertions_or_body=self._method_has_nonempty_body(node),
                    )
                )

            elif node.type == "object_creation_expression":
                created_type = ""
                for child in node.children:
                    if child.type in ("type_identifier", "scoped_type_identifier", "generic_type"):
                        created_type = self.get_node_text(child, source_code).strip()
                        break
                base_type = created_type.split("<", 1)[0].rsplit(".", 1)[-1]
                if base_type == "Pbkdf2PasswordEncoder":
                    has_deprecated_pbkdf2_constructor = True

            elif node.type == "method_invocation":
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
                    target_expr = self.get_node_text(non_comment[0], source_code).strip()
                    method_name = self.get_node_text(non_comment[-2], source_code).strip()
                    if target_expr in ("Assert", "org.springframework.util.Assert"):
                        args = [
                            c
                            for c in non_comment[-1].named_children
                            if c.type not in ("line_comment", "block_comment")
                        ]
                        spring_assert_calls.append((target_expr, method_name, len(args)))

            for child in node.children:
                if child.is_named:
                    traverse(child)

        traverse(root_node)

        has_spring_assert_import = (
            "org.springframework.util.Assert" in imports
            and not any(
                imp.endswith(".Assert") and imp != "org.springframework.util.Assert"
                for imp in imports
            )
        )
        has_deprecated_spring_assert = any(
            (target_expr == "org.springframework.util.Assert" or has_spring_assert_import)
            and (
                (method_name in SPRING_6_ASSERT_ONE_ARG_MESSAGES and arg_count == 1)
                or (method_name in SPRING_6_ASSERT_TWO_ARG_MESSAGES and arg_count == 2)
            )
            for target_expr, method_name, arg_count in spring_assert_calls
        )

        return JavaFileManifest(
            file_path=file_path.relative_to(self.repo_path).as_posix(),
            package_name=package_name,
            imports=imports,
            javax_imports=javax_imports,
            jakarta_migratable_imports=jakarta_migratable_imports,
            classes=classes,
            methods=methods,
            test_count=test_count,
            has_deprecated_pbkdf2_constructor=has_deprecated_pbkdf2_constructor,
            has_deprecated_spring_assert=has_deprecated_spring_assert,
        )

    def parse_repo(self) -> List[JavaFileManifest]:
        return [self.parse_file(file) for file in self.find_java_files()]