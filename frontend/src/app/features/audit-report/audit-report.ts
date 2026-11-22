import { Component, computed, inject, signal } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { catchError, of } from 'rxjs';

import { CodeReviveService } from '../../core/services/code-revive.service';
import { FileMigrationStatus, MigrationJobRecord, MigrationRunSummary } from '../../core/models/api.models';
import { DiffInspector } from '../diff-inspector/diff-inspector';
import { downloadBlob, formatDuration, isRemoteRepoUrl, repoName, repoWebUrl, shortSha } from '../../core/utils/format.util';

type LedgerFilter = 'all' | 'passed' | 'failed' | 'openrewrite' | 'ai' | 'multi';

@Component({
  selector: 'app-audit-report',
  imports: [RouterLink, DatePipe, FormsModule, DiffInspector],
  templateUrl: './audit-report.html',
  styleUrl: './audit-report.scss'
})
export class AuditReport {
  private readonly api = inject(CodeReviveService);
  private readonly route = inject(ActivatedRoute);

  readonly reportId = this.route.snapshot.paramMap.get('id') ?? 'latest';

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

  /** True only when a migration has actually been run in this session. */
  readonly hasJobHistory = computed(() => this.api.jobs().length > 0);

  /** The summary to display: only non-null when an actual job run backs it (no stale leakage). */
  readonly displaySummary = computed<MigrationRunSummary | null>(() => {
    const s = this.summary();
    if (!s) {
      return null;
    }
    // A "latest" report must be backed by an actual job run in this session,
    // otherwise a stale summary file from a previous session would leak through.
    if (this.reportId === 'latest' && !this.hasJobHistory()) {
      return null;
    }
    return s;
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
    this.api.refreshJobs();
    if (this.reportId === 'latest') {
      this.api
        .getLatestReport()
        .pipe(catchError(() => of(null)))
        .subscribe((s) => {
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
        .getJob(this.reportId)
        .pipe(catchError(() => of(null)))
        .subscribe((job) => {
          this.loading.set(false);
          if (!job) {
            this.error.set(`Job '${this.reportId}' not found.`);
            return;
          }
          this.job.set(job);
          this.branchName.set(job.request.branch_name);
          if (job.summary) {
            this.summary.set(job.summary);
          } else if (job.status === 'FAILED') {
            this.error.set(job.error ?? 'Job failed before producing a summary.');
          } else {
            this.error.set('Job has no summary yet — it may still be running.');
          }
        });
    }
  }

  isRemoteRepo(): boolean {
    const summary = this.summary();
    return summary ? isRemoteRepoUrl(summary.repo_url) : false;
  }

  commitUrl(): string {
    const s = this.summary();
    if (!s || !this.isRemoteRepo()) {
      return '';
    }
    const sha = s.final_commit ?? s.baseline_commit;
    return `${repoWebUrl(s.repo_url)}/commit/${sha}`;
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
    const patch = s.file_statuses
      .filter((f) => f.diff)
      .map((f) => f.diff ?? '')
      .join('\n');
    downloadBlob('coderevive-migration.patch', new Blob([patch], { type: 'text/plain' }));
  }

  exportJson(): void {
    const s = this.summary();
    if (!s) {
      return;
    }
    downloadBlob('migration-report.json', new Blob([JSON.stringify(s, null, 2)], { type: 'application/json' }));
  }
}
