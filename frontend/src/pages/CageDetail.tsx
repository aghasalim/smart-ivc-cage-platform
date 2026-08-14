import { useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ArrowLeft, Download, Loader2, AlertTriangle } from 'lucide-react';
import { toast } from 'sonner';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Separator } from '@/components/ui/separator';
import { SensorChart } from '@/components/SensorChart';
import { BehaviourGrid } from '@/components/BehaviourGrid';

import { api, Cage, Reading, Summary } from '@/lib/api';
import { relativeTime } from '@/lib/utils';

export default function CageDetailPage() {
  const { t } = useTranslation();
  const { cageId } = useParams<{ cageId: string }>();
  const enabled = !!cageId;
  const [exporting, setExporting] = useState(false);

  const { data: cage, isError: cageError } = useQuery({
    queryKey: ['cage', cageId, 'meta'],
    enabled,
    queryFn: () => api<Cage>(`/api/v1/cages/${cageId}`),
    refetchInterval: 10_000,
  });

  const { data: readings = [], isError: readingsError } = useQuery({
    queryKey: ['cage', cageId, 'recent'],
    enabled,
    queryFn: () => api<Reading[]>(`/api/v1/cages/${cageId}/readings?limit=720`),
    refetchInterval: 6000,
  });

  const { data: summary, isError: summaryError } = useQuery({
    queryKey: ['cage', cageId, 'summary'],
    enabled,
    queryFn: () => api<Summary>(`/api/v1/cages/${cageId}/summary?days=7`),
    refetchInterval: 30_000,
  });

  const series = useMemo(() => {
    const byTs = (s: string) => readings.filter((r) => r.sensor === s).slice().reverse();
    return {
      feeding_delivered: byTs('feeding').map((r) => ({ ts: r.ts, value: r.payload.delivered_g })),
      water_flow: byTs('water').map((r) => ({ ts: r.ts, value: r.payload.flow_ml })),
      rer: byTs('metabolic').map((r) => ({ ts: r.ts, value: r.payload.rer })),
      vo2: byTs('metabolic').map((r) => ({ ts: r.ts, value: r.payload.vo2_ml_min_kg })),
      movement: byTs('behaviour').map((r) => ({ ts: r.ts, value: r.payload.movement_cm })),
      weight: byTs('weighing').map((r) => ({ ts: r.ts, value: r.payload.animal_g })),
    };
  }, [readings]);

  const latestBehaviour = readings.find((r) => r.sensor === 'behaviour');
  const latestFeeding = readings.find((r) => r.sensor === 'feeding');
  const latestMetab = readings.find((r) => r.sensor === 'metabolic');

  const handleExportCsv = async () => {
    setExporting(true);
    try {
      const csv = await api<string>(`/api/v1/cages/${cageId}/export.csv`);
      const blob = new Blob([csv], { type: 'text/csv' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${cageId}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(t('cageDetail.exportFailed') + (msg ? `: ${msg}` : ''));
    } finally {
      setExporting(false);
    }
  };

  const hasError = cageError || readingsError;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <Link to="/" className="text-sm text-muted-foreground hover:text-foreground inline-flex items-center gap-1.5 mb-1">
            <ArrowLeft className="h-3 w-3" /> {t('cageDetail.backToOverview')}
          </Link>
          <h1 className="text-2xl font-semibold tracking-tight">{cage?.name ?? cageId}</h1>
          <p className="text-sm text-muted-foreground">
            <span className="font-mono">{cageId}</span> · {cage?.animal_strain || '—'} · {t('cageDetail.lastSeen', { when: relativeTime(cage?.last_seen) })}
          </p>
        </div>
        <Button variant="outline" onClick={handleExportCsv} disabled={exporting}>
          {exporting ? (
            <><Loader2 className="mr-2 h-4 w-4 animate-spin" /> {t('common.exporting')}</>
          ) : (
            <><Download className="mr-2 h-4 w-4" /> {t('cageDetail.exportCsv')}</>
          )}
        </Button>
      </div>

      {hasError && (
        <div className="flex items-center gap-2 rounded-md border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-400">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          {t('cageDetail.loadError')}
        </div>
      )}

      <Tabs defaultValue="live">
        <TabsList>
          <TabsTrigger value="live">{t('cageDetail.tabLive')}</TabsTrigger>
          <TabsTrigger value="feeding">{t('cageDetail.tabFeeding')}</TabsTrigger>
          <TabsTrigger value="water">{t('cageDetail.tabWater')}</TabsTrigger>
          <TabsTrigger value="metabolic">{t('cageDetail.tabMetabolic')}</TabsTrigger>
          <TabsTrigger value="behaviour">{t('cageDetail.tabBehaviour')}</TabsTrigger>
          <TabsTrigger value="history">{t('cageDetail.tabHistory')}</TabsTrigger>
        </TabsList>

        <TabsContent value="live" className="mt-4 grid grid-cols-1 lg:grid-cols-2 gap-4">
          <Card>
            <CardHeader><CardTitle>{t('cageDetail.movementTitle')}</CardTitle><CardDescription>{t('cageDetail.movementDesc')}</CardDescription></CardHeader>
            <CardContent><SensorChart data={series.movement} color="#10b981" label="movement cm" /></CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle>{t('cage.rer')}</CardTitle><CardDescription>{t('cageDetail.rerDesc')}</CardDescription></CardHeader>
            <CardContent><SensorChart data={series.rer} color="#f59e0b" label="RER" /></CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle>{t('cageDetail.irGridTitle')}</CardTitle><CardDescription>{t('cageDetail.irGridDesc')}</CardDescription></CardHeader>
            <CardContent className="flex items-center gap-6">
              <BehaviourGrid cells={latestBehaviour?.payload?.grid_cells ?? Array(16).fill(0)} />
              <div className="space-y-1 text-sm">
                <div className="text-muted-foreground">{t('cageDetail.movement')}</div>
                <div className="font-mono text-lg">{latestBehaviour?.payload?.movement_cm?.toFixed(2) ?? '—'} cm</div>
              </div>
            </CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle>{t('cageDetail.nowTitle')}</CardTitle><CardDescription>{t('cageDetail.nowDesc')}</CardDescription></CardHeader>
            <CardContent>
              <div className="grid grid-cols-2 gap-3 text-sm">
                <Stat label={t('cageDetail.feedDelivered')} value={latestFeeding?.payload.delivered_g?.toFixed(3)} />
                <Stat label={t('cageDetail.feedRemaining')} value={latestFeeding?.payload.remaining_g?.toFixed(2)} />
                <Stat label={t('cageDetail.feedWasted')} value={latestFeeding?.payload.wasted_g?.toFixed(3)} />
                <Stat label={t('cageDetail.gateState')} value={latestFeeding?.payload.gate_state ?? '—'} />
                <Stat label={t('cageDetail.vo2')} value={latestMetab?.payload.vo2_ml_min_kg?.toFixed(2)} />
                <Stat label={t('cageDetail.vco2')} value={latestMetab?.payload.vco2_ml_min_kg?.toFixed(2)} />
                <Stat label={t('cageDetail.eeKcalH')} value={latestMetab?.payload.ee_kcal_h?.toFixed(3)} />
                <Stat label={t('cageDetail.bodyWeight')} value={readings.find(r=>r.sensor==='weighing')?.payload.animal_g?.toFixed(2)} />
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="feeding" className="mt-4 space-y-4">
          <Card><CardHeader><CardTitle>{t('cageDetail.deliveredFeed')}</CardTitle></CardHeader>
            <CardContent><SensorChart data={series.feeding_delivered} color="#10b981" label="delivered g" /></CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="water" className="mt-4 space-y-4">
          <Card><CardHeader><CardTitle>{t('cageDetail.waterFlow')}</CardTitle></CardHeader>
            <CardContent><SensorChart data={series.water_flow} color="#38bdf8" label="flow mL" /></CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="metabolic" className="mt-4 grid grid-cols-1 lg:grid-cols-2 gap-4">
          <Card><CardHeader><CardTitle>{t('cage.rer')}</CardTitle></CardHeader>
            <CardContent><SensorChart data={series.rer} color="#f59e0b" label="RER" /></CardContent>
          </Card>
          <Card><CardHeader><CardTitle>{t('cage.vo2')}</CardTitle></CardHeader>
            <CardContent><SensorChart data={series.vo2} color="#a855f7" label="VO2" /></CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="behaviour" className="mt-4 grid grid-cols-1 lg:grid-cols-2 gap-4">
          <Card><CardHeader><CardTitle>{t('cage.movement')}</CardTitle></CardHeader>
            <CardContent><SensorChart data={series.movement} color="#10b981" label="movement cm" /></CardContent>
          </Card>
          <Card><CardHeader><CardTitle>{t('cageDetail.bodyWeightShort')}</CardTitle></CardHeader>
            <CardContent><SensorChart data={series.weight} color="#e879f9" label="weight g" /></CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="history" className="mt-4">
          <Card>
            <CardHeader>
              <CardTitle>{t('cageDetail.last7Days')}</CardTitle>
              <CardDescription>{t('cageDetail.last7DaysDesc')}</CardDescription>
            </CardHeader>
            <CardContent>
              {summaryError ? (
                <div className="flex items-center gap-2 rounded-md border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-400">
                  <AlertTriangle className="h-4 w-4 shrink-0" />
                  {t('cageDetail.loadError')}
                </div>
              ) : !summary ? (
                <p className="text-sm text-muted-foreground">{t('cageDetail.noData')}</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead className="text-left text-muted-foreground">
                      <tr>
                        <th className="py-2 pr-3">{t('cageDetail.thDate')}</th>
                        <th className="py-2 pr-3">{t('cageDetail.thIntake')}</th>
                        <th className="py-2 pr-3">{t('cageDetail.thWasted')}</th>
                        <th className="py-2 pr-3">{t('cageDetail.thWater')}</th>
                        <th className="py-2 pr-3">{t('cageDetail.thAvgRer')}</th>
                        <th className="py-2 pr-3">{t('cageDetail.thEeKcal')}</th>
                        <th className="py-2 pr-3">{t('cageDetail.thSleep')}</th>
                        <th className="py-2 pr-3">{t('cageDetail.thActive')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {summary.days.map((d) => (
                        <tr key={d.date} className="border-t">
                          <td className="py-2 pr-3 font-mono">{d.date}</td>
                          <td className="py-2 pr-3 font-mono">{d.feeding.intake_g}</td>
                          <td className="py-2 pr-3 font-mono">{d.feeding.wasted_g}</td>
                          <td className="py-2 pr-3 font-mono">{d.water_ml}</td>
                          <td className="py-2 pr-3 font-mono">{d.metabolic.avg_rer}</td>
                          <td className="py-2 pr-3 font-mono">{d.metabolic.ee_kcal}</td>
                          <td className="py-2 pr-3 font-mono">{d.behaviour.sleeping_minutes}</td>
                          <td className="py-2 pr-3 font-mono">{d.behaviour.active_minutes}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}

function Stat({ label, value }: { label: string; value?: string }) {
  return (
    <div>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="font-mono">{value ?? '—'}</div>
    </div>
  );
}
