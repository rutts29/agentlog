import { cn } from "@/lib/utils";

type Props = {
  title: string;
  body: string;
  missing?: string[];
  className?: string;
};

export function EmptyState({ title, body, missing, className }: Props) {
  return (
    <div
      className={cn(
        "flex h-full min-h-[120px] flex-col justify-center gap-2 rounded-card border border-dashed border-border bg-muted/40 px-4 py-5",
        className,
      )}
    >
      <div className="text-[13px] font-medium text-foreground">{title}</div>
      <p className="max-w-prose text-[12px] leading-relaxed text-muted-foreground">
        {body}
      </p>
      {missing && missing.length > 0 ? (
        <ul className="mt-1 list-disc pl-4 text-[12px] text-faint-foreground">
          {missing.map((m) => (
            <li key={m}>{m}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
