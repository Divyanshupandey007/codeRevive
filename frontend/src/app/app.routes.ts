import { Routes } from '@angular/router';

import { Launcher } from './features/launcher/launcher';
import { PipelineMonitor } from './features/pipeline-monitor/pipeline-monitor';
import { AuditReport } from './features/audit-report/audit-report';

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'launcher' },
  { path: 'launcher', component: Launcher, title: 'CodeRevive — Launcher' },
  { path: 'jobs', component: PipelineMonitor, title: 'CodeRevive — Pipeline Monitor' },
  { path: 'jobs/:id', component: PipelineMonitor, title: 'CodeRevive — Pipeline Monitor' },
  { path: 'reports/:id', component: AuditReport, title: 'CodeRevive — Audit Report' },
  { path: '**', redirectTo: 'launcher' }
];
