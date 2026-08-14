import { FormEvent, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Microscope } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { LanguageSwitcher } from '@/components/LanguageSwitcher';
import { useAuth } from '@/store/auth';

export default function LoginPage() {
  const { t } = useTranslation();
  const [username, setUsername] = useState(import.meta.env.DEV ? 'researcher' : '');
  const [password, setPassword] = useState(import.meta.env.DEV ? 'change-me-please' : '');
  const [loading, setLoading] = useState(false);
  const { login } = useAuth();
  const nav = useNavigate();

  async function submit(e: FormEvent) {
    e.preventDefault();
    setLoading(true);
    try {
      await login(username, password);
      nav('/');
    } catch (err: any) {
      toast.error(err?.message ?? t('login.invalidCredentials'));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen grid place-items-center bg-gradient-to-br from-slate-100 via-slate-50 to-emerald-100/50 dark:from-slate-950 dark:via-slate-900 dark:to-emerald-950/40">
      <div className="w-full max-w-md px-6">
        <div className="mb-6 flex items-center gap-3 justify-center">
          <div className="grid h-10 w-10 place-items-center rounded-lg bg-primary/20">
            <Microscope className="h-5 w-5 text-primary" />
          </div>
          <div className="leading-tight">
            <div className="text-lg font-semibold">IVC Cage</div>
            <div className="text-xs text-muted-foreground">{t('login.subtitle')}</div>
          </div>
        </div>

        <Card>
          <CardHeader>
            <CardTitle>{t('login.title')}</CardTitle>
            <CardDescription>{t('login.subtitle')}</CardDescription>
          </CardHeader>
          <CardContent>
            <form className="space-y-4" onSubmit={submit}>
              <div className="space-y-2">
                <Label htmlFor="username">{t('login.username')}</Label>
                <Input id="username" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required />
              </div>
              <div className="space-y-2">
                <Label htmlFor="password">{t('login.password')}</Label>
                <Input id="password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
              </div>
              <Button className="w-full" disabled={loading}>
                {loading ? t('login.signingIn') : t('login.signIn')}
              </Button>
            </form>
          </CardContent>
        </Card>

        <div className="mt-4 flex flex-col items-center gap-3">
          {import.meta.env.DEV && (
            <p className="text-center text-xs text-muted-foreground">
              <code className="font-mono">researcher</code> / <code className="font-mono">change-me-please</code>
            </p>
          )}
          <LanguageSwitcher variant="compact" />
        </div>
      </div>
    </div>
  );
}
