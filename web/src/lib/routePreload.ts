export const viewLoaders = {
  "/": () => import("@/views/Overview"),
  "/sessions": () => import("@/views/Sessions"),
  "/search": () => import("@/views/Search"),
  "/models": () => import("@/views/Models"),
  "/skills": () => import("@/views/Skills"),
  "/orchestration": () => import("@/views/Orchestration"),
  "/auto-review": () => import("@/views/AutoReview"),
  "/insights": () => import("@/views/Insights"),
  "/proposals": () => import("@/views/Proposals"),
  "/adjudicate": () => import("@/views/Adjudicate"),
} as const;

export function preloadView(pathname: string): void {
  void viewLoaders[pathname as keyof typeof viewLoaders]?.();
}
