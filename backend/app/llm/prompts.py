from typing import List, Optional

from app.models.execution import CompilerError, TestFailure

CODER_AGENT_SYSTEM_INSTRUCTION=(
    "You are an expert Spring Boot 3 migration engineer. You modernize legacy "
    "Java/Spring codebases (Spring Boot 2.x -> 3.x, Java 8/11 -> 17, "
    "javax.* -> jakarta.*) one file at a time.\n\n"
    "Rules you must never break:\n"
    "1. Never delete or weaken existing business logic. Only change what the "
    "migration rule below explicitly asks for.\n"
    "2. Never remove an @Test annotation, a test method, or test assertions.\n"
    "3. Only migrate the Java packages/dependencies and repair diagnostics specified "
    "in the task - leave every other import, annotation, and line of code untouched.\n"
    "4. Return the ENTIRE updated file content, not a diff, a snippet, or an "
    "excerpt - the caller writes new_content directly to disk as the whole file.\n"
    "5. Never wrap new_content in markdown code fences (```java, ```) - it must "
    "be raw source code, nothing else.\n"
    "6. Preserve multi-line Java formatting with real newline characters (\\n) "
    "between package declarations, imports, annotations, fields, and methods. "
    "Never collapse a file onto a single line or concatenate annotations with "
    "modifiers (e.g. keep '@Autowired' and 'private' on separate lines).\n"
    "7. Preserve every backslash inside Java string literals and regexes when "
    "encoding JSON (e.g., write '\\\\\\\\s', '\\\\\\\\S', '\\\\\\\\d', '\\\\\\\\/' "
    "in JSON so decoded Java keeps '\\\\s', '\\\\S', '\\\\d', '\\\\/').\n"
    "8. Keep all non-ASCII / Unicode characters (such as Chinese, Japanese, or "
    "accented text in comments and string literals) directly as raw UTF-8 characters "
    "inside JSON strings - NEVER encode them as \\\\uXXXX escape sequences."
)

def _format_diagnostics(compiler_errors:List[CompilerError],failed_tests:List[TestFailure])->str:
    lines=[]
    for error in compiler_errors:
        location=f"{error.file_path}:{error.line_number}:{error.column}" if error.file_path else "(unknown location)"
        lines.append(f"[COMPILER ERROR] {location} - {error.message}")
    for failure in failed_tests:
        lines.append(f"[TEST FAILURE] {failure.test_class}.{failure.test_method} - {failure.failure_message}")

    return "\n".join(lines)

def _format_error_line_snippets(
        source_code:str,compiler_errors:List[CompilerError],max_snippets:int=8
)->str:
    lines=source_code.splitlines()
    if not lines or not compiler_errors:
        return ""
    snippets:List[str]=[]
    seen_lines=set()
    for error in compiler_errors:
        if error.line_number is None or error.line_number<1 or error.line_number>len(lines):
            continue
        if error.line_number in seen_lines:
            continue
        seen_lines.add(error.line_number)
        idx=error.line_number-1
        start=max(0,idx-2)
        end=min(len(lines),idx+3)
        block=[f"--- line {error.line_number}"+(f" (col {error.column})" if error.column else "")+f": {error.message}"]
        for li in range(start,end):
            marker=">" if li==idx else " "
            block.append(f"{marker} {li+1:4d}: {lines[li]}")
            if li==idx and error.column and error.column>0:
                col_offset=min(max(error.column-1,0),len(lines[li]))
                block.append(f"        {' '*col_offset}^")
        snippets.append("\n".join(block))
        if len(snippets)>=max_snippets:
            break
    if not snippets:
        return ""
    return "Exact failing source line(s):\n```text\n"+"\n".join(snippets)+"\n```"


def _build_compiler_repair_hints(compiler_errors:List[CompilerError])->str:
    if not compiler_errors:
        return ""
    notes:List[str]=[]
    messages=[e.message for e in compiler_errors if e.message]
    lower_msgs=[m.lower() for m in messages]

    if any("illegal escape character" in m for m in lower_msgs):
        notes.append(
            "Note on 'illegal escape character': In Java string literals, regex/path "
            "escapes must be written with double backslashes ('\\\\s', '\\\\S', '\\\\/'), "
            "which requires quadruple backslashes ('\\\\\\\\') inside JSON output."
        )

    if any(
        "is ambiguous" in m
        or "already defined by the single-type-import" in m
        for m in lower_msgs
    ):
        notes.append(
            "Note on ambiguous / duplicate class imports:\n"
            "- Java forbids importing two different classes that share the same simple name in one file "
            "(for example two different `SecurityScheme`, `Tag`, `Parameter`, `Query`, or `Predicate` classes).\n"
            "- Remove the conflicting single-type `import` statement (keep at most ONE import for that simple name) "
            "and refer to the other type by its fully-qualified class name everywhere it is used in the file."
        )

    if any(
        (
            "cannot be converted to java.lang.string" in m
            and ("predicate" in m or "criterion" in m or "criteriaquery" in m)
        )
        or (
            ("no suitable method found" in m or "cannot be applied to given types" in m)
            and ("predicate" in m or "pagerequest" in m or "criteriaquery" in m or "jakarta.persistence.criteria" in m)
        )
        or "is not a functional interface" in m
        or "conforms to java.util.list<jakarta.persistence.criteria.predicate>" in m
        for m in lower_msgs
    ):
        notes.append(
            "Note on Criteria / Predicate / DAO overload type mismatch:\n"
            "- `jakarta.persistence.criteria.Predicate` is NOT a functional interface — NEVER pass a lambda expression where a `Predicate` is expected; build predicates using `CriteriaBuilder`.\n"
            "- `Predicate[]`, `CriteriaQuery<T>`, or `jakarta.persistence.criteria.Order` CANNOT be passed to a DAO method whose signature expects `String hql`, `Predicate...`, or `(String orderBy, boolean isAsc, Predicate...)`.\n"
            "- Check the EXACT parameter types and order listed in the compiler error and `Updated local project dependencies` (for example pass `Arrays.asList(...)` for `List<Predicate>`, or `(orderFieldName, isAsc, predicates...)` for an ordered finder).\n"
            "- If you already built a `CriteriaQuery<T> cq`, execute it directly via `getSession().createQuery(cq)` / `entityManager.createQuery(cq)` (e.g. `.uniqueResult()`, `.getResultList()`, or `.setMaxResults(1).uniqueResult()`) instead of passing `cq` into a DAO helper that expects `String` or `Predicate...`."
        )

    if any("method root(" in m and "criteriabuilder" in m for m in lower_msgs):
        notes.append(
            "Note on `CriteriaBuilder.root(...)`: `CriteriaBuilder` and `HibernateCriteriaBuilder` "
            "do NOT have a `.root(Class)` method. Create a `CriteriaQuery<T> cq = cb.createQuery(Entity.class)` "
            "first and obtain the root with `Root<T> root = cq.from(Entity.class)`."
        )

    if any("cannot find symbol" in m for m in lower_msgs):
        notes.append(
            "Note on `cannot find symbol` errors:\n"
            "- Only reference local variables, fields, and methods that are actually declared in this class, "
            "its superclass, or `Updated local project dependencies` — never invent undeclared fields or helper methods.\n"
            "- If a method uses JPA Criteria (`CriteriaBuilder`, `CriteriaQuery`, `Root`), declare them locally "
            "inside that method using the class's existing Hibernate/JPA session or `EntityManager` accessor, "
            "or call existing DAO/repository methods."
        )

    if any(
        ("tags()" in m and "tag" in m)
        or "missing a default value for the element 'name'" in m
        or "annotation type not applicable to this kind of declaration" in m
        or "apiignore" in m
        for m in lower_msgs
    ):
        notes.append(
            "Note on SpringDoc / OpenAPI 3 annotations:\n"
            "- `@io.swagger.v3.oas.annotations.tags.Tag` uses `name = \"...\"` (NEVER `tags = \"...\"` or `value = \"...\"`).\n"
            "- `@io.swagger.v3.oas.annotations.Hidden` is ONLY valid on classes (`TYPE`) and methods (`METHOD`), "
            "NEVER on method parameters! Putting `@Hidden` on a parameter causes `annotation type not applicable to this kind of declaration`. "
            "Replace `@ApiIgnore` on method parameters with `@io.swagger.v3.oas.annotations.Parameter(hidden = true)` (or omit it), "
            "and use `@Hidden` only on classes or methods."
        )

    if any(
        "list<java.lang.object[]> cannot be converted to java.util.list<t>" in m
        or "inference variable r has incompatible equality constraints" in m
        for m in lower_msgs
    ):
        notes.append(
            "Note on Hibernate 6 Query generic return type mismatch:\n"
            "- In Hibernate 6, `session.createQuery(hql)` returns `Query<Object>` (or `List<Object[]>` when selecting columns), "
            "which can clash with `<T>` or `<R>` return inference. Use `session.createQuery(hql, entityClass)` when returning entities, "
            "align `CriteriaQuery<T>` with the declared return type, or cast explicitly with `@SuppressWarnings(\"unchecked\") (List<T>)`."
        )

    if any(
        "org.apache.hc." in m
        or "closeexpiredconnections" in m
        or "stringentity" in m
        or "releaseconnection" in m
        for m in lower_msgs
    ):
        notes.append(
            "Note on Apache HttpClient 5 (if used in this repo):\n"
            "- `HttpClientConnectionManager`: use `org.apache.hc.client5.http.impl.io.PoolingHttpClientConnectionManager` "
            "(or `ConnPoolControl` / `closeExpired()` & `closeIdle(TimeValue)` on pooling manager; `HttpClientConnectionManager` itself no longer exposes `closeExpiredConnections()`).\n"
            "- `HttpGet`/`HttpPost`/`HttpDelete`/`HttpPatch`: `getURI()` -> `getUri()`, `getAllHeaders()` -> `getHeaders()`, `releaseConnection()` -> `reset()` (or try-with-resources).\n"
            "- `StringEntity`: `new StringEntity(body, ContentType.create(\"application/json\", StandardCharsets.UTF_8))` or `new StringEntity(body, StandardCharsets.UTF_8)` (do not pass a `String` charset or call removed `setContentType`/`setContentEncoding`).\n"
            "- Multipart: `org.apache.hc.client5.http.entity.mime.MultipartEntityBuilder` lives in `httpclient5`, not `org.apache.hc.core5.http.io.entity.mime`."
        )

    if any("does not exist" in m and "package " in m for m in lower_msgs):
        notes.append(
            "Note on `package ... does not exist` errors:\n"
            "- If repairing `pom.xml`, add the missing Spring Boot 3 BOM-managed starter or Jakarta EE API "
            "dependency (e.g., `org.springframework.boot:spring-boot-starter-validation` for `jakarta.validation.*`, "
            "omitting `<version>` so the Spring Boot 3 parent BOM manages it).\n"
            "- If repairing a `.java` file where the missing package is an optional nullability annotation "
            "(such as `jakarta.validation.constraints.NotNull` replacing removed `com.sun.istack.NotNull`), "
            "use `org.springframework.lang.NonNull` if `jakarta.validation` is not on the classpath."
        )

    if not notes:
        return ""
    return "\n"+"\n".join(notes)


def build_coder_prompt(
        target_file_path:str,
        source_code:str,
        migration_rule:str,
        compiler_errors:Optional[List[CompilerError]]=None,
        failed_tests:Optional[List[TestFailure]]=None,
        runtime_log_excerpt:Optional[str]=None,
        migration_context:Optional[str]=None,
        dependency_context:Optional[str]=None,
)->str:
    sections=[
        f"Target file: {target_file_path}",
        f"Migration rule: {migration_rule}",
        f"Current source_code:\n```java\n{source_code}\n```"
    ]

    if dependency_context:
        sections.append(
            "Updated local project dependencies (align overrides, implemented methods, "
            "and calls with these exact current signatures):\n"
            f"{dependency_context}"
        )

    if migration_context:
        sections.append(f"Repair context: {migration_context}")

    if compiler_errors or failed_tests:
        diagnostics=_format_diagnostics(compiler_errors or [],failed_tests or [])
        line_snippets=_format_error_line_snippets(source_code,compiler_errors or [])
        repair_hints=_build_compiler_repair_hints(compiler_errors or [])
        snippet_section=f"\n\n{line_snippets}" if line_snippets else ""
        sections.append(
            "This is a REPAIR attempt: the previous patch to this file produced "
            "the diagnostics below when built in the sandbox. You MUST fix all "
            "reported compiler errors and test failures (including moved package "
            "imports, removed transitive dependencies, or updated API signatures) "
            "while preserving existing business logic, public method contracts, "
            "and @Test assertions:\n"
            f"{diagnostics}{snippet_section}{repair_hints}"
        )

    if runtime_log_excerpt:
        sections.append(
            "[SERVER / RUNTIME LOG EXCERPT]\n"
            f"{runtime_log_excerpt}"
        )

    return "\n\n".join(sections)