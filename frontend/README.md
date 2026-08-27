# Frontend, React 18 + Vite + Tailwind + shadcn/ui

The researcher-facing dashboard. Real-time charts, behaviour heat-map,
schedule editor, alert centre, daily summaries.

## Run locally

```bash
npm install
npm run dev
```

Then open http://localhost:5173. The dev server proxies API calls to
`VITE_API_URL` (default`http://localhost:8000`).

## Tech

- **React 18** + **TypeScript**
- **Vite** (dev server + production build)
- **Tailwind CSS** with a small custom palette tuned for dark mode
- **shadcn/ui** components (Radix primitives wrapped in our house style)
- **TanStack Query** for server state (caching, polling, mutations)
- **Recharts** for time-series charts
- **react-router** for routing
- **zustand** for tiny client-side auth state
- **sonner** for toast notifications

## Project layout

```
src/
├── main.tsx
├── App.tsx
├── index.css            ← Tailwind base + design tokens
├── lib/
│   ├── api.ts           ← typed fetch wrapper + JWT
│   ├── ws.ts            ← real-time WebSocket hook
│   └── utils.ts         ← cn(), relativeTime(), formatters
├── store/
│   └── auth.ts          ← Zustand store
├── components/
│   ├── Layout.tsx       ← sidebar + header + live indicator
│   ├── SensorChart.tsx
│   ├── BehaviourGrid.tsx
│   └── ui/              ← shadcn primitives (Button, Card, …)
└── pages/
    ├── Login.tsx
    ├── Overview.tsx
    ├── CageDetail.tsx
    ├── Alerts.tsx
    ├── Schedule.tsx
    ├── Reports.tsx
    └── Settings.tsx
```
