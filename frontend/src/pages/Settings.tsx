import { useQuery } from '@tanstack/react-query';
import { ExternalLink, Globe, KeyRound, Palette } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { LanguageSwitcher } from '@/components/LanguageSwitcher';
import { ThemeToggle } from '@/components/ThemeToggle';
import { ApiKeyManager } from '@/components/ApiKeyManager';
import { API_URL, api } from '@/lib/api';
import { useAuth } from '@/store/auth';

export default function SettingsPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const { data: meta } = useQuery({
    queryKey: ['meta'],
    queryFn: () => api<{ version: string; env: string; feature_flags: Record<string, boolean> }>('/api/v1/meta'),
  });

  return (
    <div className="space-y-6 max-w-2xl">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{t('settings.title')}</h1>
        <p className="text-sm text-muted-foreground">{t('settings.subtitle')}</p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Palette className="h-4 w-4" />
            {t('settings.appearance')}
          </CardTitle>
          <CardDescription>{t('settings.appearanceDesc')}</CardDescription>
        </CardHeader>
        <CardContent>
          <ThemeToggle />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Globe className="h-4 w-4" />
            {t('settings.language')}
          </CardTitle>
          <CardDescription>{t('settings.languageDesc')}</CardDescription>
        </CardHeader>
        <CardContent>
          <LanguageSwitcher />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t('settings.account')}</CardTitle>
          <CardDescription>{t('settings.accountDesc')}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <Row label={t('settings.username')} value={user?.username ?? '—'} />
          <Row label={t('settings.role')} value={user?.role ?? '—'} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t('settings.deployment')}</CardTitle>
          <CardDescription>{t('settings.deploymentDesc')}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <Row label={t('settings.backendVersion')} value={meta?.version ?? '—'} />
          <Row label={t('settings.environment')} value={meta?.env ?? '—'} />
          <Row label={t('settings.apiBaseUrl')} value={API_URL} />
          <Separator />
          <div className="flex gap-2 flex-wrap">
            {meta && Object.entries(meta.feature_flags).map(([k, v]) => (
              <Badge key={k} variant={v ? 'success' : 'outline'}>{k}</Badge>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* API Keys — needed to connect Colab / external scripts */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <KeyRound className="h-4 w-4 text-violet-400" />
            {t('settings.apiKeysTitle')}
          </CardTitle>
          <CardDescription>
            {t('settings.apiKeysDesc')}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ApiKeyManager />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t('settings.apiResources')}</CardTitle>
          <CardDescription>{t('settings.apiResourcesDesc')}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          <Link label={t('settings.docLink')} href={`${API_URL}/docs`} />
          <Link label={t('settings.healthLink')} href={`${API_URL}/health`} />
          <Link label={t('settings.metaLink')} href={`${API_URL}/api/v1/meta`} />
        </CardContent>
      </Card>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-mono">{value}</span>
    </div>
  );
}

function Link({ label, href }: { label: string; href: string }) {
  return (
    <a className="flex items-center justify-between hover:text-primary" href={href} target="_blank" rel="noreferrer">
      <span>{label}</span>
      <ExternalLink className="h-3.5 w-3.5" />
    </a>
  );
}
