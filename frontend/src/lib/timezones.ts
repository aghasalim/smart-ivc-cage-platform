/**
 * Timezone helpers for the feeding-window scheduler.
 *
 * The backend stores HH:MM verbatim in whichever IANA timezone the user picks.
 * We default to Europe/Brussels because the lab lives in Belgium, but expose
 * an auto-detect option (uses the browser's resolved timezone) plus a hand-
 * picked list of common research / collaborator zones.
 */

export const DEFAULT_TIMEZONE = 'Europe/Brussels';

/** Best-effort detection of the browser's local timezone. */
export function detectBrowserTimezone(): string {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (tz && typeof tz === 'string') return tz;
  } catch {
    /* fall through */
  }
  return DEFAULT_TIMEZONE;
}

/**
 * Curated list of common timezones for the picker. Auto-detect always appears
 * first; we include the user's detected zone if it's not already in the list
 * so they can always select it without typing.
 */
export const COMMON_TIMEZONES: { tz: string; label: string }[] = [
  { tz: 'Europe/Brussels',    label: 'Brussels · Belgium (CET / CEST)' },
  { tz: 'Europe/Amsterdam',   label: 'Amsterdam · Netherlands (CET / CEST)' },
  { tz: 'Europe/Paris',       label: 'Paris · France (CET / CEST)' },
  { tz: 'Europe/Berlin',      label: 'Berlin · Germany (CET / CEST)' },
  { tz: 'Europe/London',      label: 'London · UK (GMT / BST)' },
  { tz: 'Europe/Madrid',      label: 'Madrid · Spain (CET / CEST)' },
  { tz: 'Europe/Rome',        label: 'Rome · Italy (CET / CEST)' },
  { tz: 'Europe/Istanbul',    label: 'Istanbul · Türkiye (TRT)' },
  { tz: 'Europe/Moscow',      label: 'Moscow · Russia (MSK)' },
  { tz: 'America/New_York',   label: 'New York · USA East (EST / EDT)' },
  { tz: 'America/Chicago',    label: 'Chicago · USA Central (CST / CDT)' },
  { tz: 'America/Denver',     label: 'Denver · USA Mountain (MST / MDT)' },
  { tz: 'America/Los_Angeles',label: 'Los Angeles · USA West (PST / PDT)' },
  { tz: 'America/Sao_Paulo',  label: 'São Paulo · Brazil (BRT)' },
  { tz: 'Asia/Dubai',         label: 'Dubai · UAE (GST)' },
  { tz: 'Asia/Kolkata',       label: 'Kolkata · India (IST)' },
  { tz: 'Asia/Shanghai',      label: 'Shanghai · China (CST)' },
  { tz: 'Asia/Tokyo',         label: 'Tokyo · Japan (JST)' },
  { tz: 'Asia/Singapore',     label: 'Singapore (SGT)' },
  { tz: 'Australia/Sydney',   label: 'Sydney · Australia (AEST / AEDT)' },
  { tz: 'UTC',                label: 'UTC (no DST)' },
];

/** Current HH:MM in the requested timezone. */
export function nowInTimezone(tz: string): string {
  try {
    return new Intl.DateTimeFormat('en-GB', {
      timeZone: tz,
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(new Date());
  } catch {
    return '—';
  }
}

/** Current weekday (0=Mon … 6=Sun) in the requested timezone. */
export function weekdayInTimezone(tz: string): number {
  try {
    // 'en-GB' returns "Mon", "Tue", … — order-stable across locales.
    const day = new Intl.DateTimeFormat('en-GB', { timeZone: tz, weekday: 'short' }).format(new Date());
    const map: Record<string, number> = { Mon: 0, Tue: 1, Wed: 2, Thu: 3, Fri: 4, Sat: 5, Sun: 6 };
    return map[day] ?? 0;
  } catch {
    return new Date().getDay();
  }
}

/** Parse "HH:MM" into a minute count (0–1439). Returns NaN on bad input. */
export function parseHHMM(hhmm: string): number {
  const m = /^(\d{2}):(\d{2})$/.exec(hhmm);
  if (!m) return NaN;
  const h = Number(m[1]);
  const min = Number(m[2]);
  if (h < 0 || h > 23 || min < 0 || min > 59) return NaN;
  return h * 60 + min;
}

/**
 * Describe the next feeding window in plain English: either "open now —
 * closes in 12 min" or "opens in 4 h 18 min (Tue 23:00)".
 *
 * Returns null when the window is inactive or no days are selected.
 */
export function describeNextWindow(
  start: string,
  end: string,
  days: number[],
  active: boolean,
  tz: string,
): { phase: 'open' | 'closed' | 'inactive'; minutesUntil: number; label: string } | null {
  if (!active || days.length === 0) {
    return { phase: 'inactive', minutesUntil: 0, label: 'inactive' };
  }
  const startMin = parseHHMM(start);
  const endMin = parseHHMM(end);
  if (!Number.isFinite(startMin) || !Number.isFinite(endMin)) return null;

  const nowHHMM = nowInTimezone(tz);
  const nowMin = parseHHMM(nowHHMM);
  if (!Number.isFinite(nowMin)) return null;

  const today = weekdayInTimezone(tz);
  const crossesMidnight = endMin <= startMin;

  // Are we inside the window right now?
  const openToday = days.includes(today);
  const yesterday = (today + 6) % 7;
  const openYesterdayOvernight = crossesMidnight && days.includes(yesterday);

  const insideTodayWindow = openToday && (
    crossesMidnight
      ? nowMin >= startMin || nowMin < endMin
      : nowMin >= startMin && nowMin < endMin
  );
  const insideOvernightFromYesterday = openYesterdayOvernight && nowMin < endMin && nowMin < startMin;

  if (insideTodayWindow || insideOvernightFromYesterday) {
    // Compute minutes until close.
    let closesIn: number;
    if (crossesMidnight) {
      closesIn = nowMin >= startMin ? (1440 - nowMin) + endMin : endMin - nowMin;
    } else {
      closesIn = endMin - nowMin;
    }
    return {
      phase: 'open',
      minutesUntil: closesIn,
      label: `open — closes in ${humanMins(closesIn)}`,
    };
  }

  // Look forward up to 7 days for the next opening.
  for (let offset = 0; offset < 7; offset++) {
    const day = (today + offset) % 7;
    if (!days.includes(day)) continue;
    const baseMin = offset * 1440 + startMin;
    if (baseMin <= nowMin && offset === 0) continue; // already passed today
    const delta = baseMin - nowMin;
    if (delta < 0) continue;
    return {
      phase: 'closed',
      minutesUntil: delta,
      label: `opens in ${humanMins(delta)}`,
    };
  }

  return { phase: 'inactive', minutesUntil: 0, label: 'no upcoming window' };
}

function humanMins(m: number): string {
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  const rest = m % 60;
  return rest === 0 ? `${h} h` : `${h} h ${rest} min`;
}
