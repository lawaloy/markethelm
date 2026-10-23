import { getLogger } from '../../utils/logger';
import React, { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router';
import { ArrowPathIcon, SunIcon, MoonIcon } from '@heroicons/react/24/outline';
import { useTheme } from '../../contexts/ThemeContext';
import { useAuth } from '../../contexts/AuthContext';
import api from '../../services/api';

const logger = getLogger('components/layout/Header');

interface HeaderProps {
  onRefreshComplete?: () => void;
  onQuickRefresh?: () => void;
  /** True when app is fetching latest data in background (no user action) */
  backgroundFetching?: boolean;
}

const Header: React.FC<HeaderProps> = ({
  onRefreshComplete,
  onQuickRefresh,
  backgroundFetching,
}) => {
  const { theme, toggleTheme } = useTheme();
  const { user, multiUserEnabled, logout } = useAuth();
  const [isRefreshing, setIsRefreshingState] = useState(false);
  const [refreshMessage, setRefreshMessage] = useState('');
  const [logoutError, setLogoutError] = useState('');
  const [isLoggingOut, setIsLoggingOut] = useState(false);
  const pollIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  /** Bumped on cancel / new fetch so in-flight status responses cannot finish after leave. */
  const pollGenerationRef = useRef(0);
  /** Mirrors isRefreshing for Quick Reload timeouts (state closures go stale). */
  const isRefreshingRef = useRef(false);
  const lastMessageRef = useRef<string>('');

  const setRefreshing = (value: boolean) => {
    isRefreshingRef.current = value;
    setIsRefreshingState(value);
  };

  useEffect(() => {
    return () => {
      pollGenerationRef.current += 1;
      if (pollIntervalRef.current) {
        clearInterval(pollIntervalRef.current);
        pollIntervalRef.current = null;
      }
    };
  }, []);

  const updateMessage = (message: string) => {
    if (lastMessageRef.current === message) return;
    lastMessageRef.current = message;
    setRefreshMessage(message);
  };

  const handleQuickRefresh = () => {
    const generation = pollGenerationRef.current;
    updateMessage('Reloading data...');
    onQuickRefresh?.();
    setTimeout(() => {
      // Generation guards unmount / newer Full refresh; ref avoids stale isRefreshing.
      if (generation !== pollGenerationRef.current) return;
      if (!isRefreshingRef.current) {
        updateMessage('');
      }
    }, 1000);
  };

  const handleFullRefresh = async () => {
    const generation = ++pollGenerationRef.current;
    setRefreshing(true);
    updateMessage('Reloading latest saved data...');
    onQuickRefresh?.();

    try {
      // Trigger refresh
      const response = await api.post('/api/refresh');
      if (generation !== pollGenerationRef.current) return;
      updateMessage(response.data.message);

      const pollMs = 2000;
      const maxWaitMs = 15 * 60 * 1000;
      const pollStarted = Date.now();

      const finishPolling = () => {
        if (pollIntervalRef.current) {
          clearInterval(pollIntervalRef.current);
          pollIntervalRef.current = null;
        }
      };

      // Poll for status
      pollIntervalRef.current = setInterval(async () => {
        try {
          if (generation !== pollGenerationRef.current) {
            finishPolling();
            return;
          }
          if (Date.now() - pollStarted > maxWaitMs) {
            finishPolling();
            setRefreshing(false);
            updateMessage('Refresh is taking too long. Check server logs or try Cancel.');
            setTimeout(() => {
              if (generation === pollGenerationRef.current) updateMessage('');
            }, 8000);
            return;
          }

          const statusRes = await api.get('/api/refresh/status');
          if (generation !== pollGenerationRef.current) return;
          const status = statusRes.data;

          if (status.progress) {
            updateMessage(status.progress);
          }

          if (!status.is_running) {
            finishPolling();
            setRefreshing(false);

            if (status.last_status === 'success') {
              updateMessage('Data refreshed successfully!');
              setTimeout(() => {
                if (generation !== pollGenerationRef.current) return;
                updateMessage('');
                onRefreshComplete?.();
              }, 2000);
            } else if (status.last_status === 'idle') {
              updateMessage('');
            } else {
              updateMessage('Refresh failed. Please try again.');
              setTimeout(() => {
                if (generation === pollGenerationRef.current) updateMessage('');
              }, 5000);
            }
          }
        } catch (err) {
          logger.error('Status poll error:', err);
        }
      }, pollMs);
    } catch (error) {
      logger.error('Refresh error:', error);
      if (generation !== pollGenerationRef.current) return;
      updateMessage('Failed to start refresh');
      setRefreshing(false);
      setTimeout(() => {
        if (generation === pollGenerationRef.current) updateMessage('');
      }, 5000);
    }
  };

  const handleCancelRefresh = async () => {
    // Invalidate any in-flight /api/refresh/status before awaiting cancel so a late
    // success cannot call onRefreshComplete after the user cancelled.
    const generation = ++pollGenerationRef.current;
    updateMessage('Cancelling refresh...');
    try {
      await api.post('/api/refresh/cancel');
      if (generation !== pollGenerationRef.current) return;
      if (pollIntervalRef.current) {
        clearInterval(pollIntervalRef.current);
        pollIntervalRef.current = null;
      }
      setRefreshing(false);
      updateMessage('Refresh cancelled.');
      setTimeout(() => {
        if (generation === pollGenerationRef.current) updateMessage('');
      }, 3000);
    } catch (error) {
      if (generation !== pollGenerationRef.current) return;
      logger.error('Cancel refresh error:', error);
      updateMessage('Failed to cancel refresh.');
      setTimeout(() => {
        if (generation === pollGenerationRef.current) updateMessage('');
      }, 5000);
    }
  };

  const handleLogout = async () => {
    if (isLoggingOut) return;
    setIsLoggingOut(true);
    setLogoutError('');
    try {
      await logout();
    } catch {
      setLogoutError('Sign out failed. You are still signed in; please try again.');
    } finally {
      setIsLoggingOut(false);
    }
  };

  return (
    <header className="sticky top-0 z-20 h-20 border-b border-slate-300 bg-[#f3f6f9]/95 backdrop-blur dark:border-[#223248] dark:bg-[#0b1725]/95">
      <div className="h-full px-4 sm:px-6 xl:px-8">
        <div className="flex h-full items-center justify-between gap-4">
          <div className="flex min-w-0 items-center gap-3 lg:gap-4">
            <h1 className="sr-only">MarketHelm</h1>
          </div>
          <div className="flex items-center gap-2">
            <div
              className={`hidden max-w-xs truncate text-xs text-slate-600 transition-opacity duration-200 md:block dark:text-slate-400 ${
                refreshMessage || backgroundFetching ? 'opacity-100' : 'opacity-0'
              }`}
            >
              {refreshMessage || (backgroundFetching ? 'Updating data...' : 'Status')}
            </div>
            <button
              onClick={handleQuickRefresh}
              className="inline-flex items-center gap-2 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-semibold text-slate-700 transition hover:border-slate-400 hover:bg-slate-50 dark:border-[#31435b] dark:bg-[#101f30] dark:text-slate-200 dark:hover:border-[#455c78] dark:hover:bg-[#15283d]"
              title="Reload data from files (instant)"
            >
              <ArrowPathIcon className="h-4 w-4" />
              <span className="hidden sm:inline">Reload</span>
            </button>
            <button
              onClick={handleFullRefresh}
              disabled={isRefreshing}
              className={`inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-bold transition ${
                isRefreshing
                  ? 'cursor-not-allowed bg-slate-200 text-slate-500 dark:bg-[#1b2a3c] dark:text-slate-500'
                  : 'bg-emerald-500 text-[#06140f] shadow-[0_8px_24px_rgba(16,185,129,0.16)] hover:bg-emerald-400'
              }`}
              title="Reload saved data instantly and fetch fresh data in background"
            >
              <ArrowPathIcon className={`h-4 w-4 ${isRefreshing ? 'animate-spin' : ''}`} />
              <span className="hidden sm:inline">{isRefreshing ? 'Fetching...' : 'Fetch New'}</span>
            </button>
            {isRefreshing && (
              <button
                onClick={handleCancelRefresh}
                className="inline-flex items-center gap-2 rounded-lg border border-red-400/40 bg-red-500/10 px-4 py-2 text-sm font-semibold text-red-500 transition hover:bg-red-500/20 dark:text-red-300"
                title="Cancel the current refresh job"
              >
                <span>Cancel</span>
              </button>
            )}
            <button
              onClick={toggleTheme}
              className="rounded-lg p-2 text-slate-500 transition hover:bg-slate-100 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-[#15283d] dark:hover:text-white"
              title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
              aria-label="Toggle theme"
            >
              {theme === 'dark' ? (
                <SunIcon className="h-5 w-5" />
              ) : (
                <MoonIcon className="h-5 w-5" />
              )}
            </button>
            {multiUserEnabled &&
              (user ? (
                <div className="flex items-center gap-2">
                  {logoutError && (
                    <span className="max-w-xs text-sm text-red-600 dark:text-red-400" role="alert">
                      {logoutError}
                    </span>
                  )}
                  <span
                    className="hidden max-w-[10rem] truncate text-sm text-slate-600 dark:text-slate-400 sm:inline"
                    title={user.email}
                  >
                    {user.email}
                  </span>
                  <Link
                    to="/account"
                    className="rounded-md px-3 py-2 text-sm font-medium text-slate-600 transition hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-700"
                  >
                    Account
                  </Link>
                  <button
                    type="button"
                    onClick={() => void handleLogout()}
                    disabled={isLoggingOut}
                    className="rounded-md px-3 py-2 text-sm font-medium text-slate-600 transition hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-700"
                  >
                    {isLoggingOut ? 'Signing out...' : 'Sign out'}
                  </button>
                </div>
              ) : (
                <Link
                  to="/sign-in?return=%2Falerts"
                  className="rounded-md px-3 py-2 text-sm font-medium text-teal-700 transition hover:bg-teal-50 dark:text-teal-400 dark:hover:bg-slate-700"
                >
                  Sign in
                </Link>
              ))}
          </div>
        </div>
      </div>
    </header>
  );
};

export default Header;
