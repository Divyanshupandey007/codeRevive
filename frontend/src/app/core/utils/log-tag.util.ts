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

/**
 * Classify a backend progress event into a colored console tag.
 * Backend emits "stage:message" strings; the stage prefix drives the tag.
 */
export function eventToLogLine(event: MigrationJobEvent): LogLine {
  const hay = `${event.stage} ${event.message}`.toLowerCase();
  let key: TagKey = 'default';
  if (hay.includes('error') || hay.includes('fail') || hay.includes('exception') || hay.includes('violation')) {
    key = 'error';
  } else if (hay.includes('openrewrite')) {
    key = 'openrewrite';
  } else if (hay.includes('repair') || hay.includes('llm') || hay.includes('ai_') || hay.includes('patch')) {
    key = 'repair';
  } else if (hay.includes('sandbox') || hay.includes('docker') || hay.includes('container')) {
    key = 'sandbox';
  } else if (
    hay.includes('build') ||
    hay.includes('maven') ||
    hay.includes('test') ||
    hay.includes('verif') ||
    hay.includes('integrity') ||
    hay.includes('commit')
  ) {
    key = 'build';
  }
  const t = TAGS[key];

  // Truncate absolute workspace paths in orchestration lines so real users
  // see the repo name instead of a machine-specific path.
  let message = event.message;
  const orchestratingMatch = /^orchestrating:(.+)$/i.exec(event.message);
  if (orchestratingMatch) {
    const fullPath = orchestratingMatch[1].trim();
    const lastSegment = fullPath.split(/[\\/]/).filter(Boolean).pop() ?? fullPath;
    message = `orchestrating:${lastSegment}`;
  }

  return { timestamp: event.timestamp, tag: t.tag, tagClass: t.tagClass, message };
}
