import type {
  CourseSummary,
  DownloadProgressEvent,
  DownloadRequest,
  DownloadResult,
  ModuleWithItems
} from './types';

export interface PreloadApi {
  canvas: {
    getFavoriteCourses: () => Promise<CourseSummary[]>;
    getCourseModules: (courseId: number) => Promise<ModuleWithItems[]>;
  };
  downloads: {
    downloadModules: (input: DownloadRequest) => Promise<DownloadResult>;
    onProgress: (listener: (event: DownloadProgressEvent) => void) => () => void;
  };
}
