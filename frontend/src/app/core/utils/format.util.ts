/** Small pure formatting helpers shared across feature components. */

export function formatDuration(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) {
    return `${h}h ${String(m).padStart(2, '0')}m`;
  }
  if (m > 0) {
    return `${m}m ${String(sec).padStart(2, '0')}s`;
  }
  return `${sec}s`;
}

export function shortSha(sha: string | null | undefined): string {
  if (!sha) {
    return '—';
  }
  return sha.length > 10 ? sha.slice(0, 7) : sha;
}

/** True only for repository URLs that have a corresponding browser location. */
export function isRemoteRepoUrl(repoUrl: string): boolean {
  const url = repoUrl.trim();
  return /^https?:\/\//i.test(url) || /^git@/i.test(url);
}

/** Normalize a git URL (https or git@) to a browsable web URL. */
export function repoWebUrl(repoUrl: string): string {
  const u = repoUrl.trim();
  const ssh = u.match(/^git@([^:]+):(.+?)(?:\.git)?$/);
  if (ssh) {
    return `https://${ssh[1]}/${ssh[2]}`;
  }
  return u.replace(/\.git$/, '').replace(/\/$/, '');
}

/** Last path segment of a repo URL, e.g. "mybatis-spring-boot-jpetstore". */
export function repoName(repoUrl: string): string {
  const web = repoWebUrl(repoUrl);
  return web.split('/').filter(Boolean).pop() ?? web;
}

export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** Trigger a browser download for a Blob. */
export function downloadBlob(fileName: string, blob: Blob): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = fileName;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
