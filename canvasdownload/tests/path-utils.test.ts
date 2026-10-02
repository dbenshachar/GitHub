import { describe, expect, it } from 'vitest';
import { buildModuleDirectory, sanitizePathSegment } from '../electron/path-utils';

describe('path utils', () => {
  it('sanitizes invalid path characters', () => {
    expect(sanitizePathSegment('Course: Intro/101')).toBe('Course_ Intro_101');
  });

  it('builds course/module directory path', () => {
    const result = buildModuleDirectory('/tmp/CanvasDownloads', 'Course 1', 'Week 1');
    expect(result).toContain('/tmp/CanvasDownloads');
    expect(result.endsWith('Course 1/Week 1')).toBe(true);
  });
});
