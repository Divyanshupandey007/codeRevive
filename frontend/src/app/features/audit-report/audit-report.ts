import { ChangeDetectionStrategy, Component, DestroyRef, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { DatePipe } from '@angular/common';
import { catchError, of } from 'rxjs';

import { CodeReviveService } from '../../core/services/code-revive.service';
import {
  FileMigrationStatus,
  JobArtifact,
  MigrationJobRecord,
  MigrationRunSummary
} from '../../core/models/api.models';
import { DiffInspector } from '../diff-inspector/diff-inspector';
import { downloadBlob, formatDuration, isRemoteRepoUrl, repoName, repoWebUrl, shortSha } from '../../core/utils/format.util';

type LedgerFilter = 'all' | 'passed' | 'failed' | 'openrewrite' | 'ai' | 'multi';

@Component({
  selector: 'app-audit-report',
  imports: [RouterLink, DatePipe, DiffInspector],
  templateUrl: './audit-report.html',
  styleUrl: './audit-report.scss',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class AuditReport {
  private readonly api = inject(CodeReviveService);
  private readonly route = inject(ActivatedRoute);
  private readonly destroyRef = inject(DestroyRef);

  readonly reportId = signal('latest');

  readonly summary = signal<MigrationRunSummary | null>(null);
  readonly job = signal<MigrationJobRecord | null>(null);
  readonly branchName = signal('migration/spring-boot-3');
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);

  readonly query = signal('');
  readonly filter = signal<LedgerFilter>('all');
  readonly expanded = signal<string | null>(null);

  readonly filters: { key: LedgerFilter; label: string }[] = [
    { key: 'all', label: 'All' },
    { key: 'passed', label: 'Passed' },
    { key: 'failed', label: 'Failed' },
    { key: 'openrewrite', label: 'OpenRewrite' },
    { key: 'ai', label: 'AI Repair' },
    { key: 'multi', label: 'Multi-attempt' }
  ];

  readonly files = computed(() => this.summary()?.file_statuses ?? []);

  readonly diffFileCount = computed(() => this.files().filter((f) => !!f.diff).length);

  readonly displaySummary = computed<MigrationRunSummary | null>(() => this.summary());

  /** Mutually-exclusive breakdown matching the File Ledger badges. */
  readonly modernizationBreakdown = computed(() => {
    const s = this.summary();
    if (!s) {
      return '';
    }
    const migrated = s.file_statuses.filter(
      (fs) =>
        fs.status === 'SUCCESS' ||
        fs.status === 'VALIDATED' ||
        fs.status === 'PATCH_APPLIED'
    );
    if (migrated.length === 0) {
      return `${s.openrewrite_files_modified} via OpenRewrite`;
    }
    let openrewriteOnly = 0;
    let hybrid = 0;
    let aiOnly = 0;
    for (const fs of migrated) {
      const via = (fs.applied_by ?? '').toLowerCase();
      const hasOr = via.includes('openrewrite');
      const hasAi = /llm|ai|repair/i.test(via);
      if (hasOr && hasAi) {
        hybrid++;
      } else if (hasOr) {
        openrewriteOnly++;
      } else if (hasAi) {
        aiOnly++;
      }
    }
    const parts: string[] = [];
    if (openrewriteOnly > 0) {
      parts.push(`${openrewriteOnly} OpenRewrite`);
    }
    if (hybrid > 0) {
      parts.push(`${hybrid} OpenRewrite + AI`);
    }
    if (aiOnly > 0) {
      parts.push(`${aiOnly} AI`);
    }
    return parts.length > 0 ? parts.join(' · ') : `${s.openrewrite_files_modified} via OpenRewrite`;
  });

  readonly filteredFiles = computed(() => {
    const q = this.query().trim().toLowerCase();
    const f = this.filter();
    return this.files().filter((fs) => {
      if (q && !fs.file_path.toLowerCase().includes(q)) {
        return false;
      }
      switch (f) {
        case 'passed':
          return fs.status === 'SUCCESS' || fs.status === 'VALIDATED' || fs.status === 'PATCH_APPLIED';
        case 'failed':
          return fs.status === 'FAILED' || fs.status === 'REJECTED';
        case 'openrewrite':
          return (fs.applied_by ?? '').toLowerCase().includes('openrewrite');
        case 'ai':
          return /llm|ai|repair/i.test(fs.applied_by ?? '');
        case 'multi':
          return fs.attempts > 1;
        default:
          return true;
      }
    });
  });

  constructor() {
    this.api.loadConfig();
    this.route.paramMap.pipe(takeUntilDestroyed(this.destroyRef)).subscribe((params) => {
      const id = params.get('id') ?? 'latest';
      this.reportId.set(id);
      this.summary.set(null);
      this.job.set(null);
      this.migratedSourceArtifact.set(null);
      this.downloadingZip.set(false);
      this.zipError.set(null);
      this.error.set(null);
      this.loading.set(true);

      if (id === 'latest') {
        this.api
          .getLatestReport()
          .pipe(catchError(() => of(null)))
          .subscribe((s) => {
            if (this.reportId() !== id) {
              return;
            }
            this.loading.set(false);
            if (s) {
              this.summary.set(s);
              this.branchName.set(this.api.config()?.migration_branch_name ?? 'migration/spring-boot-3');
            } else {
              this.error.set('No migration report found yet. Run a migration first.');
            }
          });
      } else {
        this.api
          .getJob(id)
          .pipe(catchError(() => of(null)))
          .subscribe((job) => {
            if (this.reportId() !== id) {
              return;
            }
            this.loading.set(false);
            if (!job) {
              this.error.set(`Job '${id}' not found.`);
              return;
            }
            this.job.set(job);
            this.branchName.set(job.request.branch_name);
            if (job.summary) {
              this.summary.set(job.summary);
              if (id.startsWith('gha_') && job.summary.final_state === 'COMPLETED') {
                this.loadMigratedSourceArtifact(id);
              }
            } else if (job.status === 'FAILED') {
              this.error.set(job.error ?? 'Job failed before producing a summary.');
            } else {
              this.error.set('Job has no summary yet — it may still be running.');
            }
          });
      }
    });
  }

  onQueryInput(event: Event): void {
    this.query.set((event.target as HTMLInputElement).value);
  }

  readonly copiedSha = signal(false);
  readonly copiedApplyCmd = signal(false);
  readonly migratedSourceArtifact = signal<JobArtifact | null>(null);
  readonly downloadingZip = signal(false);
  readonly zipError = signal<string | null>(null);

  /** True when viewing a remote GitHub Actions migration run. */
  readonly isGhaJob = computed(
    () => this.reportId().startsWith('gha_') || Boolean(this.job()?.gha_run_url)
  );

  /** True only when the migration produced a distinct commit not pushed to GitHub. */
  readonly hasLocalMigrationCommit = computed(() => {
    const s = this.summary();
    return Boolean(s?.final_commit && s.final_commit !== s.baseline_commit);
  });

  /** Shell commands for applying the downloaded patch on a local migration branch. */
  readonly localApplyCommand = computed(
    () => `git checkout -b ${this.branchName()}
git apply coderevive-migration.patch`
  );

  private loadMigratedSourceArtifact(jobId: string): void {
    this.api
      .listJobArtifacts(jobId)
      .pipe(catchError(() => of(null)))
      .subscribe((res) => {
        if (this.reportId() !== jobId || !res) {
          return;
        }
        const zip =
          res.artifacts.find((a) => a.name === 'migrated-source' && !a.expired) ?? null;
        this.migratedSourceArtifact.set(zip);
      });
  }

  /** GitHub URL for the baseline (upstream) commit — always safe to link externally. */
  baselineCommitUrl(): string {
    const s = this.summary();
    if (!s || !this.isRemoteRepo() || !s.baseline_commit) {
      return '';
    }
    return `${repoWebUrl(s.repo_url)}/commit/${s.baseline_commit}`;
  }

  /** Copy the local final_commit SHA to the clipboard. */
  copyFinalSha(): void {
    const sha = this.summary()?.final_commit;
    if (!sha || typeof navigator === 'undefined' || !navigator.clipboard) {
      return;
    }
    navigator.clipboard.writeText(sha).then(() => {
      this.copiedSha.set(true);
      setTimeout(() => this.copiedSha.set(false), 1500);
    });
  }

  /** Copy the local git checkout + git apply commands to the clipboard. */
  copyApplyCommand(): void {
    if (typeof navigator === 'undefined' || !navigator.clipboard) {
      return;
    }
    navigator.clipboard.writeText(this.localApplyCommand()).then(() => {
      this.copiedApplyCmd.set(true);
      setTimeout(() => this.copiedApplyCmd.set(false), 1800);
    });
  }

  /** Download the migrated-source.zip artifact from GitHub Actions via the backend. */
  downloadMigratedZip(): void {
    const artifact = this.migratedSourceArtifact();
    const jobId = this.reportId();
    if (!artifact || !jobId.startsWith('gha_') || this.downloadingZip()) {
      return;
    }
    this.downloadingZip.set(true);
    this.zipError.set(null);
    this.api
      .downloadJobArtifact(jobId, artifact.artifact_id)
      .pipe(catchError(() => of(null)))
      .subscribe((blob) => {
        this.downloadingZip.set(false);
        if (!blob) {
          this.zipError.set(
            'Could not download migrated repository archive (it may have expired after 24 hours). Use Download .patch instead.'
          );
          return;
        }
        const prefix = this.repoShort() || 'coderevive';
        downloadBlob(`${prefix}-migrated.zip`, blob);
      });
  }

  formatArtifactSize(bytes: number): string {
    if (bytes >= 1024 * 1024) {
      return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    }
    if (bytes >= 1024) {
      return `${Math.round(bytes / 1024)} KB`;
    }
    return `${bytes} B`;
  }

  isRemoteRepo(): boolean {
    const summary = this.summary();
    return summary ? isRemoteRepoUrl(summary.repo_url) : false;
  }

  githubRepoUrl(): string {
    const s = this.summary();
    return s ? repoWebUrl(s.repo_url) : '';
  }

  repoShort(): string {
    const s = this.summary();
    return s ? repoName(s.repo_url) : '';
  }

  formatDuration(sec: number): string {
    return formatDuration(sec);
  }

  shortSha(sha: string | null): string {
    return shortSha(sha);
  }

  statusBadgeClass(fs: FileMigrationStatus): string {
    switch (fs.status) {
      case 'SUCCESS':
      case 'VALIDATED':
      case 'PATCH_APPLIED':
        return 'badge badge-green';
      case 'FAILED':
      case 'REJECTED':
        return 'badge badge-red';
      case 'SKIPPED':
        return 'badge';
      default:
        return 'badge badge-blue';
    }
  }

  toggleExpand(path: string): void {
    this.expanded.update((cur) => (cur === path ? null : path));
  }

  setFilter(f: LedgerFilter): void {
    this.filter.set(f);
  }

  downloadPatch(): void {
    const s = this.summary();
    if (!s) {
      return;
    }
    const chunks = s.file_statuses
      .map((f) => (f.diff ?? '').replace(/\r\n?/g, '\n').replace(/\n+$/, ''))
      .filter((chunk) => chunk.length > 0);
    const patch = chunks.length > 0 ? `${chunks.join('\n')}\n` : '';
    downloadBlob(
      'coderevive-migration.patch',
      new Blob([patch], { type: 'text/plain;charset=utf-8' })
    );
  }

  exportJson(): void {
    const s = this.summary();
    if (!s) {
      return;
    }
    downloadBlob('migration-report.json', new Blob([JSON.stringify(s, null, 2)], { type: 'application/json' }));
  }
}
