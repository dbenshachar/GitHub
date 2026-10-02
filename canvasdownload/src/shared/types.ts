export interface CourseSummary {
  id: number;
  name: string;
  courseCode: string;
  termName?: string;
}

export interface ModuleItem {
  id: number;
  type: string;
  title: string;
  contentId?: number;
  courseId: number;
}

export interface ModuleWithItems {
  id: number;
  name: string;
  items: ModuleItem[];
}

export interface DownloadRequest {
  courseId: number;
  courseName: string;
  modules: Array<{ id: number; name: string }>;
}

export interface DownloadFailure {
  moduleId: number;
  moduleName: string;
  fileName: string;
  reason: string;
}

export interface DownloadResult {
  downloadedCount: number;
  skippedCount: number;
  failedCount: number;
  failures: DownloadFailure[];
}

export type DownloadStatus = 'queued' | 'downloading' | 'skipped' | 'done' | 'failed';

export interface DownloadProgressEvent {
  courseId: number;
  moduleId: number;
  moduleName: string;
  fileName: string;
  status: DownloadStatus;
  message?: string;
}

export interface DownloadableModuleFile {
  moduleId: number;
  moduleName: string;
  fileId: number;
  fileNameHint: string;
}
