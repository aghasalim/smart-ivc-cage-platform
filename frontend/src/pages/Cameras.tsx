import { Component, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode, ErrorInfo } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Camera as CameraIcon,
  Download,
  Expand,
  RefreshCcw,
  WifiOff,
  Activity,
  X,
  Bot,
  ScanSearch,
  Zap,
  MousePointer2,
  Settings,
  ZoomIn,
} from 'lucide-react';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Separator } from '@/components/ui/separator';
import { api, API_URL, getToken } from '@/lib/api';
import { toast } from 'sonner';

const PI_STREAM_URL = import.meta.env.VITE_PI_STREAM_URL ?? 'http://192.168.50.2:8090';

type Camera = {
  id: string;
  name: string;
  device: string;
  bus: string;
  width: number;
  height: number;
  fps: number;
};

type CamerasResponse = { cameras: Camera[] };

async function fetchCameras(): Promise<Camera[]> {
  const res = await fetch(`${PI_STREAM_URL}/api/cameras`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const json: CamerasResponse = await res.json();
  return json.cameras;
}

// Friendlier display name — collapses things like "GENERAL WEBCAM: GENERAL WEBCAM"
function prettyName(name: string): string {
  const parts = name.split(':').map((s) => s.trim());
  const unique = Array.from(new Set(parts.filter(Boolean)));
  return unique.join(' · ');
}

function busLabel(bus: string): string {
  const m = bus.match(/usb-(?:[a-z-]+\.)?([\dA-Za-z.-]+)$/);
  return m ? `USB ${m[1]}` : bus;
}

// ── Camera Controls types ─────────────────────────────────────────────────────

type ControlMenuItem = { value: number; label: string };

type CameraControl = {
  name: string;
  type: 'int' | 'bool' | 'menu';
  min?: number;
  max?: number;
  step?: number;
  default?: number;
  value: number;
  menu?: ControlMenuItem[];
};

type ControlsResponse = {
  device: string;
  controls: CameraControl[];
};

// Groups of control names for the settings panel
const CONTROL_GROUPS: { key: string; names: string[] }[] = [
  {
    key: 'image',
    names: ['brightness', 'contrast', 'saturation', 'hue', 'gamma', 'sharpness'],
  },
  {
    key: 'exposure',
    names: [
      'auto_exposure',
      'exposure_time_absolute',
      'gain',
      'exposure_dynamic_framerate',
      'backlight_compensation',
    ],
  },
  {
    key: 'colour',
    names: ['white_balance_automatic', 'white_balance_temperature'],
  },
  {
    key: 'focus',
    names: ['focus_automatic_continuous', 'focus_absolute'],
  },
  {
    key: 'other',
    names: ['power_line_frequency'],
  },
];

async function fetchControls(camId: string): Promise<ControlsResponse> {
  const res = await fetch(`${PI_STREAM_URL}/camera/${camId}/controls`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<ControlsResponse>;
}

async function postControl(
  camId: string,
  name: string,
  value: number,
): Promise<{ ok: boolean; name: string; value: number }> {
  const res = await fetch(`${PI_STREAM_URL}/camera/${camId}/controls`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, value }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<{ ok: boolean; name: string; value: number }>;
}

async function postReset(camId: string): Promise<ControlsResponse> {
  const res = await fetch(`${PI_STREAM_URL}/camera/${camId}/controls/reset`, {
    method: 'POST',
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<ControlsResponse>;
}

// ── Digital zoom state per camera ────────────────────────────────────────────

type ZoomState = {
  scale: number;
  panX: number;
  panY: number;
};

const DEFAULT_ZOOM: ZoomState = { scale: 1, panX: 0, panY: 0 };

// ── Settings dialog ───────────────────────────────────────────────────────────

function CameraSettingsDialog({
  cam,
  open,
  onClose,
  zoom,
  setZoom,
}: {
  cam: Camera;
  open: boolean;
  onClose: () => void;
  zoom: ZoomState;
  setZoom: (z: ZoomState) => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const { data, isLoading, isError } = useQuery<ControlsResponse>({
    queryKey: ['camera-controls', cam.id],
    queryFn: () => fetchControls(cam.id),
    enabled: open,
    staleTime: 30_000,
    retry: 1,
  });

  const mutation = useMutation({
    mutationFn: ({ name, value }: { name: string; value: number }) =>
      postControl(cam.id, name, value),
    onError: (_err, variables) => {
      toast.error(
        `${t('cameras.controls.setFailed', { name: variables.name })}`,
      );
      // Refetch to restore server truth
      void queryClient.invalidateQueries({ queryKey: ['camera-controls', cam.id] });
    },
  });

  const resetMutation = useMutation({
    mutationFn: () => postReset(cam.id),
    onSuccess: (fresh) => {
      queryClient.setQueryData(['camera-controls', cam.id], fresh);
      toast.success(t('cameras.controls.resetDone'));
    },
    onError: () => {
      toast.error(t('cameras.controls.resetFailed'));
      void queryClient.invalidateQueries({ queryKey: ['camera-controls', cam.id] });
    },
  });

  // Debounce timer ref for range inputs
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Optimistic local control values
  const [localValues, setLocalValues] = useState<Record<string, number>>({});

  useEffect(() => {
    if (data) {
      const vals: Record<string, number> = {};
      data.controls.forEach((c) => (vals[c.name] = c.value));
      setLocalValues(vals);
    }
  }, [data]);

  const handleChange = useCallback(
    (name: string, value: number) => {
      setLocalValues((prev) => ({ ...prev, [name]: value }));
      if (debounceRef.current) clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(() => {
        mutation.mutate({ name, value });
      }, 300);
    },
    [mutation],
  );

  // Friendly label for a raw control name
  const controlLabel = (name: string): string => {
    const key = `cameras.controls.names.${name}`;
    const translated = t(key);
    // i18next returns the key itself when missing — fall back to raw name
    return translated === key ? name : translated;
  };

  // Organise controls into groups, plus an "other" bucket for unknowns
  const grouped = useMemo(() => {
    if (!data) return [];
    const byName = new Map(data.controls.map((c) => [c.name, c]));
    const assigned = new Set<string>();
    const result: { key: string; controls: CameraControl[] }[] = [];

    CONTROL_GROUPS.forEach(({ key, names }) => {
      const matched = names.map((n) => byName.get(n)).filter(Boolean) as CameraControl[];
      matched.forEach((c) => assigned.add(c.name));
      if (matched.length > 0) result.push({ key, controls: matched });
    });

    // Remaining controls not in any group
    const rest = data.controls.filter((c) => !assigned.has(c.name));
    if (rest.length > 0) result.push({ key: 'other', controls: rest });

    return result;
  }, [data]);

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-lg max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Settings className="h-4 w-4" />
            {t('cameras.controls.title', { name: prettyName(cam.name) })}
          </DialogTitle>
        </DialogHeader>

        {/* ── Digital zoom section ────────────────────────────────── */}
        <div className="space-y-3">
          <div className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
            <ZoomIn className="h-3.5 w-3.5" />
            {t('cameras.controls.groups.zoom')}
          </div>

          {/* Zoom scale */}
          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <Label className="text-xs">{t('cameras.controls.zoom.scale')}</Label>
              <span className="text-xs font-mono text-muted-foreground">
                {zoom.scale.toFixed(1)}x
              </span>
            </div>
            <input
              type="range"
              min={1}
              max={4}
              step={0.1}
              value={zoom.scale}
              onChange={(e) =>
                setZoom({ ...zoom, scale: parseFloat(e.target.value) })
              }
              className="w-full accent-primary h-1.5 rounded cursor-pointer"
            />
          </div>

          {/* Pan X */}
          {zoom.scale > 1 && (
            <>
              <div className="space-y-1.5">
                <div className="flex items-center justify-between">
                  <Label className="text-xs">{t('cameras.controls.zoom.panX')}</Label>
                  <span className="text-xs font-mono text-muted-foreground">
                    {Math.round(zoom.panX * 100)}%
                  </span>
                </div>
                <input
                  type="range"
                  min={-1}
                  max={1}
                  step={0.01}
                  value={zoom.panX}
                  onChange={(e) =>
                    setZoom({ ...zoom, panX: parseFloat(e.target.value) })
                  }
                  className="w-full accent-primary h-1.5 rounded cursor-pointer"
                />
              </div>

              <div className="space-y-1.5">
                <div className="flex items-center justify-between">
                  <Label className="text-xs">{t('cameras.controls.zoom.panY')}</Label>
                  <span className="text-xs font-mono text-muted-foreground">
                    {Math.round(zoom.panY * 100)}%
                  </span>
                </div>
                <input
                  type="range"
                  min={-1}
                  max={1}
                  step={0.01}
                  value={zoom.panY}
                  onChange={(e) =>
                    setZoom({ ...zoom, panY: parseFloat(e.target.value) })
                  }
                  className="w-full accent-primary h-1.5 rounded cursor-pointer"
                />
              </div>
            </>
          )}

          <Button
            variant="outline"
            size="sm"
            onClick={() => setZoom(DEFAULT_ZOOM)}
            disabled={zoom.scale === 1 && zoom.panX === 0 && zoom.panY === 0}
          >
            {t('cameras.controls.zoom.reset')}
          </Button>
        </div>

        <Separator />

        {/* ── Hardware controls ───────────────────────────────────── */}
        {isLoading && (
          <div className="text-sm text-muted-foreground py-4 text-center">
            {t('common.loading')}
          </div>
        )}
        {isError && (
          <div className="text-sm text-rose-400 py-2">
            {t('cameras.controls.loadError')}
          </div>
        )}

        {data && grouped.map(({ key, controls }, gi) => (
          <div key={key} className="space-y-3">
            {gi > 0 && <Separator />}
            <div className="text-sm font-medium text-muted-foreground">
              {t(`cameras.controls.groups.${key}`)}
            </div>

            {controls.map((ctrl) => {
              const lv = localValues[ctrl.name] ?? ctrl.value;

              if (ctrl.type === 'bool') {
                return (
                  <div key={ctrl.name} className="flex items-center justify-between gap-3">
                    <Label className="text-sm">{controlLabel(ctrl.name)}</Label>
                    <Switch
                      checked={lv !== 0}
                      onCheckedChange={(checked) =>
                        handleChange(ctrl.name, checked ? 1 : 0)
                      }
                    />
                  </div>
                );
              }

              if (ctrl.type === 'menu' && ctrl.menu) {
                return (
                  <div key={ctrl.name} className="space-y-1.5">
                    <Label className="text-xs">{controlLabel(ctrl.name)}</Label>
                    <select
                      value={lv}
                      onChange={(e) =>
                        handleChange(ctrl.name, parseInt(e.target.value, 10))
                      }
                      className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm ring-offset-background focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
                    >
                      {ctrl.menu.map((opt) => (
                        <option key={opt.value} value={opt.value}>
                          {opt.label}
                        </option>
                      ))}
                    </select>
                  </div>
                );
              }

              // int — range slider
              const min = ctrl.min ?? 0;
              const max = ctrl.max ?? 100;
              const step = ctrl.step ?? 1;
              return (
                <div key={ctrl.name} className="space-y-1.5">
                  <div className="flex items-center justify-between">
                    <Label className="text-xs">{controlLabel(ctrl.name)}</Label>
                    <span className="text-xs font-mono text-muted-foreground w-12 text-right">
                      {lv}
                    </span>
                  </div>
                  <input
                    type="range"
                    min={min}
                    max={max}
                    step={step}
                    value={lv}
                    onChange={(e) =>
                      handleChange(ctrl.name, parseInt(e.target.value, 10))
                    }
                    className="w-full accent-primary h-1.5 rounded cursor-pointer"
                  />
                </div>
              );
            })}
          </div>
        ))}

        {/* ── Footer: reset to defaults ───────────────────────────── */}
        {data && (
          <>
            <Separator />
            <div className="flex justify-end">
              <Button
                variant="outline"
                size="sm"
                onClick={() => resetMutation.mutate()}
                disabled={resetMutation.isPending}
              >
                <RefreshCcw className={`h-3.5 w-3.5 mr-1.5 ${resetMutation.isPending ? 'animate-spin' : ''}`} />
                {t('cameras.controls.resetDefaults')}
              </Button>
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

export default function CamerasPage() {
  const { t } = useTranslation();
  const [now, setNow] = useState(() => Date.now());
  const [fullscreen, setFullscreen] = useState<Camera | null>(null);
  const [zoomMap, setZoomMap] = useState<Record<string, ZoomState>>({});

  // Refresh every 5s so plug/unplug + camera-rescan show without manual refresh.
  const { data: cameras = [], isError, refetch, isFetching } = useQuery({
    queryKey: ['pi-cameras'],
    queryFn: fetchCameras,
    refetchInterval: 5_000,
    refetchOnWindowFocus: true,
    retry: 1,
  });

  // Cache-bust streams every reconnect so frames flow again after a network blip
  const reload = () => {
    setNow(Date.now());
    refetch();
  };

  // Heartbeat probe so we surface offline state without waiting on stale frames
  const [online, setOnline] = useState<boolean>(true);
  useEffect(() => {
    let alive = true;
    const ping = async () => {
      try {
        const res = await fetch(`${PI_STREAM_URL}/health`, { cache: 'no-store' });
        if (alive) setOnline(res.ok);
      } catch {
        if (alive) setOnline(false);
      }
    };
    ping();
    const id = setInterval(ping, 10_000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const streamUrl = (camId: string) => `${PI_STREAM_URL}/stream/${camId}?t=${now}`;
  const snapshotUrl = (camId: string) => `${PI_STREAM_URL}/snapshot/${camId}?t=${Date.now()}`;

  const piHost = useMemo(() => {
    try {
      return new URL(PI_STREAM_URL).host;
    } catch {
      return PI_STREAM_URL;
    }
  }, []);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{t('cameras.title')}</h1>
          <p className="text-sm text-muted-foreground">
            {t('cameras.subtitle', { host: piHost })}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant={online ? 'default' : 'destructive'} className="gap-1.5">
            {online ? (
              <>
                <Activity className="h-3 w-3" /> {t('cameras.online')}
              </>
            ) : (
              <>
                <WifiOff className="h-3 w-3" /> {t('cameras.offline')}
              </>
            )}
          </Badge>
          <Button variant="outline" size="sm" onClick={reload} disabled={isFetching}>
            <RefreshCcw className={`h-4 w-4 mr-1.5 ${isFetching ? 'animate-spin' : ''}`} />
            {t('cameras.refresh')}
          </Button>
        </div>
      </div>

      {/* Offline / error state */}
      {(!online || isError) && cameras.length === 0 && (
        <Card className="border-rose-500/30 bg-rose-500/5">
          <CardHeader className="pb-3">
            <CardTitle className="text-base flex items-center gap-2">
              <WifiOff className="h-4 w-4 text-rose-500" />
              {t('cameras.unreachableTitle')}
            </CardTitle>
            <CardDescription>
              {t('cameras.unreachableDesc', { url: PI_STREAM_URL })}
            </CardDescription>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground space-y-1">
            <div>1. {t('cameras.troubleshoot.1')}</div>
            <div>2. {t('cameras.troubleshoot.2')}</div>
            <div>3. {t('cameras.troubleshoot.3')}</div>
          </CardContent>
        </Card>
      )}

      {/* Empty: server up but no cameras */}
      {online && cameras.length === 0 && !isError && (
        <Card>
          <CardContent className="p-8 text-center text-sm text-muted-foreground">
            {t('cameras.empty')}
          </CardContent>
        </Card>
      )}

      {/* Camera grid */}
      <div className="grid gap-4 grid-cols-1 sm:grid-cols-2 xl:grid-cols-2 2xl:grid-cols-3">
        {cameras.map((cam) => (
          <CameraTile
            key={cam.id}
            cam={cam}
            streamUrl={streamUrl(cam.id)}
            snapshotUrl={snapshotUrl(cam.id)}
            onFullscreen={() => setFullscreen(cam)}
            onZoomChange={(z) => setZoomMap(prev => ({ ...prev, [cam.id]: z }))}
          />
        ))}
      </div>

      {/* Live AI detection panel — isolated so crashes never kill camera feeds */}
      <DetectionErrorBoundary>
        <LiveDetection cameras={cameras} snapshotBase="" zoomMap={zoomMap} />
      </DetectionErrorBoundary>

      {/* Full-screen modal */}
      {fullscreen && (
        <FullscreenView
          cam={fullscreen}
          streamUrl={streamUrl(fullscreen.id)}
          snapshotUrl={snapshotUrl(fullscreen.id)}
          onClose={() => setFullscreen(null)}
        />
      )}
    </div>
  );
}

// ── Error boundary so AI panel crash never takes down the camera feeds ────────

class DetectionErrorBoundary extends Component<
  { children: ReactNode },
  { error: string | null }
> {
  constructor(props: { children: ReactNode }) {
    super(props);
    this.state = { error: null };
  }
  static getDerivedStateFromError(e: Error) {
    return { error: e.message ?? 'Unknown error' };
  }
  componentDidCatch(e: Error, info: ErrorInfo) {
    console.error('[LiveDetection crash]', e, info);
  }
  render() {
    if (this.state.error) {
      return (
        <Card className="border-rose-500/30">
          <CardContent className="p-4 text-sm text-rose-400">
            Live Detection panel error: {this.state.error}
          </CardContent>
        </Card>
      );
    }
    return this.props.children;
  }
}

// ── Live AI Detection panel ───────────────────────────────────────────────────

type DetectionResult = {
  confidence: number;
  class_id: number;
  class_name: string;
  bbox: number[];
};

type CamDetection = {
  available: boolean;
  stale: boolean;
  age_s: number;
  count: number;
  detections: DetectionResult[];
  source: string | null;
  cam_id: string;
  inference_ms: number | null;
  ts: number | null;
  has_frame: boolean;
};

type AllDetections = {
  live: boolean;
  total_count: number;
  camera_count: number;
  cameras: Record<string, CamDetection>;
};

async function fetchAllDetections(): Promise<AllDetections> {
  return api<AllDetections>('/api/v1/ai/detections/all');
}

// ── AI model selector ────────────────────────────────────────────────────────
type AiModel = {
  id: string;
  name: string;
  description?: string;
  accuracy?: string;
  type?: string;
};
type AiModels = { available: AiModel[]; active: string };

async function fetchAiModels(): Promise<AiModels> {
  return api<AiModels>('/api/v1/ai/models');
}

async function setActiveAiModel(id: string): Promise<AiModels> {
  return api<AiModels>('/api/v1/ai/models', {
    method: 'POST',
    body: JSON.stringify({ active: id }),
  });
}

// ── Inference settings (sliders in the AI Detection settings dialog) ─────────
type InferenceSettings = {
  conf: number;
  iou: number;
  imgsz: number;
  frame_interval_s: number;
  jpeg_quality: number;
};
type InferenceSettingsBundle = {
  settings: InferenceSettings;
  defaults: InferenceSettings;
  bounds: Record<keyof InferenceSettings, { min: number; max: number }>;
};

async function fetchInferenceSettings(): Promise<InferenceSettingsBundle> {
  return api<InferenceSettingsBundle>('/api/v1/ai/settings');
}

async function updateInferenceSettings(
  patch: Partial<InferenceSettings>,
): Promise<InferenceSettingsBundle> {
  return api<InferenceSettingsBundle>('/api/v1/ai/settings', {
    method: 'POST',
    body: JSON.stringify(patch),
  });
}

/**
 * Fetches an authenticated frame image and returns a blob URL.
 * The plain <img> tag can't send JWT headers, so we fetch manually.
 */
function useAuthFrame(camId: string, frameTs: number, enabled: boolean): string | null {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) {
      setBlobUrl(prev => { if (prev) URL.revokeObjectURL(prev); return null; });
      return;
    }
    let cancelled = false;
    const token = getToken();
    const headers: HeadersInit = token ? { Authorization: `Bearer ${token}` } : {};

    fetch(`${API_URL}/api/v1/ai/detections/frame/${camId}`, { headers, cache: 'no-store' })
      .then(r => r.ok ? r.blob() : Promise.reject(r.status))
      .then(blob => {
        if (cancelled) return;
        const url = URL.createObjectURL(blob);
        setBlobUrl(prev => { if (prev) URL.revokeObjectURL(prev); return url; });
      })
      .catch(() => { /* frame not ready yet — silently ignore */ });

    return () => { cancelled = true; };
  }, [camId, frameTs, enabled]);  // re-fetch every time frameTs bumps

  // Final cleanup on unmount
  useEffect(() => {
    return () => { setBlobUrl(prev => { if (prev) URL.revokeObjectURL(prev); return null; }); };
  }, []);

  return blobUrl;
}

function LiveDetection({
  cameras: piCameras,
  zoomMap = {},
}: {
  cameras: Camera[];
  snapshotBase: string;
  zoomMap?: Record<string, ZoomState>;
}) {
  const { t } = useTranslation();
  const [frameTs, setFrameTs] = useState(0);

  const queryClient = useQueryClient();
  const { data } = useQuery<AllDetections>({
    queryKey: ['ai-detections-all'],
    queryFn: fetchAllDetections,
    refetchInterval: 2_000,
    retry: 1,
  });

  // AI model selector — poll faster so new models / switches show quickly.
  const { data: models } = useQuery<AiModels>({
    queryKey: ['ai-models'],
    queryFn: fetchAiModels,
    refetchInterval: 2_000,
    retry: 1,
  });
  const setModel = useMutation({
    mutationFn: setActiveAiModel,
    onSuccess: (res) => {
      queryClient.setQueryData(['ai-models'], res);
      // Force an immediate frame/detection refresh so the new model's output
      // appears without waiting for the next 2s poll tick.
      queryClient.invalidateQueries({ queryKey: ['ai-detections-all'] });
      setFrameTs(Date.now());
      toast.success(t('cameras.model.switched', {
        name: res.available.find(m => m.id === res.active)?.name ?? res.active,
      }));
    },
    onError: () => toast.error(t('cameras.model.switchFailed')),
  });

  // Inference settings (sliders) — apply live on next frame
  const [settingsOpen, setSettingsOpen] = useState(false);
  const { data: settings } = useQuery<InferenceSettingsBundle>({
    queryKey: ['ai-settings'],
    queryFn: fetchInferenceSettings,
    refetchInterval: 5_000,
    retry: 1,
  });
  const patchSettings = useMutation({
    mutationFn: updateInferenceSettings,
    onSuccess: (res) => {
      queryClient.setQueryData(['ai-settings'], res);
      queryClient.invalidateQueries({ queryKey: ['ai-detections-all'] });
    },
    onError: () => toast.error(t('cameras.settings.updateFailed', 'Failed to update settings')),
  });

  // Bump frameTs whenever new data arrives so <img> src changes and reloads
  useEffect(() => {
    if (data) setFrameTs(Date.now());
  }, [data]);

  const isLive      = data?.live ?? false;
  const totalCount  = data?.total_count ?? 0;
  const camEntries  = Object.entries(data?.cameras ?? {});

  return (
    <Card className={`transition-colors ${isLive ? 'border-sky-500/40' : ''}`}>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2 text-base">
              <Bot className="h-4 w-4 text-sky-400" />
              {t('cameras.liveAiTitle')}
            </CardTitle>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            {/* Model selector dropdown */}
            {models?.available?.length ? (
              <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <span className="hidden sm:inline">{t('cameras.model.label')}</span>
                <select
                  value={models.active}
                  disabled={setModel.isPending}
                  onChange={(e) => setModel.mutate(e.target.value)}
                  title={models.available.find(m => m.id === models.active)?.description ?? ''}
                  className="h-8 rounded-md border border-input bg-background px-2 text-xs text-foreground
                             focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-50"
                  aria-label={t('cameras.model.label')}
                >
                  {models.available.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.name}{m.accuracy ? ` · ${m.accuracy}` : ''}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}
            {/* Settings dialog trigger — sliders for conf / IoU / imgsz / interval */}
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8 gap-1.5 text-xs"
              onClick={() => setSettingsOpen(true)}
              aria-label={t('cameras.settings.open', 'AI inference settings')}
              disabled={!settings}
            >
              <Settings className="h-3 w-3" />
              <span className="hidden sm:inline">
                {t('cameras.settings.label', 'Settings')}
              </span>
            </Button>
            {isLive ? (
              <>
                <Badge variant="default" className="gap-1.5 bg-sky-600 hover:bg-sky-600">
                  <Zap className="h-3 w-3" /> {t('cameras.aiLive')}
                </Badge>
                <Badge variant="secondary" className="text-base font-mono px-3">
                  <MousePointer2 className="h-3 w-3 mr-1.5" />
                  {t('cameras.miceTotal', { count: totalCount })}
                </Badge>
              </>
            ) : (
              <Badge variant="outline" className="gap-1.5 text-muted-foreground">
                <WifiOff className="h-3 w-3" /> {t('cameras.aiWaitingColab')}
              </Badge>
            )}
            <Badge variant="secondary" className="gap-1.5 text-xs font-mono">
              <ScanSearch className="h-3 w-3" /> YOLOv8 · mAP50 95.2%
            </Badge>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-4">

        {/* Per-camera grid */}
        {camEntries.length > 0 ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4">
            {camEntries.map(([camId, cam]) => (
              <CamDetectionTile
                key={camId}
                camId={camId}
                cam={cam}
                frameTs={frameTs}
                zoom={zoomMap[camId]}
                piCamName={piCameras.find(c => c.id === camId)?.name}
              />
            ))}
          </div>
        ) : (
          /* Setup instructions when nothing is running */
          <div className="border border-dashed rounded-lg p-5 space-y-3 bg-muted/20">
            <div className="font-medium flex items-center gap-2 text-sm">
              <Bot className="h-4 w-4 text-sky-400" />
              {t('cameras.setupTitle')}
            </div>
            <ol className="space-y-1.5 text-xs text-muted-foreground list-decimal list-inside">
              <li>{t('cameras.setupStep1')}</li>
              <li>{t('cameras.setupStep2')}</li>
              <li>{t('cameras.setupStep3')}</li>
            </ol>
          </div>
        )}
      </CardContent>

      {/* AI inference settings dialog — sliders apply live (next-frame). */}
      {settings ? (
        <AiSettingsDialog
          open={settingsOpen}
          onOpenChange={setSettingsOpen}
          bundle={settings}
          onPatch={(p) => patchSettings.mutate(p)}
          isPending={patchSettings.isPending}
        />
      ) : null}
    </Card>
  );
}

// ── AI Settings dialog — confidence / IoU / size / interval / JPEG sliders ───
const SETTINGS_FIELDS: Array<{
  key: keyof InferenceSettings;
  label: string;
  hint: string;
  step: number;
  unit?: string;
  fmt: (v: number) => string;
}> = [
  { key: 'conf',             label: 'Confidence threshold',
    hint: 'Only boxes with this score or higher are kept. Lower = more (noisier) detections.',
    step: 0.01, fmt: (v) => v.toFixed(2) },
  { key: 'iou',              label: 'NMS IoU threshold',
    hint: 'How much overlap is allowed before two boxes are merged. Lower = more aggressive merge.',
    step: 0.01, fmt: (v) => v.toFixed(2) },
  { key: 'imgsz',            label: 'Inference image size',
    hint: 'Pixels on the longest side. Bigger = better small-mouse detection, slower.',
    step: 32, unit: 'px', fmt: (v) => `${Math.round(v)} px` },
  { key: 'frame_interval_s', label: 'Frame interval',
    hint: 'Extra delay between frames. 0 = run as fast as possible.',
    step: 0.05, unit: 's', fmt: (v) => `${v.toFixed(2)}s` },
  { key: 'jpeg_quality',     label: 'Annotated JPEG quality',
    hint: 'Quality of the live image sent to the dashboard. Lower = less bandwidth.',
    step: 1, unit: '%', fmt: (v) => `${Math.round(v)}%` },
];

function AiSettingsDialog({
  open, onOpenChange, bundle, onPatch, isPending,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  bundle: InferenceSettingsBundle;
  onPatch: (patch: Partial<InferenceSettings>) => void;
  isPending: boolean;
}) {
  const { t } = useTranslation();
  // Local draft for snappy slider UX; server is the source of truth otherwise.
  const [draft, setDraft] = useState<InferenceSettings>(bundle.settings);
  useEffect(() => { setDraft(bundle.settings); }, [bundle.settings]);

  // Debounce server updates so dragging a slider doesn't flood the backend.
  const pendingRef = useRef<Partial<InferenceSettings>>({});
  const timerRef = useRef<number | null>(null);
  const flush = () => {
    const p = pendingRef.current;
    pendingRef.current = {};
    if (Object.keys(p).length) onPatch(p);
  };
  const schedule = (patch: Partial<InferenceSettings>) => {
    pendingRef.current = { ...pendingRef.current, ...patch };
    if (timerRef.current) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(flush, 200);
  };
  useEffect(() => () => { if (timerRef.current) window.clearTimeout(timerRef.current); }, []);

  const resetAll = () => {
    setDraft(bundle.defaults);
    pendingRef.current = { ...bundle.defaults };
    flush();
  };

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) flush(); onOpenChange(v); }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Settings className="h-4 w-4 text-sky-400" />
            {t('cameras.settings.title', 'AI Inference Settings')}
          </DialogTitle>
        </DialogHeader>
        <div className="space-y-5 pt-1">
          <p className="text-xs text-muted-foreground">
            {t('cameras.settings.desc',
              'Changes apply live on the next frame. The inference loop reads these from each push response.')}
          </p>
          {SETTINGS_FIELDS.map((f) => {
            const b = bundle.bounds[f.key];
            const v = draft[f.key];
            return (
              <div key={f.key} className="space-y-1.5">
                <div className="flex items-baseline justify-between gap-3">
                  <Label htmlFor={`ai-${f.key}`} className="text-sm font-medium">
                    {f.label}
                  </Label>
                  <span className="text-xs font-mono tabular-nums text-foreground">
                    {f.fmt(v)}
                  </span>
                </div>
                <input
                  id={`ai-${f.key}`}
                  type="range"
                  min={b.min}
                  max={b.max}
                  step={f.step}
                  value={v}
                  disabled={isPending}
                  onChange={(e) => {
                    const next = Number(e.target.value);
                    setDraft((d) => ({ ...d, [f.key]: next }));
                    schedule({ [f.key]: next } as Partial<InferenceSettings>);
                  }}
                  className="w-full h-1.5 bg-muted rounded-lg appearance-none cursor-pointer
                             accent-sky-500 disabled:opacity-50"
                  aria-label={f.label}
                />
                <div className="flex items-center justify-between text-[10px] text-muted-foreground font-mono">
                  <span>{f.fmt(b.min)}</span>
                  <span className="italic font-sans normal-case">{f.hint}</span>
                  <span>{f.fmt(b.max)}</span>
                </div>
              </div>
            );
          })}
          <Separator />
          <div className="flex items-center justify-between">
            <span className="text-xs text-muted-foreground">
              {isPending ? t('cameras.settings.saving', 'Saving…') : t('cameras.settings.live', 'Live — applies next frame')}
            </span>
            <Button variant="outline" size="sm" onClick={resetAll} disabled={isPending}>
              <RefreshCcw className="h-3 w-3 mr-1.5" />
              {t('cameras.settings.reset', 'Reset to defaults')}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function CamDetectionTile({
  camId, cam, frameTs, zoom, piCamName,
}: {
  camId: string;
  cam: CamDetection;
  frameTs: number;
  zoom?: ZoomState;
  piCamName?: string;
}) {
  const { t } = useTranslation();
  const isLive   = !cam.stale;
  const frameUrl = useAuthFrame(camId, frameTs, cam.has_frame && isLive);
  const zoomStyle = zoom && zoom.scale > 1
    ? { transform: `scale(${zoom.scale}) translate(${zoom.panX * 50}%, ${zoom.panY * 50}%)`, transformOrigin: 'center center' }
    : undefined;

  // The primary detection's class_name carries "Mouse · <state> · <behaviour>"
  // (state = Sleep/Rest/Active, behaviour = Normal/Repetitive). Split it out so
  // we can render the classification as distinct colour-coded chips.
  const primaryLabel = cam.detections[0]?.class_name ?? '';
  const labelParts   = primaryLabel.split('·').map((s) => s.trim()).filter(Boolean);
  const behaviourChips = labelParts.slice(1); // drop the leading "Mouse"
  const chipColor = (txt: string): string => {
    const tl = txt.toLowerCase();
    if (tl.includes('sleep'))      return 'bg-slate-500/20 text-slate-700 dark:text-slate-300 border-slate-500/30';
    if (tl.includes('rest') || tl.includes('quiet')) return 'bg-violet-500/20 text-violet-700 dark:text-violet-300 border-violet-500/30';
    if (tl.includes('wake') || tl.includes('active')) return 'bg-emerald-500/20 text-emerald-700 dark:text-emerald-300 border-emerald-500/30';
    if (tl.includes('repetitive')) return 'bg-rose-500/20 text-rose-700 dark:text-rose-300 border-rose-500/30';
    if (tl.includes('normal'))     return 'bg-sky-500/20 text-sky-700 dark:text-sky-300 border-sky-500/30';
    return 'bg-muted/40 text-muted-foreground border-border';
  };

  return (
    <div className={`border rounded-lg overflow-hidden transition-colors ${isLive ? 'border-sky-500/30' : 'border-border'}`}>
      {/* Tile header */}
      <div className="flex items-center justify-between px-3 py-2 bg-muted/30 border-b">
        <span className="text-xs font-medium truncate">
          {piCamName ? piCamName.split(':')[0].trim() : `Cam #${camId}`}
        </span>
        <div className="flex items-center gap-1.5 shrink-0">
          {isLive ? (
            <Badge variant="default" className="h-4 text-[10px] px-1.5 bg-sky-600 hover:bg-sky-600">
              <Zap className="h-2.5 w-2.5 mr-0.5" /> {t('cameras.aiLive')}
            </Badge>
          ) : (
            <Badge variant="outline" className="h-4 text-[10px] px-1.5 text-muted-foreground">
              {t('cameras.aiWaitingColab')}
            </Badge>
          )}
        </div>
      </div>

      {/* Annotated frame */}
      <div className="relative bg-black aspect-video overflow-hidden">
        {frameUrl ? (
          <img
            src={frameUrl}
            alt={`Cam ${camId} detections`}
            className="w-full h-full object-contain transition-transform duration-150"
            style={zoomStyle}
          />
        ) : (
          <div className="absolute inset-0 grid place-items-center text-muted-foreground/40">
            <CameraIcon className="h-8 w-8" />
          </div>
        )}
        {/* Mouse count badge overlay */}
        {isLive && (
          <div className="absolute top-2 right-2 bg-black/70 rounded px-2 py-0.5 text-sky-400 font-mono text-sm font-bold">
            {cam.count} 🐭
          </div>
        )}
      </div>

      {/* Classification chips (state + behaviour from the primary detection) */}
      {isLive && behaviourChips.length > 0 && (
        <div className="flex flex-wrap gap-1.5 px-2.5 pt-2">
          {behaviourChips.map((chip, i) => (
            <span key={i}
              className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium ${chipColor(chip)}`}>
              {chip}
            </span>
          ))}
        </div>
      )}

      {/* Detection list */}
      <div className="p-2.5 space-y-1">
        {isLive && cam.detections.length > 0 ? (
          cam.detections.slice(0, 4).map((d, i) => (
            <div key={i} className="flex items-center justify-between text-[11px] font-mono gap-2">
              <span className="text-sky-400 shrink-0">#{i + 1} {d.class_name.split('·')[0].trim()}</span>
              <div className="flex items-center gap-1.5 flex-1 justify-end">
                <div className="w-16 h-1 bg-muted rounded-full overflow-hidden">
                  <div className="h-full bg-sky-400 rounded-full"
                       style={{ width: `${d.confidence * 100}%` }} />
                </div>
                <span className="text-muted-foreground w-8 text-right">
                  {Math.round(d.confidence * 100)}%
                </span>
              </div>
            </div>
          ))
        ) : (
          <div className="text-[11px] text-muted-foreground/50 italic text-center py-1">
            {isLive ? t('cameras.noMiceInFrame') : t('cameras.lastSeenAgo', { sec: cam.age_s })}
          </div>
        )}
        {cam.inference_ms != null && (
          <div className="text-[10px] text-muted-foreground/50 text-right pt-0.5">
            {cam.inference_ms.toFixed(1)}ms
          </div>
        )}
      </div>
    </div>
  );
}

// Detect whether we're using the direct Pi LAN address or a tunnel/proxy.
// MJPEG streaming works natively on LAN; tunnels (Cloudflare etc.) buffer
// chunked responses and break MJPEG, so we fall back to snapshot polling.
const isDirectLan = PI_STREAM_URL.startsWith('http://192.168.') ||
                    PI_STREAM_URL.startsWith('http://10.') ||
                    PI_STREAM_URL.startsWith('http://172.');

// Long-polling: the Pi holds /snapshot/:id/next open until a new frame is
// captured, then flushes it. We re-fetch immediately on response, so frames
// arrive back-to-back at capture rate with zero idle gap — no setInterval
// throttle, no wasted RTT. Through Cloudflare we get ~15-25 fps instead of
// the 5 fps the old setInterval(200ms) gave us.
//
// useLongPollFrame returns (blobUrl, fpsEstimate, errored). It's shared between
// the live tile and the fullscreen view so they behave identically.
function useLongPollFrame(
  snapshotBase: string,        // e.g. https://cam.example.org/snapshot/0
  enabled: boolean,
): { src: string; fps: number | null; errored: boolean } {
  const [src, setSrc]         = useState<string>('');
  const [fps, setFps]         = useState<number | null>(null);
  const [errored, setErrored] = useState(false);

  useEffect(() => {
    if (!enabled) return;
    let alive   = true;
    let lastSeq = 0;
    let frames  = 0;
    let lastTs  = performance.now();
    let currentUrl = '';

    const loop = async () => {
      while (alive) {
        try {
          const r = await fetch(`${snapshotBase}/next?since=${lastSeq}`, {
            cache: 'no-store',
          });
          if (!alive) return;
          if (r.status === 204) {
            // No new frame within the server's 25 s timeout — just retry.
            continue;
          }
          if (!r.ok) {
            setErrored(true);
            await new Promise(res => setTimeout(res, 500));
            continue;
          }
          const seqHdr = r.headers.get('X-Frame-Seq');
          if (seqHdr) lastSeq = parseInt(seqHdr, 10) || lastSeq;

          const blob = await r.blob();
          if (!alive) return;
          const objUrl = URL.createObjectURL(blob);
          // Revoke the *previous* URL only after assigning the new one to avoid
          // a flash of blank — the browser holds the bitmap once decoded.
          if (currentUrl) URL.revokeObjectURL(currentUrl);
          currentUrl = objUrl;
          setSrc(objUrl);
          setErrored(false);

          frames += 1;
          const now = performance.now();
          if (now - lastTs >= 1000) {
            setFps(Math.round((frames * 1000) / (now - lastTs)));
            frames = 0;
            lastTs = now;
          }
        } catch {
          if (!alive) return;
          setErrored(true);
          await new Promise(res => setTimeout(res, 500));
        }
      }
    };
    loop();

    return () => {
      alive = false;
      if (currentUrl) URL.revokeObjectURL(currentUrl);
      setSrc('');
    };
  }, [snapshotBase, enabled]);

  return { src, fps, errored };
}

// ----- Single camera tile -------------------------------------------------

function CameraTile({
  cam,
  streamUrl,
  snapshotUrl,
  onFullscreen,
  onZoomChange,
}: {
  cam: Camera;
  streamUrl: string;
  snapshotUrl: string;
  onFullscreen: () => void;
  onZoomChange?: (z: ZoomState) => void;
}) {
  const { t } = useTranslation();
  const imgRef = useRef<HTMLImageElement>(null);
  const [loaded, setLoaded] = useState(false);
  const [lanErrored, setLanErrored] = useState(false);
  const [lanFpsEst, setLanFpsEst] = useState<number | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [zoom, setZoom] = useState<ZoomState>(DEFAULT_ZOOM);
  const handleZoomChange = (z: ZoomState) => { setZoom(z); onZoomChange?.(z); };

  // Tunnel mode: long-polling — see useLongPollFrame for details.
  const snapshotBase = snapshotUrl.split('?')[0];
  const tunnel = useLongPollFrame(snapshotBase, !isDirectLan);

  // MJPEG native streaming for LAN mode
  useEffect(() => {
    if (!isDirectLan) return;
    const el = imgRef.current;
    if (!el) return;
    let frames = 0;
    let last = performance.now();
    const onLoad = () => {
      setLoaded(true);
      setLanErrored(false);
      frames += 1;
      const now = performance.now();
      if (now - last >= 1000) {
        setLanFpsEst(Math.round((frames * 1000) / (now - last)));
        frames = 0;
        last = now;
      }
    };
    const onError = () => setLanErrored(true);
    el.addEventListener('load', onLoad);
    el.addEventListener('error', onError);
    return () => {
      el.removeEventListener('load', onLoad);
      el.removeEventListener('error', onError);
    };
  }, [streamUrl]);

  // Unified state for the render block below.
  const pollSrc = tunnel.src;
  const errored = isDirectLan ? lanErrored : tunnel.errored;
  const fpsEst  = isDirectLan ? lanFpsEst  : tunnel.fps;
  // In tunnel mode we know the image is "loaded" the moment we have a blob URL;
  // in LAN mode the <img>'s onLoad handler flips `loaded`.
  const isLoaded = isDirectLan ? loaded : pollSrc !== '';

  const downloadSnapshot = async () => {
    setDownloading(true);
    try {
      const res = await fetch(snapshotUrl);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `cam-${cam.id}-${new Date().toISOString().replace(/[:.]/g, '-')}.jpg`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(`${t('cameras.snapshotFailed')}: ${msg}`);
    } finally {
      setDownloading(false);
    }
  };

  // Compute CSS transform for digital zoom
  const zoomTransform =
    zoom.scale === 1
      ? undefined
      : {
          transform: `scale(${zoom.scale}) translate(${zoom.panX * 50}%, ${zoom.panY * 50}%)`,
          transformOrigin: 'center center',
        };

  return (
    <Card className="overflow-hidden">
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <CardTitle className="text-base flex items-center gap-2 truncate">
              <CameraIcon className="h-4 w-4 text-primary shrink-0" />
              <span className="truncate">{prettyName(cam.name)}</span>
            </CardTitle>
            <CardDescription className="text-xs">
              {cam.device} · {busLabel(cam.bus)} · {cam.width}×{cam.height}
            </CardDescription>
          </div>
          <div className="flex items-center gap-1 shrink-0">
            {isLoaded && !errored && (
              <Badge variant="secondary" className="text-xs h-5">
                {fpsEst !== null ? `${fpsEst} fps` : t('cameras.live')}
              </Badge>
            )}
            {zoom.scale > 1 && (
              <Badge variant="secondary" className="text-xs h-5 gap-1">
                <ZoomIn className="h-3 w-3" />
                {zoom.scale.toFixed(1)}x
              </Badge>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent className="p-0">
        <div className="relative bg-black aspect-video overflow-hidden">
          {!isLoaded && !errored && (
            <div className="absolute inset-0 grid place-items-center text-xs text-muted-foreground">
              {t('cameras.connecting')}
            </div>
          )}
          {errored && (
            <div className="absolute inset-0 grid place-items-center text-xs text-rose-400 gap-1.5">
              <WifiOff className="h-5 w-5" />
              {t('cameras.streamError')}
            </div>
          )}
          {/* eslint-disable-next-line jsx-a11y/img-redundant-alt */}
          <img
            ref={imgRef}
            src={isDirectLan ? streamUrl : pollSrc}
            alt={`Camera ${cam.id} live stream`}
            className="w-full h-full object-contain"
            style={{
              opacity: errored || !isLoaded ? 0 : 1,
              transition: 'transform 0.15s ease',
              ...zoomTransform,
            }}
          />
        </div>
        <div className="flex items-center justify-between gap-2 p-2 border-t bg-muted/30">
          <span className="text-xs text-muted-foreground">
            {t('cameras.cameraId', { id: cam.id })}
          </span>
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setSettingsOpen(true)}
              title={t('cameras.controls.openSettings')}
              aria-label={t('cameras.controls.openSettings')}
            >
              <Settings className="h-3.5 w-3.5" />
            </Button>
            <Button variant="ghost" size="sm" onClick={downloadSnapshot} title={t('cameras.snapshot')} disabled={downloading} aria-label={t('cameras.snapshot')}>
              <Download className="h-3.5 w-3.5" />
            </Button>
            <Button variant="ghost" size="sm" onClick={onFullscreen} title={t('cameras.fullscreen')}>
              <Expand className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>
      </CardContent>

      {/* Settings dialog — rendered here so it has access to tile state */}
      <CameraSettingsDialog
        cam={cam}
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        zoom={zoom}
        setZoom={handleZoomChange}
      />
    </Card>
  );
}

// ----- Full-screen overlay ------------------------------------------------

function FullscreenView({
  cam,
  streamUrl,
  snapshotUrl,
  onClose,
}: {
  cam: Camera;
  streamUrl: string;
  snapshotUrl: string;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [downloading, setDownloading] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  // Long-polling for tunnel mode (see useLongPollFrame).
  const snapshotBase = snapshotUrl.split('?')[0];
  const { src: pollSrc } = useLongPollFrame(snapshotBase, !isDirectLan);

  const downloadSnapshot = async () => {
    setDownloading(true);
    try {
      const res = await fetch(`${snapshotUrl.split('?')[0]}?t=${Date.now()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `cam-${cam.id}-${new Date().toISOString().replace(/[:.]/g, '-')}.jpg`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(`${t('cameras.snapshotFailed')}: ${msg}`);
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/95 grid place-items-center p-4" onClick={onClose}>
      <div className="relative w-full max-w-7xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-3 text-white">
          <div>
            <div className="text-base font-medium">{prettyName(cam.name)}</div>
            <div className="text-xs text-white/60">{cam.device} · {busLabel(cam.bus)}</div>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="secondary" size="sm" onClick={downloadSnapshot} disabled={downloading}>
              <Download className="h-4 w-4 mr-1.5" />
              {downloading ? t('cameras.downloading') : t('cameras.snapshot')}
            </Button>
            <Button variant="secondary" size="sm" onClick={onClose}>
              <X className="h-4 w-4 mr-1.5" />
              {t('common.cancel')}
            </Button>
          </div>
        </div>
        <img
          src={isDirectLan ? streamUrl : pollSrc}
          alt={`Camera ${cam.id} full-screen`}
          className="w-full max-h-[85vh] object-contain bg-black rounded"
        />
      </div>
    </div>
  );
}
