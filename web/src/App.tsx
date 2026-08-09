import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AppShell } from "@/components/layout/AppShell";
import { Overview } from "@/views/Overview";
import { Sessions } from "@/views/Sessions";
import { Models } from "@/views/Models";
import { Skills } from "@/views/Skills";
import { Insights } from "@/views/Insights";

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
            <Route path="models" element={<Models />} />
            <Route path="skills" element={<Skills />} />
            <Route path="insights" element={<Insights />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
