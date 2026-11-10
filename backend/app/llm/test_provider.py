from pathlib import Path
import sys

backend_root=Path(__file__).resolve().parents[2]
if str(backend_root) not in sys.path:
    sys.path.insert(0,str(backend_root))

from app.llm.base import LLMProviderError
from app.llm.prompts import CODER_AGENT_SYSTEM_INSTRUCTION, build_coder_prompt
from app.llm.factory import LLMFactory
from app.models.agent import FilePatchResult

TARGET_RELATIVE_PATH="src/main/java/com/kazuki43zoo/jpetstore/component/validation/NumericCharacters.java"

MIGRATION_RULE=(
    "Migrate all javax.validation.* imports and annotations in this file to "
    "their jakarta.validation.* equivalents. Do not change anything else "
    "about this file."
)

def main()->int:
    repo_path=Path(__file__).resolve().parents[2]/"workspace"/"mybatis-spring-boot-jpetstore"
    target_file=repo_path/TARGET_RELATIVE_PATH

    print(f"Target file: {target_file}")
    if not target_file.is_file():
        print("File not found - run git_manager.py first to clone the repo")
        return 1

    source_code=target_file.read_text(encoding="utf-8")

    prompt=build_coder_prompt(
        target_file_path=TARGET_RELATIVE_PATH,
        source_code=source_code,
        migration_rule=MIGRATION_RULE
    )

    try:
        provider=LLMFactory.create()
    except (ValueError,ImportError) as exc:
        print(f"\nCould not create an LLM provider: {exc}")
        return 1

    print(f"Using provider: {type(provider).__name__}")

    try:
        result=provider.generate_structured(
            prompt=prompt,
            response_schema=FilePatchResult,
            system_instruction=CODER_AGENT_SYSTEM_INSTRUCTION
        )
    except LLMProviderError as exc:
        print(f"\nProvider call failed: {exc}")
        return 1

    print("\n========== FILE PATCH RESULT ==========")
    print(f"file_path: {result.file_path}")
    print(f"explanation: {result.explanation}")
    print(f"new_content ({len(result.new_content.splitlines())} lines):")
    print(result.new_content)
 
    checks={
        "returned a valid FilePatchResult": isinstance(result,FilePatchResult),
        "new_content contains 'import jakarta.validation.Constraint;'":
            "import jakarta.validation.Constraint;" in result.new_content,
        "no javax.validation left in new_content":
            "javax.validation" not in result.new_content,
        "no markdown code fences leaked into new_content":
            "```" not in result.new_content
    }
 
    print("\n========== DEFINITION OF DONE CHECKS ==========")
    all_passed=True
    for description,passed in checks.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {description}")
        all_passed=all_passed and passed
 
    return 0 if all_passed else 1
 
 
if __name__=="__main__":
    sys.exit(main())