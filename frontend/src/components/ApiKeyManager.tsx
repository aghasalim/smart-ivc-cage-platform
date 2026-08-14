/**
 * ApiKeyManager — list / create / revoke long-lived API keys.
 *
 * Plaintext key is shown ONLY ONCE on creation (security). The widget puts a
 * giant "copy" prompt so users don't lose it. After dismissal we only ever show
 * the first 12 chars (the prefix) for identification.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Copy, Check, Trash2, Plus, KeyRound, Terminal, ShieldAlert } from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';
import { api, API_URL } from '@/lib/api';
import { cn } from '@/lib/utils';

type ApiKey = {
  id: number;
  label: string;
  prefix: string;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  revoked: boolean;
};

type ApiKeyCreated = ApiKey & { key: string };

export function ApiKeyManager({ cageId }: { cageId?: string }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [label, setLabel] = useState('');
  const [justCreated, setJustCreated] = useState<ApiKeyCreated | null>(null);
  const [copied, setCopied] = useState<'key' | 'curl' | 'py' | null>(null);

  const { data: keys = [], isLoading } = useQuery<ApiKey[]>({
    queryKey: ['api-keys'],
    queryFn: () => api<ApiKey[]>('/api/v1/api-keys'),
  });

  const createMutation = useMutation({
    mutationFn: (lbl: string) =>
      api<ApiKeyCreated>('/api/v1/api-keys', {
        method: 'POST',
        body: JSON.stringify({ label: lbl }),
      }),
    onSuccess: (k) => {
      setJustCreated(k);
      setLabel('');
      qc.invalidateQueries({ queryKey: ['api-keys'] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const revokeMutation = useMutation({
    mutationFn: (id: number) => api(`/api/v1/api-keys/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['api-keys'] });
      toast.success(t('apiKeys.revoked'));
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const doCopy = async (kind: 'key' | 'curl' | 'py', text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(kind);
      window.setTimeout(() => setCopied(null), 1500);
    } catch {
      toast.error(t('apiKeys.copyFailed'));
    }
  };

  if (justCreated) {
    const curlEx = cageId
      ? `curl -H "X-API-Key: ${justCreated.key}" \\\n  ${API_URL}/api/v1/cages/${cageId}/readings?limit=100`
      : `curl -H "X-API-Key: ${justCreated.key}" \\\n  ${API_URL}/api/v1/ai/detections/latest`;
    const pyEx = cageId
      ? `import requests\nr = requests.get(\n    "${API_URL}/api/v1/cages/${cageId}/readings",\n    headers={"X-API-Key": "${justCreated.key}"},\n    params={"limit": 100},\n)\nprint(r.json())`
      : `# Paste into your Colab notebook\nAPI_KEY     = "${justCreated.key}"\nBACKEND_URL = "${API_URL}"\n\n# Then run docs/colab_live_inference.py`;
    return (
      <div className="space-y-4">
        <div className="flex items-start gap-3 rounded-md border border-amber-500/30 bg-amber-500/5 p-3.5">
          <ShieldAlert className="h-5 w-5 text-amber-400 shrink-0 mt-0.5" />
          <div className="text-sm">
            <div className="font-semibold text-amber-300 mb-0.5">{t('apiKeys.justCreatedTitle')}</div>
            <div className="text-muted-foreground text-xs leading-relaxed">
              {t('apiKeys.justCreatedDesc')}
            </div>
          </div>
        </div>

        <div className="space-y-1.5">
          <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {t('apiKeys.yourKey')}
          </div>
          <div className="flex items-center gap-2">
            <code className="flex-1 break-all rounded-md border bg-muted/50 px-3 py-2 text-xs font-mono">
              {justCreated.key}
            </code>
            <Button
              variant="outline"
              size="sm"
              onClick={() => doCopy('key', justCreated.key)}
              className="shrink-0"
            >
              {copied === 'key' ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
            </Button>
          </div>
        </div>

        <Separator />

        <div className="space-y-3">
          <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground flex items-center gap-1.5">
            <Terminal className="h-3.5 w-3.5" /> {t('apiKeys.exampleUsage')}
          </div>

          <CodeBlock
            language="bash"
            text={curlEx}
            copied={copied === 'curl'}
            onCopy={() => doCopy('curl', curlEx)}
          />
          <CodeBlock
            language="python"
            text={pyEx}
            copied={copied === 'py'}
            onCopy={() => doCopy('py', pyEx)}
          />
        </div>

        <Button onClick={() => setJustCreated(null)} className="w-full">
          {t('apiKeys.continue')}
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <div>
          <div className="text-sm font-semibold flex items-center gap-1.5">
            <KeyRound className="h-4 w-4 text-violet-400" />
            {t('apiKeys.title')}
          </div>
          <div className="text-xs text-muted-foreground">{t('apiKeys.subtitle')}</div>
        </div>
      </div>

      {/* Create row */}
      <div className="flex gap-2">
        <input
          type="text"
          placeholder={t('apiKeys.labelPlaceholder')}
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          className="flex-1 h-9 rounded-md border bg-background px-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
        />
        <Button
          onClick={() => createMutation.mutate(label)}
          disabled={createMutation.isPending}
          size="sm"
        >
          <Plus className="h-4 w-4 mr-1" />
          {t('apiKeys.create')}
        </Button>
      </div>

      {/* Existing keys */}
      <div className="space-y-1.5 max-h-64 overflow-auto pr-1">
        {isLoading && <div className="text-xs text-muted-foreground">{t('common.loading')}</div>}
        {!isLoading && keys.length === 0 && (
          <div className="text-xs text-muted-foreground text-center py-4 border border-dashed rounded-md">
            {t('apiKeys.empty')}
          </div>
        )}
        {keys.map((k) => (
          <Card key={k.id} className={cn('overflow-hidden', k.revoked && 'opacity-50')}>
            <CardContent className="p-3 flex items-center justify-between gap-3">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <span className="font-medium text-sm truncate">{k.label}</span>
                  {k.revoked && <Badge variant="destructive" className="text-[10px]">revoked</Badge>}
                  {!k.revoked && k.expires_at && new Date(k.expires_at) < new Date() && (
                    <Badge variant="destructive" className="text-[10px]">expired</Badge>
                  )}
                </div>
                <div className="text-[11px] text-muted-foreground font-mono mt-0.5">
                  {k.prefix}••••••
                  <span className="ml-2 not-italic font-sans">
                    {t('apiKeys.created')}: {new Date(k.created_at).toLocaleDateString()}
                  </span>
                  {k.last_used_at && (
                    <span className="ml-2 not-italic font-sans">
                      · {t('apiKeys.lastUsed')}: {new Date(k.last_used_at).toLocaleDateString()}
                    </span>
                  )}
                </div>
              </div>
              {!k.revoked && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => revokeMutation.mutate(k.id)}
                  className="h-8 text-rose-400 hover:text-rose-300 hover:bg-rose-500/10"
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
              )}
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}

function CodeBlock({
  language,
  text,
  copied,
  onCopy,
}: {
  language: string;
  text: string;
  copied: boolean;
  onCopy: () => void;
}) {
  return (
    <div className="relative">
      <div className="absolute right-2 top-2 z-10">
        <Button variant="outline" size="sm" onClick={onCopy} className="h-7 px-2">
          {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
        </Button>
      </div>
      <div className="absolute left-3 top-2 text-[10px] uppercase font-mono text-muted-foreground tracking-wider">
        {language}
      </div>
      <pre className="rounded-md border bg-muted/30 px-3 pt-6 pb-3 text-xs font-mono overflow-x-auto leading-relaxed">
        {text}
      </pre>
    </div>
  );
}
