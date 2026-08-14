import { useTranslation } from 'react-i18next';
import { Languages } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { LANGUAGES, type LanguageCode } from '@/lib/i18n';
import { cn } from '@/lib/utils';

export function LanguageSwitcher({ variant = 'full' }: { variant?: 'full' | 'compact' }) {
  const { i18n } = useTranslation();
  const current = i18n.resolvedLanguage as LanguageCode | undefined;

  if (variant === 'compact') {
    return (
      <div className="flex items-center gap-1.5">
        <Languages className="h-3.5 w-3.5 text-muted-foreground" />
        {LANGUAGES.map((l) => (
          <button
            key={l.code}
            onClick={() => i18n.changeLanguage(l.code)}
            className={cn(
              'text-xs font-medium uppercase tracking-wide px-1.5 py-0.5 rounded transition-colors',
              current === l.code
                ? 'bg-primary/15 text-primary'
                : 'text-muted-foreground hover:text-foreground',
            )}
          >
            {l.code}
          </button>
        ))}
      </div>
    );
  }

  return (
    <div className="flex flex-wrap gap-2">
      {LANGUAGES.map((l) => (
        <Button
          key={l.code}
          variant={current === l.code ? 'default' : 'outline'}
          size="sm"
          onClick={() => i18n.changeLanguage(l.code)}
        >
          <span className="mr-2">{l.flag}</span>
          {l.label}
        </Button>
      ))}
    </div>
  );
}
