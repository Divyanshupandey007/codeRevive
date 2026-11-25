import { MigrationJobEvent } from '../models/api.models';

/** A console line derived from a backend job event. */
export interface LogLine {
  timestamp: string;
  tag: string;
  tagClass: string;
  message: string;
}

const TAGS = {
  error: { tag: 'ERROR', tagClass: 'tag-error' },
  openrewrite: { tag: 'OPENREWRITE', tagClass: 'tag-openrewrite' },
  repair: { tag: 'AI_REPAIR', tagClass: 'tag-repair' },
  sandbox: { tag: 'SANDBOX', tagClass: 'tag-sandbox' },
  build: { tag: 'BUILD_GREEN', tagClass: 'tag-build' },
  default: { tag: 'PIPELINE', tagClass: 'tag-default' }
} as const;

type TagKey = keyof typeof TAGS;

const ERROR_RE = /\b(error|fail|failed|failure|exception|violation|violations|aborted|timeout)\b/i;
const OPENREWRITE_RE = /\bopenrewrite\b/i;
const REPAIR_RE = /\b(repair|repairing|llm|ai_repair|generating_patch|patch|patched)\b/i;
const SANDBOX_RE = /\b(sandbox|docker|container)\b/i;
const BUILD_RE = /\b(build|maven|test|tests|verif\w*|integrity|commit|commiting|committing)\b/i;

/** Normalize raw backend progress text so production logs match local logs and never mention GitHub Actions. */
export function sanitizeLogMessage(rawMessage: string): string {
  let message = (rawMessage ?? '').trim();

  // Normalize any legacy/remote dispatch or GHA phrasing defensively
  message = message.replace(
    /^Migration dispatched to GitHub Actions run \d+ for\s+/i,
    'Migration job queued for '
  );
  message = message.replace(
    /^GitHub Actions run \d+ completed successfully\.?$/i,
    'Migration completed successfully.'
  );
  message = message.replace(
    /^GitHub Actions run status:\s*(.+)$/i,
    (_m, status: string) => `Migration status: ${status}`
  );
  message = message.replace(/^GitHub Actions step:\s*/i, '');
  message = message.replace(/GitHub Actions run \d+/gi, 'Migration run');
  message = message.replace(/GitHub Actions(?:\s+runner)?/gi, 'Migration worker');

  // Truncate absolute workspace paths in orchestration lines so real users
  // see the repo name instead of a machine-specific path.
  const orchestratingMatch = /^orchestrating:(.+)$/i.exec(message);
  if (orchestratingMatch) {
    const fullPath = orchestratingMatch[1].trim();
    const lastSegment = fullPath.split(/[\\/]/).filter(Boolean).pop() ?? fullPath;
    message = `orchestrating:${lastSegment}`;
  }

  return message;
}

/**
 * Classify a backend progress event into a colored console tag.
 * Backend emits "stage:message" strings; the stage prefix drives the tag.
 */
export function eventToLogLine(event: MigrationJobEvent): LogLine {
  const message = sanitizeLogMessage(event.message);
  const stage = (event.stage ?? '').trim().toLowerCase();
  const hay = `${stage} ${message}`;

  let key: TagKey = 'default';
  if (stage === 'queued' || stage === 'starting' || stage === 'ingesting' || stage === 'orchestrating') {
    key = ERROR_RE.test(message) ? 'error' : 'default';
  } else if (ERROR_RE.test(hay)) {
    key = 'error';
  } else if (OPENREWRITE_RE.test(hay)) {
    key = 'openrewrite';
  } else if (REPAIR_RE.test(hay)) {
    key = 'repair';
  } else if (SANDBOX_RE.test(hay)) {
    key = 'sandbox';
  } else if (BUILD_RE.test(hay)) {
    key = 'build';
  }

  const t = TAGS[key];
  return { timestamp: event.timestamp, tag: t.tag, tagClass: t.tagClass, message };
}
