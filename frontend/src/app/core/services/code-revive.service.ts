import { Injectable, inject, signal } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable, catchError, of, tap } from 'rxjs';

import {
  BackendConfig,
  DeleteJobResponse,
  HealthResponse,
  JobEventsResponse,
  JobListResponse,
  JobStatus,
  MigrationJobRecord,
  MigrationJobRequest,
  MigrationRunSummary
} from '../models/api.models';

export type BackendStatus = 'checking' | 'connected' | 'disconnected';

/**
 * Single injectable facade over the CodeRevive FastAPI backend.
 * All cross-component state lives here as signals.
 */
@Injectable({ providedIn: 'root' })
export class CodeReviveService {
  private readonly http = inject(HttpClient);
  private readonly api =
    typeof window !== 'undefined' &&
    (window.location.hostname === 'localhost' ||
      window.location.hostname === '127.0.0.1')
      ? '/api'
      : 'https://coderevive.hostless.app/api';

  readonly backendStatus = signal<BackendStatus>('checking');
  readonly config = signal<BackendConfig | null>(null);
  readonly jobs = signal<MigrationJobRecord[]>([]);
  readonly jobsTotal = signal(0);
  readonly latestAvailable = signal<boolean | null>(null);

  /** GET /api/health — liveness probe used by the top-nav status pill. */
  checkHealth(): void {
    this.http
      .get<HealthResponse>(`${this.api}/health`)
      .pipe(
        tap(() => this.backendStatus.set('connected')),
        catchError(() => {
          this.backendStatus.set('disconnected');
          return of(null);
        })
      )
      .subscribe();
  }

  /** GET /api/config — active non-secret backend defaults (LLM provider, targets). */
  loadConfig(): void {
    if (this.config()) {
      return;
    }
    this.http
      .get<BackendConfig>(`${this.api}/config`)
      .pipe(
        tap((c) => this.config.set(c)),
        catchError(() => of(null))
      )
      .subscribe();
  }

  /** GET /api/migrations/jobs — newest-first job history for the launcher. */
  refreshJobs(status?: JobStatus): void {
    let params = new HttpParams().set('limit', 50).set('offset', 0);
    if (status) {
      params = params.set('status', status);
    }
    this.http
      .get<JobListResponse>(`${this.api}/migrations/jobs`, { params })
      .pipe(
        tap((res) => {
          this.jobs.set(res.items);
          this.jobsTotal.set(res.total);
          this.latestAvailable.set(res.latest_available ?? null);
        }),
        catchError(() => of(null))
      )
      .subscribe();
  }

  /** POST /api/migrations/jobs — queue an async migration job (202). */
  submitJob(request: MigrationJobRequest): Observable<MigrationJobRecord> {
    return this.http.post<MigrationJobRecord>(`${this.api}/migrations/jobs`, request);
  }

  /** GET /api/migrations/jobs/{id} — job status + result. */
  getJob(jobId: string): Observable<MigrationJobRecord> {
    return this.http.get<MigrationJobRecord>(`${this.api}/migrations/jobs/${jobId}`);
  }

  /** GET /api/migrations/jobs/{id}/events — streamed progress events. */
  getJobEvents(jobId: string): Observable<JobEventsResponse> {
    return this.http.get<JobEventsResponse>(`${this.api}/migrations/jobs/${jobId}/events`);
  }

  /** DELETE /api/migrations/jobs/{id}. */
  deleteJob(jobId: string): Observable<DeleteJobResponse> {
    return this.http.delete<DeleteJobResponse>(`${this.api}/migrations/jobs/${jobId}`);
  }

  /** POST /api/migrations/jobs/{id}/cancel — request cancellation of a queued or running job. */
  cancelJob(jobId: string): Observable<DeleteJobResponse> {
    return this.http.post<DeleteJobResponse>(`${this.api}/migrations/jobs/${jobId}/cancel`, null);
  }

  /** GET /api/migrations/latest — latest completed migration summary (404 if none). */
  getLatestReport(): Observable<MigrationRunSummary> {
    return this.http.get<MigrationRunSummary>(`${this.api}/migrations/latest`);
  }
}
