import type { CourseSummary } from '../shared/types';

interface CourseListProps {
  courses: CourseSummary[];
  selectedCourseId?: number;
  filter: string;
  onFilterChange: (value: string) => void;
  onSelectCourse: (courseId: number) => void;
}

export function CourseList({
  courses,
  selectedCourseId,
  filter,
  onFilterChange,
  onSelectCourse
}: CourseListProps): JSX.Element {
  const normalizedFilter = filter.trim().toLowerCase();
  const filteredCourses = courses.filter((course) => {
    if (!normalizedFilter) {
      return true;
    }

    return (
      course.name.toLowerCase().includes(normalizedFilter) ||
      course.courseCode.toLowerCase().includes(normalizedFilter)
    );
  });

  return (
    <aside className="panel courses-panel">
      <div className="panel-header">
        <h2>Favorited Courses</h2>
      </div>
      <input
        className="search-input"
        type="text"
        placeholder="Search courses"
        value={filter}
        onChange={(event) => onFilterChange(event.target.value)}
      />
      <div className="course-list">
        {filteredCourses.map((course) => (
          <button
            key={course.id}
            className={course.id === selectedCourseId ? 'course-item active' : 'course-item'}
            onClick={() => onSelectCourse(course.id)}
            type="button"
          >
            <div className="course-name">{course.name}</div>
            <div className="course-meta">{course.courseCode || 'No course code'}</div>
          </button>
        ))}
      </div>
    </aside>
  );
}
