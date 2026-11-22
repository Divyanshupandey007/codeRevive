import { Component, DestroyRef, computed, effect, inject, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { DatePipe } from '@angular/common';

import { CodeReviveService } from '../../core/services/code-revive.service';
import { MigrationJobRecord, MigrationJobRequest, RepositoryPreset } from '../../core/models/api.models';
import { isRemoteRepoUrl, repoName, repoWebUrl } from '../../core/utils/format.util';

const REMOTE_PRESETS: RepositoryPreset[] = [
  { label: 'mybatis-spring-boot-jpetstore', url: 'https://github.com/kazuki43zoo/mybatis-spring-boot-jpetstore.git' },
];

const PRODUCTION_PROFILE = {
  targetBoot: '3.2.5',
  targetJava: '17',
  strategy: 'hybrid',
  useOpenrewrite: true
} as const;

@Component({
  selector: 'app-launcher',
  imports: [FormsModule, RouterLink, DatePipe],
  templateUrl: './launcher.html',
  styleUrl: './launcher.scss'
})
export class Launcher {
  private readonly api = inject(CodeReviveService);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);

  readonly presets = computed(() => this.api.config()?.repository_presets ?? REMOTE_PRESETS);
  readonly profile = PRODUCTION_PROFILE;

  // Form state (bound to MigrationJobRequest)
  readonly repoUrl = signal('');
  readonly branchName = signal('migration/spring-boot-3');
  readonly retryBudget = signal(5);

  readonly submitting = signal(false);
  readonly error = signal<string | null>(null);
  readonly historyError = signal<string | null>(null);
  readonly deleteCandidateId = signal<string | null>(null);
  readonly deletingJobId = signal<string | null>(null);

  readonly jobs = this.api.jobs;
  readonly jobsTotal = this.api.jobsTotal;

  readonly jobsSummary = computed(() => {
    const jobs = this.api.jobs();
    return {
      total: jobs.length,
      completed: jobs.filter((j) => j.status === 'COMPLETED').length,
      failed: jobs.filter((j) => j.status === 'FAILED').length,
      running: jobs.filter((j) => j.status === 'RUNNING').length
    };
  });

  private defaultsApplied = false;

  constructor() {
    this.api.refreshJobs();
    const refresh = setInterval(() => this.api.refreshJobs(), 15_000);
    this.destroyRef.onDestroy(() => clearInterval(refresh));

    // Seed the form from GET /api/config once it arrives.
    effect(() => {
      const c = this.api.config();
      if (c && !this.defaultsApplied) {
        this.defaultsApplied = true;
        this.repoUrl.set(c.repo_url);
        this.branchName.set(c.migration_branch_name);
        this.retryBudget.set(c.default_retry_budget);
      }
    });
    this.api.loadConfig();
  }

  applyPreset(preset: RepositoryPreset): void {
    this.repoUrl.set(preset.url);
  }

  refreshHistory(): void {
    this.historyError.set(null);
    this.api.refreshJobs();
  }

  repoOnGithub(): string {
    return repoWebUrl(this.repoUrl().trim());
  }

  isRemoteRepo(): boolean {
    return isRemoteRepoUrl(this.repoUrl());
  }

  shortRepo(url: string): string {
    return repoName(url);
  }

  jobBadgeClass(job: MigrationJobRecord): string {
    switch (job.status) {
      case 'COMPLETED':
        return 'badge badge-green';
      case 'FAILED':
        return 'badge badge-red';
      case 'RUNNING':
        return 'badge badge-amber';
      default:
        return 'badge badge-blue';
    }
  }

  requestDelete(job: MigrationJobRecord): void {
    if (job.status === 'QUEUED' || job.status === 'RUNNING') {
      return;
    }
    this.historyError.set(null);
    this.deleteCandidateId.update((current) => (current === job.job_id ? null : job.job_id));
  }

  cancelDelete(): void {
    if (!this.deletingJobId()) {
      this.deleteCandidateId.set(null);
    }
  }

  confirmDelete(job: MigrationJobRecord): void {
    if (this.deletingJobId() || job.status === 'QUEUED' || job.status === 'RUNNING') {
      return;
    }
    this.deletingJobId.set(job.job_id);
    this.historyError.set(null);
    this.api.deleteJob(job.job_id).subscribe({
      next: () => {
        this.api.jobs.update((jobs) => jobs.filter((item) => item.job_id !== job.job_id));
        this.api.jobsTotal.update((total) => Math.max(0, total - 1));
        this.deletingJobId.set(null);
        this.deleteCandidateId.set(null);
      },
      error: (err) => {
        this.deletingJobId.set(null);
        this.historyError.set(err?.error?.detail ?? err?.message ?? `Failed to delete job '${job.job_id}'.`);
      }
    });
  }

  submit(): void {
    const repo = this.repoUrl().trim();
    if (!repo || this.submitting()) {
      return;
    }
    const request: MigrationJobRequest = {
      repo_url: repo,
      target_spring_boot: this.profile.targetBoot,
      target_java: this.profile.targetJava,
      branch_name: this.branchName().trim() || 'migration/spring-boot-3',
      use_openrewrite: this.profile.useOpenrewrite,
      openrewrite_mode: this.profile.strategy,
      retry_budget: Math.max(0, Math.floor(this.retryBudget() || 0)),
      clean: true
    };
    this.submitting.set(true);
    this.error.set(null);
    this.api.submitJob(request).subscribe({
      next: (job) => {
        this.submitting.set(false);
        this.api.refreshJobs();
        this.router.navigate(['/jobs', job.job_id]);
      },
      error: (err) => {
        this.submitting.set(false);
        this.error.set(err?.error?.detail ?? err?.message ?? 'Failed to queue migration job.');
      }
    });
  }
}
