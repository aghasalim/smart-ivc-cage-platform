import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { Download, Droplet, FlaskConical, Moon, UtensilsCrossed, Activity, AlertTriangle, AlertCircle } from 'lucide-react';
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ExportDialog } from '@/components/ExportDialog';
import { TimeSeriesExplorer } from '@/components/TimeSeriesExplorer';
import { BehaviorReports } from '@/components/BehaviorReports';
import { api, Cage, Summary } from '@/lib/api';

export default function ReportsPage() {
  const { t } = useTranslation();
  const { data: cages = [], isError: cagesError } = useQuery({
    queryKey: ['cages'],
    queryFn: () => api<Cage[]>('/api/v1/cages'),
  });

  if (cagesError) {
    return (
      <div className="flex items-center gap-2 rounded-md border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-400">
        <AlertCircle className="h-4 w-4 shrink-0" />
        {t('reportsPage.loadError')}
      </div>
    );
  }

  if (cages.length === 0) return <p className="text-sm text-muted-foreground">{t('reports.noCages')}</p>;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{t('reports.title')}</h1>
        <p className="text-sm text-muted-foreground">{t('reports.subtitle')}</p>
      </div>
      <Tabs defaultValue={cages[0].id}>
        <TabsList className="flex flex-wrap h-auto">
          {cages.map((c) => (
            <TabsTrigger key={c.id} value={c.id}>{c.name}</TabsTrigger>
          ))}
        </TabsList>
        {cages.map((c) => (
          <TabsContent key={c.id} value={c.id}>
            <CageReport cage={c} />
          </TabsContent>
        ))}
      </Tabs>
    </div>
  );
}

function CageReport({ cage }: { cage: Cage }) {
  const { t } = useTranslation();
  const [days, setDays] = useState(7);
  const [exportOpen, setExportOpen] = useState(false);
  const { data: summary, isError: summaryError } = useQuery({
    queryKey: ['cage', cage.id, 'summary', days],
    queryFn: () => api<Summary>(`/api/v1/cages/${cage.id}/summary?days=${days}`),
    refetchInterval: 30_000,
  });

  const dayRows = summary?.days ?? [];

  const intakeData = dayRows.map((d) => ({
    date: d.date.slice(5),
    intake: Number(d.feeding.intake_g.toFixed(2)),
    wasted: Number(d.feeding.wasted_g.toFixed(2)),
  }));
  const waterData = dayRows.map((d) => ({ date: d.date.slice(5), water: Number(d.water_ml.toFixed(1)) }));
  const metabolicData = dayRows.map((d) => ({
    date: d.date.slice(5),
    rer: Number(d.metabolic.avg_rer.toFixed(3)),
    ee: Number(d.metabolic.ee_kcal.toFixed(2)),
  }));
  const behaviourData = dayRows.map((d) => ({
    date: d.date.slice(5),
    sleeping: Math.round(d.behaviour.sleeping_minutes),
    active: Math.round(d.behaviour.active_minutes),
  }));

  // Totals
  const totals = dayRows.reduce(
    (acc, d) => ({
      intake: acc.intake + d.feeding.intake_g,
      wasted: acc.wasted + d.feeding.wasted_g,
      water: acc.water + d.water_ml,
      ee: acc.ee + d.metabolic.ee_kcal,
      sleep: acc.sleep + d.behaviour.sleeping_minutes,
      active: acc.active + d.behaviour.active_minutes,
    }),
    { intake: 0, wasted: 0, water: 0, ee: 0, sleep: 0, active: 0 },
  );

  const n = Math.max(dayRows.length, 1);
  const avgRer =
    dayRows.length > 0
      ? dayRows.reduce((s, d) => s + d.metabolic.avg_rer, 0) / dayRows.length
      : 0;

  // Distribution: sleep vs active (pie)
  const pieData = [
    { name: t('reports.sleeping'), value: Math.round(totals.sleep), color: '#64748b' },
    { name: t('reports.active'), value: Math.round(totals.active), color: '#10b981' },
  ];

  const wasteRatio = totals.intake + totals.wasted > 0
    ? (totals.wasted / (totals.intake + totals.wasted)) * 100
    : 0;

  return (
    <div className="space-y-4 mt-4">
      {summaryError && (
        <div className="flex items-center gap-2 rounded-md border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-400">
          <AlertCircle className="h-4 w-4 shrink-0" />
          {t('reportsPage.loadError')}
        </div>
      )}
      {/* Controls */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex gap-1.5">
          {[7, 14, 30].map((d) => (
            <Button
              key={d}
              size="sm"
              variant={days === d ? 'default' : 'outline'}
              onClick={() => setDays(d)}
            >
              {t(`reports.days${d}` as const)}
            </Button>
          ))}
        </div>
        <Button variant="outline" size="sm" onClick={() => setExportOpen(true)}>
          <Download className="mr-2 h-4 w-4" /> {t('common.export')}
        </Button>
        <ExportDialog
          open={exportOpen}
          onOpenChange={setExportOpen}
          target={{ cageId: cage.id, cageName: cage.name }}
        />
      </div>

      {/* Interactive explorer — pick any metric, granularity, aggregation, chart type */}
      <div>
        <div className="mb-2">
          <h2 className="text-base font-semibold">{t('reportsPage.explorerTitle')}</h2>
          <p className="text-xs text-muted-foreground">
            {t('reportsPage.explorerDesc')}
          </p>
        </div>
        <TimeSeriesExplorer cageId={cage.id} />
      </div>

      {/* Daily summary section header */}
      <div className="pt-2">
        <h2 className="text-base font-semibold">{t('reportsPage.dailySummaryTitle')}</h2>
        <p className="text-xs text-muted-foreground">{t('reportsPage.dailySummaryDesc')}</p>
      </div>

      {/* KPI strip */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
        <KpiCard
          icon={UtensilsCrossed}
          label={t('reports.kpi.avgIntake')}
          value={`${(totals.intake / n).toFixed(1)} g`}
          color="text-emerald-400"
        />
        <KpiCard
          icon={AlertTriangle}
          label={t('reports.kpi.wasteRatio')}
          value={`${wasteRatio.toFixed(1)} %`}
          color={wasteRatio > 15 ? 'text-amber-400' : 'text-muted-foreground'}
        />
        <KpiCard
          icon={Droplet}
          label={t('reports.kpi.avgWater')}
          value={`${(totals.water / n).toFixed(1)} mL`}
          color="text-sky-400"
        />
        <KpiCard
          icon={FlaskConical}
          label={t('reports.kpi.meanRer')}
          value={avgRer.toFixed(3)}
          color={avgRer < 0.7 || avgRer > 1.0 ? 'text-amber-400' : 'text-violet-400'}
        />
        <KpiCard
          icon={Moon}
          label={t('reports.kpi.avgSleep')}
          value={`${(totals.sleep / n / 60).toFixed(1)} h`}
          color="text-slate-400"
        />
        <KpiCard
          icon={Activity}
          label={t('reports.kpi.avgActive')}
          value={`${(totals.active / n / 60).toFixed(1)} h`}
          color="text-emerald-400"
        />
      </div>

      {/* Feeding chart */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">{t('reports.feedingTitle')}</CardTitle>
          <CardDescription>{t('reports.feedingDesc')}</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={intakeData}>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                <XAxis dataKey="date" stroke="hsl(var(--border))" tick={{ fontSize: 11, fill: 'hsl(var(--muted-foreground))' }} />
                <YAxis stroke="hsl(var(--border))" tick={{ fontSize: 11, fill: 'hsl(var(--muted-foreground))' }} />
                <Tooltip contentStyle={tooltipStyle} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                <Bar dataKey="intake" name={t('reports.intake')} fill="#10b981" stackId="a" />
                <Bar dataKey="wasted" name={t('reports.wasted')} fill="#f59e0b" stackId="a" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Water area */}
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">{t('reports.waterTitle')}</CardTitle>
            <CardDescription>{t('reports.waterDesc')}</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="h-56">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={waterData}>
                  <defs>
                    <linearGradient id="waterFill" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="#0ea5e9" stopOpacity={0.45} />
                      <stop offset="100%" stopColor="#0ea5e9" stopOpacity={0.02} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                  <XAxis dataKey="date" stroke="hsl(var(--border))" tick={{ fontSize: 11, fill: 'hsl(var(--muted-foreground))' }} />
                  <YAxis stroke="hsl(var(--border))" tick={{ fontSize: 11, fill: 'hsl(var(--muted-foreground))' }} />
                  <Tooltip contentStyle={tooltipStyle} />
                  <Area type="monotone" dataKey="water" stroke="#0ea5e9" strokeWidth={2} fill="url(#waterFill)" />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </CardContent>
        </Card>

        {/* RER & EE dual */}
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">{t('reports.metabolicTitle')}</CardTitle>
            <CardDescription>{t('reports.metabolicDesc')}</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="h-56">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={metabolicData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                  <XAxis dataKey="date" stroke="hsl(var(--border))" tick={{ fontSize: 11, fill: 'hsl(var(--muted-foreground))' }} />
                  <YAxis yAxisId="r" domain={[0.6, 1.1]} stroke="#a78bfa" tick={{ fontSize: 11, fill: 'hsl(var(--muted-foreground))' }} />
                  <YAxis yAxisId="ee" orientation="right" stroke="#f59e0b" tick={{ fontSize: 11, fill: 'hsl(var(--muted-foreground))' }} />
                  <Tooltip contentStyle={tooltipStyle} />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  <Line yAxisId="r" type="monotone" dataKey="rer" name={t('overview.rer')} stroke="#a78bfa" strokeWidth={2} dot={false} />
                  <Line yAxisId="ee" type="monotone" dataKey="ee" name={t('reportsPage.eeKcal')} stroke="#f59e0b" strokeWidth={2} dot={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </CardContent>
        </Card>

        {/* Behaviour stacked area */}
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">{t('reports.behaviourTitle')}</CardTitle>
            <CardDescription>{t('reports.behaviourDesc')}</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="h-56">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={behaviourData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                  <XAxis dataKey="date" stroke="hsl(var(--border))" tick={{ fontSize: 11, fill: 'hsl(var(--muted-foreground))' }} />
                  <YAxis stroke="hsl(var(--border))" tick={{ fontSize: 11, fill: 'hsl(var(--muted-foreground))' }} />
                  <Tooltip contentStyle={tooltipStyle} />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  <Area type="monotone" dataKey="sleeping" name={t('reports.sleeping')} stackId="b" stroke="#64748b" fill="#64748b" fillOpacity={0.4} />
                  <Area type="monotone" dataKey="active" name={t('reports.active')} stackId="b" stroke="#10b981" fill="#10b981" fillOpacity={0.4} />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </CardContent>
        </Card>

        {/* Behaviour distribution pie */}
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">{t('reports.pieTitle')}</CardTitle>
            <CardDescription>{t('reports.pieDesc', { days: dayRows.length })}</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="h-56">
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie
                    data={pieData}
                    dataKey="value"
                    nameKey="name"
                    cx="50%"
                    cy="50%"
                    innerRadius={50}
                    outerRadius={85}
                    paddingAngle={2}
                  >
                    {pieData.map((entry) => (
                      <Cell key={entry.name} fill={entry.color} />
                    ))}
                  </Pie>
                  <Tooltip contentStyle={tooltipStyle} formatter={(v: number) => [`${v} min`]} />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                </PieChart>
              </ResponsiveContainer>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Camera-driven behavioural analytics — 6 reports (time-in-state) */}
      <BehaviorReports cageId={cage.id} />
    </div>
  );
}

const tooltipStyle = {
  background: 'hsl(var(--popover))',
  border: '1px solid hsl(var(--border))',
  borderRadius: 8,
  fontSize: 12,
};

function KpiCard({
  icon: Icon,
  label,
  value,
  color,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: string;
  color: string;
}) {
  return (
    <Card>
      <CardContent className="p-3">
        <div className="flex items-center gap-2 text-xs text-muted-foreground mb-1">
          <Icon className="h-3.5 w-3.5" />
          {label}
        </div>
        <div className={`font-mono text-lg font-bold ${color}`}>{value}</div>
      </CardContent>
    </Card>
  );
}
