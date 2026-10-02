export interface PaginationLinks {
  next?: string;
  prev?: string;
  first?: string;
  last?: string;
}

export function parseLinkHeader(linkHeader: string | null): PaginationLinks {
  if (!linkHeader) {
    return {};
  }

  return linkHeader
    .split(',')
    .map((part) => part.trim())
    .reduce<PaginationLinks>((acc, part) => {
      const match = part.match(/^<([^>]+)>;\s*rel="([a-z]+)"$/i);
      if (!match) {
        return acc;
      }

      const [, url, rel] = match;
      if (rel === 'next' || rel === 'prev' || rel === 'first' || rel === 'last') {
        acc[rel] = url;
      }
      return acc;
    }, {});
}
