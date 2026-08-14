import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart,
  Pie, PieChart, ReferenceArea, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { Activity, Moon, BedDouble, AlertTriangle, Users, Clock } from 'lucide-react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { api } from '@/lib/api';

/**
 * Camera-driven behavioural analytics — 6 reports built from the per-mouse
 * state classifications (BehavioralMonitor) persisted by the backend.
 * Reports: time budget · 24h ethogram · sleep bouts · per-mouse comparison ·
 * circadian pattern · social & anomalies.
 */

const STATES = ['Wake / Active', 'Quiet / Rest', 'Possible Sleep'] as const;
const COLOR: Record<string, string> = {
  'Wake / Active': '#10b981',   // green
  'Quiet / Rest': '#38bdf8',    // sky
  'Possible Sleep': '#818cf8',  // indigo
};
const SHORT: Record<string, string> = {
  'Wake / Active': 'Active', 'Quiet / Rest': 'Rest', 'Possible Sleep': 'Sleep',
};
const SOCIAL_COLOR: Record<string, string> = {
  Huddling: '#818cf8', Normal: '#10b981', Exploration: '#f59e0b', Unknown: '#64748b',
};
const tip = {
  background: 'hsl(var(--popover))', border: '1px solid hsl(var(--border))',
  borderRadius: 8, fontSize: 12,
};
const axis = { fontSize: 11, fill: 'hsl(var(--muted-foreground))' };

// Mice are nocturnal — dark phase is the active phase. Lab default lights-on 07–19 (UTC-ish).
const DARK_START = 19;
const DARK_END = 7;

function useReport<T = any>(cageId: string, path: string, qs: string, enabled = true) {
  return useQuery({
    queryKey: ['cage', cageId, 'behavior', path, qs],
    queryFn: () => api<T>(`/api/v1/cages/${cageId}/${path}?${qs}`),
    refetchInterval: 30_000,
    enabled,
  });
}

export function BehaviorReports({ cageId }: { cageId: string }) {
  const [days, setDays] = useState(7);
  const [mouse, setMouse] = useState('all');
  const mq = mouse === 'all' ? '' : `&mouse=${encodeURIComponent(mouse)}`;

  const budget = useReport(cageId, 'state-budget', `days=${days}${mq}`);
  const bouts = useReport(cageId, 'sleep-bouts', `days=${days}${mq}`);
  const circ = useReport(cageId, 'state-circadian', `days=${days}${mq}`);
  const social = useReport(cageId, 'social-summary', `days=${days}`);
  const anom = useReport(cageId, 'behavior-anomalies', `days=${days}${mq}`);
  const timeline = useReport(cageId, 'state-timeline', `bucket=300${mq}`);

  const mice: string[] = useMemo(
    () => (budget.data?.mice ?? []).map((m: any) => m.mouse_id),
    [budget.data],
  );

  const noData = budget.isSuccess && (budget.data?.mice?.length ?? 0) === 0;

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2 text-base">
              <Activity className="h-4 w-4 text-emerald-400" /> Behavioural analytics — cameras
            </CardTitle>
            <CardDescription>
              Per-mouse time-in-state from live detection + classification (Active / Rest / Sleep).
            </CardDescription>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <select
              value={mouse}
              onChange={(e) => setMouse(e.target.value)}
              className="h-8 rounded-md border border-border bg-background px-2 text-xs"
            >
              <option value="all">All mice</option>
              {mice.map((m) => <option key={m} value={m}>Mouse {m}</option>)}
            </select>
            <div className="flex gap-1">
              {[1, 7, 14, 30].map((d) => (
                <Button key={d} size="sm" variant={days === d ? 'default' : 'outline'}
                  className="h-8 px-2.5 text-xs" onClick={() => setDays(d)}>
                  {d === 1 ? '24h' : `${d}d`}
                </Button>
              ))}
            </div>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {noData ? (
          <p className="text-sm text-muted-foreground py-6 text-center">
            No camera behaviour data yet for this cage. Run the detection pipeline and it will
            accrue here (history builds forward from when the cameras start classifying).
          </p>
        ) : (
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
            <TimeBudget budget={budget.data} days={days} />
            <Comparison budget={budget.data} days={days} />
            <Ethogram timeline={timeline.data} />
            <SleepBouts bouts={bouts.data} />
            <Circadian circ={circ.data} />
            <SocialAnomalies social={social.data} anom={anom.data} mice={mice} />
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ── 1 · Time budget ───────────────────────────────────────────────────────────
function TimeBudget({ budget, days }: { budget: any; days: number }) {
  const totals = budget?.cage_totals?.states ?? {};
  const pie = STATES.map((s) => ({ name: SHORT[s], value: totals[s]?.hours ?? 0, color: COLOR[s] }))
    .filter((d) => d.value > 0);
  const sleepH = (totals['Possible Sleep']?.hours ?? 0) / days;
  const activeH = (totals['Wake / Active']?.hours ?? 0) / days;
  const restH = (totals['Quiet / Rest']?.hours ?? 0) / days;
  // per-mouse 100% stacked
  const bars = (budget?.mice ?? []).map((m: any) => ({
    mouse: m.mouse_id,
    Active: m.states['Wake / Active']?.pct ?? 0,
    Rest: m.states['Quiet / Rest']?.pct ?? 0,
    Sleep: m.states['Possible Sleep']?.pct ?? 0,
  }));
  return (
    <ReportCard icon={Moon} title="Time budget" desc="Share of time per state — cage + per mouse">
      <div className="grid grid-cols-3 gap-2 mb-3">
        <Kpi label="Sleep / day" value={`${sleepH.toFixed(1)} h`} color="text-indigo-400" />
        <Kpi label="Rest / day" value={`${restH.toFixed(1)} h`} color="text-sky-400" />
        <Kpi label="Active / day" value={`${activeH.toFixed(1)} h`} color="text-emerald-400" />
      </div>
      <div className="grid grid-cols-2 gap-2">
        <div className="h-44">
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie data={pie} dataKey="value" nameKey="name" cx="50%" cy="50%" innerRadius={38} outerRadius={68} paddingAngle={2}>
                {pie.map((e) => <Cell key={e.name} fill={e.color} />)}
              </Pie>
              <Tooltip contentStyle={tip} formatter={(v: number) => [`${v.toFixed(2)} h`]} />
              <Legend wrapperStyle={{ fontSize: 10 }} />
            </PieChart>
          </ResponsiveContainer>
        </div>
        <div className="h-44">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={bars} layout="vertical" stackOffset="expand" margin={{ left: 4, right: 4 }}>
              <XAxis type="number" hide />
              <YAxis type="category" dataKey="mouse" width={20} tick={axis} />
              <Tooltip contentStyle={tip} formatter={(v: number) => [`${v}%`]} />
              <Bar dataKey="Sleep" stackId="a" fill={COLOR['Possible Sleep']} />
              <Bar dataKey="Rest" stackId="a" fill={COLOR['Quiet / Rest']} />
              <Bar dataKey="Active" stackId="a" fill={COLOR['Wake / Active']} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
    </ReportCard>
  );
}

// ── 2 · 24h ethogram ────────────────────────────────────────────────────────
function Ethogram({ timeline }: { timeline: any }) {
  const mice = timeline?.mice ?? [];
  return (
    <ReportCard icon={Clock} title="24-hour ethogram" desc="State across today (5-min buckets) per mouse">
      {mice.length === 0 ? <Empty /> : (
        <div className="space-y-2">
          {mice.map((m: any) => (
            <div key={m.mouse_id} className="flex items-center gap-2">
              <span className="w-5 text-xs text-muted-foreground font-mono">{m.mouse_id}</span>
              <div className="flex h-5 flex-1 overflow-hidden rounded">
                {m.segments.map((s: any, i: number) => (
                  <div key={i} className="flex-1" style={{ background: COLOR[s.state] ?? '#334155' }}
                    title={`${new Date(s.t).toLocaleTimeString()} · ${SHORT[s.state] ?? s.state}`} />
                ))}
              </div>
            </div>
          ))}
          <div className="flex gap-3 pt-1 text-[10px] text-muted-foreground">
            {STATES.map((s) => (
              <span key={s} className="flex items-center gap-1">
                <span className="inline-block h-2 w-2 rounded-sm" style={{ background: COLOR[s] }} />{SHORT[s]}
              </span>
            ))}
          </div>
        </div>
      )}
    </ReportCard>
  );
}

// ── 3 · Sleep bouts ───────────────────────────────────────────────────────────
function SleepBouts({ bouts }: { bouts: any }) {
  const mice = bouts?.mice ?? [];
  const bars = mice.map((m: any) => ({ mouse: m.mouse_id, sleepMin: m.total_sleep_min, bouts: m.bout_count }));
  return (
    <ReportCard icon={BedDouble} title="Sleep analysis" desc="Bouts, total sleep & fragmentation per mouse">
      {mice.length === 0 ? <Empty /> : (
        <>
          <div className="h-40 mb-2">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={bars}>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                <XAxis dataKey="mouse" tick={axis} />
                <YAxis tick={axis} />
                <Tooltip contentStyle={tip} formatter={(v: number, n: string) => [n === 'sleepMin' ? `${v} min` : v, n === 'sleepMin' ? 'Total sleep' : 'Bouts']} />
                <Bar dataKey="sleepMin" fill={COLOR['Possible Sleep']} radius={[3, 3, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <div className="space-y-1">
            {mice.map((m: any) => (
              <div key={m.mouse_id} className="flex items-center justify-between text-xs border-t border-border/50 pt-1">
                <span className="font-mono text-muted-foreground">Mouse {m.mouse_id}</span>
                <span>{m.bout_count} bouts · mean {m.mean_bout_min}m · max {m.max_bout_min}m · frag {m.fragmentation_index}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </ReportCard>
  );
}

// ── 4 · Per-mouse comparison ──────────────────────────────────────────────────
function Comparison({ budget, days }: { budget: any; days: number }) {
  const data = (budget?.mice ?? []).map((m: any) => ({
    mouse: m.mouse_id,
    Sleep: +((m.states['Possible Sleep']?.hours ?? 0) / days).toFixed(2),
    Active: +((m.states['Wake / Active']?.hours ?? 0) / days).toFixed(2),
    Rest: +((m.states['Quiet / Rest']?.hours ?? 0) / days).toFixed(2),
  }));
  return (
    <ReportCard icon={Users} title="Per-mouse comparison" desc="Hours/day per state — spot the outlier">
      {data.length === 0 ? <Empty /> : (
        <div className="h-52">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data}>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
              <XAxis dataKey="mouse" tick={axis} />
              <YAxis tick={axis} unit="h" />
              <Tooltip contentStyle={tip} formatter={(v: number) => [`${v} h/day`]} />
              <Legend wrapperStyle={{ fontSize: 10 }} />
              <Bar dataKey="Sleep" fill={COLOR['Possible Sleep']} radius={[3, 3, 0, 0]} />
              <Bar dataKey="Rest" fill={COLOR['Quiet / Rest']} radius={[3, 3, 0, 0]} />
              <Bar dataKey="Active" fill={COLOR['Wake / Active']} radius={[3, 3, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </ReportCard>
  );
}

// ── 5 · Circadian ─────────────────────────────────────────────────────────────
function Circadian({ circ }: { circ: any }) {
  const data = (circ?.hours ?? []).map((h: any) => ({ hour: `${h.hour}`, Active: h.active_pct, Sleep: h.sleep_pct }));
  return (
    <ReportCard icon={Activity} title="Circadian pattern" desc="Activity by hour of day (UTC) — mice are nocturnal">
      {data.length === 0 ? <Empty /> : (
        <div className="h-52">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data}>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
              <ReferenceArea x1="0" x2={`${DARK_END}`} fill="#1e293b" fillOpacity={0.5} />
              <ReferenceArea x1={`${DARK_START}`} x2="23" fill="#1e293b" fillOpacity={0.5} />
              <XAxis dataKey="hour" tick={axis} />
              <YAxis tick={axis} unit="%" domain={[0, 100]} />
              <Tooltip contentStyle={tip} formatter={(v: number) => [`${v}%`]} labelFormatter={(l) => `${l}:00`} />
              <Legend wrapperStyle={{ fontSize: 10 }} />
              <Line type="monotone" dataKey="Active" stroke={COLOR['Wake / Active']} dot={false} strokeWidth={2} />
              <Line type="monotone" dataKey="Sleep" stroke={COLOR['Possible Sleep']} dot={false} strokeWidth={2} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </ReportCard>
  );
}

// ── 6 · Social & anomalies ────────────────────────────────────────────────────
function SocialAnomalies({ social, anom, mice }: { social: any; anom: any; mice: string[] }) {
  const socialKeys = useMemo(() => {
    const set = new Set<string>();
    (social?.days ?? []).forEach((d: any) => Object.keys(d.social ?? {}).forEach((k) => set.add(k)));
    return Array.from(set);
  }, [social]);
  const socialData = (social?.days ?? []).map((d: any) => ({ date: d.date.slice(5), ...d.social }));
  // anomalies: repetitive minutes per day per mouse -> rows per date with mouse columns
  const anomData = (anom?.days ?? []).map((d: any) => {
    const row: any = { date: d.date.slice(5) };
    d.mice.forEach((m: any) => { row[m.mouse_id] = m.repetitive_min; });
    return row;
  });
  const repTotal = anom?.totals?.repetitive_min ?? 0;
  return (
    <ReportCard icon={Users} title="Social & anomalies" desc="Group state over days + stereotypy (repetitive behaviour)">
      <div className="h-36 mb-2">
        {socialData.length === 0 ? <Empty /> : (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={socialData} stackOffset="expand">
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
              <XAxis dataKey="date" tick={axis} />
              <YAxis tick={axis} tickFormatter={(v) => `${Math.round(v * 100)}%`} />
              <Tooltip contentStyle={tip} formatter={(v: number) => [`${v}%`]} />
              <Legend wrapperStyle={{ fontSize: 10 }} />
              {socialKeys.map((k) => (
                <Area key={k} type="monotone" dataKey={k} stackId="s" stroke={SOCIAL_COLOR[k] ?? '#64748b'}
                  fill={SOCIAL_COLOR[k] ?? '#64748b'} fillOpacity={0.45} />
              ))}
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
      <div className="flex items-center gap-2 text-xs text-muted-foreground mb-1">
        <AlertTriangle className="h-3.5 w-3.5 text-amber-400" />
        Repetitive behaviour — {repTotal} min total over range
      </div>
      <div className="h-28">
        {anomData.length === 0 ? <Empty small /> : (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={anomData}>
              <XAxis dataKey="date" tick={axis} />
              <YAxis tick={axis} unit="m" />
              <Tooltip contentStyle={tip} formatter={(v: number) => [`${v} min`]} />
              {mice.map((m, i) => (
                <Bar key={m} dataKey={m} stackId="r" fill={['#f59e0b', '#ef4444', '#f97316', '#fbbf24', '#dc2626'][i % 5]} />
              ))}
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>
    </ReportCard>
  );
}

// ── shared bits ───────────────────────────────────────────────────────────────
function ReportCard({ icon: Icon, title, desc, children }: {
  icon: React.ComponentType<{ className?: string }>; title: string; desc: string; children: React.ReactNode;
}) {
  return (
    <Card className="border-border/60">
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm">
          <Icon className="h-4 w-4 text-muted-foreground" /> {title}
        </CardTitle>
        <CardDescription className="text-xs">{desc}</CardDescription>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}

function Kpi({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <div className="rounded-md border border-border/60 p-2">
      <div className="text-[10px] text-muted-foreground uppercase tracking-wide">{label}</div>
      <div className={`font-mono text-base font-bold ${color}`}>{value}</div>
    </div>
  );
}

function Empty({ small }: { small?: boolean }) {
  return <p className={`text-xs text-muted-foreground text-center ${small ? 'py-2' : 'py-8'}`}>No data yet</p>;
}
