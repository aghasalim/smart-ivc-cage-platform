/**
 * TimeSeriesExplorer — interactive, animated metric chart.
 *
 * Lets the user pick:
 *   • metric        (Temperature, CO₂, Animal weight, Movement, …)
 *   • granularity   (second / minute / hour / day)
 *   • aggregation   (avg / sum / min / max / last / count)
 *   • chart type    (line / area / bar / scatter)
 *   • window        (how many buckets to look back)
 *
 * Backed by GET /api/v1/cages/{id}/timeseries. Smooth animated transitions
 * on every control change. Live stat strip (now / min / max / avg / trend).
 */
import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Line, LineChart,
  ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis, ZAxis,
} from 'recharts';
import {
  Activity, ArrowDownRight, ArrowRight, ArrowUpRight, BarChart3,
  LineChart as LineIcon, ScatterChart as ScatterIcon, AreaChart as AreaIcon,
  Loader2,
} from 'lucide-react';
import { Card, CardContent } from '@/components/ui/card';
import { api } from '@/lib/api';

type MetricDef = { key: string; label: string; unit: string; sensor: string; default_agg: string };
type MetricsResponse = {
  metrics: MetricDef[];
  granularities: string[];
  aggregations: string[];
};
type Point = { t: string; v: number; n: number };
type SeriesResponse = {
  metric: string; label: string; unit: string;
  granularity: string; agg: string; window: number;
  stats: { count: number; min: number | null; max: number | null; avg: number | null; first: number | null; last: number | null };
  points: Point[];
};

type ChartType = 'line' | 'area' | 'bar' | 'scatter';
type Granularity = 'second' | 'minute' | 'hour' | 'day';

const GRAN_LABEL: Record<Granularity, string> = {
  second: 'Second', minute: 'Minute', hour: 'Hour', day: 'Day',
};
// Sensible default look-back windows per granularity (number of buckets)
const GRAN_WINDOWS: Record<Granularity, { label: string; window: number }[]> = {
  second: [{ label: '1 min', window: 60 }, { label: '5 min', window: 300 }, { label: '15 min', window: 900 }],
  minute: [{ label: '1 h', window: 60 }, { label: '6 h', window: 360 }, { label: '24 h', window: 1440 }],
  hour:   [{ label: '24 h', window: 24 }, { label: '3 d', window: 72 }, { label: '7 d', window: 168 }],
  day:    [{ label: '7 d', window: 7 }, { label: '30 d', window: 30 }, { label: '90 d', window: 90 }],
};

const AGG_LABEL: Record<string, string> = {
  avg: 'Average', sum: 'Sum', min: 'Minimum', max: 'Maximum', last: 'Last', count: 'Count',
};

const CHART_TYPES: { id: ChartType; icon: React.ComponentType<{ className?: string }>; label: string }[] = [
  { id: 'line', icon: LineIcon, label: 'Line' },
  { id: 'area', icon: AreaIcon, label: 'Area' },
  { id: 'bar', icon: BarChart3, label: 'Bar' },
  { id: 'scatter', icon: ScatterIcon, label: 'Scatter' },
];

const ACCENT = '#10b981';

const tooltipStyle = {
  background: 'hsl(var(--popover))',
  border: '1px solid hsl(var(--border))',
  borderRadius: 8,
  fontSize: 12,
};

function fmtTick(iso: string, gran: Granularity): string {
  const d = new Date(iso);
  if (gran === 'day') return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  if (gran === 'hour') return d.toLocaleString(undefined, { month: 'numeric', day: 'numeric', hour: '2-digit' });
  if (gran === 'minute') return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export function TimeSeriesExplorer({ cageId }: { cageId: string }) {
  const [metric, setMetric] = useState('temperature_c');
  const [granularity, setGranularity] = useState<Granularity>('hour');
  const [agg, setAgg] = useState('avg');
  const [chart, setChart] = useState<ChartType>('area');
  const [window, setWindowVal] = useState(24);

  // Metric catalogue (for the dropdown)
  const { data: catalog } = useQuery({
    queryKey: ['ts-metrics'],
    queryFn: () => api<MetricsResponse>('/api/v1/cages/timeseries/metrics'),
    staleTime: 5 * 60_000,
  });

  // The series itself — re-fetches whenever any control changes
  const { data: series, isFetching } = useQuery({
    queryKey: ['ts', cageId, metric, granularity, agg, window],
    queryFn: () => api<SeriesResponse>(
      `/api/v1/cages/${cageId}/timeseries?metric=${metric}&granularity=${granularity}&agg=${agg}&window=${window}`,
    ),
    refetchInterval: granularity === 'second' ? 5_000 : 30_000,
    placeholderData: (prev) => prev,
  });

  const chartData = useMemo(
    () => (series?.points ?? []).map((p) => ({ t: p.t, v: p.v, n: p.n })),
    [series],
  );

  const unit = series?.unit ?? '';
  const stats = series?.stats;
  const trend = stats?.first != null && stats?.last != null ? stats.last - stats.first : 0;

  // When granularity changes, snap window to that granularity's default
  function changeGranularity(g: Granularity) {
    setGranularity(g);
    setWindowVal(GRAN_WINDOWS[g][0].window);
  }

  return (
    <Card className="overflow-hidden">
      <CardContent className="p-4 space-y-4">
        {/* ── Control bar ─────────────────────────────────────────── */}
        <div className="flex flex-wrap items-end gap-x-5 gap-y-3">
          {/* Metric */}
          <div className="space-y-1">
            <label className="block text-[10px] uppercase tracking-wide text-muted-foreground">Metric</label>
            <select
              value={metric}
              onChange={(e) => {
                setMetric(e.target.value);
                const def = catalog?.metrics.find((m) => m.key === e.target.value);
                if (def) setAgg(def.default_agg);
              }}
              className="h-8 rounded-md border border-border bg-card px-2 text-sm font-medium focus:outline-none focus:ring-1 focus:ring-primary min-w-[150px]"
            >
              {(catalog?.metrics ?? [{ key: 'temperature_c', label: 'Temperature', unit: '°C', sensor: '', default_agg: 'avg' }]).map((m) => (
                <option key={m.key} value={m.key}>{m.label}{m.unit ? ` (${m.unit})` : ''}</option>
              ))}
            </select>
          </div>

          {/* Granularity */}
          <div className="space-y-1">
            <label className="block text-[10px] uppercase tracking-wide text-muted-foreground">Granularity</label>
            <SegGroup
              options={(['second', 'minute', 'hour', 'day'] as Granularity[]).map((g) => ({ id: g, label: GRAN_LABEL[g] }))}
              value={granularity}
              onChange={(g) => changeGranularity(g as Granularity)}
            />
          </div>

          {/* Aggregation */}
          <div className="space-y-1">
            <label className="block text-[10px] uppercase tracking-wide text-muted-foreground">Aggregation</label>
            <SegGroup
              options={(catalog?.aggregations ?? ['avg', 'sum', 'min', 'max', 'last']).map((a) => ({ id: a, label: AGG_LABEL[a] ?? a }))}
              value={agg}
              onChange={setAgg}
            />
          </div>

          {/* Window */}
          <div className="space-y-1">
            <label className="block text-[10px] uppercase tracking-wide text-muted-foreground">Window</label>
            <SegGroup
              options={GRAN_WINDOWS[granularity].map((w) => ({ id: String(w.window), label: w.label }))}
              value={String(window)}
              onChange={(v) => setWindowVal(Number(v))}
            />
          </div>

          {/* Chart type */}
          <div className="space-y-1 ml-auto">
            <label className="block text-[10px] uppercase tracking-wide text-muted-foreground">Chart</label>
            <div className="flex gap-1">
              {CHART_TYPES.map((c) => {
                const Icon = c.icon;
                const active = chart === c.id;
                return (
                  <button
                    key={c.id}
                    onClick={() => setChart(c.id)}
                    title={c.label}
                    className={`grid h-8 w-8 place-items-center rounded-md border transition-colors ${
                      active ? 'border-primary bg-primary/15 text-primary' : 'border-border text-muted-foreground hover:text-foreground'
                    }`}
                  >
                    <Icon className="h-4 w-4" />
                  </button>
                );
              })}
            </div>
          </div>
        </div>

        {/* ── Stat strip ──────────────────────────────────────────── */}
        <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
          <Stat label="Now" value={stats?.last} unit={unit} accent />
          <Stat label="Min" value={stats?.min} unit={unit} />
          <Stat label="Max" value={stats?.max} unit={unit} />
          <Stat label="Avg" value={stats?.avg} unit={unit} />
          <TrendStat trend={trend} unit={unit} />
        </div>

        {/* ── Chart ───────────────────────────────────────────────── */}
        <div className="relative h-72">
          {isFetching && (
            <div className="absolute right-2 top-1 z-10 flex items-center gap-1 text-[10px] text-muted-foreground">
              <Loader2 className="h-3 w-3 animate-spin" /> updating
            </div>
          )}
          {chartData.length === 0 ? (
            <div className="grid h-full place-items-center text-sm text-muted-foreground">
              <div className="flex flex-col items-center gap-2">
                <Activity className="h-8 w-8 opacity-40" />
                No data for this metric in the selected window.
              </div>
            </div>
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              {renderChart(chart, chartData, granularity, unit)}
            </ResponsiveContainer>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function renderChart(type: ChartType, data: { t: string; v: number; n: number }[], gran: Granularity, unit: string) {
  const xAxis = (
    <XAxis dataKey="t" tickFormatter={(v) => fmtTick(v, gran)} minTickGap={28}
      stroke="hsl(var(--border))" tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }} />
  );
  const yAxis = (
    <YAxis stroke="hsl(var(--border))" tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }}
      width={44} domain={['auto', 'auto']} />
  );
  const grid = <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" opacity={0.4} />;
  const tip = (
    <Tooltip contentStyle={tooltipStyle}
      labelFormatter={(v) => new Date(v as string).toLocaleString()}
      formatter={(v: number) => [`${v}${unit ? ' ' + unit : ''}`, '']} />
  );
  const anim = { isAnimationActive: true, animationDuration: 600, animationEasing: 'ease-out' as const };

  if (type === 'line') {
    return (
      <LineChart data={data}>
        {grid}{xAxis}{yAxis}{tip}
        <Line type="monotone" dataKey="v" stroke={ACCENT} strokeWidth={2} dot={false} {...anim} />
      </LineChart>
    );
  }
  if (type === 'bar') {
    return (
      <BarChart data={data}>
        {grid}{xAxis}{yAxis}{tip}
        <Bar dataKey="v" fill={ACCENT} radius={[3, 3, 0, 0]} {...anim} />
      </BarChart>
    );
  }
  if (type === 'scatter') {
    return (
      <ScatterChart>
        {grid}{xAxis}{yAxis}<ZAxis range={[40, 40]} />{tip}
        <Scatter data={data} dataKey="v" fill={ACCENT} {...anim} />
      </ScatterChart>
    );
  }
  // area
  return (
    <AreaChart data={data}>
      <defs>
        <linearGradient id="tsFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={ACCENT} stopOpacity={0.45} />
          <stop offset="100%" stopColor={ACCENT} stopOpacity={0.02} />
        </linearGradient>
      </defs>
      {grid}{xAxis}{yAxis}{tip}
      <Area type="monotone" dataKey="v" stroke={ACCENT} strokeWidth={2} fill="url(#tsFill)" {...anim} />
    </AreaChart>
  );
}

function SegGroup({ options, value, onChange }: {
  options: { id: string; label: string }[];
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="inline-flex rounded-md border border-border overflow-hidden">
      {options.map((o) => {
        const active = o.id === value;
        return (
          <button
            key={o.id}
            onClick={() => onChange(o.id)}
            className={`px-2.5 h-8 text-xs font-medium transition-colors ${
              active ? 'bg-primary text-primary-foreground' : 'bg-card text-muted-foreground hover:text-foreground'
            }`}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

function Stat({ label, value, unit, accent }: { label: string; value?: number | null; unit: string; accent?: boolean }) {
  return (
    <div className="rounded-md border border-border bg-card/50 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className={`font-mono text-lg font-bold tabular-nums ${accent ? 'text-primary' : 'text-foreground'}`}>
        {value == null ? '—' : value}
        {value != null && unit ? <span className="ml-0.5 text-xs font-normal text-muted-foreground">{unit}</span> : null}
      </div>
    </div>
  );
}

function TrendStat({ trend, unit }: { trend: number; unit: string }) {
  const up = trend > 0.0001;
  const down = trend < -0.0001;
  const Icon = up ? ArrowUpRight : down ? ArrowDownRight : ArrowRight;
  const color = up ? 'text-emerald-400' : down ? 'text-rose-400' : 'text-muted-foreground';
  return (
    <div className="rounded-md border border-border bg-card/50 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Trend</div>
      <div className={`flex items-center gap-1 font-mono text-lg font-bold tabular-nums ${color}`}>
        <Icon className="h-4 w-4" />
        {Math.abs(trend) < 0.0001 ? '0' : `${trend > 0 ? '+' : ''}${trend.toFixed(2)}`}
        {unit ? <span className="text-xs font-normal text-muted-foreground">{unit}</span> : null}
      </div>
    </div>
  );
}
