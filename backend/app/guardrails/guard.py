from pathlib import Path
import sys

from git import Repo

backend_root=Path(__file__).resolve().parents[2]
if str(backend_root) not in sys.path:
    sys.path.insert(0,str(backend_root))

from app.guardrails.pre_execution import SecurityViolationError, validate_patch_safety
from app.ingestion.patch_applicator import apply_patch, commit_patch, rollback_patch
from app.models.agent import FilePatchResult

REPO_PATH=Path(__file__).resolve().parents[2]/"workspace"/"mybatis-spring-boot-jpetstore"
TARGET_RELATIVE_PATH="src/main/java/com/kazuki43zoo/jpetstore/component/validation/NumericCharacters.java"
MIGRATION_RULE=(
    "Migrate all javax.validation.* imports and annotations in this file to "
    "their jakarta.validation.* equivalents. Do not change anything else "
    "about this file."
    )

def get_real_patch()->FilePatchResult:
    target_file=REPO_PATH/TARGET_RELATIVE_PATH
    source_code=target_file.read_text(encoding="utf-8")

    try:
        from app.llm.factory import LLMFactory
        from app.llm.prompts import CODER_AGENT_SYSTEM_INSTRUCTION, build_coder_prompt
        provider=LLMFactory.create()
        prompt=build_coder_prompt(TARGET_RELATIVE_PATH,source_code,MIGRATION_RULE)
        patch=provider.generate_structured(
            prompt=prompt,
            response_schema=FilePatchResult,
            system_instruction=CODER_AGENT_SYSTEM_INSTRUCTION
        )
        print(f"  (used a live {type(provider).__name__} call)")
        return patch
    except Exception as exc:
        print(f"  (no live LLM call available ({exc}); using a locally-built equivalent patch)")
        migrated=source_code.replace("javax.validation","jakarta.validation")
        return FilePatchResult(
            file_path=TARGET_RELATIVE_PATH,
            new_content=migrated,
            explanation="Swapped javax.validation for jakarta.validation"
        )
 
 
def main()->int:
    if not REPO_PATH.is_dir():
        print(f"Repo not found at {REPO_PATH} - run git_manager.py (or pipeline.py) first.")
        return 1
 
    all_passed=True
 
    # ---------- 1. Security test ----------
    print("========== SECURITY TEST ==========")
 
    traversal_patch=FilePatchResult(
        file_path="../../evil.sh",
        new_content="#!/bin/sh\nrm -rf /\n",
        explanation="malicious test patch"
    )
    try:
        validate_patch_safety(REPO_PATH,traversal_patch)
        print("  [FAIL] path traversal (../../evil.sh) was NOT blocked")
        all_passed=False
    except SecurityViolationError as exc:
        print(f"  [PASS] path traversal blocked -> {exc}")
 
    absolute_patch=FilePatchResult(
        file_path="/etc/passwd",
        new_content="root:x:0:0::/root:/bin/sh\n",
        explanation="malicious test patch (absolute path)"
    )
    try:
        validate_patch_safety(REPO_PATH,absolute_patch)
        print("  [FAIL] absolute-path escape (/etc/passwd) was NOT blocked")
        all_passed=False
    except SecurityViolationError as exc:
        print(f"  [PASS] absolute-path escape blocked -> {exc}")
 
    forbidden_pattern_patch=FilePatchResult(
        file_path=TARGET_RELATIVE_PATH,
        new_content="class X { void run() { Runtime.getRuntime().exec(\"rm -rf /\"); } }",
        explanation="malicious test patch (forbidden pattern)"
    )
    try:
        validate_patch_safety(REPO_PATH,forbidden_pattern_patch)
        print("  [FAIL] forbidden pattern (Runtime.getRuntime().exec) was NOT blocked")
        all_passed=False
    except SecurityViolationError as exc:
        print(f"  [PASS] forbidden pattern blocked -> {exc}")
 
    # ---------- 2. Application test ----------
    print("\n========== APPLICATION TEST ==========")
    print("Getting the real patch for NumericCharacters.java...")
    patch=get_real_patch()
 
    try:
        validate_patch_safety(REPO_PATH,patch)
        print("  [PASS] real patch passes safety validation")
    except SecurityViolationError as exc:
        print(f"  [FAIL] real patch was blocked unexpectedly: {exc}")
        all_passed=False
        return 1 if not all_passed else 0
 
    written_path=apply_patch(REPO_PATH,patch)
    on_disk=written_path.read_text(encoding="utf-8")
    applied_ok="jakarta.validation" in on_disk and "javax.validation" not in on_disk
    print(f"  [{'PASS' if applied_ok else 'FAIL'}] written file contains jakarta.validation, no javax.validation left")
    all_passed=all_passed and applied_ok
 
    commit_sha=commit_patch(
        REPO_PATH,
        commit_message="Migrate NumericCharacters.java: javax.validation -> jakarta.validation",
        file_path=TARGET_RELATIVE_PATH
    )
    print(f"  Committed: {commit_sha}")
 
    # Verify independently via GitPython - not just trusting our own
    # function's return value.
    repo=Repo(REPO_PATH)
    is_clean=not repo.is_dirty(untracked_files=False)
    head_commit=repo.head.commit
    commit_matches=head_commit.hexsha==commit_sha
    message_matches=head_commit.message.strip().startswith("Migrate NumericCharacters.java")
    on_correct_branch=repo.active_branch.name=="migration/spring-boot-3"
 
    print(f"  [{'PASS' if is_clean else 'FAIL'}] working tree clean after commit (git status equivalent)")
    print(f"  [{'PASS' if commit_matches else 'FAIL'}] HEAD matches the SHA commit_patch returned")
    print(f"  [{'PASS' if message_matches else 'FAIL'}] HEAD commit message matches (git log equivalent)")
    print(f"  [{'PASS' if on_correct_branch else 'FAIL'}] committed on migration/spring-boot-3 (currently: {repo.active_branch.name})")
    all_passed=all_passed and is_clean and commit_matches and message_matches and on_correct_branch
 
    # ---------- 3. Rollback test ----------
    print("\n========== ROLLBACK TEST ==========")
    rollback_target="src/main/java/com/kazuki43zoo/jpetstore/component/validation/RepeatedField.java"
    rollback_file=REPO_PATH/rollback_target
    original_content=rollback_file.read_text(encoding="utf-8")
 
    dirty_patch=FilePatchResult(
        file_path=rollback_target,
        new_content=original_content+"\n// THIS LINE SHOULD NEVER SURVIVE A ROLLBACK\n",
        explanation="deliberately dirty, uncommitted change to test rollback"
    )
    apply_patch(REPO_PATH,dirty_patch)
    dirtied=rollback_file.read_text(encoding="utf-8")
    was_dirtied="SHOULD NEVER SURVIVE" in dirtied
    print(f"  [{'PASS' if was_dirtied else 'FAIL'}] file was actually dirtied before rollback")
 
    rollback_patch(REPO_PATH,rollback_target)
    restored=rollback_file.read_text(encoding="utf-8")
    rollback_ok=restored==original_content
    print(f"  [{'PASS' if rollback_ok else 'FAIL'}] file restored to original content after rollback_patch")
    all_passed=all_passed and was_dirtied and rollback_ok
 
    print(f"\n========== OVERALL: {'PASS' if all_passed else 'FAIL'} ==========")
    return 0 if all_passed else 1
 
 
if __name__=="__main__":
    sys.exit(main())
