import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { downloadModules } from '../electron/download-service';
import type { DownloadProgressEvent, ModuleWithItems } from '../src/shared/types';

const createdDirs: string[] = [];

afterEach(async () => {
  await Promise.all(
    createdDirs.splice(0, createdDirs.length).map(async (directoryPath) => {
      await fs.rm(directoryPath, { recursive: true, force: true });
    })
  );
});

async function createTempDir(): Promise<string> {
  const directoryPath = await fs.mkdtemp(path.join(os.tmpdir(), 'canvas-download-test-'));
  createdDirs.push(directoryPath);
  return directoryPath;
}

function createModuleFixture(): ModuleWithItems[] {
  return [
    {
      id: 200,
      name: 'Week 1',
      items: [{ id: 1, type: 'File', title: 'Lecture Notes.pdf', contentId: 999, courseId: 5 }]
    }
  ];
}

describe('downloadModules', () => {
  it('downloads files into course/module folders', async () => {
    const tempRoot = await createTempDir();
    const events: DownloadProgressEvent[] = [];

    const result = await downloadModules(
      {
        courseId: 5,
        courseName: 'History 101',
        modules: [{ id: 200, name: 'Week 1' }]
      },
      {
        client: {
          getCourseModules: async () => createModuleFixture(),
          getFile: async () => ({ id: 999, fileName: 'Lecture Notes.pdf', downloadUrl: 'https://example.com/file' })
        },
        fetchFile: async () => new Response(Buffer.from('test-content'), { status: 200 }),
        downloadRootFromEnv: tempRoot,
        logger: { log: async () => undefined }
      },
      (event) => events.push(event)
    );

    expect(result.downloadedCount).toBe(1);
    expect(result.failedCount).toBe(0);

    const destination = path.join(tempRoot, 'History 101', 'Week 1', 'Lecture Notes.pdf');
    const downloadedContent = await fs.readFile(destination, 'utf8');
    expect(downloadedContent).toBe('test-content');
    expect(events.some((event) => event.status === 'done')).toBe(true);
  });

  it('skips existing files by filename', async () => {
    const tempRoot = await createTempDir();
    const targetDirectory = path.join(tempRoot, 'Math 101', 'Week 2');
    await fs.mkdir(targetDirectory, { recursive: true });
    await fs.writeFile(path.join(targetDirectory, 'Worksheet.pdf'), 'existing');

    let fetchCalls = 0;
    const result = await downloadModules(
      {
        courseId: 5,
        courseName: 'Math 101',
        modules: [{ id: 201, name: 'Week 2' }]
      },
      {
        client: {
          getCourseModules: async () => [
            {
              id: 201,
              name: 'Week 2',
              items: [{ id: 2, type: 'File', title: 'Worksheet.pdf', contentId: 1234, courseId: 5 }]
            }
          ],
          getFile: async () => ({ id: 1234, fileName: 'Worksheet.pdf', downloadUrl: 'https://example.com/worksheet' })
        },
        fetchFile: async () => {
          fetchCalls += 1;
          return new Response(Buffer.from('new-content'), { status: 200 });
        },
        downloadRootFromEnv: tempRoot,
        logger: { log: async () => undefined }
      },
      () => undefined
    );

    expect(result.skippedCount).toBe(1);
    expect(result.downloadedCount).toBe(0);
    expect(fetchCalls).toBe(0);
  });
});
