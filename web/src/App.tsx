import { lazy, Suspense, type ReactNode } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AppShell } from "@/components/layout/AppShell";
import { LoadingOrb } from "@/components/LoadingOrb";
import { viewLoaders } from "@/lib/routePreload";

const Overview = lazy(() => viewLoaders["/"]().then(({ Overview: view }) => ({ default: view })));
const Sessions = lazy(() => viewLoaders["/sessions"]().then(({ Sessions: view }) => ({ default: view })));
const SessionDetail = lazy(() => import("@/views/SessionDetail").then(({ SessionDetail: view }) => ({ default: view })));
const Search = lazy(() => viewLoaders["/search"]().then(({ Search: view }) => ({ default: view })));
const Models = lazy(() => viewLoaders["/models"]().then(({ Models: view }) => ({ default: view })));
const Skills = lazy(() => viewLoaders["/skills"]().then(({ Skills: view }) => ({ default: view })));
const Orchestration = lazy(() => viewLoaders["/orchestration"]().then(({ Orchestration: view }) => ({ default: view })));
const AutoReview = lazy(() => viewLoaders["/auto-review"]().then(({ AutoReview: view }) => ({ default: view })));
const Insights = lazy(() => viewLoaders["/insights"]().then(({ Insights: view }) => ({ default: view })));
const Proposals = lazy(() => viewLoaders["/proposals"]().then(({ Proposals: view }) => ({ default: view })));
const Adjudicate = lazy(() => viewLoaders["/adjudicate"]().then(({ Adjudicate: view }) => ({ default: view })));

function RouteLoader({ children }: { children: ReactNode }) {
  return <Suspense fallback={<LoadingOrb label="Opening view" />}>{children}</Suspense>;
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
    },
  },
});

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<RouteLoader><Overview /></RouteLoader>} />
            <Route path="sessions" element={<RouteLoader><Sessions /></RouteLoader>} />
            <Route path="sessions/:sessionId" element={<RouteLoader><SessionDetail /></RouteLoader>} />
            <Route path="search" element={<RouteLoader><Search /></RouteLoader>} />
            <Route path="models" element={<RouteLoader><Models /></RouteLoader>} />
            <Route path="skills" element={<RouteLoader><Skills /></RouteLoader>} />
            <Route path="orchestration" element={<RouteLoader><Orchestration /></RouteLoader>} />
            <Route path="auto-review" element={<RouteLoader><AutoReview /></RouteLoader>} />
            <Route path="insights" element={<RouteLoader><Insights /></RouteLoader>} />
            <Route path="proposals" element={<RouteLoader><Proposals /></RouteLoader>} />
            <Route path="adjudicate" element={<RouteLoader><Adjudicate /></RouteLoader>} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
