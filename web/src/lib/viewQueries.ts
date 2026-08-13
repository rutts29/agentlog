import { queryOptions, type QueryKey } from "@tanstack/react-query";

export const VIEW_RANGE_STALE_TIME = 30_000;
export const VIEW_RANGE_GC_TIME = 30 * 60_000;

export function rangeViewQueryOptions<T>(options: {
  queryKey: QueryKey;
  queryFn: (signal: AbortSignal) => Promise<T>;
}) {
  return queryOptions({
    ...options,
    queryFn: ({ signal }) => options.queryFn(signal),
    staleTime: VIEW_RANGE_STALE_TIME,
    gcTime: VIEW_RANGE_GC_TIME,
  });
}
