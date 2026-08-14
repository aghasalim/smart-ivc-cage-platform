import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import {
  Activity,
  AlertTriangle,
  Droplet,
  FlaskConical,
  Thermometer,
  Droplets,
  UtensilsCrossed,
  Wifi,
  WifiOff,
  BrainCircuit,
} from 'lucide-react';

import {
  LineChart,
  Line,
  ResponsiveContainer,
  Tooltip as RechartsTip,
} from 'recharts';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { api, Cage, Reading, Alert as ApiAlert } from '@/lib/api';
import { useLabels } from '@/store/labels';
import { relativeTime } from '@/lib/utils';

const LABEL_COLORS: Record<string, string> = {
  sleeping: 'bg-slate-500/20 text-slate-700 dark:text-slate-400 border-slate-500/30',
  resting: 'bg-violet-500/20 text-violet-700 dark:text-violet-400 border-violet-500/30',
  active: 'bg-emerald-500/20 text-emerald-700 dark:text-emerald-400 border-emerald-500/30',
  exploring: 'bg-amber-500/20 text-amber-700 dark:text-amber-400 border-amber-500/30',
  eating: 'bg-orange-500/20 text-orange-700 dark:text-orange-400 border-orange-500/30',
  drinking: 'bg-sky-500/20 text-sky-700 dark:text-sky-400 border-sky-500/30',
  stereotyped: 'bg-rose-500/20 text-rose-700 dark:text-rose-400 border-rose-500/30',
};

export default function OverviewPage() {
  const { t } = useTranslation();
  const { data: cages = [], isError: cagesError } = useQuery({
    queryKey: ['cages'],
    queryFn: () => api<Cage[]>('/api/v1/cages'),
    refetchInterval: 8000,
  });

  const { data: alerts = [], isError: alertsError } = useQuery({
    queryKey: ['alerts'],
    queryFn: () => api<ApiAlert[]>('/api/v1/alerts?acknowledged=false&limit=50'),
    refetchInterval: 8000,
  });

  const onlineCages = cages.filter(
    (c) => c.last_seen && Date.now() - new Date(c.last_seen).getTime() < 60_000,
  );

  const hasError = cagesError || alertsError;

  return (
    <div className="space-y-6">
      {hasError && (
        <div className="flex items-center gap-2 rounded-md border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-400">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          {t('overview.loadError')}
        </div>
      )}
      {/* Header */}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{t('overview.title')}</h1>
          <p className="text-sm text-muted-foreground">
            {t('overview.subtitle')} · {new Date().toLocaleDateString(undefined, { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' })}
          </p>
        </div>
      </div>

      {/* Stats bar */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <StatCard
          label={t('overview.totalCages')}
          value={String(cages.length)}
          sub={t('overview.registered')}
          color="text-foreground"
        />
        <StatCard
          label={t('overview.online')}
          value={String(onlineCages.length)}
          sub={t('overview.ofN', { n: cages.length })}
          color="text-emerald-400"
          icon={<Wifi className="h-4 w-4 text-emerald-400" />}
        />
        <StatCard
          label={t('overview.openAlerts')}
          value={String(alerts.length)}
          sub={alerts.length > 0 ? t('common.actionNeeded') : t('common.allClear')}
          color={alerts.length > 0 ? 'text-amber-400' : 'text-emerald-400'}
          icon={alerts.length > 0 ? <AlertTriangle className="h-4 w-4 text-amber-400" /> : undefined}
        />
        <StatCard
          label={t('overview.offline')}
          value={String(cages.length - onlineCages.length)}
          sub={t('overview.cages')}
          color={cages.length - onlineCages.length > 0 ? 'text-rose-400' : 'text-muted-foreground'}
          icon={cages.length - onlineCages.length > 0 ? <WifiOff className="h-4 w-4 text-rose-400" /> : undefined}
        />
      </div>

      {/* Cage grid */}
      {cages.length === 0 ? (
        <Card>
          <CardContent className="py-12 text-center text-sm text-muted-foreground">
            {t('overview.noCages')}
          </CardContent>
        </Card>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {cages.map((c) => (
            <CageCard key={c.id} cage={c} alerts={alerts.filter((a) => a.cage_id === c.id)} />
          ))}
        </div>
      )}
    </div>
  );
}

function StatCard({
  label,
  value,
  sub,
  color,
  icon,
}: {
  label: string;
  value: string;
  sub: string;
  color: string;
  icon?: React.ReactNode;
}) {
  return (
    <Card className="hover:border-primary/40 transition-colors">
      <CardContent className="p-4">
        <div className="flex items-center justify-between mb-1">
          <span className="text-xs text-muted-foreground">{label}</span>
          {icon}
        </div>
        <div className={`font-mono text-2xl font-bold ${color}`}>{value}</div>
        <div className="text-xs text-muted-foreground mt-0.5">{sub}</div>
      </CardContent>
    </Card>
  );
}

function CageCard({ cage, alerts }: { cage: Cage; alerts: ApiAlert[] }) {
  const { t } = useTranslation();
  const { labels } = useLabels();
  const aiLabel = labels[cage.id];

  const { data: readings = [] } = useQuery({
    queryKey: ['cage', cage.id, 'latest'],
    queryFn: () => api<Reading[]>(`/api/v1/cages/${cage.id}/readings?limit=24`),
    refetchInterval: 6000,
  });

  const latest = (sensor: string) => readings.find((r) => r.sensor === sensor);
  const feeding = latest('feeding');
  const water = latest('water');
  const metab = latest('metabolic');
  const beh = latest('behaviour');
  const env = latest('environment');

  const offline = !cage.last_seen || Date.now() - new Date(cage.last_seen).getTime() > 60_000;

  const movementSpark = readings
    .filter((r) => r.sensor === 'behaviour')
    .slice()
    .reverse()
    .slice(-16)
    .map((r, i) => ({ i, v: r.payload.movement_cm ?? 0 }));

  const rerSpark = readings
    .filter((r) => r.sensor === 'metabolic')
    .slice()
    .reverse()
    .slice(-16)
    .map((r, i) => ({ i, v: r.payload.rer ?? 0 }));

  const labelClass = aiLabel ? (LABEL_COLORS[aiLabel.behaviour] ?? 'bg-muted/30 text-muted-foreground') : '';

  return (
    <Link to={`/cages/${cage.id}`} className="block group">
      <Card className="transition-all group-hover:border-primary/60 group-hover:shadow-md group-hover:shadow-primary/5">
        <CardHeader className="pb-2">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <CardTitle className="text-base truncate">{cage.name}</CardTitle>
              <CardDescription className="font-mono text-xs">{cage.id}</CardDescription>
            </div>
            <div className="flex items-center gap-1.5 flex-shrink-0">
              {alerts.length > 0 && (
                <Badge variant="warning" className="gap-1 text-xs">
                  <AlertTriangle className="h-3 w-3" />
                  {alerts.length}
                </Badge>
              )}
              <Badge variant={offline ? 'destructive' : 'success'} className="text-xs">
                {offline ? t('common.offline') : t('common.live')}
              </Badge>
            </div>
          </div>

          {/* Model label chip */}
          {aiLabel && (
            <div className={`inline-flex items-center gap-1.5 mt-2 px-2 py-0.5 rounded-full border text-xs font-medium w-fit ${labelClass}`}>
              <BrainCircuit className="h-3 w-3" />
              {aiLabel.behaviour}
              <span className="opacity-60">· {(aiLabel.confidence * 100).toFixed(0)}%</span>
            </div>
          )}
        </CardHeader>

        <CardContent className="pt-0 space-y-3">
          {/* Metrics */}
          <div className="grid grid-cols-3 gap-2 text-sm">
            <Metric icon={UtensilsCrossed} label={t('overview.delivered')} value={feeding ? `${feeding.payload.delivered_g.toFixed(2)} g` : '—'} />
            <Metric icon={Droplet} label={t('overview.waterFlow')} value={water ? `${water.payload.flow_ml.toFixed(2)} mL` : '—'} />
            <Metric icon={Thermometer} label={t('overview.temperature')} value={env?.payload?.temperature_c != null ? `${env.payload.temperature_c.toFixed(1)} °C` : '—'} alert={env && (env.payload.temperature_c < 20 || env.payload.temperature_c > 26)} />
            <Metric icon={Droplets} label={t('overview.humidity')} value={env?.payload?.humidity_pct != null ? `${env.payload.humidity_pct.toFixed(1)} %` : '—'} alert={env && (env.payload.humidity_pct < 30 || env.payload.humidity_pct > 70)} />
            <Metric icon={FlaskConical} label={t('overview.rer')} value={metab ? metab.payload.rer.toFixed(3) : '—'} alert={metab && (metab.payload.rer < 0.7 || metab.payload.rer > 1.0)} />
            <Metric icon={Activity} label={t('overview.movement')} value={beh ? `${beh.payload.movement_cm.toFixed(1)} cm` : '—'} />
          </div>

          {/* Sparklines */}
          {(movementSpark.length > 2 || rerSpark.length > 2) && (
            <div className="grid grid-cols-2 gap-2 pt-1">
              <div>
                <div className="text-xs text-muted-foreground mb-1">{t('overview.movement')}</div>
                <Sparkline data={movementSpark} color="#10b981" />
              </div>
              <div>
                <div className="text-xs text-muted-foreground mb-1">{t('overview.rer')}</div>
                <Sparkline data={rerSpark} color="#f59e0b" />
              </div>
            </div>
          )}

          <div className="text-xs text-muted-foreground pt-0.5">
            {t('overview.lastSeen', { when: relativeTime(cage.last_seen) })} · {cage.animal_strain || '—'} · {t('overview.age', { days: cage.animal_age_days })}
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}

function Sparkline({ data, color }: { data: { i: number; v: number }[]; color: string }) {
  return (
    <div className="h-10 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data}>
          <Line
            type="monotone"
            dataKey="v"
            dot={false}
            strokeWidth={1.5}
            stroke={color}
            isAnimationActive={false}
          />
          <RechartsTip
            contentStyle={{ background: 'hsl(var(--popover))', border: '1px solid hsl(var(--border))', borderRadius: 6, fontSize: 11, padding: '2px 8px' }}
            formatter={(v: number) => [v.toFixed(2)]}
            labelFormatter={() => ''}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function Metric({
  icon: Icon,
  label,
  value,
  alert,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: string;
  alert?: boolean;
}) {
  return (
    <div className="flex items-center gap-2">
      <div className={`grid h-8 w-8 place-items-center rounded-md flex-shrink-0 ${alert ? 'bg-amber-500/10' : 'bg-muted'}`}>
        <Icon className={`h-4 w-4 ${alert ? 'text-amber-400' : 'text-primary'}`} />
      </div>
      <div className="leading-tight min-w-0">
        <div className="text-xs text-muted-foreground">{label}</div>
        <div className={`font-mono text-sm ${alert ? 'text-amber-400' : ''}`}>{value}</div>
      </div>
    </div>
  );
}
