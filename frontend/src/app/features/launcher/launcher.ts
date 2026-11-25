import { ChangeDetectionStrategy, Component, computed, effect, inject, signal } from '@angular/core';
import { Router } from '@angular/router';

import { CodeReviveService } from '../../core/services/code-revive.service';
import { MigrationJobRequest, RepositoryPreset } from '../../core/models/api.models';
import { isRemoteRepoUrl, repoWebUrl } from '../../core/utils/format.util';

const REMOTE_PRESETS: RepositoryPreset[] = [
  { label: 'mybatis-spring-boot-jpetstore', url: 'https://github.com/kazuki43zoo/mybatis-spring-boot-jpetstore.git' },
];

@Component({
  selector: 'app-launcher',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [],
  templateUrl: './launcher.html',
  styleUrl: './launcher.scss'
})
export class Launcher {
  private readonly api = inject(CodeReviveService);
  private readonly router = inject(Router);

  readonly presets = computed(() => this.api.config()?.repository_presets ?? REMOTE_PRESETS);

  // Repository URL intentionally starts empty; only populated when user selects a preset or types one.
  readonly repoUrl = signal('');
  readonly branchName = signal('migration/spring-boot-3');
  readonly retryBudget = signal(5);

  // Dynamic profile values sourced from GET /api/config (CODEREVIVE_* env vars)
  readonly targetBoot = signal<string>('3.2.5');
  readonly targetJava = signal<string>('17');
  readonly useOpenrewrite = signal(true);
  readonly openrewriteMode = signal<'hybrid' | 'deterministic' | 'maven_only'>('hybrid');

  readonly submitting = signal(false);
  readonly error = signal<string | null>(null);

  private defaultsApplied = false;

  constructor() {
    // Seed non-repo defaults from GET /api/config once it arrives.
    effect(() => {
      const c = this.api.config();
      if (c && !this.defaultsApplied) {
        this.defaultsApplied = true;
        this.branchName.set(c.migration_branch_name || 'migration/spring-boot-3');
        this.retryBudget.set(typeof c.default_retry_budget === 'number' ? c.default_retry_budget : 5);
        this.targetBoot.set(c.target_spring_boot_version || '3.2.5');
        this.targetJava.set(c.target_java_version || '17');
        this.useOpenrewrite.set(c.use_openrewrite ?? true);
        this.openrewriteMode.set(
          (c.openrewrite_mode as 'hybrid' | 'deterministic' | 'maven_only') || 'hybrid'
        );
      }
    });
    this.api.loadConfig();
  }

  applyPreset(preset: RepositoryPreset): void {
    this.repoUrl.set(preset.url);
    this.error.set(null);
  }

  onRepoUrlInput(event: Event): void {
    this.repoUrl.set((event.target as HTMLInputElement).value);
    this.error.set(null);
  }

  onBranchInput(event: Event): void {
    this.branchName.set((event.target as HTMLInputElement).value);
  }

  onRetryBudgetInput(event: Event): void {
    const val = Number((event.target as HTMLInputElement).value);
    this.retryBudget.set(Number.isFinite(val) ? val : 0);
  }

  repoOnGithub(): string {
    return repoWebUrl(this.repoUrl().trim());
  }

  isRemoteRepo(): boolean {
    return isRemoteRepoUrl(this.repoUrl());
  }

  submit(): void {
    const repo = this.repoUrl().trim();
    if (!repo || this.submitting()) {
      return;
    }
    const request: MigrationJobRequest = {
      repo_url: repo,
      target_spring_boot: this.targetBoot(),
      target_java: this.targetJava(),
      branch_name: this.branchName().trim() || 'migration/spring-boot-3',
      use_openrewrite: this.useOpenrewrite(),
      openrewrite_mode: this.openrewriteMode(),
      retry_budget: Math.max(0, Math.min(20, Math.floor(this.retryBudget() || 0))),
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
