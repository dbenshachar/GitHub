import { contextBridge, ipcRenderer } from 'electron';
import type { PreloadApi } from '../src/shared/preload-api';

const api: PreloadApi = {
  canvas: {
    getFavoriteCourses: () => ipcRenderer.invoke('canvas:getFavoriteCourses'),
    getCourseModules: (courseId) => ipcRenderer.invoke('canvas:getCourseModules', courseId)
  },
  downloads: {
    downloadModules: (input) => ipcRenderer.invoke('downloads:downloadModules', input),
    onProgress: (listener) => {
      const handler = (_event: Electron.IpcRendererEvent, payload: unknown) => {
        listener(payload as Parameters<typeof listener>[0]);
      };

      ipcRenderer.on('downloads:progress', handler);
      return () => ipcRenderer.removeListener('downloads:progress', handler);
    }
  }
};

contextBridge.exposeInMainWorld('api', api);
