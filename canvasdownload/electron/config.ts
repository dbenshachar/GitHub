import path from 'node:path';
import dotenv from 'dotenv';
import { z } from 'zod';

const envSchema = z.object({
  CANVAS_BASE_URL: z
    .string({ required_error: 'CANVAS_BASE_URL is required in .env' })
    .trim()
    .url('CANVAS_BASE_URL must be a valid URL'),
  CANVAS_API_KEY: z
    .string({ required_error: 'CANVAS_API_KEY is required in .env' })
    .trim()
    .min(1, 'CANVAS_API_KEY cannot be empty'),
  DOWNLOAD_ROOT: z.string().trim().optional()
});

export interface AppConfig {
  canvasBaseUrl: string;
  canvasApiKey: string;
  downloadRoot?: string;
}

let cachedConfig: AppConfig | undefined;

export function getConfig(): AppConfig {
  if (cachedConfig) {
    return cachedConfig;
  }

  dotenv.config({ path: path.resolve(process.cwd(), '.env') });

  const parsed = envSchema.safeParse(process.env);
  if (!parsed.success) {
    const issue = parsed.error.issues[0];
    throw new Error(`Environment configuration error: ${issue.message}`);
  }

  cachedConfig = {
    canvasBaseUrl: parsed.data.CANVAS_BASE_URL.replace(/\/+$/, ''),
    canvasApiKey: parsed.data.CANVAS_API_KEY,
    downloadRoot: parsed.data.DOWNLOAD_ROOT
  };

  return cachedConfig;
}
