import { useState } from "react";
import { cn } from "@/lib/utils";

export function CopyPath({
  path,
  className,
}: {
  path: string | null | undefined;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  if (!path) {
    return (
      <span className={cn("text-[12px] text-faint-foreground", className)}>
        No source artifact path
      </span>
    );
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(path!);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className={cn("flex min-w-0 items-center gap-2", className)}>
      <code className="min-w-0 flex-1 truncate font-mono text-[11px] text-muted-foreground">
        {path}
      </code>
      <button
        type="button"
        onClick={copy}
        className="shrink-0 rounded-control border border-border px-2 py-1 text-[11px] text-muted-foreground hover:text-foreground"
      >
        {copied ? "Copied" : "Copy path"}
      </button>
    </div>
  );
}
