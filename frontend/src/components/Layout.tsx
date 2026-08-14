import { Link, NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { Activity, BellRing, CalendarClock, Camera as CameraIcon, Droplets, FileText, Home, LogOut, Microscope, Server, Settings, Thermometer, Users } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Separator } from '@/components/ui/separator';
import { LanguageSwitcher } from '@/components/LanguageSwitcher';
import { ThemeToggle } from '@/components/ThemeToggle';
import { cn } from '@/lib/utils';
import { useAuth } from '@/store/auth';
import { useLabels } from '@/store/labels';
import { useWebSocket } from '@/lib/ws';
import { useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';

const NAV = [
  { to: '/', key: 'overview', icon: Home },
  { to: '/cameras', key: 'cameras', icon: CameraIcon },
  { to: '/alerts', key: 'alerts', icon: BellRing },
  { to: '/schedule', key: 'schedule', icon: CalendarClock },
  { to: '/reports', key: 'reports', icon: FileText },
  { to: '/system', key: 'system', icon: Server },
  { to: '/settings', key: 'settings', icon: Settings },
  { to: '/about', key: 'team', icon: Users },
];

export default function Layout() {
  const { t } = useTranslation();
  const { user, logout, ready } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const { setLabel } = useLabels();

  useEffect(() => {
    if (ready && !user && location.pathname !== '/login') navigate('/login', { replace: true });
  }, [user, ready, location.pathname, navigate]);

  const { connected } = useWebSocket(
    (msg) => {
      if (msg.type === 'alert') {
        toast.warning(`${msg.cage_id}: ${msg.message}`);
        queryClient.invalidateQueries({ queryKey: ['alerts'] });
      }
      if (msg.type === 'reading') {
        queryClient.invalidateQueries({ queryKey: ['cage', msg.cage_id], exact: false });
      }
      if (msg.type === 'label') {
        setLabel(msg.cage_id, { behaviour: msg.behaviour, confidence: msg.confidence, ts: msg.ts });
        queryClient.invalidateQueries({ queryKey: ['cage', msg.cage_id], exact: false });
      }
    },
    !!user,
  );

  if (!user) return <Outlet />;

  return (
    <div className="flex min-h-screen">
      <aside className="hidden md:flex md:w-60 lg:w-64 flex-col border-r bg-card/40 sticky top-0 h-screen self-start shrink-0 overflow-y-auto">
        <div className="flex items-center gap-3 px-6 py-5 shrink-0">
          <div className="grid h-9 w-9 place-items-center rounded-lg bg-primary/15">
            <Microscope className="h-5 w-5 text-primary" />
          </div>
          <div className="leading-tight">
            <div className="text-sm font-semibold">IVC Cage</div>
            <div className="text-xs text-muted-foreground">{t('nav.researcher')}</div>
          </div>
        </div>
        <Separator />
        <nav className="flex-1 space-y-1 px-3 py-4 overflow-y-auto">
          {NAV.map(({ to, key, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors',
                  isActive
                    ? 'bg-primary/10 text-primary'
                    : 'text-muted-foreground hover:bg-accent hover:text-foreground',
                )
              }
            >
              <Icon className="h-4 w-4" />
              {t(`nav.${key}`)}
            </NavLink>
          ))}
        </nav>
        <Separator />
        <div className="p-4 space-y-3 shrink-0">
          <div className="flex items-center gap-2 text-xs">
            <span
              className={cn(
                'h-2 w-2 rounded-full',
                connected ? 'bg-emerald-500 animate-pulse-dot' : 'bg-rose-500',
              )}
            />
            <span className="text-muted-foreground">{connected ? t('nav.live') : t('nav.reconnecting')}</span>
          </div>
          <div className="flex items-center justify-between gap-2">
            <LanguageSwitcher variant="compact" />
            <ThemeToggle variant="compact" />
          </div>
          <div className="text-xs text-muted-foreground">
            {t('nav.loggedInAs')}{' '}
            <span className="font-medium text-foreground">{user.username}</span> ({user.role})
          </div>
          <Button
            variant="outline"
            size="sm"
            className="w-full justify-start"
            onClick={() => {
              logout();
              navigate('/login');
            }}
          >
            <LogOut className="mr-2 h-4 w-4" />
            {t('nav.signOut')}
          </Button>
        </div>
      </aside>

      <main className="flex-1 min-w-0" role="main">
        <header className="flex h-14 items-center justify-between gap-3 border-b px-6">
          <Link to="/" className="flex items-center gap-2 md:hidden">
            <Microscope className="h-5 w-5 text-primary" />
            <span className="font-semibold">IVC Cage</span>
          </Link>
          <div className="hidden md:flex items-center gap-2 text-sm text-muted-foreground">
            <Activity className="h-4 w-4 text-primary" />
            {t('layout.productName')}
          </div>
          <div className="flex items-center gap-3">
            <LiveEnvBadge />
            <span className="text-xs text-muted-foreground hidden sm:inline">v1.0.0</span>
          </div>
        </header>
        <div className="p-6">
          <Outlet />
        </div>
      </main>
    </div>
  );
}

// ── Live DHT11 readout in the top header ────────────────────────────────────
// Polls the Pi camera-stream service every 5s. Falls back gracefully to "—"
// if the sensor is unreachable. The big chart card lives on Overview; this
// is the always-visible mini version.

type LiveEnv = {
  connected: boolean;
  temperature_c: number | null;
  humidity_pct: number | null;
  age_s: number | null;
};

const ENV_LIVE_URL = (import.meta.env.VITE_PI_STREAM_URL as string | undefined)
  ? `${import.meta.env.VITE_PI_STREAM_URL}/sensor/env`
  : null;

function tempColor(c: number | null): string {
  if (c === null) return 'text-muted-foreground';
  if (c < 18) return 'text-sky-400';
  if (c <= 26) return 'text-emerald-400';
  if (c <= 30) return 'text-amber-400';
  return 'text-rose-400';
}

function humColor(h: number | null): string {
  if (h === null) return 'text-muted-foreground';
  if (h < 30) return 'text-amber-400';
  if (h <= 70) return 'text-emerald-400';
  return 'text-sky-400';
}

function LiveEnvBadge() {
  const { t } = useTranslation();
  const { data, isError } = useQuery<LiveEnv>({
    queryKey: ['env-live-badge'],
    queryFn: async () => {
      if (!ENV_LIVE_URL) throw new Error('no live URL');
      const r = await fetch(ENV_LIVE_URL, { cache: 'no-store' });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    },
    refetchInterval: 5_000,
    retry: 1,
    enabled: !!ENV_LIVE_URL,
  });

  if (!ENV_LIVE_URL) return null;

  const fresh = !isError && data && data.connected
                && data.temperature_c !== null
                && (data.age_s ?? 99) < 60;
  const temp = fresh ? data!.temperature_c : null;
  const hum  = fresh ? data!.humidity_pct  : null;

  return (
    <div
      className="hidden sm:flex items-center gap-2 rounded-md border bg-card px-2.5 py-1 text-xs font-mono shadow-sm"
      title={fresh ? t('layout.dht11Title', { age: data!.age_s?.toFixed(0) }) : t('layout.dht11Unreachable')}
    >
      <span className="flex items-center gap-1">
        <span
          className={`h-1.5 w-1.5 rounded-full ${fresh ? 'bg-emerald-400 animate-pulse' : 'bg-rose-500'}`}
          aria-hidden
        />
        <Thermometer className={`h-3.5 w-3.5 ${tempColor(temp)}`} />
        <span className={`tabular-nums ${tempColor(temp)}`}>
          {temp !== null ? `${temp.toFixed(1)}°C` : '—'}
        </span>
      </span>
      <span className="text-muted-foreground/50">·</span>
      <span className="flex items-center gap-1">
        <Droplets className={`h-3.5 w-3.5 ${humColor(hum)}`} />
        <span className={`tabular-nums ${humColor(hum)}`}>
          {hum !== null ? `${hum.toFixed(0)}%` : '—'}
        </span>
      </span>
    </div>
  );
}
