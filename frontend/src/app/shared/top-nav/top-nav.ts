import { Component, DestroyRef, computed, inject } from '@angular/core';
import { RouterLink, RouterLinkActive } from '@angular/router';

import { CodeReviveService } from '../../core/services/code-revive.service';

@Component({
  selector: 'app-top-nav',
  imports: [RouterLink, RouterLinkActive],
  templateUrl: './top-nav.html',
  styleUrl: './top-nav.scss'
})
export class TopNav {
  private readonly api = inject(CodeReviveService);
  private readonly destroyRef = inject(DestroyRef);

  readonly llmLabel = computed(() => {
    const c = this.api.config();
    if (!c) {
      return 'No LLM found';
    }
    return c.llm_model ?? c.llm_provider ?? '—';
  });
  readonly monitorLink = computed(() => {
    const first = this.api.jobs()[0];
    return first ? (['/jobs', first.job_id] as const) : (['/jobs'] as const);
  });

  constructor() {
    this.api.loadConfig();
    this.api.refreshJobs();
    const timer = setInterval(() => {
      this.api.loadConfig();
    }, 10_000);
    this.destroyRef.onDestroy(() => clearInterval(timer));
  }
}
