from pathlib import Path
import sys
from typing import List, Set, Tuple

from pydantic import BaseModel, Field

backend_root=Path(__file__).resolve().parents[2]
if str(backend_root) not in sys.path:
    sys.path.insert(0,str(backend_root))

from app.ingestion.ast_parser import JavaAstParser
from app.models.manifest import JavaFileManifest

class IntegrityViolationError(Exception):
    """Raised when a migrated repository fails a post-execution integrity check"""

class MethodSignatureDiffResult(BaseModel):
    missing_methods:List[str]=Field(default_factory=list)

    @property
    def passed(self)->bool:
        return not self.missing_methods

class PostExecutionIntegrityResult(BaseModel):
    baseline_test_count:int
    final_test_count:int
    test_count_passed:bool
    missing_public_methods:List[str]=Field(default_factory=list)
    method_signatures_passed:bool
    passed:bool
    violations:List[str]=Field(default_factory=list)

def _public_method_identifiers(manifests:List[JavaFileManifest])->Set[str]:
    identifiers:Set[str]=set()
    for manifest in manifests:
        for method in manifest.methods:
            if method.is_public and not method.is_test and method.name:
                identifiers.add(f"{manifest.file_path}#{method.name}")
    return identifiers

def check_test_count(manifests:List[JavaFileManifest],baseline_test_count:int)->Tuple[int,bool]:
    final_test_count=sum(m.test_count for m in manifests)
    return final_test_count,final_test_count>=baseline_test_count

def check_method_signatures(baseline_manifests:List[JavaFileManifest],current_manifests:List[JavaFileManifest])->MethodSignatureDiffResult:
    baseline_methods=_public_method_identifiers(baseline_manifests)
    current_methods=_public_method_identifiers(current_manifests)
    missing=sorted(baseline_methods-current_methods)
    return MethodSignatureDiffResult(missing_methods=missing)

def run_post_execution_integrity(repo_path:Path,baseline_manifests:List[JavaFileManifest],baseline_test_count:int)->PostExecutionIntegrityResult:
    current_manifests=JavaAstParser(str(repo_path)).parse_repo()
    final_test_count,test_count_passed=check_test_count(current_manifests,baseline_test_count)
    signature_diff=check_method_signatures(baseline_manifests,current_manifests)

    violations:List[str]=[]
    if not test_count_passed:
        violations.append(
            "Aggent deleted unit test methods to force build pass. "
            f"baseline_test_count={baseline_test_count}, final_test_count={final_test_count}"
        )

    if not signature_diff.passed:
        violations.append(
            "Agent removed or renamed public method(s) that existed before "
            f"migration: {','.join(signature_diff.missing_methods)}"
        )

    return PostExecutionIntegrityResult(
        baseline_test_count=baseline_test_count,
        final_test_count=final_test_count,
        test_count_passed=test_count_passed,
        missing_public_methods=signature_diff.missing_methods,
        method_signatures_passed=signature_diff.passed,
        passed=test_count_passed and signature_diff.passed,
        violations=violations
    )

def assert_integrity(result:PostExecutionIntegrityResult)->None:
    if not result.passed:
        raise IntegrityViolationError("; ".join(result.violations))

if __name__=="__main__":
    from app.models.manifest import JavaClassManifest, JavaMethodManifest
 
    def _manifest(file_path:str,method_names_and_flags,test_count:int)->JavaFileManifest:
        return JavaFileManifest(
            file_path=file_path,
            package_name="com.example",
            imports=[],
            javax_imports=[],
            classes=[JavaClassManifest(name=Path(file_path).stem)],
            methods=[
                JavaMethodManifest(name=name,is_public=is_public,is_test=is_test)
                for name,is_public,is_test in method_names_and_flags
            ],
            test_count=test_count
        )
 
    all_passed=True
 
    print("========== TEST COUNT REGRESSION ==========")
    baseline=[_manifest("A.java",[("doThing",True,False),("testThing",True,True)],1)]
 
    healthy=[_manifest("A.java",[("doThing",True,False),("testThing",True,True)],1)]
    cheated=[_manifest("A.java",[("doThing",True,False)],0)]
 
    # Exercise the two checks directly on synthetic manifests - no real repo,
    # LLM, or Docker daemon needed. run_post_execution_integrity() itself is
    # a thin wrapper that re-parses a real repo_path before calling these.
    final_count,count_ok=check_test_count(healthy,baseline_test_count=1)
    print(f"  [{'PASS' if count_ok else 'FAIL'}] unchanged test count ({final_count}) does not trip the guardrail")
    all_passed=all_passed and count_ok
 
    final_count,count_ok=check_test_count(cheated,baseline_test_count=1)
    print(f"  [{'PASS' if not count_ok else 'FAIL'}] dropped test count ({final_count} < 1) IS caught")
    all_passed=all_passed and (not count_ok)
 
    print("\n========== PUBLIC METHOD SIGNATURE DIFF ==========")
    diff=check_method_signatures(baseline,healthy)
    print(f"  [{'PASS' if diff.passed else 'FAIL'}] no methods missing when nothing changed")
    all_passed=all_passed and diff.passed
 
    deleted_method=[_manifest("A.java",[("testThing",True,True)],1)]
    diff=check_method_signatures(baseline,deleted_method)
    print(f"  [{'PASS' if not diff.passed else 'FAIL'}] deleted public method is caught -> {diff.missing_methods}")
    all_passed=all_passed and (not diff.passed)
 
    print("\n========== assert_integrity() ==========")
    good_result=PostExecutionIntegrityResult(
        baseline_test_count=1,final_test_count=1,test_count_passed=True,
        missing_public_methods=[],method_signatures_passed=True,passed=True,violations=[]
    )
    try:
        assert_integrity(good_result)
        print("  [PASS] a passing result does not raise")
    except IntegrityViolationError as exc:
        print(f"  [FAIL] a passing result raised unexpectedly: {exc}")
        all_passed=False
 
    bad_result=PostExecutionIntegrityResult(
        baseline_test_count=1,final_test_count=0,test_count_passed=False,
        missing_public_methods=["A.java#doThing"],method_signatures_passed=False,passed=False,
        violations=["Agent deleted unit test methods to force build pass."]
    )
    try:
        assert_integrity(bad_result)
        print("  [FAIL] a failing result did NOT raise")
        all_passed=False
    except IntegrityViolationError as exc:
        print(f"  [PASS] a failing result raises -> {exc}")
 
    print(f"\n========== OVERALL: {'PASS' if all_passed else 'FAIL'} ==========")
    sys.exit(0 if all_passed else 1)