import { parseLinkHeader } from '../src/shared/pagination';
import type { CourseSummary, ModuleItem, ModuleWithItems } from '../src/shared/types';

interface CanvasCourse {
  id: number;
  name: string;
  course_code?: string;
  term?: {
    name?: string;
  };
}

interface CanvasModuleItem {
  id: number;
  type: string;
  title?: string;
  content_id?: number;
  course_id: number;
}

interface CanvasModule {
  id: number;
  name: string;
  items?: CanvasModuleItem[];
}

interface CanvasFile {
  id: number;
  display_name?: string;
  filename?: string;
  url: string;
}

export interface CanvasFileInfo {
  id: number;
  fileName: string;
  downloadUrl: string;
}

const MAX_API_RETRIES = 2;

export class CanvasClient {
  constructor(
    private readonly baseUrl: string,
    private readonly token: string
  ) {}

  async getFavoriteCourses(): Promise<CourseSummary[]> {
    const courses = await this.getPaginated<CanvasCourse>('/api/v1/users/self/favorites/courses?per_page=100');
    return courses.map((course) => ({
      id: course.id,
      name: course.name,
      courseCode: course.course_code ?? '',
      termName: course.term?.name
    }));
  }

  async getCourseModules(courseId: number): Promise<ModuleWithItems[]> {
    const modules = await this.getPaginated<CanvasModule>(
      `/api/v1/courses/${courseId}/modules?include[]=items&per_page=100`
    );

    return modules.map((moduleItem) => ({
      id: moduleItem.id,
      name: moduleItem.name,
      items: (moduleItem.items ?? []).map((item) => this.mapModuleItem(item))
    }));
  }

  async getFile(fileId: number): Promise<CanvasFileInfo> {
    const response = await this.request(`/api/v1/files/${fileId}`);
    const file = (await response.json()) as CanvasFile;

    return {
      id: file.id,
      fileName: file.display_name ?? file.filename ?? `file-${file.id}`,
      downloadUrl: file.url
    };
  }

  private mapModuleItem(item: CanvasModuleItem): ModuleItem {
    return {
      id: item.id,
      type: item.type,
      title: item.title ?? `Item ${item.id}`,
      contentId: item.content_id,
      courseId: item.course_id
    };
  }

  private async getPaginated<T>(relativePath: string): Promise<T[]> {
    let nextUrl: string | undefined = new URL(relativePath, this.baseUrl).toString();
    const results: T[] = [];

    while (nextUrl) {
      const response = await this.request(nextUrl, true);
      const page = (await response.json()) as T[];
      results.push(...page);

      const links = parseLinkHeader(response.headers.get('link'));
      nextUrl = links.next ? new URL(links.next, this.baseUrl).toString() : undefined;
    }

    return results;
  }

  private async request(input: string, isAbsolute = false, attempt = 0): Promise<Response> {
    const url = isAbsolute ? input : new URL(input, this.baseUrl).toString();
    const response = await fetch(url, {
      method: 'GET',
      headers: {
        Authorization: `Bearer ${this.token}`
      }
    });

    if (response.status === 401) {
      throw new Error('Canvas authorization failed. Check that CANVAS_API_KEY is valid.');
    }

    if ((response.status === 429 || response.status >= 500) && attempt < MAX_API_RETRIES) {
      const delayMs = this.calculateBackoffDelay(response, attempt);
      await sleep(delayMs);
      return this.request(input, isAbsolute, attempt + 1);
    }

    if (!response.ok) {
      const errorText = (await response.text()).slice(0, 200);
      throw new Error(`Canvas request failed (${response.status}): ${errorText || response.statusText}`);
    }

    return response;
  }

  private calculateBackoffDelay(response: Response, attempt: number): number {
    const retryAfter = response.headers.get('retry-after');
    if (retryAfter && /^\d+$/.test(retryAfter)) {
      return Number(retryAfter) * 1000;
    }

    const rateRemaining = response.headers.get('x-rate-limit-remaining');
    const rateReset = response.headers.get('x-rate-limit-reset');
    if (rateRemaining === '0' && rateReset && /^\d+$/.test(rateReset)) {
      const resetMs = Number(rateReset) * 1000;
      return Math.max(resetMs - Date.now(), 500);
    }

    return 500 * 2 ** attempt;
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
