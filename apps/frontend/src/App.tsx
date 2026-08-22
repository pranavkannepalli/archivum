import { useEffect } from 'react';
import { BrowserRouter, Routes, Route, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { AppProvider, applyTheme, useAppState, useAppDispatch } from './store';
import { ToastProvider } from './components/ui/Toast';
import AppShell from './shell/AppShell';
import StreamSurface from './surfaces/StreamSurface';
import EverythingSurface from './surfaces/EverythingSurface';
import VisualizedSurface from './surfaces/VisualizedSurface';
import SelfSurface from './surfaces/SelfSurface';
import WikiPage from './pages/WikiPage';
import LoginPage from './pages/LoginPage';
import NotFoundPage from './pages/NotFoundPage';
import SharePage from './pages/SharePage';
import SharedWithMePage from './pages/SharedWithMePage';
import ClaimPage from './pages/ClaimPage';
import PublicWikiPage from './pages/PublicWikiPage';
import SettingsPage from './pages/SettingsPage';
import IngestPanel from './components/IngestPanel';
import SetupPage from './pages/SetupPage';
import { getOwner, listPages, refreshSession } from './api';

function ProtectedRoutes() {
  const { isAuthenticated, pagesLoaded, theme } = useAppState();
  const dispatch = useAppDispatch();
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  useEffect(() => {
    async function loadPages() {
      try {
        const pages = await listPages();
        dispatch({ type: 'SET_AUTH', value: true });
        dispatch({ type: 'SET_PAGES', pages });
      } catch {
        try {
          await refreshSession();
          const pages = await listPages();
          dispatch({ type: 'SET_AUTH', value: true });
          dispatch({ type: 'SET_PAGES', pages });
        } catch {
          dispatch({ type: 'SET_AUTH', value: false });
          navigate('/login', { replace: true });
        }
      }
    }

    loadPages();
  }, [dispatch, navigate]);

  // A vault whose person:self has never been named has nothing to show and no
  // root to hang anything off, so send the owner to setup rather than to an
  // empty stream. Once they name themselves the flag flips and this stops.
  useEffect(() => {
    if (!isAuthenticated) return;
    if (location.pathname.startsWith('/setup')) return;
    let cancelled = false;
    getOwner()
      .then((owner) => {
        if (!cancelled && owner.needs_setup) navigate('/setup', { replace: true });
      })
      .catch(() => {
        // Never block the app on this check.
      });
    return () => {
      cancelled = true;
    };
  }, [isAuthenticated, location.pathname, navigate]);

  if (!pagesLoaded && !isAuthenticated) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-sm text-muted-foreground">Opening your vault...</div>
      </div>
    );
  }

  return (
    <Routes>
      {/* Full-screen, outside the shell: setup has no vault to show yet. */}
      <Route path="/setup" element={<SetupPage />} />

      <Route element={<AppShell />}>
        <Route path="/" element={<StreamSurface />} />
        <Route path="/entries" element={<EverythingSurface />} />
        <Route path="/visualized" element={<VisualizedSurface />} />
        <Route path="/me" element={<SelfSurface />} />
        <Route path="/wiki/*" element={<WikiPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        {/* Ingest keeps its own surface: dropping a PDF in is a different job
            from writing a note, and it needs progress and history. */}
        <Route
          path="/ingest"
          element={
            <div className="surface on">
              <div className="col-wide" style={{ paddingTop: 28 }}>
                <IngestPanel />
              </div>
            </div>
          }
        />
      </Route>

      {/* The old information architecture folded into four surfaces. These
          redirects keep existing links and bookmarks working. */}
      <Route path="/library" element={<Navigate to="/entries" replace />} />
      <Route path="/search" element={<Navigate to="/entries" replace />} />
      <Route path="/topics" element={<Navigate to="/entries?kind=note" replace />} />
      <Route path="/people" element={<Navigate to="/entries?kind=person" replace />} />
      <Route path="/repos" element={<Navigate to="/entries" replace />} />
      <Route path="/sources" element={<Navigate to="/entries?kind=source" replace />} />
      <Route path="/review" element={<Navigate to="/entries?needs_review=1" replace />} />
      <Route path="/graph" element={<Navigate to="/visualized" replace />} />
      <Route path="/query" element={<Navigate to="/" replace />} />
      <Route path="/lint" element={<Navigate to="/entries?needs_review=1" replace />} />
      <Route path="/daily" element={<Navigate to="/entries?kind=daily" replace />} />
      <Route path="/projects" element={<Navigate to="/entries" replace />} />
      <Route path="/tasks" element={<Navigate to="/entries" replace />} />
      <Route path="/decisions" element={<Navigate to="/entries?kind=decision" replace />} />
      <Route path="/activity" element={<Navigate to="/" replace />} />
      <Route path="/workflows/*" element={<Navigate to="/" replace />} />
      <Route path="/tools/settings" element={<Navigate to="/settings" replace />} />
      <Route path="/tools/graph" element={<Navigate to="/visualized" replace />} />
      <Route path="/tools/memory" element={<Navigate to="/me" replace />} />
      <Route path="/tools/ingest" element={<Navigate to="/ingest" replace />} />
      <Route path="/tools/*" element={<Navigate to="/visualized" replace />} />

      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}

function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/share/:token" element={<SharePage />} />
      <Route path="/shared/view" element={<SharePage />} />
      <Route path="/shared" element={<SharedWithMePage />} />
      <Route path="/claim/:token" element={<ClaimPage />} />
      <Route path="/public" element={<PublicWikiPage />} />
      <Route path="/public/wiki/*" element={<PublicWikiPage />} />
      <Route path="/*" element={<ProtectedRoutes />} />
    </Routes>
  );
}

export default function App() {
  return (
    <ToastProvider>
      <AppProvider>
        <BrowserRouter>
          <AppRoutes />
        </BrowserRouter>
      </AppProvider>
    </ToastProvider>
  );
}
