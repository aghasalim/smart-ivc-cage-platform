/**
 * 4×4 IR-grid heat-map for the latest behaviour reading.
 */
export function BehaviourGrid({ cells }: { cells: number[] }) {
  const max = Math.max(1, ...cells);
  return (
    <div className="inline-grid grid-cols-4 gap-1 rounded-lg border bg-muted/30 p-2">
      {cells.map((c, i) => {
        const intensity = c / max;
        const bg = `rgba(16,185,129,${0.08 + 0.65 * intensity})`;
        return (
          <div
            key={i}
            title={`cell ${i}: ${c}`}
            className="h-8 w-8 rounded text-[10px] grid place-items-center text-emerald-50/80"
            style={{ background: bg }}
          >
            {c > 0 ? c : ''}
          </div>
        );
      })}
    </div>
  );
}
