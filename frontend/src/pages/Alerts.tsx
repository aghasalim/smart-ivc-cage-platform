import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CheckCircle2, AlertTriangle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { api, Alert } from '@/lib/api';
import { relativeTime } from '@/lib/utils';

export default function AlertsPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data: alerts = [], isError: alertsError } = useQuery({
    queryKey: ['alerts'],
    queryFn: () => api<Alert[]>('/api/v1/alerts?limit=200'),
    refetchInterval: 6000,
  });

  const ack = useMutation({
    mutationFn: (id: number) => api(`/api/v1/alerts/${id}/ack`, { method: 'POST' }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alerts'] });
      toast.success(t('alerts.acknowledged'));
    },
    onError: () => {
      toast.error(t('alerts.ackFailed'));
    },
  });

  const open = alerts.filter((a) => !a.acknowledged_at);
  const closed = alerts.filter((a) => a.acknowledged_at);

  return (
    <div className="space-y-6">
      {alertsError && (
        <div className="flex items-center gap-2 rounded-md border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-400">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          {t('alerts.loadError')}
        </div>
      )}
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{t('alerts.title')}</h1>
        <p className="text-sm text-muted-foreground">
          {t('alerts.openCount', { open: open.length, acked: closed.length })}
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{t('alerts.open')}</CardTitle>
          <CardDescription>{t('alerts.openDesc')}</CardDescription>
        </CardHeader>
        <CardContent>
          {open.length === 0 ? (
            <p className="text-sm text-muted-foreground">{t('alerts.noOpen')}</p>
          ) : (
            <ul className="space-y-3">
              {open.map((a) => (
                <li key={a.id} className="flex items-start justify-between gap-3 border-b last:border-0 pb-3 last:pb-0">
                  <div className="space-y-1">
                    <div className="flex items-center gap-2">
                      <Badge variant={a.severity === 'critical' ? 'destructive' : 'warning'}>{a.severity}</Badge>
                      <span className="font-mono text-xs text-muted-foreground">{a.cage_id}</span>
                      <span className="text-xs text-muted-foreground">· {relativeTime(a.ts)}</span>
                    </div>
                    <div className="text-sm">{a.message}</div>
                    <div className="text-xs text-muted-foreground">{t('alerts.rule')}: <code>{a.rule}</code></div>
                  </div>
                  <Button size="sm" variant="outline" onClick={() => ack.mutate(a.id)} disabled={ack.isPending}>
                    <CheckCircle2 className="mr-2 h-4 w-4" /> {t('alerts.acknowledge')}
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t('alerts.resolved')}</CardTitle>
          <CardDescription>{t('alerts.resolvedDesc')}</CardDescription>
        </CardHeader>
        <CardContent>
          {closed.length === 0 ? (
            <p className="text-sm text-muted-foreground">—</p>
          ) : (
            <ul className="space-y-2 text-sm">
              {closed.slice(0, 20).map((a) => (
                <li key={a.id} className="flex items-center gap-3 text-muted-foreground">
                  <Badge variant="outline">{a.severity}</Badge>
                  <span className="font-mono text-xs">{a.cage_id}</span>
                  <span className="truncate">{a.message}</span>
                  <span className="ml-auto text-xs">{relativeTime(a.acknowledged_at)}</span>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
