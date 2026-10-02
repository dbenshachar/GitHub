import { describe, expect, it } from 'vitest';
import { countUnsupportedModuleItems, getDownloadableModuleFiles } from '../src/shared/module-filters';
import type { ModuleWithItems } from '../src/shared/types';

const moduleFixture: ModuleWithItems = {
  id: 10,
  name: 'Week 1',
  items: [
    { id: 1, type: 'File', title: 'Syllabus.pdf', contentId: 101, courseId: 7 },
    { id: 2, type: 'Page', title: 'Overview', courseId: 7 },
    { id: 3, type: 'File', title: 'Missing Content ID', courseId: 7 }
  ]
};

describe('module filters', () => {
  it('keeps only file items with contentId', () => {
    const files = getDownloadableModuleFiles(moduleFixture);
    expect(files).toHaveLength(1);
    expect(files[0].fileId).toBe(101);
  });

  it('counts unsupported items', () => {
    expect(countUnsupportedModuleItems(moduleFixture)).toBe(2);
  });
});
