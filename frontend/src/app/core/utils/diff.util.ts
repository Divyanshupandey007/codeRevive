import { parse } from 'diff2html/lib-esm/diff-parser';
import { LineType } from 'diff2html/lib-esm/types';
import hljs from 'highlight.js/lib/core';
import java from 'highlight.js/lib/languages/java';
import xml from 'highlight.js/lib/languages/xml';
import properties from 'highlight.js/lib/languages/properties';
import yaml from 'highlight.js/lib/languages/yaml';
import jsonLang from 'highlight.js/lib/languages/json';
import bash from 'highlight.js/lib/languages/bash';
import plaintext from 'highlight.js/lib/languages/plaintext';

import { FileMigrationStatus } from '../models/api.models';
import { escapeHtml } from './format.util';

hljs.registerLanguage('java', java);
hljs.registerLanguage('xml', xml);
hljs.registerLanguage('properties', properties);
hljs.registerLanguage('yaml', yaml);
hljs.registerLanguage('json', jsonLang);
hljs.registerLanguage('bash', bash);
hljs.registerLanguage('plaintext', plaintext);

export type DiffLineType = 'ins' | 'del' | 'ctx';

export interface DiffLineView {
  type: DiffLineType;
  oldNo: number | null;
  newNo: number | null;
  text: string;
  html: string;
}

export interface DiffHunkView {
  header: string;
  oldStart: number;
  newStart: number;
  lines: DiffLineView[];
}

export interface ParsedFileDiff {
  path: string;
  added: number;
  deleted: number;
  language: string;
  hunks: DiffHunkView[];
  raw: string;
}

export interface SxsRow {
  left: DiffLineView | null;
  right: DiffLineView | null;
}

export interface SxsHunk {
  header: string;
  oldStart: number;
  newStart: number;
  rows: SxsRow[];
}

export function languageForPath(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() ?? '';
  switch (ext) {
    case 'java':
    case 'gradle':
    case 'kts':
      return 'java';
    case 'xml':
      return 'xml';
    case 'properties':
      return 'properties';
    case 'yml':
    case 'yaml':
      return 'yaml';
    case 'json':
      return 'json';
    case 'sh':
    case 'bash':
      return 'bash';
    default:
      return 'plaintext';
  }
}

export function highlightLine(text: string, language: string): string {
  if (!text) {
    return '';
  }
  try {
    return hljs.highlight(text, { language, ignoreIllegals: true }).value;
  } catch {
    return escapeHtml(text);
  }
}

/**
 * Parse one file's unified git diff (from FileMigrationStatus.diff)
 * via the diff2html parser into a render-ready view model.
 */
export function parseFileDiff(file: FileMigrationStatus): ParsedFileDiff | null {
  if (!file.diff) {
    return null;
  }
  let files: ReturnType<typeof parse>;
  try {
    files = parse(file.diff);
  } catch {
    return null;
  }
  const f = files[0];
  if (!f) {
    return null;
  }
  const language = languageForPath(file.file_path) || f.language || 'plaintext';
  const hunks: DiffHunkView[] = f.blocks.map((b) => ({
    header: b.header,
    oldStart: b.oldStartLine,
    newStart: b.newStartLine,
    lines: b.lines.map((l) => ({
      type: l.type === LineType.INSERT ? 'ins' : l.type === LineType.DELETE ? 'del' : 'ctx',
      oldNo: l.oldNumber ?? null,
      newNo: l.newNumber ?? null,
      text: l.content,
      html: highlightLine(l.content, language)
    }))
  }));
  return {
    path: file.file_path,
    added: f.addedLines,
    deleted: f.deletedLines,
    language,
    hunks,
    raw: file.diff
  };
}

/** Pair delete/insert runs into side-by-side rows. */
export function toSideBySide(hunks: DiffHunkView[]): SxsHunk[] {
  return hunks.map((h) => {
    const rows: SxsRow[] = [];
    const lines = h.lines;
    let i = 0;
    while (i < lines.length) {
      const l = lines[i];
      if (l.type === 'ctx') {
        rows.push({ left: l, right: l });
        i++;
        continue;
      }
      if (l.type === 'del') {
        const dels: DiffLineView[] = [];
        while (i < lines.length && lines[i].type === 'del') {
          dels.push(lines[i]);
          i++;
        }
        const ins: DiffLineView[] = [];
        while (i < lines.length && lines[i].type === 'ins') {
          ins.push(lines[i]);
          i++;
        }
        const n = Math.max(dels.length, ins.length);
        for (let k = 0; k < n; k++) {
          rows.push({ left: dels[k] ?? null, right: ins[k] ?? null });
        }
        continue;
      }
      rows.push({ left: null, right: l });
      i++;
    }
    return { header: h.header, oldStart: h.oldStart, newStart: h.newStart, rows };
  });
}
