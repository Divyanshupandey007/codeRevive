import { isRemoteRepoUrl, repoWebUrl } from './format.util';

describe('repository URL helpers', () => {
  it('allows browser links only for remote HTTP and SSH repositories', () => {
    expect(isRemoteRepoUrl('https://github.com/org/demo.git')).toBe(true);
    expect(isRemoteRepoUrl('git@github.com:org/demo.git')).toBe(true);
    expect(isRemoteRepoUrl('D:/Projects/codeRevive/backend/tests/fixtures/demo')).toBe(false);
    expect(isRemoteRepoUrl('/home/user/demo')).toBe(false);
  });

  it('normalizes SSH URLs for browser navigation', () => {
    expect(repoWebUrl('git@github.com:org/demo.git')).toBe('https://github.com/org/demo');
  });
});
