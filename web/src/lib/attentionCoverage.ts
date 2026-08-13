import type { AttentionTailSignalCoverage } from "./api";

export function attentionCoverageNote(
  coverage: AttentionTailSignalCoverage | undefined,
): string | null {
  if (!coverage || (coverage.complete && coverage.ignored_sessions === 0)) {
    return null;
  }
  const unverified = coverage.ignored_sessions
    ? ` · ${coverage.ignored_sessions} unverified`
    : "";
  return `partial source signals · ${coverage.covered_sessions}/${coverage.eligible_sessions} indexed${unverified}`;
}
