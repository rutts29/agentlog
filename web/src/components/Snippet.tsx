/** Render FTS snippets that use « » around matches. */
export function Snippet({ text }: { text: string }) {
  const parts = text.split(/(«[^»]*»)/g);
  return (
    <span className="text-[13px] leading-relaxed text-muted-foreground">
      {parts.map((part, i) => {
        if (part.startsWith("«") && part.endsWith("»")) {
          return (
            <mark key={i}>{part.slice(1, -1)}</mark>
          );
        }
        return <span key={i}>{part}</span>;
      })}
    </span>
  );
}
