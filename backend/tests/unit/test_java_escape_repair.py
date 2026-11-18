from app.llm.prompts import CODER_AGENT_SYSTEM_INSTRUCTION, build_coder_prompt
from app.models.execution import CompilerError
from app.orchestrator.workflow import (
    _repair_illegal_java_string_escapes,
    _restore_stripped_java_backslashes,
)


def test_restore_stripped_java_backslashes_restores_regex_even_with_reindentation():
    original = (
        "package com.example;\n"
        "\n"
        "public class HtmlCleaner {\n"
        "\tpublic String clean(String htmlStr) {\n"
        '\t\tString regExScript = "<[\\\\s]*?script[^>]*?>[\\\\s\\\\S]*?<[\\\\s]*?\\\\/[\\\\s]*?script[\\\\s]*?>";\n'
        '\t\treturn htmlStr.replaceAll("<(.*)>(.*)<\\\\/(.*)>|<(.*)\\\\/>", "");\n'
        "\t}\n"
        "}\n"
    )
    # Simulate LLM JSON decoding collapsing \\ -> \ and changing leading tabs to spaces
    patched = (
        "package com.example;\n"
        "\n"
        "public class HtmlCleaner {\n"
        "    public String clean(String htmlStr) {\n"
        '        String regExScript = "<[\\s]*?script[^>]*?>[\\s\\S]*?<[\\s]*?\\/[\\s]*?script[\\s]*?>";\n'
        '        return htmlStr.replaceAll("<(.*)>(.*)<\\/(.*)>|<(.*)\\/>", "");\n'
        "    }\n"
        "}\n"
    )

    restored = _restore_stripped_java_backslashes("HtmlCleaner.java", original, patched)

    assert '<[\\\\s]*?script[^>]*?>[\\\\s\\\\S]*?<[\\\\s]*?\\\\/[\\\\s]*?script[\\\\s]*?>' in restored
    assert '<(.*)>(.*)<\\\\/(.*)>|<(.*)\\\\/>' in restored
    # Leading space indentation from patched output is preserved
    assert '        String regExScript' in restored


def test_restore_stripped_java_backslashes_skips_non_java_and_modified_lines():
    original = 'pattern = "\\\\s+\\\\S+"\nkeep = "\\\\d+"\n'
    patched = 'pattern = "\\s+\\S+"\nkeep = "\\\\d+modified"\n'

    # Non-Java file is untouched
    assert _restore_stripped_java_backslashes("notes.txt", original, patched) == patched

    # Modified line that does not match collapsed original is untouched
    java_orig = 'class A {\n  String a = "\\\\s+";\n  String b = "\\\\d+";\n}\n'
    java_patched = 'class A {\n  String a = "\\s+";\n  String b = "\\w+";\n}\n'
    res = _restore_stripped_java_backslashes("A.java", java_orig, java_patched)
    assert 'String a = "\\\\s+";' in res
    assert 'String b = "\\w+";' in res


def test_repair_illegal_java_string_escapes_handles_target_lines_and_ignores_comments_and_text_blocks():
    java_src = (
        "package com.example;\n"
        "public class Escapes {\n"
        "    // comment with \\S and \\/ stays untouched\n"
        "    /* block comment \\S \\/ */\n"
        '    String ok = "line1\\n\\t\\"\\\\\\u0020";\n'
        '    String broken = "<[\\s]*?>[\\S]*?<\\/>\\d";\n'
        '    String otherLine = "\\S";\n'
        '    String block = """\n'
        "        keep \\S inside text block\n"
        '        """;\n'
        "}\n"
    )
    # Line 6 is `String broken = ...`, line 7 is `String otherLine = ...`
    repaired = _repair_illegal_java_string_escapes(java_src, target_lines={6})
    lines = repaired.splitlines()

    assert lines[2] == "    // comment with \\S and \\/ stays untouched"
    assert lines[3] == "    /* block comment \\S \\/ */"
    assert lines[4] == '    String ok = "line1\\n\\t\\"\\\\\\u0020";'
    # \s is a valid escape check? Wait: \s IS valid in Java 15+ (Text Blocks / JEP 378) or NOT in _VALID_JAVA_STRING_ESCAPES!
    # In _VALID_JAVA_STRING_ESCAPES ("btnfrs\"'\\u01234567"), 's' IS in the set, whereas 'S', '/', 'd' are NOT!
    assert lines[5] == '    String broken = "<[\\s]*?>[\\\\S]*?<\\\\/>\\\\d";'
    # Line 7 was outside target_lines={6}
    assert lines[6] == '    String otherLine = "\\S";'
    assert lines[8] == "        keep \\S inside text block"


def test_prompts_include_backslash_rule_and_illegal_escape_diagnostic_hint():
    assert "\\\\\\\\s" in CODER_AGENT_SYSTEM_INSTRUCTION

    prompt_without = build_coder_prompt(
        target_file_path="Foo.java",
        source_code="class Foo {}",
        migration_rule="migrate",
        compiler_errors=[
            CompilerError(file_path="Foo.java", line_number=1, column=1, message="cannot find symbol")
        ],
    )
    assert "Note on 'illegal escape character'" not in prompt_without

    prompt_with = build_coder_prompt(
        target_file_path="Foo.java",
        source_code="class Foo {}",
        migration_rule="migrate",
        compiler_errors=[
            CompilerError(file_path="Foo.java", line_number=10, column=5, message="illegal escape character")
        ],
    )
    assert "Note on 'illegal escape character'" in prompt_with
    assert "quadruple backslashes" in prompt_with
