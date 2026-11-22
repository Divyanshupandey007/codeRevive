/**
 * TypeScript mirrors of the remaining backend Pydantic contracts:
 *   backend/app/models/manifest.py   (ingestion / planning models)
 *   backend/app/models/execution.py  (sandbox build results)
 *   backend/app/models/agent.py      (LLM patch results)
 *   backend/app/orchestrator/service.py (service-level wrappers)
 *
 * These are not all exposed by GET endpoints today, but they shape the
 * run pipeline (ingestion report -> plan -> build results -> summary).
 */

import { MigrationRunSummary } from './api.models';

/* ---------- models/manifest.py ---------- */

export interface MavenDependency {
  group_id: string;
  artifact_id: string;
  version: string | null;
  scope: string | null;
}

export interface PomManifest {
  spring_boot_version: string | null;
  parent_group_id: string | null;
  parent_artifact_id: string | null;
  java_version: string | null;
  packaging: string | null;
  is_multi_module: boolean;
  modules: string[];
  properties: Record<string, string>;
  repositories: string[];
  dependencies: MavenDependency[];
}

export interface JavaClassManifest {
  name: string | null;
  extends: string[];
  implements: string[];
}

export interface JavaMethodManifest {
  name: string | null;
  return_type: string | null;
  parameters: string[];
  modifiers: string[];
  annotations: string[];
  is_test: boolean;
  is_public: boolean;
  has_assertions_or_body: boolean;
}

export interface JavaFileManifest {
  file_path: string;
  package_name: string | null;
  imports: string[];
  javax_imports: string[];
  jakarta_migratable_imports: string[];
  classes: JavaClassManifest[];
  methods: JavaMethodManifest[];
  test_count: number;
  has_deprecated_pbkdf2_constructor: boolean | null;
  has_deprecated_spring_assert: boolean | null;
}

export interface MigrationTarget {
  identifier: string;
  file_path: string;
  package_name: string | null;
  migration_depth: number;
  in_degree: number;
  out_degree: number;
  is_leaf: boolean;
  is_test: boolean;
  has_javax: boolean;
  matched_rules: string[];
}

export interface BlastRadiusEntry {
  identifier: string;
  file_path: string;
  in_degree: number;
  affected_count: number;
  affected_identifiers: string[];
}

export interface DependencyGraphManifest {
  total_nodes: number;
  total_edges: number;
  unresolved_import_count: number;
  migration_order: MigrationTarget[];
  high_blast_radius: BlastRadiusEntry[];
}

/** Pydantic Tuple[str, str] serializes as a 2-element array. */
export type Exclusion = [string, string];

export interface AdditionalArtifactSpec {
  group_id: string;
  artifact_id: string;
  version: string | null;
  classifier: string | null;
  exclusions: Exclusion[];
}

export interface DependencyMigrationAction {
  group_id: string;
  artifact_id: string;
  current_version: string | null;
  target_strategy: string;
  target_group_id: string | null;
  target_artifact_id: string | null;
  target_version: string | null;
  target_classifier: string | null;
  remove_version: boolean;
  exclusions: Exclusion[];
  additional_artifacts: AdditionalArtifactSpec[];
  notes: string;
}

export interface MigrationPlan {
  source_spring_boot: string | null;
  target_spring_boot: string;
  source_java: string | null;
  target_java: string;
  dependencies: DependencyMigrationAction[];
  properties_to_remove: string[];
  properties_to_update: Record<string, string>;
  repositories_to_remove: string[];
  openrewrite_recipes: string[];
}

export interface RepoInfo {
  repo_url: string;
  target_path: string;
  active_branch: string;
  baseline_sha: string;
  isolated_workspace: boolean;
}

export interface ProjectIngestionReport {
  repo_info: RepoInfo;
  pom: PomManifest;
  graph: DependencyGraphManifest;
  files_to_migrate: MigrationTarget[];
  baseline_test_count: number;
  migration_plan: MigrationPlan | null;
}

/* ---------- models/execution.py ---------- */

export interface CompilerError {
  file_path: string | null;
  line_number: number | null;
  column: number | null;
  message: string;
}

export interface TestFailure {
  test_class: string;
  test_method: string;
  failure_message: string;
  stack_trace: string | null;
}

export interface BuildResult {
  success: boolean;
  exit_code: number;
  duration_seconds: number;
  compiler_errors: CompilerError[];
  failed_tests: TestFailure[];
  raw_logs: string;
}

/* ---------- models/agent.py ---------- */

export interface FilePatchResult {
  file_path: string;
  new_content: string;
  explanation: string;
}

/* ---------- orchestrator/service.py wrappers ---------- */

export interface MigrationServiceResult {
  ingestion_report: ProjectIngestionReport;
  summary: MigrationRunSummary;
  summary_path: string;
}

/** POST /api/migrations (synchronous) response body. */
export interface MigrationSyncResponse {
  summary: MigrationRunSummary;
  summary_path: string;
}
