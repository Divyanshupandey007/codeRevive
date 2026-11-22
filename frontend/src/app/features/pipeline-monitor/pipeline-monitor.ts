import { Component, DestroyRef, ElementRef, computed, effect, inject, signal, viewChild } from '@angular/core';
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
  styleUrl: './pipeline-monitor.scss'
})
export class PipelineMonitor {
  private readonly api = inject(CodeReviveService);
  private readonly route = inject(ActivatedRoute);
  private readonly destroyRef = inject(DestroyRef);

  readonly jobId = this.route.snapshot.paramMap.get('id') ?? '';
  readonly steps = STEPS;

  readonly job = signal<MigrationJobRecord | null>(null);
  readonly events = signal<MigrationJobEvent[]>([]);
  readonly paused = signal(false);
  readonly now = signal(Date.now());
  readonly loadError = signal<string | null>(null);

  private readonly consoleRef = viewChild<ElementRef<HTMLDivElement>>('console');

  readonly logLines = computed(() => this.events().map(eventToLogLine));

  /** Console lines annotated with whether a minimal loader should follow them.
   * A loader appears after the last line while the job is still running, or
   * after any line when the gap to the next line exceeds the load threshold. */
  readonly LOAD_GAP_MS = 1000;

  readonly consoleLines = computed(() => {
    const events = this.events();
    const job = this.job();
    const isRunning = job?.status === 'RUNNING';
    const now = this.now();
    return events.map((event, index) => {
      const line = eventToLogLine(event);
      let showLoader = false;
      if (index === events.length - 1 && isRunning) {
        const t = Date.parse(event.timestamp);
        if (!Number.isNaN(t) && now - t > this.LOAD_GAP_MS) {
          // Last printed line and no new activity for over a second while still running:
          // show a minimal loader so the job does not look stuck.
          showLoader = true;
        }
      }
      return { line, showLoader };
    });
  });
  readonly failed = computed(() => this.job()?.status === 'FAILED');
  readonly completed = computed(() => this.job()?.status === 'COMPLETED');

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
    if (this.job()?.status !== 'RUNNING') {
      return '';
    }
    switch (this.activeStep()) {
      case 1:
        return 'OpenRewrite is running. Large repositories can take several minutes.';
      case 2:
        return 'Maven verification is running in Docker. This can take several minutes.';
      case 3:
        return 'AI repair is in progress. The sandbox will verify each repaired build.';
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
    if (this.jobId) {
      this.poll();
      const timer = setInterval(() => {
        this.now.set(Date.now());
        const status = this.job()?.status;
        if (this.loadError() || status === 'COMPLETED' || status === 'FAILED') {
          return;
        }
        this.poll();
      }, 1_000);
      this.destroyRef.onDestroy(() => clearInterval(timer));
    }

    // Auto-scroll the console unless paused.
    effect(() => {
      const lines = this.logLines();
      const paused = this.paused();
      if (!paused && lines.length > 0) {
        queueMicrotask(() => {
          const el = this.consoleRef()?.nativeElement;
          if (el) {
            el.scrollTop = el.scrollHeight;
          }
        });
      }
    });
  }

  private poll(): void {
    if (!this.jobId) {
      return;
    }
    this.api
      .getJob(this.jobId)
      .pipe(
        catchError((error: HttpErrorResponse) => {
          if (error.status === 404) {
            this.loadError.set(`Job '${this.jobId}' was not found. It may have been deleted.`);
          }
          return of(null);
        })
      )
      .subscribe((job) => {
        if (job) {
          this.loadError.set(null);
          this.job.set(job);
        }
      });
    this.api
      .getJobEvents(this.jobId)
      .pipe(catchError(() => of(null)))
      .subscribe((res) => {
        if (res) {
          this.events.set(res.events);
        }
      });
  }

  refresh(): void {
    this.loadError.set(null);
    this.poll();
  }

  togglePause(): void {
    this.paused.update((p) => !p);
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
