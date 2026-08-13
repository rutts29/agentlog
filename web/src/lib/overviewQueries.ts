import { queryOptions } from "@tanstack/react-query";
import { fetchGraph, fetchOverview } from "./api.ts";

export function overviewQueryOptions(range: string) {
  return queryOptions({
    queryKey: ["overview", range] as const,
    queryFn: ({ signal }) => fetchOverview(range, signal),
    staleTime: 30_000,
  });
}

export function overviewGraphQueryOptions(range: string, enabled = true) {
  return queryOptions({
    queryKey: ["graph", range] as const,
    queryFn: ({ signal }) => fetchGraph(range, signal),
    enabled,
    staleTime: 30_000,
  });
}

export type QueryContentState = "pending" | "ready" | "error";

export function queryContentState(query: {
  data: unknown;
  isError: boolean;
}): QueryContentState {
  if (query.data !== undefined) return "ready";
  return query.isError ? "error" : "pending";
}
