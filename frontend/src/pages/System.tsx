/**
 * System / operations page.
 *
 * Single pane-of-glass for the rubric's "comprehensive and usable logging
 * facility" + "easy management, adaptability, and configurability" criteria.
 * Pulls live state from /api/v1/system/* — no shell access required.
 *
 *  - Service health (backend / cameras / env-ingester)
 *  - Network endpoints (example.org + cam.example.org tunnels)
 *  - Auto-deploy pipeline status + recent deploys
 *  - Tail of pi-deploy.log (filterable)
 *  - Security posture (HTTPS, HSTS, CSP, rate limiting, JWT TTL)
 */
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import {
  Activity,
  AlertTriangle,
  BrainCircuit,
  CheckCircle2,
  Clock,
  Cloud,
  Cpu,
  ExternalLink,
  Fan,
  FileTerminal,
  GitBranch,
  Globe,
  HardDrive,
  Power,
  PowerOff,
  RefreshCw,
  RotateCcw,
  ScrollText,
  Server,
  ShieldCheck,
  Sparkles,
  Thermometer,
  Timer,
  Wind,
  WifiOff,
  XCircle,
} from 'lucide-react';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import { Separator } from '@/components/ui/separator';
import { toast } from 'sonner';
import { api } from '@/lib/api';

type AiModel = {
  type: string;
  purpose: string;
  labels?: string[];
  metrics?: string[];
  cages_warm?: number;
};

type SystemStatus = {
  app: { name: string; version: string; env: string; git_sha: string | null };
  ai_models: Record<string, AiModel>;
  runtime: { uptime_s: number; python: string; platform: string; hostname: string; pid: number };
  services: Record<string, string>;
  endpoints: Record<string, string>;
  deploy: {
    auto_deploy: boolean;
    source: string;
    branch: string;
    poll_interval_s: number;
    last_deploy_ts: number | null;
    deploy_log_available: boolean;
  };
  security: {
    https: boolean;
    hsts: boolean;
    csp: boolean;
    rate_limited_endpoints: string[];
    jwt_expire_minutes: number;
  };
};

type LogLine = { ts: string | null; msg: string };
type LogsResponse = { available: boolean; lines: LogLine[]; path: string | null };
type Deploy = { ts: string; sha: string };
type DeploysResponse = { available: boolean; deploys: Deploy[] };

// ── Pi hardware control ─────────────────────────────────────────────────────

type PiStatus = {
  cpu_percent: number | null;
  cpu_temp_c: number | null;
  memory: { total_mb: number; used_mb: number; available_mb: number; percent: number } | null;
  fan: { level: number | null; max_level: number; pwm: number | null; percent: number | null } | null;
  uptime_s: number | null;
};

async function fetchPiStatus(): Promise<PiStatus> {
  return api<PiStatus>('/api/v1/system/pi');
}

function piPost<T = unknown>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  return api<T>(path, { method: 'POST', body: JSON.stringify(body) });
}

const FAN_LEVELS = [
  { level: -1, label: 'Auto' },
  { level: 0,  label: 'Off'  },
  { level: 1,  label: 'Low'  },
  { level: 2,  label: 'Med'  },
  { level: 3,  label: 'High' },
  { level: 4,  label: 'Max'  },
] as const;

// Index 0 = Off, 1-4 = Low-Max (Auto uses the daemon-reported level for animation)
const FAN_SPIN_DURATIONS = ['0s', '2.4s', '1.4s', '0.8s', '0.4s'];

function fanLevelLabel(level: number | null | undefined): string {
  if (level == null) return '?';
  const entry = FAN_LEVELS.find(f => f.level === level);
  return entry ? entry.label : String(level);
}

function PiHardwareControl() {
  const { t } = useTranslation();
  const { data: pi, isError, refetch } = useQuery<PiStatus>({
    queryKey: ['pi-status'],
    queryFn: fetchPiStatus,
    refetchInterval: 5_000,
    retry: 1,
  });

  const [fanBusy, setFanBusy]                 = useState(false);
  const [rebootConfirm, setRebootConfirm]     = useState(false);
  const [shutdownConfirm, setShutdownConfirm] = useState(false);
  const [actionBusy, setActionBusy]           = useState<'reboot' | 'shutdown' | null>(null);

  const reachable = pi !== undefined && !isError;

  async function setFanLevel(level: number) {
    setFanBusy(true);
    try {
      await piPost('/api/v1/system/pi/fan', { level });
      await refetch();
      toast.success(`Fan → ${fanLevelLabel(level)}`);
    } catch {
      toast.error(t('system.fanFailed'));
    } finally {
      setFanBusy(false);
    }
  }

  async function doReboot() {
    setActionBusy('reboot');
    try {
      await piPost('/api/v1/system/pi/reboot');
      toast.success(t('system.rebootSent'));
      setRebootConfirm(false);
    } catch {
      toast.error(t('system.rebootFailed'));
    } finally {
      setActionBusy(null);
    }
  }

  async function doShutdown() {
    setActionBusy('shutdown');
    try {
      await piPost('/api/v1/system/pi/shutdown');
      toast.success(t('system.shutdownSent'));
      setShutdownConfirm(false);
    } catch {
      toast.error(t('system.shutdownFailed'));
    } finally {
      setActionBusy(null);
    }
  }

  const temp   = pi?.cpu_temp_c;
  const cpuPct = pi?.cpu_percent;
  const mem    = pi?.memory;
  const fan    = pi?.fan;
  const uptime = pi?.uptime_s;

  const tempColor =
    temp == null ? 'text-muted-foreground' :
    temp > 80    ? 'text-red-500'          :
    temp > 65    ? 'text-amber-500'        :
                   'text-emerald-400';

  // For animation we always use what the Pi reports as actual level.
  const spinLevel = fan?.level ?? 0;
  const spinDur   = FAN_SPIN_DURATIONS[Math.min(Math.max(spinLevel, 0), 4)];

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2">
              <Cpu className="h-4 w-4" />
              {t('system.piHardwareTitle')}
            </CardTitle>
            <CardDescription>{t('system.piHardwareDesc')}</CardDescription>
          </div>
          {reachable ? (
            <Badge variant="default" className="gap-1.5 shrink-0">
              <Activity className="h-3 w-3" /> {t('system.piOnline')}
            </Badge>
          ) : (
            <Badge variant="destructive" className="gap-1.5 shrink-0">
              <WifiOff className="h-3 w-3" /> {t('system.piOffline')}
            </Badge>
          )}
        </div>
      </CardHeader>

      <CardContent className="space-y-5">

        {/* Metric tiles */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <div className="border rounded-lg p-3 space-y-1.5 bg-card">
            <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Thermometer className="h-3 w-3" /> {t('system.cpuTemp')}
            </div>
            <div className={`text-2xl font-bold font-mono leading-none ${tempColor}`}>
              {temp != null ? `${temp}°C` : '—'}
            </div>
            <Progress
              value={temp != null ? Math.min(temp, 100) : 0}
              className="h-1"
              style={temp != null && temp > 80 ? { '--progress-fill': 'rgb(239 68 68)' } as React.CSSProperties : {}}
            />
          </div>

          <div className="border rounded-lg p-3 space-y-1.5 bg-card">
            <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Cpu className="h-3 w-3" /> {t('system.cpuUsage')}
            </div>
            <div className="text-2xl font-bold font-mono leading-none">
              {cpuPct != null ? `${cpuPct}%` : '—'}
            </div>
            <Progress value={cpuPct ?? 0} className="h-1" />
          </div>

          <div className="border rounded-lg p-3 space-y-1.5 bg-card">
            <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <HardDrive className="h-3 w-3" /> {t('system.memory')}
            </div>
            <div className="text-2xl font-bold font-mono leading-none">
              {mem ? `${mem.percent}%` : '—'}
            </div>
            <Progress value={mem?.percent ?? 0} className="h-1" />
            {mem && (
              <div className="text-[10px] text-muted-foreground font-mono">
                {mem.used_mb} / {mem.total_mb} MB
              </div>
            )}
          </div>

          <div className="border rounded-lg p-3 space-y-1.5 bg-card">
            <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Clock className="h-3 w-3" /> {t('system.piUptime')}
            </div>
            <div className="text-2xl font-bold font-mono leading-none text-emerald-400">
              {uptime != null ? formatUptime(uptime) : '—'}
            </div>
          </div>
        </div>

        {/* Fan control */}
        <div className="border rounded-lg p-4 space-y-3 bg-card">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 font-medium text-sm">
              <Wind className="h-4 w-4 text-sky-400" />
              {t('system.fanSpeed')}
            </div>
            <span className="text-xs text-muted-foreground font-mono">
              {fan != null
                ? `Level ${fan.level ?? '?'} / ${fan.max_level} · ${fan.percent ?? '?'}%${fan.pwm != null ? ` · PWM ${fan.pwm}` : ''}`
                : '—'}
            </span>
          </div>

          <div className="flex justify-center py-3">
            <Fan
              className={[
                'h-14 w-14 transition-colors',
                spinLevel === 0
                  ? 'text-muted-foreground/25'
                  : spinLevel === 4
                    ? 'text-sky-400 drop-shadow-[0_0_10px_rgba(56,189,248,0.55)]'
                    : 'text-sky-500/70',
                spinLevel > 0 ? 'animate-spin' : '',
              ].join(' ')}
              style={spinLevel > 0 ? { animationDuration: spinDur } : {}}
            />
          </div>

          <div className="grid grid-cols-6 gap-1.5">
            {FAN_LEVELS.map(({ level, label }) => (
              <Button
                key={level}
                size="sm"
                variant={fan?.level === level ? 'default' : 'outline'}
                disabled={fanBusy || !reachable}
                onClick={() => setFanLevel(level)}
                className="text-xs h-8 font-medium"
              >
                {label}
              </Button>
            ))}
          </div>

          <p className="text-[10px] text-muted-foreground leading-snug">
            {t('system.fanDesc')}
          </p>
        </div>

        {/* Power control */}
        <div className="border rounded-lg p-4 space-y-3 bg-card">
          <div className="flex items-center gap-2 font-medium text-sm">
            <Power className="h-4 w-4 text-muted-foreground" />
            {t('system.power')}
          </div>

          <div className="grid grid-cols-2 gap-3">
            {!rebootConfirm ? (
              <Button
                variant="outline"
                size="sm"
                disabled={!reachable || actionBusy !== null}
                onClick={() => { setShutdownConfirm(false); setRebootConfirm(true); }}
                className="gap-2"
                aria-label={t('system.rebootPi')}
              >
                <RotateCcw className="h-3.5 w-3.5" />
                {t('system.rebootPi')}
              </Button>
            ) : (
              <div className="flex gap-1.5">
                <Button
                  size="sm"
                  variant="destructive"
                  className="flex-1 text-xs"
                  disabled={actionBusy !== null}
                  onClick={doReboot}
                >
                  {actionBusy === 'reboot' ? t('system.rebooting') : t('system.confirmReboot')}
                </Button>
                <Button size="sm" variant="ghost" className="px-2"
                  onClick={() => setRebootConfirm(false)}>✕</Button>
              </div>
            )}

            {!shutdownConfirm ? (
              <Button
                variant="outline"
                size="sm"
                disabled={!reachable || actionBusy !== null}
                onClick={() => { setRebootConfirm(false); setShutdownConfirm(true); }}
                className="gap-2 text-red-400 hover:text-red-300 border-red-500/30 hover:border-red-500/60"
                aria-label={t('system.shutdownPi')}
              >
                <PowerOff className="h-3.5 w-3.5" />
                {t('system.shutdownPi')}
              </Button>
            ) : (
              <div className="flex gap-1.5">
                <Button
                  size="sm"
                  variant="destructive"
                  className="flex-1 text-xs"
                  disabled={actionBusy !== null}
                  onClick={doShutdown}
                >
                  {actionBusy === 'shutdown' ? t('system.shuttingDown') : t('system.confirmShutdown')}
                </Button>
                <Button size="sm" variant="ghost" className="px-2"
                  onClick={() => setShutdownConfirm(false)}>✕</Button>
              </div>
            )}
          </div>

          <p className="text-[10px] text-muted-foreground">
            ⚠ {t('system.afterShutdownNote')}
          </p>
        </div>

      </CardContent>
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────────────────────

function formatUptime(s: number): string {
  if (!Number.isFinite(s)) return '—';
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${sec}s`;
  return `${sec}s`;
}

function StatePill({ state }: { state: string }) {
  const ok = state === 'active' || state === 'activating';
  const unknown = state === 'unknown';
  return (
    <Badge variant={ok ? 'success' : unknown ? 'outline' : 'destructive'} className="gap-1">
      {ok ? <CheckCircle2 className="h-3 w-3" /> : <XCircle className="h-3 w-3" />}
      {state}
    </Badge>
  );
}

export default function SystemPage() {
  const { t } = useTranslation();
  const [filter, setFilter] = useState('');
  const [logLimit, setLogLimit] = useState(80);

  const { data: status, isError: statusError, refetch: refetchStatus, isFetching: statusFetching } = useQuery<SystemStatus>({
    queryKey: ['system', 'status'],
    queryFn: () => api<SystemStatus>('/api/v1/system/status'),
    refetchInterval: 10_000,
  });

  const { data: logs, refetch: refetchLogs } = useQuery<LogsResponse>({
    queryKey: ['system', 'logs', logLimit],
    queryFn: () => api<LogsResponse>(`/api/v1/system/logs?lines=${logLimit}`),
    refetchInterval: 15_000,
  });

  const { data: deploys } = useQuery<DeploysResponse>({
    queryKey: ['system', 'deploys'],
    queryFn: () => api<DeploysResponse>('/api/v1/system/deploys?limit=10'),
    refetchInterval: 30_000,
  });

  const filteredLogs = useMemo(() => {
    const all = logs?.lines ?? [];
    if (!filter) return all;
    const f = filter.toLowerCase();
    return all.filter((l) => l.msg.toLowerCase().includes(f));
  }, [logs, filter]);

  const lastDeploy = status?.deploy.last_deploy_ts
    ? new Date(status.deploy.last_deploy_ts * 1000).toLocaleString()
    : '—';

  return (
    <div className="space-y-6">
      {statusError && (
        <div className="flex items-center gap-2 rounded-md border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-400">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          {t('system.loadError')}
        </div>
      )}
      {/* Header */}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{t('system.title')}</h1>
          <p className="text-sm text-muted-foreground">{t('system.subtitle')}</p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            refetchStatus();
            refetchLogs();
          }}
          disabled={statusFetching}
        >
          <RefreshCw className={`mr-2 h-4 w-4 ${statusFetching ? 'animate-spin' : ''}`} />
          {t('system.refresh')}
        </Button>
      </div>

      {/* Top stats row */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <StatCard
          icon={<Server className="h-4 w-4 text-primary" />}
          label={t('system.backend')}
          value={status?.app.version ?? '—'}
          sub={status?.app.env ?? '—'}
        />
        <StatCard
          icon={<Timer className="h-4 w-4 text-emerald-400" />}
          label={t('system.uptime')}
          value={status ? formatUptime(status.runtime.uptime_s) : '—'}
          sub={`PID ${status?.runtime.pid ?? '—'}`}
        />
        <StatCard
          icon={<Cpu className="h-4 w-4 text-sky-400" />}
          label={t('system.host')}
          value={status?.runtime.hostname ?? '—'}
          sub={status?.runtime.platform ?? '—'}
        />
        <StatCard
          icon={<GitBranch className="h-4 w-4 text-violet-400" />}
          label={t('system.gitCommit')}
          value={status?.app.git_sha ?? '—'}
          sub={`Python ${status?.runtime.python ?? '—'}`}
        />
      </div>

      {/* Pi hardware control */}
      <PiHardwareControl />

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Services */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Activity className="h-4 w-4 text-primary" /> {t('system.services')}
            </CardTitle>
            <CardDescription>{t('system.servicesDesc')}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2.5 text-sm">
            {status &&
              Object.entries(status.services).map(([svc, state]) => (
                <div key={svc} className="flex items-center justify-between">
                  <span className="font-mono">{svc}</span>
                  <StatePill state={state} />
                </div>
              ))}
            {!status && <div className="text-muted-foreground">{t('system.loading')}</div>}
          </CardContent>
        </Card>

        {/* Network endpoints */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Globe className="h-4 w-4 text-primary" /> {t('system.endpoints')}
            </CardTitle>
            <CardDescription>{t('system.endpointsDesc')}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2.5 text-sm">
            {status &&
              Object.entries(status.endpoints).map(([name, url]) => {
                const full = url.startsWith('http') ? url : `https://example.org${url}`;
                return (
                  <a
                    key={name}
                    href={full}
                    target="_blank"
                    rel="noreferrer"
                    className="flex items-center justify-between hover:text-primary"
                  >
                    <span className="text-muted-foreground capitalize">{name.replace('_', ' ')}</span>
                    <span className="font-mono inline-flex items-center gap-1">
                      {url}
                      <ExternalLink className="h-3 w-3" />
                    </span>
                  </a>
                );
              })}
          </CardContent>
        </Card>

        {/* Auto-deploy */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Cloud className="h-4 w-4 text-primary" /> {t('system.autoDeploy')}
            </CardTitle>
            <CardDescription>
              {t('system.autoDeployDesc', { s: status?.deploy.poll_interval_s ?? 60 })}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-2.5 text-sm">
            <Row
              label={t('system.status')}
              value={
                <Badge variant={status?.deploy.auto_deploy ? 'success' : 'destructive'}>
                  {status?.deploy.auto_deploy ? t('system.enabled') : t('system.disabled')}
                </Badge>
              }
            />
            <Row label={t('system.source')} value={<code className="text-xs">{status?.deploy.source ?? '—'}</code>} />
            <Row label={t('system.branch')} value={<code className="text-xs">{status?.deploy.branch ?? '—'}</code>} />
            <Row label={t('system.lastDeploy')} value={lastDeploy} />
            <Separator />
            <div>
              <div className="text-xs text-muted-foreground mb-2">{t('system.recentDeploys')}</div>
              {deploys?.deploys.length ? (
                <ul className="space-y-1 font-mono text-xs">
                  {deploys.deploys.map((d, i) => (
                    <li key={i} className="flex items-center justify-between">
                      <span className="text-muted-foreground">{d.ts}</span>
                      <a
                        href={`https://github.com/MustafazadaAghasalim/industryprojectfinal/commit/${d.sha}`}
                        target="_blank"
                        rel="noreferrer"
                        className="hover:text-primary inline-flex items-center gap-1"
                      >
                        {d.sha}
                        <ExternalLink className="h-3 w-3" />
                      </a>
                    </li>
                  ))}
                </ul>
              ) : (
                <div className="text-xs text-muted-foreground">{t('system.noDeployYet')}</div>
              )}
            </div>
          </CardContent>
        </Card>

        {/* AI models */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <BrainCircuit className="h-4 w-4 text-violet-400" /> {t('system.aiModels')}
            </CardTitle>
            <CardDescription>{t('system.aiModelsDesc')}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            {status &&
              Object.entries(status.ai_models).map(([name, model]) => (
                <div key={name} className="rounded-md border bg-card/50 p-3">
                  <div className="flex items-center justify-between mb-1">
                    <span className="font-semibold capitalize">{name.replace(/_/g, ' ')}</span>
                    <Badge variant="outline" className="gap-1 text-xs">
                      <Sparkles className="h-3 w-3" /> {model.type}
                    </Badge>
                  </div>
                  <div className="text-xs text-muted-foreground mb-2">{model.purpose}</div>
                  {model.labels && (
                    <div className="flex flex-wrap gap-1">
                      {model.labels.map((l) => (
                        <Badge key={l} variant="secondary" className="text-xs">
                          {l}
                        </Badge>
                      ))}
                    </div>
                  )}
                  {model.metrics && (
                    <div className="space-y-1">
                      <div className="flex flex-wrap gap-1">
                        {model.metrics.map((m) => (
                          <Badge key={m} variant="secondary" className="text-xs font-mono">
                            {m}
                          </Badge>
                        ))}
                      </div>
                      {model.cages_warm !== undefined && (
                        <div className="text-xs text-muted-foreground">
                          {t('system.warmedCages', { n: model.cages_warm })}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              ))}
          </CardContent>
        </Card>

        {/* Security */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <ShieldCheck className="h-4 w-4 text-emerald-400" /> {t('system.security')}
            </CardTitle>
            <CardDescription>{t('system.securityDesc')}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2.5 text-sm">
            {status && (
              <>
                <Row label={t('system.https')} value={<Bool ok={status.security.https} t={t} />} />
                <Row label={t('system.hsts')} value={<Bool ok={status.security.hsts} t={t} />} />
                <Row label={t('system.csp')} value={<Bool ok={status.security.csp} t={t} />} />
                <Row label={t('system.jwtLifetime')} value={t('system.jwtMin', { n: status.security.jwt_expire_minutes })} />
                <Row
                  label={t('system.rateLimited')}
                  value={
                    <span className="font-mono text-xs">
                      {status.security.rate_limited_endpoints.join(', ')}
                    </span>
                  }
                />
                <Separator />
                <div className="text-xs text-muted-foreground">
                  {t('system.extraHeaders')}
                </div>
              </>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Deploy log */}
      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <CardTitle className="flex items-center gap-2 text-base">
                <ScrollText className="h-4 w-4 text-primary" /> {t('system.deployLog')}
              </CardTitle>
              <CardDescription>
                {logs?.path ? <code className="text-xs">{logs.path}</code> : t('system.deployLogUnavailable')}
              </CardDescription>
            </div>
            <div className="flex items-center gap-2">
              <input
                type="search"
                placeholder={t('system.filterPlaceholder')}
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                className="h-8 rounded-md border bg-background px-2 text-xs w-40"
              />
              <select
                className="h-8 rounded-md border bg-background px-2 text-xs"
                value={logLimit}
                onChange={(e) => setLogLimit(Number(e.target.value))}
                aria-label={t('system.logFilterLabel')}
              >
                <option value={40}>40</option>
                <option value={80}>80</option>
                <option value={200}>200</option>
                <option value={500}>500</option>
              </select>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {logs?.available ? (
            <pre className="text-xs font-mono bg-muted/40 rounded-md p-3 max-h-96 overflow-auto leading-relaxed">
              {filteredLogs.length === 0 ? (
                <span className="text-muted-foreground">{t('system.deployLogEmpty')}</span>
              ) : (
                filteredLogs.map((l, i) => (
                  <div key={i} className="flex gap-3">
                    {l.ts && <span className="text-muted-foreground shrink-0">{l.ts}</span>}
                    <span className={logLineColor(l.msg)}>{l.msg}</span>
                  </div>
                ))
              )}
            </pre>
          ) : (
            <div className="text-sm text-muted-foreground py-6 text-center">
              <FileTerminal className="h-6 w-6 mx-auto mb-2 opacity-60" />
              {t('system.deployLogUnavailable')}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function logLineColor(msg: string): string {
  if (/ERROR|fail/i.test(msg)) return 'text-rose-400';
  if (/WARN/i.test(msg)) return 'text-amber-400';
  if (/Deploy complete/i.test(msg)) return 'text-emerald-400';
  if (/Pulling/i.test(msg)) return 'text-sky-400';
  return '';
}

function StatCard({
  icon,
  label,
  value,
  sub,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  sub: string;
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-center justify-between mb-1">
          <span className="text-xs text-muted-foreground">{label}</span>
          {icon}
        </div>
        <div className="font-mono text-lg font-semibold truncate">{value}</div>
        <div className="text-xs text-muted-foreground mt-0.5 truncate">{sub}</div>
      </CardContent>
    </Card>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-muted-foreground">{label}</span>
      <span>{value}</span>
    </div>
  );
}

function Bool({ ok, t }: { ok: boolean; t: (k: string) => string }) {
  return ok ? (
    <Badge variant="success" className="gap-1">
      <CheckCircle2 className="h-3 w-3" /> {t('system.on')}
    </Badge>
  ) : (
    <Badge variant="destructive" className="gap-1">
      <XCircle className="h-3 w-3" /> {t('system.off')}
    </Badge>
  );
}
