import os from 'node:os';
import path from 'node:path';

const INVALID_PATH_CHARS = /[<>:\\"/|?*\u0000-\u001F]/g;

export function sanitizePathSegment(value: string): string {
  const sanitized = value
    .replace(INVALID_PATH_CHARS, '_')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\.+$/g, '');

  return sanitized.length > 0 ? sanitized : 'untitled';
}

export function resolveDownloadRoot(downloadRootFromEnv?: string): string {
  if (downloadRootFromEnv && downloadRootFromEnv.trim().length > 0) {
    return downloadRootFromEnv.replace(/^~(?=$|\/|\\)/, os.homedir());
  }

  return path.join(os.homedir(), 'Downloads', 'CanvasDownloads');
}

export function buildModuleDirectory(root: string, courseName: string, moduleName: string): string {
  return path.join(root, sanitizePathSegment(courseName), sanitizePathSegment(moduleName));
}
