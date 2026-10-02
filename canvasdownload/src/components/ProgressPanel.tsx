import type { DownloadProgressEvent, DownloadResult } from '../shared/types';

interface ProgressPanelProps {
  events: DownloadProgressEvent[];
  isDownloading: boolean;
  lastResult?: DownloadResult;
}

export function ProgressPanel({ events, isDownloading, lastResult }: ProgressPanelProps): JSX.Element {
  return (
    <section className="panel progress-panel">
      <div className="panel-header">
        <h3>Download Progress</h3>
        <span>{isDownloading ? 'Running...' : 'Idle'}</span>
      </div>

      {lastResult && (
        <div className="summary-box">
          Downloaded: {lastResult.downloadedCount} · Skipped: {lastResult.skippedCount} · Failed:{' '}
          {lastResult.failedCount}
        </div>
      )}

      <div className="events-list">
        {events.length === 0 ? (
          <div className="empty-state">No download events yet.</div>
        ) : (
          events
            .slice()
            .reverse()
            .map((event, index) => (
              <div className={`event-line status-${event.status}`} key={`${event.moduleId}-${event.fileName}-${index}`}>
                <strong>{event.status.toUpperCase()}</strong> {event.moduleName} / {event.fileName}
                {event.message ? ` (${event.message})` : ''}
              </div>
            ))
        )}
      </div>

      {lastResult && lastResult.failures.length > 0 && (
        <div className="failure-list">
          <h4>Failures</h4>
          {lastResult.failures.map((failure, index) => (
            <div key={`${failure.moduleId}-${failure.fileName}-${index}`}>
              {failure.moduleName} / {failure.fileName}: {failure.reason}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
