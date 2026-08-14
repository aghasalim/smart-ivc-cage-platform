import { useTranslation } from 'react-i18next';
import { Monitor, Moon, Sun } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { useTheme, type Theme } from '@/store/theme';
import { cn } from '@/lib/utils';

const OPTIONS: { value: Theme; icon: typeof Sun; key: string }[] = [
  { value: 'light', icon: Sun, key: 'light' },
  { value: 'dark', icon: Moon, key: 'dark' },
  { value: 'system', icon: Monitor, key: 'system' },
];

export function ThemeToggle({ variant = 'full' }: { variant?: 'full' | 'compact' }) {
  const { t } = useTranslation();
  const theme = useTheme((s) => s.theme);
  const setTheme = useTheme((s) => s.setTheme);

  if (variant === 'compact') {
    return (
      <div className="flex items-center gap-1.5" role="group" aria-label={t('theme.label')}>
        {OPTIONS.map(({ value, icon: Icon, key }) => (
          <button
            key={value}
            onClick={() => setTheme(value)}
            title={t(`theme.${key}`)}
            aria-label={t(`theme.${key}`)}
            aria-pressed={theme === value}
            className={cn(
              'rounded p-1 transition-colors',
              theme === value
                ? 'bg-primary/15 text-primary'
                : 'text-muted-foreground hover:text-foreground',
            )}
          >
            <Icon className="h-3.5 w-3.5" />
          </button>
        ))}
      </div>
    );
  }

  return (
    <div className="flex flex-wrap gap-2">
      {OPTIONS.map(({ value, icon: Icon, key }) => (
        <Button
          key={value}
          variant={theme === value ? 'default' : 'outline'}
          size="sm"
          onClick={() => setTheme(value)}
        >
          <Icon className="mr-2 h-4 w-4" />
          {t(`theme.${key}`)}
        </Button>
      ))}
    </div>
  );
}
