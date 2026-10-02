import { app, BrowserWindow, ipcMain } from 'electron';
import path from 'node:path';
import { z } from 'zod';
import { CanvasClient } from './canvas-client';
import { getConfig } from './config';
import { createDownloadLogger } from './download-logger';
import { downloadModules } from './download-service';

const courseIdSchema = z.number().int().positive();
const downloadRequestSchema = z.object({
  courseId: z.number().int().positive(),
  courseName: z.string().min(1),
  modules: z
    .array(
      z.object({
        id: z.number().int().positive(),
        name: z.string().min(1)
      })
    )
    .min(1)
});

let startupConfigError = '';
let canvasClient: CanvasClient | undefined;

function createWindow(): BrowserWindow {
  const window = new BrowserWindow({
    width: 1400,
    height: 920,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  if (app.isPackaged) {
    window.loadFile(path.join(__dirname, '..', '..', 'dist', 'index.html'));
  } else {
    window.loadURL('http://localhost:5173');
    window.webContents.openDevTools({ mode: 'detach' });
  }

  return window;
}

function requireClient(): CanvasClient {
  if (startupConfigError) {
    throw new Error(startupConfigError);
  }

  if (!canvasClient) {
    throw new Error('Canvas client has not been initialized.');
  }

  return canvasClient;
}

function registerIpcHandlers(): void {
  ipcMain.handle('canvas:getFavoriteCourses', async () => {
    return requireClient().getFavoriteCourses();
  });

  ipcMain.handle('canvas:getCourseModules', async (_event, unknownCourseId) => {
    const courseId = courseIdSchema.parse(unknownCourseId);
    return requireClient().getCourseModules(courseId);
  });

  ipcMain.handle('downloads:downloadModules', async (event, unknownRequest) => {
    const request = downloadRequestSchema.parse(unknownRequest);
    const config = getConfig();
    const logger = await createDownloadLogger();

    return downloadModules(
      request,
      {
        client: requireClient(),
        downloadRootFromEnv: config.downloadRoot,
        fetchFile: (url: string) =>
          fetch(url, {
            method: 'GET',
            redirect: 'follow',
            headers: {
              Authorization: `Bearer ${config.canvasApiKey}`
            }
          }),
        logger
      },
      (progressEvent) => {
        event.sender.send('downloads:progress', progressEvent);
      }
    );
  });
}

app.whenReady().then(() => {
  try {
    const config = getConfig();
    canvasClient = new CanvasClient(config.canvasBaseUrl, config.canvasApiKey);
  } catch (error) {
    startupConfigError = error instanceof Error ? error.message : 'Failed to load app configuration.';
  }

  registerIpcHandlers();
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});
