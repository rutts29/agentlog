import * as React from "react";
import { cn } from "@/lib/utils";

export function Card({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("elev-card rounded-card border border-border bg-card p-4", className)}
      {...props}
    />
  );
}

export function CardTitle({
  className,
  ...props
}: React.HTMLAttributes<HTMLHeadingElement>) {
  return <h3 className={cn("microlabel text-muted-foreground", className)} {...props} />;
}

/** Card with a bordered header row — for tables and dense lists. */
export function PanelCard({
  title,
  aside,
  className,
  children,
}: {
  title: React.ReactNode;
  aside?: React.ReactNode;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={cn("elev-card overflow-hidden rounded-card border border-border bg-card", className)}>
      <div className="flex items-baseline justify-between gap-3 border-b border-border px-4 py-2.5">
        <CardTitle>{title}</CardTitle>
        {aside ? (
          <div className="tabular text-[12px] text-muted-foreground">{aside}</div>
        ) : null}
      </div>
      {children}
    </div>
  );
}
