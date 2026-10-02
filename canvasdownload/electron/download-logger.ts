import { app } from 'electron';
import fs from 'node:fs/promises';
import path from 'node:path';

export interface DownloadLogger {
  log: (event: string, payload: unknown) => Promise<void>;
}

export async function createDownloadLogger(): Promise<DownloadLogger> {
  const logDir = path.join(app.getPath('userData'), 'logs');
  await fs.mkdir(logDir, { recursive: true });

  const logFile = path.join(logDir, `download-${new Date().toISOString().replace(/[:.]/g, '-')}.jsonl`);

  return {
    async log(event, payload) {
      const line = JSON.stringify({ timestamp: new Date().toISOString(), event, payload });
      await fs.appendFile(logFile, `${line}\n`, 'utf8');
    }
  };
}
