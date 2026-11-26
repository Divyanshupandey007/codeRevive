import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  computed,
  effect,
  inject,
  signal,
  viewChild
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { DatePipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { catchError, of } from 'rxjs';

import { CodeReviveService } from '../../core/services/code-revive.service';
import { MigrationJobEvent, MigrationJobRecord } from '../../core/models/api.models';
import { eventToLogLine } from '../../core/utils/log-tag.util';
import { formatDuration, repoName, shortSha } from '../../core/utils/format.util';
import { activeStepForJob, failedStepForJob } from './pipeline-progress.util';

const STEPS = ['Init', 'Pass 1 (OpenRewrite)', 'Sandbox Verify', 'AI Repair', 'Integrity Check', 'Done'] as const;

@Component({
  selector: 'app-pipeline-monitor',
  imports: [RouterLink, DatePipe],
  templateUrl: './pipeline-monitor.html',
  styleUrl: './pipeline-monitor.scss',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class PipelineMonitor {
  private readonly api = inject(CodeReviveService);
  private readonly route = inject(ActivatedRoute);
  private readonly destroyRef = inject(DestroyRef);

  readonly jobId = signal<string>('');
  readonly steps = STEPS;

  readonly job = signal<MigrationJobRecord | null>(null);
  readonly events = signal<MigrationJobEvent[]>([]);
  readonly now = signal(Date.now());
  readonly loadError = signal<string | null>(null);
  readonly cancelling = signal(false);

  private readonly consoleRef = viewChild<ElementRef<HTMLDivElement>>('console');

  private readonly pollIntervalMs = 2_500;

  readonly logLines = computed(() => this.events().map(eventToLogLine));
  readonly isActiveJob = computed(() => {
    const status = this.job()?.status;
    return status === 'QUEUED' || status === 'RUNNING';
  });
  readonly showWorkingLoader = computed(() => this.isActiveJob());
  readonly failed = computed(() => this.job()?.status === 'FAILED');
  readonly completed = computed(() => this.job()?.status === 'COMPLETED');
  readonly hasAuditReport = computed(() => {
    const j = this.job();
    return Boolean(j && (j.status === 'COMPLETED' || j.summary));
  });
  readonly errorBadgeLabel = computed(() => {
    const errorType = (this.job()?.error_type ?? '').toLowerCase();
    if (errorType.includes('integrity') || errorType.includes('guardrail')) {
      return 'GUARDRAIL';
    }
    if (errorType.includes('environment')) {
      return 'ENV FAILURE';
    }
    if (errorType.includes('unsupported')) {
      return 'UNSUPPORTED';
    }
    return 'ERROR';
  });

  readonly failedStep = computed(() => {
    const job = this.job();
    return job && this.failed() ? failedStepForJob(job) : -1;
  });

  readonly activeStep = computed(() => {
    const job = this.job();
    if (!job) {
      return 0;
    }
    return this.failed() ? this.failedStep() : activeStepForJob(job);
  });

  readonly stageHint = computed(() => {
    if (!this.isActiveJob()) {
      return '';
    }
    switch (this.activeStep()) {
      case 0:
        return 'Preparing migration workspace and checking out repository…';
      case 1:
        return 'OpenRewrite is running. Large repositories can take several minutes.';
      case 2:
        return 'Maven verification is running in Docker. This can take several minutes.';
      case 3:
        return 'AI repair is in progress. The sandbox will verify each repaired build.';
      case 4:
        return 'Running post-migration integrity verification…';
      default:
        return '';
    }
  });

  readonly elapsedSeconds = computed(() => {
    const j = this.job();
    if (!j) {
      return 0;
    }
    const start = Date.parse(j.started_at ?? j.created_at);
    if (Number.isNaN(start)) {
      return 0;
    }
    const end = j.completed_at ? Date.parse(j.completed_at) : this.now();
    return Math.max(0, (end - start) / 1000);
  });

  readonly activeRound = computed(() => {
    const j = this.job();
    if (j?.summary) {
      return j.summary.total_retries;
    }
    return this.events().filter((e) => /repair|llm/i.test(e.stage)).length;
  });

  constructor() {
    this.route.paramMap.pipe(takeUntilDestroyed(this.destroyRef)).subscribe((params) => {
      const id = params.get('id') ?? '';
      this.jobId.set(id);
      this.job.set(null);
      this.events.set([]);
      this.loadError.set(null);
      this.cancelling.set(false);
      this.now.set(Date.now());
      if (id) {
        this.api.setActiveJobId(id);
        this.poll(id);
      }
    });

    if (typeof window !== 'undefined') {
      const nowTimer = setInterval(() => this.now.set(Date.now()), 1_000);
      const pollTimer = setInterval(() => {
        const id = this.jobId();
        const status = this.job()?.status;
        if (!id || this.loadError() || status === 'COMPLETED' || status === 'FAILED' || status === 'CANCELLED') {
          return;
        }
        this.poll(id);
      }, this.pollIntervalMs);
      this.destroyRef.onDestroy(() => {
        clearInterval(nowTimer);
        clearInterval(pollTimer);
      });
    }

    // Auto-scroll the console when already near the bottom.
    effect(() => {
      const count = this.logLines().length;
      const working = this.showWorkingLoader();
      if (count > 0 || working) {
        queueMicrotask(() => {
          const el = this.consoleRef()?.nativeElement;
          if (!el) {
            return;
          }
          const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 64;
          if (nearBottom) {
            el.scrollTop = el.scrollHeight;
          }
        });
      }
    });
  }

  private poll(id: string): void {
    if (!id) {
      return;
    }
    this.api
      .getJob(id)
      .pipe(
        catchError((error: HttpErrorResponse) => {
          if (this.jobId() !== id) {
            return of(null);
          }
          if (error.status === 404) {
            this.loadError.set(`Job '${id}' was not found. It may have been cleaned up or deleted.`);
          }
          return of(null);
        })
      )
      .subscribe((job) => {
        if (job && this.jobId() === id) {
          this.loadError.set(null);
          this.job.set(job);
        }
      });
    this.api
      .getJobEvents(id)
      .pipe(catchError(() => of(null)))
      .subscribe((res) => {
        if (res && this.jobId() === id) {
          this.events.set(res.events);
        }
      });
  }

  retryLoad(): void {
    const id = this.jobId();
    if (!id) {
      return;
    }
    this.loadError.set(null);
    this.poll(id);
  }

  cancel(): void {
    const id = this.jobId();
    if (!id || this.cancelling()) {
      return;
    }
    this.cancelling.set(true);
    this.api.cancelJob(id).subscribe({
      next: () => {
        this.cancelling.set(false);
        if (this.jobId() === id) {
          this.poll(id);
        }
      },
      error: () => {
        this.cancelling.set(false);
      }
    });
  }

  formatDuration(seconds: number): string {
    return formatDuration(seconds);
  }

  repoShort(): string {
    const j = this.job();
    return j ? repoName(j.request.repo_url) : '';
  }

  commitShort(): string {
    return shortSha(this.job()?.summary?.final_commit ?? this.job()?.summary?.baseline_commit ?? null);
  }
}
