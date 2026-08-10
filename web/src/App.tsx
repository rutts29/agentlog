import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AppShell } from "@/components/layout/AppShell";
import { Overview } from "@/views/Overview";
import { Sessions } from "@/views/Sessions";
import { SessionDetail } from "@/views/SessionDetail";
import { Search } from "@/views/Search";
import { Models } from "@/views/Models";
import { Skills } from "@/views/Skills";
import { Orchestration } from "@/views/Orchestration";
import { AutoReview } from "@/views/AutoReview";
import { Insights } from "@/views/Insights";
import { Proposals } from "@/views/Proposals";
import { Adjudicate } from "@/views/Adjudicate";

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
            <Route index element={<Overview />} />
            <Route path="sessions" element={<Sessions />} />
            <Route path="sessions/:sessionId" element={<SessionDetail />} />
            <Route path="search" element={<Search />} />
            <Route path="models" element={<Models />} />
            <Route path="skills" element={<Skills />} />
            <Route path="orchestration" element={<Orchestration />} />
            <Route path="auto-review" element={<AutoReview />} />
            <Route path="insights" element={<Insights />} />
            <Route path="proposals" element={<Proposals />} />
            <Route path="adjudicate" element={<Adjudicate />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
