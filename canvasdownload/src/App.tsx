import { useEffect, useMemo, useState } from 'react';
import { Navigate, Route, Routes, useNavigate, useParams } from 'react-router-dom';
import { CourseList } from './components/CourseList';
import { ModuleList } from './components/ModuleList';
import { ProgressPanel } from './components/ProgressPanel';
import type { CourseSummary, DownloadProgressEvent, DownloadResult, ModuleWithItems } from './shared/types';

function CanvasDownloaderPage(): JSX.Element {
  const navigate = useNavigate();
  const { courseId } = useParams<{ courseId?: string }>();

  const [courses, setCourses] = useState<CourseSummary[]>([]);
  const [coursesLoading, setCoursesLoading] = useState(true);
  const [modules, setModules] = useState<ModuleWithItems[]>([]);
  const [modulesLoading, setModulesLoading] = useState(false);
  const [selectedModuleIds, setSelectedModuleIds] = useState<Set<number>>(new Set());
  const [courseFilter, setCourseFilter] = useState('');
  const [isDownloading, setIsDownloading] = useState(false);
  const [events, setEvents] = useState<DownloadProgressEvent[]>([]);
  const [lastResult, setLastResult] = useState<DownloadResult>();
  const [toast, setToast] = useState('');
  const [error, setError] = useState('');

  const selectedCourseId = courseId ? Number(courseId) : undefined;
  const selectedCourse = useMemo(
    () => courses.find((course) => course.id === selectedCourseId),
    [courses, selectedCourseId]
  );

  useEffect(() => {
    if (typeof window.api === 'undefined') {
      setError('Desktop bridge is unavailable. Launch the app through Electron.');
      setCoursesLoading(false);
      return;
    }

    let isActive = true;
    setCoursesLoading(true);

    window.api.canvas
      .getFavoriteCourses()
      .then((data) => {
        if (!isActive) {
          return;
        }
        setCourses(data);
      })
      .catch((fetchError) => {
        if (!isActive) {
          return;
        }
        setError(fetchError instanceof Error ? fetchError.message : 'Unable to load favorited courses.');
      })
      .finally(() => {
        if (isActive) {
          setCoursesLoading(false);
        }
      });

    return () => {
      isActive = false;
    };
  }, []);

  useEffect(() => {
    if (!courseId || coursesLoading) {
      return;
    }

    const targetId = Number(courseId);
    if (Number.isNaN(targetId)) {
      navigate('/', { replace: true });
      return;
    }

    const isFavorite = courses.some((course) => course.id === targetId);
    if (!isFavorite) {
      navigate('/', { replace: true });
    }
  }, [courseId, courses, coursesLoading, navigate]);

  useEffect(() => {
    if (typeof window.api === 'undefined') {
      return;
    }

    if (!selectedCourse) {
      setModules([]);
      setSelectedModuleIds(new Set());
      return;
    }

    let isActive = true;
    setModulesLoading(true);
    setError('');

    window.api.canvas
      .getCourseModules(selectedCourse.id)
      .then((data) => {
        if (!isActive) {
          return;
        }

        setModules(data);
      })
      .catch((fetchError) => {
        if (!isActive) {
          return;
        }

        setError(fetchError instanceof Error ? fetchError.message : 'Unable to load modules.');
      })
      .finally(() => {
        if (isActive) {
          setModulesLoading(false);
          setSelectedModuleIds(new Set());
        }
      });

    return () => {
      isActive = false;
    };
  }, [selectedCourse]);

  useEffect(() => {
    if (typeof window.api === 'undefined') {
      return;
    }

    const unsubscribe = window.api.downloads.onProgress((event) => {
      setEvents((previousEvents) => [...previousEvents.slice(-299), event]);
    });

    return unsubscribe;
  }, []);

  useEffect(() => {
    if (!toast) {
      return;
    }

    const timeoutId = setTimeout(() => setToast(''), 5000);
    return () => clearTimeout(timeoutId);
  }, [toast]);

  const selectedModules = useMemo(
    () => modules.filter((module) => selectedModuleIds.has(module.id)),
    [modules, selectedModuleIds]
  );

  const handleCourseSelect = (nextCourseId: number): void => {
    navigate(`/course/${nextCourseId}`);
  };

  const handleToggleModule = (moduleId: number): void => {
    setSelectedModuleIds((previous) => {
      const next = new Set(previous);
      if (next.has(moduleId)) {
        next.delete(moduleId);
      } else {
        next.add(moduleId);
      }

      return next;
    });
  };

  const handleDownload = async (): Promise<void> => {
    if (typeof window.api === 'undefined') {
      setError('Desktop bridge is unavailable. Launch the app through Electron.');
      return;
    }

    if (!selectedCourse || selectedModules.length === 0) {
      return;
    }

    setIsDownloading(true);
    setLastResult(undefined);
    setError('');
    setEvents([]);

    try {
      const result = await window.api.downloads.downloadModules({
        courseId: selectedCourse.id,
        courseName: selectedCourse.name,
        modules: selectedModules.map((module) => ({ id: module.id, name: module.name }))
      });

      setLastResult(result);
      setToast(
        `Download complete: ${result.downloadedCount} downloaded, ${result.skippedCount} skipped, ${result.failedCount} failed.`
      );
    } catch (downloadError) {
      setError(downloadError instanceof Error ? downloadError.message : 'Download failed.');
    } finally {
      setIsDownloading(false);
    }
  };

  return (
    <div className="app-shell">
      {toast && <div className="toast">{toast}</div>}
      <CourseList
        courses={courses}
        selectedCourseId={selectedCourse?.id}
        filter={courseFilter}
        onFilterChange={setCourseFilter}
        onSelectCourse={handleCourseSelect}
      />

      <main className="panel main-panel">
        <div className="panel-header">
          <h1>{selectedCourse ? selectedCourse.name : 'Select a favorited course'}</h1>
          <button
            className="download-button"
            onClick={() => void handleDownload()}
            type="button"
            disabled={isDownloading || selectedModules.length === 0 || !selectedCourse}
          >
            {isDownloading ? 'Downloading...' : 'Download Selected Modules'}
          </button>
        </div>

        {error && <div className="error-banner">{error}</div>}

        {coursesLoading && <div className="empty-state">Loading favorited courses...</div>}
        {!coursesLoading && !selectedCourse && (
          <div className="empty-state">Pick a favorited course from the left panel to browse modules.</div>
        )}

        {selectedCourse && modulesLoading && <div className="empty-state">Loading modules...</div>}

        {selectedCourse && !modulesLoading && (
          <ModuleList
            modules={modules}
            selectedModuleIds={selectedModuleIds}
            onToggleModule={handleToggleModule}
          />
        )}

        <ProgressPanel events={events} isDownloading={isDownloading} lastResult={lastResult} />
      </main>
    </div>
  );
}

export function App(): JSX.Element {
  return (
    <Routes>
      <Route path="/" element={<CanvasDownloaderPage />} />
      <Route path="/course/:courseId" element={<CanvasDownloaderPage />} />
      <Route path="*" element={<Navigate replace to="/" />} />
    </Routes>
  );
}
