import { create } from 'zustand';

export type Theme = 'light' | 'dark' | 'system';
export type ResolvedTheme = 'light' | 'dark';

// Keep this key in sync with the inline no-flash script in index.html.
const STORAGE_KEY = 'ivc-theme';

function getStored(): Theme {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === 'light' || v === 'dark' || v === 'system') return v;
  } catch {
    /* localStorage unavailable (SSR / privacy mode) — fall through */
  }
  return 'dark'; // preserve the dashboard's original look as the default
}

function systemPrefersDark(): boolean {
  return typeof window !== 'undefined'
    && window.matchMedia('(prefers-color-scheme: dark)').matches;
}

export function resolveTheme(theme: Theme): ResolvedTheme {
  if (theme === 'system') return systemPrefersDark() ? 'dark' : 'light';
  return theme;
}

/** Reflect the resolved theme onto <html>: the `dark` class drives Tailwind,
 *  while `color-scheme` keeps native controls/scrollbars in sync. */
function applyTheme(theme: Theme): ResolvedTheme {
  const resolved = resolveTheme(theme);
  const root = document.documentElement;
  root.classList.toggle('dark', resolved === 'dark');
  root.style.colorScheme = resolved;
  return resolved;
}

interface ThemeStore {
  theme: Theme;
  resolved: ResolvedTheme;
  setTheme: (theme: Theme) => void;
}

const initial = getStored();

export const useTheme = create<ThemeStore>((set) => ({
  theme: initial,
  resolved: resolveTheme(initial),
  setTheme: (theme) => {
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      /* ignore persistence failures */
    }
    set({ theme, resolved: applyTheme(theme) });
  },
}));

// Apply once on load so the class is correct even without the inline script.
applyTheme(initial);

// While following the OS ("system"), react to live light/dark changes.
if (typeof window !== 'undefined') {
  window
    .matchMedia('(prefers-color-scheme: dark)')
    .addEventListener('change', () => {
      if (useTheme.getState().theme === 'system') {
        useTheme.setState({ resolved: applyTheme('system') });
      }
    });
}
