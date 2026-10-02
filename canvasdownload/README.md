# Canvas Favorites Module Downloader

Desktop app (Electron + React + TypeScript) that lists only your favorited Canvas courses, lets you select modules, and downloads module file attachments into your Downloads folder.

## Features

- Favorites-only course navigation from Canvas API.
- Module selection UI with downloadable-file counts.
- Downloads saved under:
  - `~/Downloads/CanvasDownloads/<Course>/<Module>/`
- Existing files are skipped by filename.
- Progress statuses: `queued`, `downloading`, `skipped`, `done`, `failed`.
- Retry for transient file download failures (2 retries).
- Structured download logs in Electron user data logs directory.

## Setup

1. Copy `.env.example` to `.env` and fill values:
   - `CANVAS_BASE_URL`
   - `CANVAS_API_KEY`
   - Optional `DOWNLOAD_ROOT`
2. Install dependencies:

```bash
npm install
```

3. Run in development:

```bash
npm run dev
```

4. Build:

```bash
npm run build
```

## Test

```bash
npm test
```

## Security Notes

- Canvas token is only used in Electron main process.
- Renderer has no direct filesystem/token access.
- `contextIsolation` is enabled and `nodeIntegration` is disabled.
