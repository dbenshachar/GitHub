import { describe, expect, it } from 'vitest';
import { parseLinkHeader } from '../src/shared/pagination';

describe('parseLinkHeader', () => {
  it('extracts next and last links', () => {
    const links = parseLinkHeader(
      '<https://canvas.example/api/v1/courses?page=2>; rel="next", <https://canvas.example/api/v1/courses?page=8>; rel="last"'
    );

    expect(links.next).toBe('https://canvas.example/api/v1/courses?page=2');
    expect(links.last).toBe('https://canvas.example/api/v1/courses?page=8');
  });

  it('returns empty object for null header', () => {
    expect(parseLinkHeader(null)).toEqual({});
  });
});
