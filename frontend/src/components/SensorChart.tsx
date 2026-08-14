import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

type Datum = { ts: string; value: number };

export function SensorChart({ data, color = '#10b981', label }: { data: Datum[]; color?: string; label: string }) {
  const formatted = data.map((d) => ({ ts: d.ts, t: new Date(d.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }), value: d.value }));
  return (
    <div className="h-56 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={formatted} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id={`g-${label}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.4} />
              <stop offset="100%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
          <XAxis dataKey="t" tick={{ fontSize: 11, fill: 'hsl(var(--muted-foreground))' }} stroke="hsl(var(--border))" />
          <YAxis tick={{ fontSize: 11, fill: 'hsl(var(--muted-foreground))' }} stroke="hsl(var(--border))" width={40} />
          <Tooltip
            contentStyle={{ background: 'hsl(var(--popover))', border: '1px solid hsl(var(--border))', borderRadius: 8, fontSize: 12 }}
            labelStyle={{ color: 'hsl(var(--muted-foreground))' }}
            formatter={(v: number) => [v.toFixed(2), label]}
          />
          <Area type="monotone" dataKey="value" stroke={color} fill={`url(#g-${label})`} strokeWidth={2} isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
