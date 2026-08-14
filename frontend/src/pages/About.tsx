import { useTranslation } from 'react-i18next';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';
import { Activity, BookOpen, Microscope } from 'lucide-react';

const TEAM = [
  {
    name: 'Aghasalim (Salim) Mustafazada',
    initials: 'AM',
    color: 'bg-emerald-500',
    role: 'Full-Stack Developer & ML Engineer',
    contributions: ['ML behaviour classifier', 'Backend API', 'Device simulator'],
  },
  {
    name: 'Hadi Hleihel',
    initials: 'HH',
    color: 'bg-sky-500',
    role: 'Backend Developer & System Architect',
    contributions: ['Data pipeline', 'WebSocket fan-out', 'Auth & security'],
  },
  {
    name: 'Gražvydas Stalmokas',
    initials: 'GS',
    color: 'bg-violet-500',
    role: 'Frontend Developer & UX Designer',
    contributions: ['React dashboard', 'Real-time charts', 'Accessibility (WCAG 2.1 AA)'],
  },
];

const TECH = ['FastAPI', 'React 18', 'scikit-learn', 'SQLite', 'WebSocket', 'Docker', 'Tailwind CSS', 'Recharts'];

export default function AboutPage() {
  const { t } = useTranslation();
  const stats = [
    { value: '5', label: t('about.sensorTypes') },
    { value: '7', label: t('about.behaviourLabels') },
    { value: '0.996', label: t('about.macroF1') },
    { value: '110 ms', label: t('about.ingestionP95') },
  ];

  return (
    <div className="max-w-4xl mx-auto space-y-10">
      {/* Hero */}
      <div className="text-center space-y-4 py-8">
        <div className="inline-grid h-16 w-16 place-items-center rounded-2xl bg-primary/15 mx-auto">
          <Microscope className="h-8 w-8 text-primary" />
        </div>
        <h1 className="text-3xl font-bold tracking-tight">{t('about.platformTitle')}</h1>
        <p className="text-sm text-muted-foreground max-w-xl mx-auto leading-relaxed">
          {t('about.platformDesc')}
        </p>
        <div className="flex flex-wrap justify-center gap-2 pt-1">
          {TECH.map((tag) => (
            <Badge key={tag} variant="outline" className="text-xs">{tag}</Badge>
          ))}
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
        {stats.map((s) => (
          <Card key={s.label} className="text-center hover:border-primary/40 transition-colors">
            <CardContent className="pt-5 pb-4">
              <div className="font-mono text-2xl font-bold text-primary">{s.value}</div>
              <div className="text-xs text-muted-foreground mt-1">{s.label}</div>
            </CardContent>
          </Card>
        ))}
      </div>

      <Separator />

      {/* Team */}
      <div className="space-y-5">
        <h2 className="text-xl font-semibold">{t('about.teamTitle')}</h2>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-5">
          {TEAM.map((m) => (
            <Card key={m.name} className="hover:border-primary/50 transition-colors">
              <CardHeader className="pb-3">
                <div className="flex items-center gap-3">
                  <div
                    className={`h-12 w-12 rounded-full ${m.color} grid place-items-center text-white font-bold text-base flex-shrink-0 shadow-sm`}
                  >
                    {m.initials}
                  </div>
                  <div className="min-w-0">
                    <CardTitle className="text-sm leading-tight">{m.name}</CardTitle>
                    <p className="text-xs text-muted-foreground mt-0.5">{m.role}</p>
                  </div>
                </div>
              </CardHeader>
              <CardContent className="pt-0">
                <ul className="space-y-1">
                  {m.contributions.map((c) => (
                    <li key={c} className="text-xs text-muted-foreground flex items-center gap-1.5">
                      <span className="h-1.5 w-1.5 rounded-full bg-primary/60 flex-shrink-0" />
                      {c}
                    </li>
                  ))}
                </ul>
              </CardContent>
            </Card>
          ))}
        </div>
      </div>

      <Separator />

      {/* Project info */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-5">
        <Card>
          <CardHeader>
            <CardTitle className="text-base flex items-center gap-2">
              <Activity className="h-4 w-4 text-primary" /> {t('about.platformOverview')}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-sm text-muted-foreground">
            <p>{t('about.platformOverviewLine1')}</p>
            <p>{t('about.platformOverviewLine2')}</p>
            <p>{t('about.platformOverviewLine3')}</p>
            <p>{t('about.platformOverviewLine4')}</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="text-base flex items-center gap-2">
              <BookOpen className="h-4 w-4 text-primary" /> {t('about.module')}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-sm text-muted-foreground">
            <p>{t('about.moduleLine1')}</p>
            <p>{t('about.moduleLine2')} (<span className="font-mono text-xs text-foreground">contact@example.org</span>)</p>
            <p>{t('about.moduleLine3')}</p>
            <p>{t('about.moduleLine4')}</p>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
