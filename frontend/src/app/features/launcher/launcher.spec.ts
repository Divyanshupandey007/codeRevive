import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter, Router } from '@angular/router';

import { CodeReviveService } from '../../core/services/code-revive.service';
import { MigrationJobRecord } from '../../core/models/api.models';
import { Launcher } from './launcher';

function makeJob(overrides: Partial<MigrationJobRecord> = {}): MigrationJobRecord {
  return {
    job_id: 'mig_test',
    status: 'COMPLETED',
    stage: 'completed',
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
    completed_at: '2026-01-01T00:00:03Z',
    events: [],
    summary: null,
    summary_path: null,
    error: null,
    error_type: null,
    ...overrides
  };
}

describe('Launcher', () => {
  let fixture: ComponentFixture<Launcher>;
  let component: Launcher;
  let http: HttpTestingController;
  let api: CodeReviveService;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [Launcher],
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])]
    }).compileComponents();

    http = TestBed.inject(HttpTestingController);
    api = TestBed.inject(CodeReviveService);
    fixture = TestBed.createComponent(Launcher);
    component = fixture.componentInstance;

    http.expectOne('/api/migrations/jobs?limit=50&offset=0').flush({ items: [], total: 0, limit: 50, offset: 0 });
    http.expectOne('/api/config').flush({
      repository_presets: [
        { label: 'mybatis-spring-boot-jpetstore', url: 'https://github.com/kazuki43zoo/mybatis-spring-boot-jpetstore.git' },
      ],
      repo_url: 'https://github.com/org/demo.git',
      migration_branch_name: 'migration/spring-boot-3',
      target_spring_boot_version: '3.5.0',
      target_java_version: '21',
      use_openrewrite: false,
      openrewrite_mode: 'maven_only',
      default_retry_budget: 5,
      llm_provider: 'gemini',
      llm_model: 'gemini-2.5-flash',
      workspace_root: 'C:/portable/workspace'
    });
    fixture.detectChanges();
  });

  afterEach(() => {
    http.verify();
  });

  it('uses repository presets supplied by the backend', () => {
    expect(component.presets().length).toBe(1);
    expect(component.presets()[0].label).toBe('mybatis-spring-boot-jpetstore');
  });

  it('renders the locked production profile without selectable controls', () => {
    const root: HTMLElement = fixture.nativeElement;
    const profile = root.querySelector('.profile-indicators');

    expect(root.querySelector('select')).toBeNull();
    expect(profile?.textContent).toContain('3.2.5');
    expect(profile?.textContent).toContain('17');
    expect(profile?.textContent).not.toContain('Migration Strategy');
    expect(profile?.textContent).not.toContain('Hybrid');
    expect(profile?.textContent).not.toContain('3.5.0');
    expect(profile?.textContent).not.toContain('21');
    expect(profile?.textContent).not.toContain('Maven-only');
  });

  it('always submits the locked profile despite conflicting backend defaults', () => {
    vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    component.repoUrl.set('https://github.com/org/demo.git');
    component.submit();

    const request = http.expectOne('/api/migrations/jobs');
    expect(request.request.body.target_spring_boot).toBe('3.2.5');
    expect(request.request.body.target_java).toBe('17');
    expect(request.request.body.use_openrewrite).toBe(true);
    expect(request.request.body.openrewrite_mode).toBe('hybrid');
    request.flush(makeJob({ status: 'QUEUED', stage: 'queued', completed_at: null }));
    http.expectOne('/api/migrations/jobs?limit=50&offset=0').flush({ items: [], total: 0, limit: 50, offset: 0 });
  });

  it('links completed jobs directly to reports and removes confirmed deletions', () => {
    const job = makeJob();
    api.jobs.set([job]);
    api.jobsTotal.set(1);
    fixture.detectChanges();

    const reportLink: HTMLAnchorElement | null = fixture.nativeElement.querySelector('a[href="/reports/mig_test"]');
    expect(reportLink?.textContent).toContain('View Report');

    component.requestDelete(job);
    component.confirmDelete(job);
    const request = http.expectOne('/api/migrations/jobs/mig_test');
    expect(request.request.method).toBe('DELETE');
    request.flush({ deleted: true, job_id: job.job_id });

    expect(api.jobs()).toEqual([]);
    expect(api.jobsTotal()).toBe(0);
    expect(component.deleteCandidateId()).toBeNull();
    expect(component.deletingJobId()).toBeNull();
  });
});
