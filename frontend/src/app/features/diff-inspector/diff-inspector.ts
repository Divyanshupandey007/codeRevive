import { Component, computed, effect, input, signal } from '@angular/core';

import { FileMigrationStatus } from '../../core/models/api.models';
import { ParsedFileDiff, parseFileDiff, toSideBySide } from '../../core/utils/diff.util';

/**
 * Git Diff Inspector: left file drawer + right diff pane with
 * Unified / Side-by-Side toggle, Copy Patch and collapse control.
 * Diffs are parsed with diff2html and syntax-highlighted with highlight.js,
 * rendered in GitHub Dark High Contrast colors.
 */
@Component({
  selector: 'app-diff-inspector',
  imports: [],
  templateUrl: './diff-inspector.html',
  styleUrl: './diff-inspector.scss'
})
export class DiffInspector {
  readonly files = input.required<FileMigrationStatus[]>();

  readonly viewMode = signal<'unified' | 'sxs'>('unified');
  readonly selectedPath = signal<string | null>(null);
  readonly collapsed = signal(false);
  readonly copied = signal(false);

  readonly parsed = computed(() =>
    this.files()
      .map((f) => parseFileDiff(f))
      .filter((p): p is ParsedFileDiff => p !== null)
  );

  readonly selected = computed(() => {
    const path = this.selectedPath();
    const all = this.parsed();
    return all.find((p) => p.path === path) ?? all[0] ?? null;
  });

  readonly sxs = computed(() => toSideBySide(this.selected()?.hunks ?? []));

  constructor() {
    effect(() => {
      const all = this.parsed();
      if (all.length > 0 && !all.some((p) => p.path === this.selectedPath())) {
        this.selectedPath.set(all[0].path);
      }
    });
  }

  select(path: string): void {
    this.selectedPath.set(path);
  }

  fileName(path: string): string {
    return path.replace(/\\/g, '/').split('/').pop() || path;
  }

  directoryPath(path: string): string {
    const normalized = path.replace(/\\/g, '/');
    const separator = normalized.lastIndexOf('/');
    return separator >= 0 ? normalized.slice(0, separator) : 'Repository root';
  }

  copyPatch(): void {
    const sel = this.selected();
    if (!sel) {
      return;
    }
    navigator.clipboard.writeText(sel.raw).then(
      () => {
        this.copied.set(true);
        setTimeout(() => this.copied.set(false), 1600);
      },
      () => this.copied.set(false)
    );
  }
}
