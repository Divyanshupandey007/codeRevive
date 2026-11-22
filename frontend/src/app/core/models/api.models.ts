/**
 * TypeScript mirrors of the backend Pydantic contracts:
 *   backend/app/api/routes.py, backend/app/api/jobs.py,
 *   backend/app/orchestrator/service.py, backend/app/models/orchestrator.py
 */

export type JobStatus = 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'CANCELLED';

export type WorkflowState =
  | 'INITIALIZING'
  | 'OPENREWRITE_PASS'
  | 'MIGRATING_FILE'
  | 'GENERATING_PATCH'
  | 'VERIFYING_SANDBOX'
  | 'REPAIRING'
  | 'POST_INTEGRITY_CHECK'
  | 'COMMITING'
  | 'COMPLETED'
  | 'FAILED';

export type FileStatus =
  | 'PENDING'
  | 'GENERATING'
  | 'PATCH_APPLIED'
  | 'VALIDATED'
  | 'SUCCESS'
  | 'FAILED'
  | 'REJECTED'
  | 'SKIPPED';

/** POST /api/migrations and POST /api/migrations/jobs request body. */
export interface MigrationJobRequest {
  repo_url: string;
  target_spring_boot: string;
  target_java: string;
  branch_name: string;
  use_openrewrite: boolean;
  openrewrite_mode: string;
  retry_budget: number;
  clean?: boolean;
}

export interface MigrationJobEvent {
  timestamp: string;
  stage: string;
  message: string;
}

export interface FileMigrationStatus {
  file_path: string;
  attempts: number;
  status: FileStatus;
  applied_by: string | null;
  commit_sha: string | null;
  diff: string | null;
  errors_encountered: string[];
}

/** GET /api/migrations/latest and MigrationJobRecord.summary. */
export interface MigrationRunSummary {
  repo_url: string;
  baseline_commit: string;
  final_commit: string | null;
  target_spring_boot_version: string;
  openrewrite_used: boolean;
  openrewrite_files_modified: number;
  total_files_migrated: number;
  total_retries: number;
  file_statuses: FileMigrationStatus[];
  integrity_passed: boolean;
  total_duration_seconds: number;
  final_state: WorkflowState;
  integrity_violations: string[];
  last_build_errors: string[];
  last_build_logs_tail: string | null;
  generated_at: string;
}

export interface MigrationJobRecord {
  job_id: string;
  status: JobStatus;
  stage: string;
  request: MigrationJobRequest;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  events: MigrationJobEvent[];
  summary: MigrationRunSummary | null;
  summary_path: string | null;
  error: string | null;
  error_type: string | null;
}

export interface JobListResponse {
  items: MigrationJobRecord[];
  total: number;
  limit: number;
  offset: number;
  latest_available?: boolean | null;
}

export interface JobEventsResponse {
  job_id: string;
  status: string;
  stage: string;
  events: MigrationJobEvent[];
}

export interface DeleteJobResponse {
  deleted: boolean;
  job_id: string;
}

export interface HealthResponse {
  status: string;
  service: string;
}

export interface RepositoryPreset {
  label: string;
  url: string;
}

/** GET /api/config — active non-secret backend configuration. */
export interface BackendConfig {
  repository_presets: RepositoryPreset[];
  repo_url: string;
  migration_branch_name: string;
  target_spring_boot_version: string;
  target_java_version: string;
  use_openrewrite: boolean;
  openrewrite_mode: string;
  default_retry_budget: number;
  llm_provider: string;
  llm_model: string;
  workspace_root: string;
}

export interface RouteDescriptor {
  path: string;
  methods: string[];
  summary: string;
}

export interface ServiceIndexResponse {
  status: string;
  service: string;
  api_prefix: string;
  docs_url: string;
  openapi_url: string;
  routes: RouteDescriptor[];
}
