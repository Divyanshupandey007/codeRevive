import { MigrationJobRecord } from '../../core/models/api.models';

type JobProgress = Pick<MigrationJobRecord, 'stage' | 'summary' | 'error' | 'error_type' | 'events'>;

/** Map the backend workflow state to the six-step pipeline shown in the UI. */
export function activeStepForJob(job: JobProgress): number {
  const state = `${job.summary?.final_state ?? ''} ${job.stage}`.toLowerCase();
  if (state.includes('completed')) {
    return 5;
  }
  if (state.includes('commit') || state.includes('integrity')) {
    return 4;
  }
  if (state.includes('repair') || state.includes('generating_patch')) {
    return 3;
  }
  if (state.includes('sandbox') || state.includes('verif') || state.includes('migrating_file')) {
    return 2;
  }
  if (state.includes('openrewrite')) {
    return 1;
  }
  return 0;
}

/** Identify the failed stage without discarding the progress made before it. */
export function failedStepForJob(job: JobProgress): number {
  const summary = job.summary;
  if (summary?.final_state === 'FAILED') {
    if (summary.last_build_errors.length === 0 && summary.integrity_violations.length > 0) {
      return 4;
    }
    return summary.total_retries > 0 ? 3 : 2;
  }

  const failure = `${job.error_type ?? ''} ${job.error ?? ''}`.toLowerCase();
  if (/openrewrite/.test(failure)) {
    return 1;
  }
  if (/integrity|commit/.test(failure)) {
    return 4;
  }
  if (/llm|repair|gemini|nvidia|openrouter|securityviolation|patch formatting/.test(failure)) {
    return 3;
  }
  if (/docker|sandbox|maven|container|build/.test(failure)) {
    return 2;
  }

  const previousEvent = [...job.events].reverse().find((event) => event.stage !== 'failed');
  const prevStep = previousEvent ? activeStepForJob({ ...job, stage: previousEvent.stage }) : 0;
  if (
    prevStep === 2 &&
    job.events.some((event) => /repair|generating_patch|llm/i.test(event.stage))
  ) {
    return 3;
  }
  return prevStep;
}
