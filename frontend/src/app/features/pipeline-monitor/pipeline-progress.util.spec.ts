import { MigrationJobRecord, MigrationRunSummary } from '../../core/models/api.models';
import { activeStepForJob, failedStepForJob } from './pipeline-progress.util';

function makeJob(overrides: Partial<MigrationJobRecord> = {}): MigrationJobRecord {
  return {
    job_id: 'mig_test',
    status: 'RUNNING',
    stage: 'orchestrating',
    request: {
      repo_url: 'https://github.com/org/demo.git',
      target_spring_boot: '3.2.5',
      target_java: '17',
      branch_name: 'migration/spring-boot-3',
      use_openrewrite: true,
      openrewrite_mode: 'hybrid',
      retry_budget: 5,
      clean: false
    },
    created_at: '2026-01-01T00:00:00Z',
    started_at: '2026-01-01T00:00:01Z',
    completed_at: null,
    events: [{ timestamp: '2026-01-01T00:00:01Z', stage: 'orchestrating', message: 'orchestrating' }],
    summary: null,
    summary_path: null,
    error: null,
    error_type: null,
    ...overrides
  };
}

function makeSummary(overrides: Partial<MigrationRunSummary> = {}): MigrationRunSummary {
  return {
    repo_url: 'https://github.com/org/demo.git',
    baseline_commit: 'abc1234',
    final_commit: null,
    target_spring_boot_version: '3.2.5',
    openrewrite_used: true,
    openrewrite_files_modified: 1,
    total_files_migrated: 0,
    total_retries: 0,
    file_statuses: [],
    integrity_passed: false,
    total_duration_seconds: 2,
    final_state: 'FAILED',
    integrity_violations: [],
    last_build_errors: ['Compilation failed'],
    last_build_logs_tail: null,
    generated_at: '2026-01-01T00:00:03Z',
    ...overrides
  };
}

describe('pipeline progress', () => {
  it('maps active workflow stages to the stepper', () => {
    expect(activeStepForJob(makeJob({ stage: 'openrewrite_pass' }))).toBe(1);
    expect(activeStepForJob(makeJob({ stage: 'verifying_sandbox' }))).toBe(2);
    expect(activeStepForJob(makeJob({ stage: 'repairing' }))).toBe(3);
    expect(activeStepForJob(makeJob({ stage: 'post_integrity_check' }))).toBe(4);
    expect(activeStepForJob(makeJob({ stage: 'completed' }))).toBe(5);
  });

  it('marks only sandbox verification failed for Docker daemon errors', () => {
    const job = makeJob({
      status: 'FAILED',
      stage: 'failed',
      error_type: 'ContainerExecutionError',
      error: 'Docker daemon is not running'
    });

    expect(failedStepForJob(job)).toBe(2);
  });

  it('keeps OpenRewrite failures distinct from Docker verification failures', () => {
    const job = makeJob({
      status: 'FAILED',
      stage: 'failed',
      error: 'OpenRewrite Docker container exited unsuccessfully'
    });

    expect(failedStepForJob(job)).toBe(1);
  });

  it('distinguishes exhausted repairs from integrity failures', () => {
    expect(failedStepForJob(makeJob({ summary: makeSummary({ total_retries: 2 }) }))).toBe(3);
    expect(
      failedStepForJob(
        makeJob({
          summary: makeSummary({ last_build_errors: [], integrity_violations: ['Missing public method'] })
        })
      )
    ).toBe(4);
  });

  it('falls back to the last known event for an unclassified failure', () => {
    const job = makeJob({
      status: 'FAILED',
      stage: 'failed',
      error: 'Unexpected error',
      events: [
        { timestamp: '2026-01-01T00:00:01Z', stage: 'openrewrite_pass', message: 'Pass 1' },
        { timestamp: '2026-01-01T00:00:02Z', stage: 'failed', message: 'Unexpected error' }
      ]
    });

    expect(failedStepForJob(job)).toBe(1);
  });
});
