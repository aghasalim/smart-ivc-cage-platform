import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import {
  Activity, WifiOff, Droplets, Wheat, Globe2, Clock,
  ArrowUp, ArrowDown, ArrowLeft, ArrowRight, Square, Mouse,
} from 'lucide-react';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Badge } from '@/components/ui/badge';
import { api, Cage, Schedule, ScheduleWindow } from '@/lib/api';
import {
  COMMON_TIMEZONES,
  DEFAULT_TIMEZONE,
  describeNextWindow,
  detectBrowserTimezone,
  nowInTimezone,
} from '@/lib/timezones';

const PI_STREAM_URL = import.meta.env.VITE_PI_STREAM_URL ?? 'http://192.168.50.2:8090';

// ── Types ──────────────────────────────────────────────────────────────────────

type ServoStatus = {
  connected: boolean;
  port: string | null;
  feed: number;
  water: number;
};

// ── Mouse (RC-toy) status / commands ───────────────────────────────────────────

type MouseStatus = {
  backend: string;
  // GPIO pins per direction. One pin each: Up=17, Down=27, Left=22, Right=23.
  pins: { forward: number[]; back: number[]; left: number[]; right: number[] };
  active: { forward: boolean; back: boolean; left: boolean; right: boolean };
  default_pulse_s: number;
  min_interval_s: number;
  max_pulse_s: number;
};

// Mouse traffic goes through the backend (example.org) so it works for users
// who aren't on the Pi's LAN. The backend proxies to the camera-stream
// service's /mouse/* endpoints; auth is the standard JWT / API-key flow.
async function fetchMouseStatus(): Promise<MouseStatus> {
  return api<MouseStatus>('/api/v1/mouse/status');
}

// Wrap every mouse POST in a 5-second AbortController timeout so a hung
// network request can never permanently freeze the UI.
function mousePost<T = unknown>(path: string, body: string): Promise<T> {
  const ctrl = new AbortController();
  const timer = globalThis.setTimeout(() => ctrl.abort(), 5_000);
  return api<T>(path, { method: 'POST', body, signal: ctrl.signal }).finally(
    () => globalThis.clearTimeout(timer),
  );
}

// Hold-to-drive: keeps the GPIO pin HIGH. Call immediately on pointer-down
// and refresh every 400 ms while held. The server auto-releases after 1.5 s
// if refreshes stop (e.g. browser disconnects).
async function holdDirection(direction: 'forward' | 'back' | 'left' | 'right'): Promise<void> {
  await mousePost(`/api/v1/mouse/hold/${direction}`, '{}');
}

async function releaseAll(): Promise<void> {
  await mousePost('/api/v1/mouse/stop', '{}');
}

// ── Helpers ────────────────────────────────────────────────────────────────────

async function fetchServoStatus(): Promise<ServoStatus> {
  const res = await fetch(`${PI_STREAM_URL}/servo/status`, { cache: 'no-store' });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function setServo(
  servo: 'feed' | 'water',
  angle: number,
  pulse?: number,
): Promise<ServoStatus> {
  const body: Record<string, number> = { angle };
  if (pulse !== undefined) body.pulse = pulse;
  const res = await fetch(`${PI_STREAM_URL}/servo/${servo}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// Water pump — closed-loop exact-volume dose (Arduino reads YF-S401 flow sensor
// and stops the pump + closes the siphon-prevention valve at the target mL).
async function dispensePump(volumeMl: number): Promise<{ ok: boolean }> {
  const res = await fetch(`${PI_STREAM_URL}/pump`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ dose: volumeMl }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function stopPump(): Promise<{ ok: boolean }> {
  const res = await fetch(`${PI_STREAM_URL}/pump`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pump: 'off' }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// ── Schedule page ──────────────────────────────────────────────────────────────

export default function SchedulePage() {
  const { t } = useTranslation();
  const { data: cages = [] } = useQuery({
    queryKey: ['cages'],
    queryFn: () => api<Cage[]>('/api/v1/cages'),
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{t('schedule.title')}</h1>
        <p className="text-sm text-muted-foreground">{t('schedule.subtitle')}</p>
      </div>

      {/* Cage-specific controls — hidden when no cages are provisioned */}
      {cages.length > 0 ? (
        <Tabs defaultValue={cages[0].id}>
          <TabsList className="flex flex-wrap h-auto">
            {cages.map((c) => (
              <TabsTrigger key={c.id} value={c.id}>{c.name}</TabsTrigger>
            ))}
          </TabsList>
          {cages.map((c) => (
            <TabsContent key={c.id} value={c.id} className="space-y-6">
              <ScheduleEditor cageId={c.id} />
              <ValveControl />
              <MouseControl />
            </TabsContent>
          ))}
        </Tabs>
      ) : (
        <p className="text-sm text-muted-foreground">{t('reports.noCages')}</p>
      )}

    </div>
  );
}

// ── Schedule editor ────────────────────────────────────────────────────────────

function ScheduleEditor({ cageId }: { cageId: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: ['schedule', cageId],
    queryFn: () => api<Schedule>(`/api/v1/cages/${cageId}/schedule`),
  });
  const [w, setW] = useState<ScheduleWindow | null>(null);
  // `mode` controls how the timezone field is populated: 'auto' follows the
  // browser's resolved zone live, 'manual' uses the user's explicit pick.
  const browserTz = useMemo(() => detectBrowserTimezone(), []);
  const [mode, setMode] = useState<'auto' | 'manual'>('auto');

  const baseWindow: ScheduleWindow = w ?? data?.windows?.[0] ?? {
    start_local: '23:00',
    end_local: '00:30',
    days: [0, 1, 2, 3, 4, 5, 6],
    active: true,
    timezone: browserTz || DEFAULT_TIMEZONE,
  };
  const window = baseWindow;

  // Re-tick the live clock once a second so the "now in X" preview stays fresh.
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = globalThis.setInterval(() => setTick((n) => n + 1), 1000);
    return () => globalThis.clearInterval(id);
  }, []);

  const mutate = useMutation({
    mutationFn: (windows: ScheduleWindow[]) =>
      api(`/api/v1/cages/${cageId}/schedule`, { method: 'PUT', body: JSON.stringify(windows) }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['schedule', cageId] });
      toast.success(t('schedule.savedToast'));
    },
    onError: (err: any) => toast.error(err?.message ?? t('schedule.saveFailed')),
  });

  function toggleDay(d: number) {
    const days = window.days.includes(d) ? window.days.filter((x) => x !== d) : [...window.days, d].sort();
    setW({ ...window, days });
  }

  // Build the picker options. Always pin auto + Belgium at top, then add any
  // resolved-zone-not-in-list, then the curated rest.
  const tzOptions = useMemo(() => {
    const seen = new Set<string>();
    const out: { value: string; label: string }[] = [];
    out.push({ value: '__auto__', label: `${t('schedule.tz.auto')} (${browserTz})` });
    const ensure = (tz: string, label?: string) => {
      if (seen.has(tz)) return;
      seen.add(tz);
      out.push({ value: tz, label: label ?? tz });
    };
    ensure(DEFAULT_TIMEZONE, t('schedule.tz.belgium'));
    if (browserTz && browserTz !== DEFAULT_TIMEZONE) ensure(browserTz, `${browserTz} · ${t('schedule.tz.yourBrowser')}`);
    for (const entry of COMMON_TIMEZONES) ensure(entry.tz, entry.label);
    return out;
  }, [t, browserTz]);

  const effectiveTz = mode === 'auto' ? browserTz || DEFAULT_TIMEZONE : window.timezone;
  const nowStr = nowInTimezone(effectiveTz);
  const preview = describeNextWindow(window.start_local, window.end_local, window.days, window.active, effectiveTz);

  function onTimezoneChange(value: string) {
    if (value === '__auto__') {
      setMode('auto');
      setW({ ...window, timezone: browserTz || DEFAULT_TIMEZONE });
    } else {
      setMode('manual');
      setW({ ...window, timezone: value });
    }
  }

  function handleSave() {
    mutate.mutate([{ ...window, timezone: effectiveTz }]);
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t('schedule.windowTitle')}</CardTitle>
        <CardDescription>{t('schedule.windowDesc')}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6 max-w-xl">
        {/* Timezone picker */}
        <div className="space-y-2">
          <Label className="flex items-center gap-2">
            <Globe2 className="h-3.5 w-3.5" /> {t('schedule.tz.title')}
          </Label>
          <select
            value={mode === 'auto' ? '__auto__' : window.timezone}
            onChange={(e) => onTimezoneChange(e.target.value)}
            className="h-9 w-full rounded-md border bg-background px-3 text-sm"
          >
            {tzOptions.map((opt) => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Clock className="h-3 w-3" />
            <span>
              {t('schedule.tz.currentlyInZone', { zone: effectiveTz })}: <span className="font-mono">{nowStr}</span>
            </span>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div className="space-y-2">
            <Label>{t('schedule.startLocal')}</Label>
            <Input
              type="time"
              value={window.start_local}
              onChange={(e) => setW({ ...window, start_local: e.target.value })}
            />
          </div>
          <div className="space-y-2">
            <Label>{t('schedule.endLocal')}</Label>
            <Input
              type="time"
              value={window.end_local}
              onChange={(e) => setW({ ...window, end_local: e.target.value })}
            />
          </div>
        </div>

        <div className="space-y-2">
          <Label>{t('schedule.days')}</Label>
          <div className="flex gap-1.5 flex-wrap">
            {[0, 1, 2, 3, 4, 5, 6].map((i) => {
              const on = window.days.includes(i);
              return (
                <button
                  type="button"
                  key={i}
                  onClick={() => toggleDay(i)}
                  className={
                    'h-9 px-3 rounded-md border text-sm transition-colors ' +
                    (on ? 'bg-primary text-primary-foreground border-primary' : 'bg-background hover:bg-accent')
                  }
                >
                  {t(`schedule.dayShort.${i}`)}
                </button>
              );
            })}
          </div>
        </div>

        <div className="flex items-center gap-3">
          <Switch checked={window.active} onCheckedChange={(checked) => setW({ ...window, active: checked })} />
          <Label>{t('schedule.active')}</Label>
        </div>

        {/* Live preview of the resolved feeding window */}
        {preview && (
          <div
            className={
              'rounded-md border px-3 py-2 text-sm ' +
              (preview.phase === 'open'
                ? 'border-emerald-500/30 bg-emerald-500/5 text-emerald-700 dark:text-emerald-300'
                : preview.phase === 'closed'
                  ? 'border-sky-500/30 bg-sky-500/5 text-sky-700 dark:text-sky-300'
                  : 'border-muted bg-muted/30 text-muted-foreground')
            }
          >
            <div className="font-medium">
              {preview.phase === 'open' && t('schedule.preview.openNow')}
              {preview.phase === 'closed' && t('schedule.preview.closedNow')}
              {preview.phase === 'inactive' && t('schedule.preview.inactive')}
            </div>
            {preview.phase !== 'inactive' && (
              <div className="text-xs opacity-80 mt-0.5">{preview.label}</div>
            )}
          </div>
        )}

        <Button onClick={handleSave} disabled={mutate.isPending}>
          {mutate.isPending ? t('common.loading') : t('schedule.saveSchedule')}
        </Button>
      </CardContent>
    </Card>
  );
}

// ── Valve control ──────────────────────────────────────────────────────────────

const OPEN_ANGLE  = 90;
const CLOSE_ANGLE = 0;

function ValveControl() {
  const { t } = useTranslation();

  // Poll servo status every 5 s
  const { data: status, isError } = useQuery<ServoStatus>({
    queryKey: ['servo-status'],
    queryFn: fetchServoStatus,
    refetchInterval: 5_000,
    retry: 1,
  });

  const queryClient = useQueryClient();
  const invalidate = useCallback(
    () => queryClient.invalidateQueries({ queryKey: ['servo-status'] }),
    [queryClient],
  );

  const arduinoOnline = status?.connected ?? false;
  const hasStatus = status !== undefined && !isError;

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2">
              {t('schedule.valves.title')}
            </CardTitle>
            <CardDescription>{t('schedule.valves.desc')}</CardDescription>
          </div>
          {hasStatus && (
            <Badge variant={arduinoOnline ? 'default' : 'destructive'} className="gap-1.5 shrink-0">
              {arduinoOnline ? (
                <><Activity className="h-3 w-3" /> {t('schedule.valves.arduinoOnline')}</>
              ) : (
                <><WifiOff className="h-3 w-3" /> {t('schedule.valves.arduinoOffline')}</>
              )}
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Pi unreachable */}
        {isError && (
          <p className="text-sm text-muted-foreground border rounded-md p-3 bg-muted/30">
            {t('schedule.valves.noArduino')}
          </p>
        )}

        {/* Arduino offline warning (Pi up but Mega not detected) */}
        {!isError && !arduinoOnline && (
          <p className="text-sm text-amber-600 dark:text-amber-400 border border-amber-500/30 rounded-md p-3 bg-amber-500/5">
            {t('schedule.valves.noArduino')}
          </p>
        )}

        <div className="grid gap-4 sm:grid-cols-2">
          <ValveCard
            servo="feed"
            icon={<Wheat className="h-4 w-4 text-amber-500" />}
            label={t('schedule.valves.feed')}
            angle={status?.feed ?? CLOSE_ANGLE}
            disabled={!arduinoOnline}
            onUpdate={invalidate}
          />
          <WaterPumpCard
            icon={<Droplets className="h-4 w-4 text-blue-500" />}
            label={t('schedule.pump.title')}
            disabled={!arduinoOnline}
          />
        </div>
      </CardContent>
    </Card>
  );
}

// ── Single valve card ──────────────────────────────────────────────────────────

function ValveCard({
  servo,
  icon,
  label,
  angle,
  disabled,
  onUpdate,
}: {
  servo: 'feed';
  icon: React.ReactNode;
  label: string;
  angle: number;
  disabled: boolean;
  onUpdate: () => void;
}) {
  const { t } = useTranslation();
  // "Open" = any position off the closed stop, so custom open angles below
  // OPEN_ANGLE (e.g. 80°) still register as open in the badge/buttons.
  const isOpen = angle > CLOSE_ANGLE;

  const [openAngle, setOpenAngle]       = useState(OPEN_ANGLE);
  const [pulseDuration, setPulseDuration] = useState(3);
  const [busy, setBusy]                 = useState<'opening' | 'closing' | 'pulsing' | null>(null);

  const send = async (action: 'open' | 'close' | 'pulse') => {
    if (busy) return;
    setBusy(action === 'close' ? 'closing' : 'opening');
    try {
      if (action === 'pulse') {
        await setServo(servo, openAngle, pulseDuration);
      } else {
        await setServo(servo, action === 'open' ? openAngle : CLOSE_ANGLE);
      }
      onUpdate();
    } catch {
      toast.error(t('schedule.valves.commandFailed', { label }));
    } finally {
      setBusy(null);
    }
  };

  const statusLabel = isOpen ? t('schedule.valves.statusOpen') : t('schedule.valves.statusClosed');

  return (
    <div className="border rounded-lg p-4 space-y-4 bg-card">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 font-medium text-sm">
          {icon}
          {label}
        </div>
        <Badge variant={isOpen ? 'default' : 'secondary'} className="text-xs">
          {statusLabel}
        </Badge>
      </div>

      {/* Open angle */}
      <div className="space-y-1">
        <Label className="text-xs text-muted-foreground">{t('schedule.valves.openAngle')}</Label>
        <Input
          type="number"
          min={1}
          max={180}
          value={openAngle}
          onChange={(e) => setOpenAngle(Number(e.target.value))}
          className="h-8 text-sm"
          disabled={disabled}
        />
      </div>

      {/* Open / Close */}
      <div className="grid grid-cols-2 gap-2">
        <Button
          size="sm"
          variant={isOpen ? 'default' : 'outline'}
          disabled={disabled || busy !== null}
          onClick={() => send('open')}
        >
          {busy === 'opening' ? t('schedule.valves.opening') : t('schedule.valves.open')}
        </Button>
        <Button
          size="sm"
          variant={!isOpen ? 'default' : 'outline'}
          disabled={disabled || busy !== null}
          onClick={() => send('close')}
        >
          {busy === 'closing' ? t('schedule.valves.closing') : t('schedule.valves.close')}
        </Button>
      </div>

      {/* Pulse / timed open */}
      <div className="space-y-2 pt-1 border-t">
        <Label className="text-xs text-muted-foreground">{t('schedule.valves.pulseDuration')}</Label>
        <div className="flex items-center gap-2">
          <Input
            type="number"
            min={1}
            max={60}
            value={pulseDuration}
            onChange={(e) => setPulseDuration(Number(e.target.value))}
            className="h-8 text-sm w-20"
            disabled={disabled}
          />
          <span className="text-xs text-muted-foreground">{t('schedule.valves.pulseSec')}</span>
          <Button
            size="sm"
            variant="outline"
            className="ml-auto"
            disabled={disabled || busy !== null}
            onClick={() => send('pulse')}
          >
            {busy === 'pulsing' ? t('schedule.valves.opening') : t('schedule.valves.pulse')}
          </Button>
        </div>
      </div>
    </div>
  );
}

// ── Water pump card (replaces the legacy water-valve servo control) ───────────
// The water side is a real pump + flow sensor (closed-loop dose by mL), not a
// servo. The Arduino reads the YF-S401 flow sensor and stops the pump at the
// target volume; the solenoid valve closes on stop to defeat siphon overshoot.

function WaterPumpCard({
  icon,
  label,
  disabled,
}: {
  icon: React.ReactNode;
  label: string;
  disabled: boolean;
}) {
  const { t } = useTranslation();
  const [volumeMl, setVolumeMl] = useState(15);
  const [busy, setBusy] = useState<'dosing' | 'stopping' | null>(null);

  const dose = async (ml: number) => {
    if (busy) return;
    setBusy('dosing');
    try {
      await dispensePump(ml);
      toast.success(t('schedule.pump.dosing', { ml }));
    } catch {
      toast.error(t('schedule.pump.commandFailed'));
    } finally {
      setBusy(null);
    }
  };

  const stop = async () => {
    if (busy) return;
    setBusy('stopping');
    try {
      await stopPump();
      toast.success(t('schedule.pump.stopped'));
    } catch {
      toast.error(t('schedule.pump.commandFailed'));
    } finally {
      setBusy(null);
    }
  };

  const presets = [5, 10, 15, 25, 50];

  return (
    <div className="border rounded-lg p-4 space-y-4 bg-card">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 font-medium text-sm">
          {icon}
          {label}
        </div>
        <Badge variant="secondary" className="text-xs">
          {t('schedule.pump.closedLoop')}
        </Badge>
      </div>

      {/* Volume input */}
      <div className="space-y-1">
        <Label className="text-xs text-muted-foreground">{t('schedule.pump.volumeMl')}</Label>
        <div className="flex items-center gap-2">
          <Input
            type="number"
            min={1}
            max={1000}
            value={volumeMl}
            onChange={(e) => setVolumeMl(Number(e.target.value))}
            className="h-8 text-sm"
            disabled={disabled}
          />
          <span className="text-xs text-muted-foreground">mL</span>
        </div>
      </div>

      {/* Quick presets */}
      <div className="flex flex-wrap gap-1">
        {presets.map((ml) => (
          <Button
            key={ml}
            type="button"
            size="sm"
            variant={volumeMl === ml ? 'default' : 'outline'}
            className="h-7 px-2 text-xs"
            disabled={disabled || busy !== null}
            onClick={() => setVolumeMl(ml)}
            aria-label={`${ml} mL`}
          >
            {ml}
          </Button>
        ))}
      </div>

      {/* Dispense + Stop */}
      <div className="grid grid-cols-2 gap-2">
        <Button
          size="sm"
          disabled={disabled || busy !== null || volumeMl < 1}
          onClick={() => dose(volumeMl)}
        >
          {busy === 'dosing' ? t('schedule.pump.dispensing') : t('schedule.pump.dispense')}
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={disabled || busy !== null}
          onClick={stop}
          aria-label={t('schedule.pump.stop')}
        >
          {busy === 'stopping' ? t('schedule.pump.stopping') : t('schedule.pump.stop')}
        </Button>
      </div>

      <p className="text-xs text-muted-foreground leading-snug pt-1 border-t">
        {t('schedule.pump.hint')}
      </p>
    </div>
  );
}

// ── RC-toy mouse control ──────────────────────────────────────────────────────

function MouseControl() {
  const { t } = useTranslation();

  const { data: status, isError } = useQuery<MouseStatus>({
    queryKey: ['mouse-status'],
    queryFn: fetchMouseStatus,
    refetchInterval: 500,
    retry: 1,
  });

  const reachable  = status !== undefined && !isError;
  const dryrun     = status?.backend === 'dryrun';
  const active     = status?.active ?? { forward: false, back: false, left: false, right: false };
  const anyActive  = active.forward || active.back || active.left || active.right;
  const activeName = (['forward', 'back', 'left', 'right'] as const).find((k) => active[k]);

  const [held, setHeld] = useState<'forward' | 'back' | 'left' | 'right' | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const startHold = useCallback(
    (dir: 'forward' | 'back' | 'left' | 'right') => {
      if (!reachable) return;
      setHeld(dir);
      holdDirection(dir).catch(() => {});
      intervalRef.current = setInterval(() => {
        holdDirection(dir).catch(() => {});
      }, 400);
    },
    [reachable],
  );

  const endHold = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
    setHeld(null);
    releaseAll().catch(() => {});
  }, []);

  // On mount: blanket GPIO release so we start from a clean LOW state.
  // (Random-walk was ripped out server-side, so /stop is now the only
  // legitimate way to ensure all pins are LOW.)
  useEffect(() => {
    releaseAll().catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Release on unmount (e.g. navigate away while holding)
  useEffect(
    () => () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
      releaseAll().catch(() => {});
    },
    [],
  );

  const holdProps = (dir: 'forward' | 'back' | 'left' | 'right') => ({
    onPointerDown: (e: React.PointerEvent) => {
      e.currentTarget.setPointerCapture(e.pointerId);
      startHold(dir);
    },
    onPointerUp:     () => endHold(),
    onPointerLeave:  () => { if (held === dir) endHold(); },
    onPointerCancel: () => endHold(),
    onContextMenu:   (e: React.MouseEvent) => e.preventDefault(),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2">
              <Mouse className="h-4 w-4" />
              {t('schedule.mouse.title')}
            </CardTitle>
            <CardDescription>{t('schedule.mouse.desc')}</CardDescription>
          </div>
          <div className="flex flex-col items-end gap-1 shrink-0">
            {reachable ? (
              <Badge variant={dryrun ? 'warning' : 'default'} className="gap-1.5">
                <Activity className="h-3 w-3" />
                {dryrun ? t('schedule.mouse.dryrun') : t('schedule.mouse.online', { backend: status!.backend })}
              </Badge>
            ) : (
              <Badge variant="destructive" className="gap-1.5">
                <WifiOff className="h-3 w-3" />
                {t('schedule.mouse.offline')}
              </Badge>
            )}
            {reachable && (
              <Badge
                variant={anyActive ? 'default' : 'secondary'}
                className={
                  'gap-1.5 text-[10px] transition-colors ' +
                  (anyActive
                    ? 'bg-emerald-500/20 text-emerald-700 dark:text-emerald-300 border-emerald-500/40'
                    : 'bg-muted text-muted-foreground')
                }
              >
                <span
                  className={
                    'inline-block h-1.5 w-1.5 rounded-full ' +
                    (anyActive ? 'bg-emerald-400 animate-pulse' : 'bg-muted-foreground/40')
                  }
                />
                {anyActive
                  ? t('schedule.mouse.activeNow', { name: t(`schedule.mouse.${activeName}`) })
                  : t('schedule.mouse.idle')}
              </Badge>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* ── Offline / dryrun notices ─────────────────────────────────────── */}
        {!reachable && (
          <p className="text-sm text-muted-foreground border rounded-md p-3 bg-muted/30">
            {t('schedule.mouse.serviceDown')}
          </p>
        )}
        {dryrun && (
          <p className="text-sm text-amber-700 dark:text-amber-300 border border-amber-500/30 rounded-md p-3 bg-amber-500/5">
            {t('schedule.mouse.dryrunNote')}
          </p>
        )}

        {/* ── D-pad (hold-to-drive) ────────────────────────────────────────── */}
        <div className="grid grid-cols-3 gap-2 max-w-xs mx-auto">
          <div />
          <HoldButton
            label={t('schedule.mouse.forward')}
            icon={<ArrowUp className="h-5 w-5" />}
            active={active.forward}
            held={held === 'forward'}
            disabled={!reachable}
            {...holdProps('forward')}
          />
          <div />

          <HoldButton
            label={t('schedule.mouse.left')}
            icon={<ArrowLeft className="h-5 w-5" />}
            active={active.left}
            held={held === 'left'}
            disabled={!reachable}
            {...holdProps('left')}
          />
          {/* Centre stop button — releases on pointer-down for instant stop */}
          <button
            type="button"
            disabled={!reachable}
            aria-label={t('schedule.mouse.stop')}
            onPointerDown={() => endHold()}
            className={[
              'h-16 rounded-xl border-2 flex flex-col items-center justify-center gap-1',
              'transition-all touch-none select-none',
              !reachable
                ? 'opacity-40 cursor-not-allowed border-border bg-muted/30'
                : 'border-border bg-card hover:bg-accent cursor-pointer active:scale-95',
            ].join(' ')}
          >
            <Square className="h-5 w-5" />
            <span className="text-[10px] font-medium">{t('schedule.mouse.stop')}</span>
          </button>
          <HoldButton
            label={t('schedule.mouse.right')}
            icon={<ArrowRight className="h-5 w-5" />}
            active={active.right}
            held={held === 'right'}
            disabled={!reachable}
            {...holdProps('right')}
          />

          <div />
          <HoldButton
            label={t('schedule.mouse.back')}
            icon={<ArrowDown className="h-5 w-5" />}
            active={active.back}
            held={held === 'back'}
            disabled={!reachable}
            {...holdProps('back')}
          />
          <div />
        </div>

        {/* ── Pin map ──────────────────────────────────────────────────────── */}
        {status && (
          <div className="text-[10px] font-mono text-muted-foreground/60 text-center border-t pt-2">
            GPIO · Up:{status.pins.forward.join('+')} Down:{status.pins.back.join('+')}
            {' '}Left:{status.pins.left.join('+')} Right:{status.pins.right.join('+')}
            {' · '}cooldown {status.min_interval_s.toFixed(2)}s
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// Hold-button: lights up while GPIO is HIGH on the Pi (via polled status) and
// also while the local pointer is held down. Pointer-capture keeps the hold
// alive even when the finger drifts outside the button boundary.
function HoldButton({
  label,
  icon,
  active,
  held,
  disabled,
  onPointerDown,
  onPointerUp,
  onPointerLeave,
  onPointerCancel,
  onContextMenu,
}: {
  label: string;
  icon: React.ReactNode;
  active: boolean;
  held: boolean;
  disabled: boolean;
  onPointerDown: (e: React.PointerEvent) => void;
  onPointerUp: () => void;
  onPointerLeave: () => void;
  onPointerCancel: () => void;
  onContextMenu: (e: React.MouseEvent) => void;
}) {
  const lit = held || active;
  return (
    <button
      type="button"
      disabled={disabled}
      aria-label={label}
      onPointerDown={onPointerDown}
      onPointerUp={onPointerUp}
      onPointerLeave={onPointerLeave}
      onPointerCancel={onPointerCancel}
      onContextMenu={onContextMenu}
      className={[
        'h-16 rounded-xl border-2 flex flex-col items-center justify-center gap-1',
        'transition-all active:scale-95 touch-none select-none',
        disabled
          ? 'opacity-40 cursor-not-allowed border-border bg-muted/30'
          : lit
            ? 'border-emerald-500 bg-emerald-500/20 text-emerald-700 dark:text-emerald-300 shadow-[0_0_14px_-2px_rgba(16,185,129,0.5)] scale-[1.04] cursor-pointer'
            : 'border-border bg-card hover:bg-accent cursor-pointer',
      ].join(' ')}
    >
      {icon}
      <span className="text-[10px] font-medium">{label}</span>
    </button>
  );
}
