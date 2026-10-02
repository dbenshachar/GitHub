import fs from 'node:fs/promises';
import path from 'node:path';
import { getDownloadableModuleFiles } from '../src/shared/module-filters';
import { buildModuleDirectory, resolveDownloadRoot, sanitizePathSegment } from './path-utils';
import type { CanvasFileInfo } from './canvas-client';
import type { DownloadLogger } from './download-logger';
import type {
  DownloadFailure,
  DownloadProgressEvent,
  DownloadRequest,
  DownloadResult,
  DownloadStatus,
  ModuleWithItems
} from '../src/shared/types';

interface DownloadDeps {
  client: {
    getCourseModules: (courseId: number) => Promise<ModuleWithItems[]>;
    getFile: (fileId: number) => Promise<CanvasFileInfo>;
  };
  fetchFile: (url: string) => Promise<Response>;
  downloadRootFromEnv?: string;
  logger: DownloadLogger;
}

interface DownloadTask {
  moduleId: number;
  moduleName: string;
  fileId: number;
  fileNameHint: string;
}

const MAX_FILE_RETRIES = 2;

export async function downloadModules(
  request: DownloadRequest,
  deps: DownloadDeps,
  emitProgress: (event: DownloadProgressEvent) => void
): Promise<DownloadResult> {
  const root = resolveDownloadRoot(deps.downloadRootFromEnv);
  await fs.mkdir(root, { recursive: true });

  const selectedModuleIdSet = new Set(request.modules.map((module) => module.id));
  const moduleNameById = new Map(request.modules.map((module) => [module.id, module.name]));

  const modules = await deps.client.getCourseModules(request.courseId);
  const selectedModules = modules.filter((module) => selectedModuleIdSet.has(module.id));

  const tasks: DownloadTask[] = selectedModules.flatMap((module) =>
    getDownloadableModuleFiles(module).map((downloadableFile) => ({
      moduleId: downloadableFile.moduleId,
      moduleName: downloadableFile.moduleName,
      fileId: downloadableFile.fileId,
      fileNameHint: downloadableFile.fileNameHint
    }))
  );

  for (const task of tasks) {
    pushProgress(emitProgress, {
      courseId: request.courseId,
      moduleId: task.moduleId,
      moduleName: task.moduleName,
      fileName: task.fileNameHint,
      status: 'queued'
    });
  }

  let downloadedCount = 0;
  let skippedCount = 0;
  let failedCount = 0;
  const failures: DownloadFailure[] = [];

  await deps.logger.log('download_start', {
    courseId: request.courseId,
    courseName: request.courseName,
    moduleCount: selectedModules.length,
    fileCount: tasks.length,
    downloadRoot: root
  });

  const workerTasks = tasks.map((task) => async () => {
    const moduleName = moduleNameById.get(task.moduleId) ?? task.moduleName;

    try {
      const fileInfo = await deps.client.getFile(task.fileId);
      const fileName = sanitizePathSegment(fileInfo.fileName || task.fileNameHint);
      const moduleDir = buildModuleDirectory(root, request.courseName, moduleName);
      await fs.mkdir(moduleDir, { recursive: true });
      const destination = path.join(moduleDir, fileName);

      if (await fileExists(destination)) {
        skippedCount += 1;
        pushProgress(emitProgress, {
          courseId: request.courseId,
          moduleId: task.moduleId,
          moduleName,
          fileName,
          status: 'skipped',
          message: 'File already exists'
        });

        await deps.logger.log('download_skipped', {
          moduleId: task.moduleId,
          moduleName,
          fileName,
          destination
        });
        return;
      }

      pushProgress(emitProgress, {
        courseId: request.courseId,
        moduleId: task.moduleId,
        moduleName,
        fileName,
        status: 'downloading'
      });

      const dataBuffer = await downloadFileWithRetry(fileInfo, deps.fetchFile);
      await fs.writeFile(destination, dataBuffer);

      downloadedCount += 1;
      pushProgress(emitProgress, {
        courseId: request.courseId,
        moduleId: task.moduleId,
        moduleName,
        fileName,
        status: 'done'
      });

      await deps.logger.log('download_done', {
        moduleId: task.moduleId,
        moduleName,
        fileName,
        destination,
        bytes: dataBuffer.length
      });
    } catch (error) {
      const reason = error instanceof Error ? error.message : 'Unknown error';
      const fileName = sanitizePathSegment(task.fileNameHint);
      failedCount += 1;
      failures.push({
        moduleId: task.moduleId,
        moduleName,
        fileName,
        reason
      });

      pushProgress(emitProgress, {
        courseId: request.courseId,
        moduleId: task.moduleId,
        moduleName,
        fileName,
        status: 'failed',
        message: reason
      });

      await deps.logger.log('download_failed', {
        moduleId: task.moduleId,
        moduleName,
        fileName,
        reason
      });
    }
  });

  await runWithConcurrency(workerTasks, 4);

  const result: DownloadResult = {
    downloadedCount,
    skippedCount,
    failedCount,
    failures
  };

  await deps.logger.log('download_finish', result);
  return result;
}

async function downloadFileWithRetry(fileInfo: CanvasFileInfo, fetchFile: (url: string) => Promise<Response>): Promise<Buffer> {
  let attempt = 0;

  while (attempt <= MAX_FILE_RETRIES) {
    try {
      const response = await fetchFile(fileInfo.downloadUrl);
      if (!response.ok) {
        if ((response.status === 429 || response.status >= 500) && attempt < MAX_FILE_RETRIES) {
          await sleep(500 * 2 ** attempt);
          attempt += 1;
          continue;
        }
        throw new Error(`Download failed (${response.status}) for ${fileInfo.fileName}`);
      }

      const arrayBuffer = await response.arrayBuffer();
      return Buffer.from(arrayBuffer);
    } catch (error) {
      if (attempt < MAX_FILE_RETRIES) {
        await sleep(500 * 2 ** attempt);
        attempt += 1;
        continue;
      }

      throw error;
    }
  }

  throw new Error(`Download failed for ${fileInfo.fileName}`);
}

async function runWithConcurrency(tasks: Array<() => Promise<void>>, concurrency: number): Promise<void> {
  if (tasks.length === 0) {
    return;
  }

  let index = 0;
  const workerCount = Math.min(concurrency, tasks.length);

  const workers = Array.from({ length: workerCount }, async () => {
    while (index < tasks.length) {
      const currentIndex = index;
      index += 1;
      await tasks[currentIndex]();
    }
  });

  await Promise.all(workers);
}

async function fileExists(filePath: string): Promise<boolean> {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

function pushProgress(
  emitProgress: (event: DownloadProgressEvent) => void,
  event: {
    courseId: number;
    moduleId: number;
    moduleName: string;
    fileName: string;
    status: DownloadStatus;
    message?: string;
  }
): void {
  emitProgress(event);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
