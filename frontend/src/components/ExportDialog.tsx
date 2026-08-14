/**
 * ExportDialog — animated multi-format export modal.
 *
 *  Formats:
 *   - CSV    raw readings, one row per record (the original)
 *   - XLSX   formatted Excel workbook (Summary / Readings / Behaviour / Alerts sheets)
 *   - PDF    branded PDF report with KPIs + alerts + recent readings
 *   - DOCX   Word document for non-technical stakeholders
 *   - JSON   structured machine-readable export
 *   - API    not a download — opens the API-key manager so external apps can
 *            pull live data via X-API-Key
 *
 *  Translation keys live under `export.*` in every locale (en / nl / fr / zh).
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Download,
  FileSpreadsheet,
  FileText,
  FileType,
  FileJson,
  Key,
  Loader2,
  Check,
  type LucideIcon,
} from 'lucide-react';
import { toast } from 'sonner';

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { cn } from '@/lib/utils';
import { API_URL, getToken } from '@/lib/api';
import { ApiKeyManager } from './ApiKeyManager';

type ExportFormat = 'csv' | 'xlsx' | 'pdf' | 'docx' | 'json' | 'api';

type FormatSpec = {
  id: ExportFormat;
  ext: string;
  icon: LucideIcon;
  accent: string;
  iconBg: string;
};

const FORMATS: FormatSpec[] = [
  { id: 'csv',  ext: 'csv',  icon: FileText,        accent: 'text-emerald-400', iconBg: 'bg-emerald-500/10' },
  { id: 'xlsx', ext: 'xlsx', icon: FileSpreadsheet, accent: 'text-green-400',   iconBg: 'bg-green-500/10' },
  { id: 'pdf',  ext: 'pdf',  icon: FileType,        accent: 'text-rose-400',    iconBg: 'bg-rose-500/10' },
  { id: 'docx', ext: 'docx', icon: FileText,        accent: 'text-sky-400',     iconBg: 'bg-sky-500/10' },
  { id: 'json', ext: 'json', icon: FileJson,        accent: 'text-amber-400',   iconBg: 'bg-amber-500/10' },
  { id: 'api',  ext: '',     icon: Key,             accent: 'text-violet-400',  iconBg: 'bg-violet-500/10' },
];

export type ExportTarget = { cageId: string; cageName?: string };

export function ExportDialog({
  open,
  onOpenChange,
  target,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  target: ExportTarget;
}) {
  const { t } = useTranslation();
  const [days, setDays] = useState(7);
  const [busy, setBusy] = useState<ExportFormat | null>(null);
  const [done, setDone] = useState<ExportFormat | null>(null);
  const [showKeys, setShowKeys] = useState(false);

  const reset = () => {
    setBusy(null);
    setDone(null);
    setShowKeys(false);
  };

  const handlePick = async (fmt: ExportFormat) => {
    if (fmt === 'api') {
      setShowKeys(true);
      return;
    }
    await runDownload(fmt);
  };

  const runDownload = async (fmt: ExportFormat) => {
    const token = getToken();
    if (!token) {
      toast.error(t('common.notSignedIn'));
      return;
    }
    setBusy(fmt);
    setDone(null);
    try {
      const url = `${API_URL}/api/v1/cages/${target.cageId}/export.${fmt}?days=${days}`;
      const res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.error?.message ?? `Export failed (${res.status})`);
      }
      const blob = await res.blob();
      const dl = URL.createObjectURL(blob);
      const stem = (target.cageName ?? target.cageId).replace(/[^a-z0-9]+/gi, '_');
      const a = document.createElement('a');
      a.href = dl;
      a.download = `${stem}_${new Date().toISOString().slice(0, 10)}.${fmt}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(dl);
      toast.success(`${t('common.exportSuccess')} — ${target.cageName ?? target.cageId}`);
      setDone(fmt);
      window.setTimeout(() => {
        onOpenChange(false);
        reset();
      }, 900);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('common.exportFailed'));
    } finally {
      setBusy(null);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) reset();
        onOpenChange(v);
      }}
    >
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Download className="h-5 w-5 text-primary" />
            {t('export.title')}
          </DialogTitle>
          <DialogDescription>
            {t('export.subtitle', { cage: target.cageName ?? target.cageId })}
          </DialogDescription>
        </DialogHeader>

        {!showKeys ? (
          <>
            {/* Range picker */}
            <div className="space-y-2">
              <div className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
                {t('export.range')}
              </div>
              <div className="flex flex-wrap gap-2">
                {[7, 14, 30, 90].map((d) => (
                  <Button
                    key={d}
                    size="sm"
                    variant={days === d ? 'default' : 'outline'}
                    onClick={() => setDays(d)}
                    className="transition-all"
                  >
                    {t(`reports.days${d}` as const, { defaultValue: `${d} days` })}
                  </Button>
                ))}
              </div>
            </div>

            <Separator />

            {/* Format grid */}
            <div className="space-y-2">
              <div className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
                {t('export.chooseFormat')}
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-2.5">
                {FORMATS.map((f, i) => {
                  const Icon = f.icon;
                  const isBusy = busy === f.id;
                  const isDone = done === f.id;
                  return (
                    <button
                      key={f.id}
                      onClick={() => handlePick(f.id)}
                      disabled={busy !== null}
                      style={{ animationDelay: `${i * 40}ms` }}
                      className={cn(
                        'group relative flex flex-col items-start gap-1.5 rounded-lg border bg-card p-3.5 text-left',
                        'transition-all duration-200',
                        'hover:border-primary/50 hover:bg-accent/40 hover:scale-[1.02] hover:shadow-md',
                        'focus:outline-none focus:ring-2 focus:ring-ring',
                        'disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:scale-100',
                        'animate-in fade-in slide-in-from-bottom-2',
                        isDone && 'border-emerald-500 bg-emerald-500/5',
                      )}
                    >
                      <div className={cn('rounded-md p-2 transition-transform group-hover:scale-110', f.iconBg)}>
                        {isBusy ? (
                          <Loader2 className={cn('h-5 w-5 animate-spin', f.accent)} />
                        ) : isDone ? (
                          <Check className="h-5 w-5 text-emerald-400" />
                        ) : (
                          <Icon className={cn('h-5 w-5', f.accent)} />
                        )}
                      </div>
                      <div className="flex items-center gap-2">
                        <span className="font-semibold text-sm">
                          {t(`export.formats.${f.id}.label`)}
                        </span>
                        {f.ext && (
                          <Badge variant="outline" className="text-[10px] uppercase font-mono px-1.5 py-0">
                            .{f.ext}
                          </Badge>
                        )}
                      </div>
                      <span className="text-xs text-muted-foreground leading-snug">
                        {t(`export.formats.${f.id}.desc`)}
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>

            <div className="text-[11px] text-muted-foreground border-t pt-3 leading-relaxed">
              {t('export.hint')}
            </div>
          </>
        ) : (
          /* API key manager sub-view */
          <div className="space-y-4 animate-in fade-in slide-in-from-right-2 duration-200">
            <ApiKeyManager cageId={target.cageId} />
            <Button variant="outline" size="sm" onClick={() => setShowKeys(false)}>
              ← {t('export.backToFormats')}
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
