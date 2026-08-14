import { lazy, Suspense, useEffect } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';

import Layout from '@/components/Layout';
import { useAuth } from '@/store/auth';

const LoginPage      = lazy(() => import('@/pages/Login'));
const OverviewPage   = lazy(() => import('@/pages/Overview'));
const CageDetailPage = lazy(() => import('@/pages/CageDetail'));
const AlertsPage     = lazy(() => import('@/pages/Alerts'));
const SchedulePage   = lazy(() => import('@/pages/Schedule'));
const ReportsPage    = lazy(() => import('@/pages/Reports'));
const SettingsPage   = lazy(() => import('@/pages/Settings'));
const SystemPage     = lazy(() => import('@/pages/System'));
const AboutPage      = lazy(() => import('@/pages/About'));
const CamerasPage    = lazy(() => import('@/pages/Cameras'));

function PageFallback() {
  return (
    <div className="min-h-[40vh] grid place-items-center">
      <div className="text-sm text-muted-foreground">Loading…</div>
    </div>
  );
}

export default function App() {
  const { user, ready, refresh } = useAuth();
  useEffect(() => {
    refresh();
  }, [refresh]);

  if (!ready) {
    return (
      <div className="min-h-screen grid place-items-center">
        <div className="text-sm text-muted-foreground">Loading…</div>
      </div>
    );
  }

  return (
    <Suspense fallback={<PageFallback />}>
      <Routes>
        <Route path="/login" element={user ? <Navigate to="/" replace /> : <LoginPage />} />
        <Route element={<Layout />}>
          <Route index element={<OverviewPage />} />
          <Route path="/cages/:cageId" element={<CageDetailPage />} />
          <Route path="/cameras" element={<CamerasPage />} />
          <Route path="/alerts" element={<AlertsPage />} />
          <Route path="/schedule" element={<SchedulePage />} />
          <Route path="/reports" element={<ReportsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/system" element={<SystemPage />} />
          <Route path="/about" element={<AboutPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </Suspense>
  );
}
