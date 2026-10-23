import { getLogger } from '../utils/logger';
import React, { useEffect, useRef, useState } from 'react';
import axios from 'axios';
import { ArrowRightIcon, BellAlertIcon, BoltIcon } from '@heroicons/react/24/outline';
import { marketApi } from '../services/api';
import KPICard from '../components/cards/KPICard';
import ForecastPreview from '../components/cards/ForecastPreview';
import MarketPulseChart from '../components/charts/MarketPulseChart';
import ExportButton from '../components/common/ExportButton';
import Summary from './Summary';
import { formatPercentage } from '../utils/formatters';
import type { MarketOverview, StockMover } from '../types';

const logger = getLogger('pages/Dashboard');

interface DashboardProps {
  refreshKey?: number;
}

/** Map Axios/network failures to actionable dashboard empty-state copy. */
export function dashboardLoadErrorMessage(err: unknown): string {
  if (axios.isAxiosError(err)) {
    const status = err.response?.status;
    if (status === 404) {
      return (
        'No data yet. Use "Fetch New" in the header (needs a Finnhub API key set on the server), ' +
        'or run the markethelm CLI once to populate the data folder.'
      );
    }
    if (status === 502 || status === 503) {
      return (
        'Cannot reach the MarketHelm API (proxy bad gateway). ' +
        'Port 3000 is often used by another app on this machine — use http://localhost:3001 with the backend on port 8001, ' +
        'or run scripts/restart-dashboard-backend.ps1 and npm run dev on the default ports.'
      );
    }
    if (status != null && status >= 500) {
      return 'Service is temporarily unavailable. Please try again later.';
    }
    if (!err.response) {
      return 'Cannot reach the API. Start the backend (port 8000), or if you use the Vite dev server, ensure it can proxy /api.';
    }
    const data = err.response.data as { detail?: unknown } | undefined;
    const detail = data?.detail;
    if (typeof detail === 'string') {
      return `Could not load dashboard: ${detail}`;
    }
  }
  return 'Service is temporarily unavailable. Please try again later.';
}

const Dashboard: React.FC<DashboardProps> = ({ refreshKey = 0 }) => {
  const [marketOverview, setMarketOverview] = useState<MarketOverview | null>(null);
  const [gainers, setGainers] = useState<StockMover[]>([]);
  const [losers, setLosers] = useState<StockMover[]>([]);
  const [loading, setLoading] = useState(true);
  const [secondaryLoading, setSecondaryLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [secondaryError, setSecondaryError] = useState<string | null>(null);
  const dashboardRef = useRef<HTMLDivElement>(null);
  const isInitialMount = useRef(true);
  /** Bumped on each load / unmount so late phase-1/phase-2 responses are ignored. */
  const loadGenerationRef = useRef(0);

  useEffect(() => {
    return () => {
      loadGenerationRef.current += 1;
    };
  }, []);

  useEffect(() => {
    void fetchDashboardData(false);
  }, []);

  useEffect(() => {
    if (isInitialMount.current) return;
    void fetchDashboardData(true);
  }, [refreshKey]);

  const fetchDashboardData = async (silent = false) => {
    const generation = ++loadGenerationRef.current;
    if (!silent) {
      setLoading(true);
    }
    setError(null);
    setSecondaryError(null);

    try {
      // Phase 1: core data for fast initial render
      const marketRes = await marketApi.getOverview();

      if (generation !== loadGenerationRef.current) return;

      setMarketOverview(marketRes.data);

      // Phase 2: secondary data loads in background
      if (!silent) setSecondaryLoading(true);
      try {
        const [gainersRes, losersRes] = await Promise.all([
          marketApi.getMovers('gainers', 10),
          marketApi.getMovers('losers', 10),
        ]);

        if (generation !== loadGenerationRef.current) return;

        setGainers(gainersRes.data.data);
        setLosers(losersRes.data.data);
      } catch (secondaryErr) {
        if (generation !== loadGenerationRef.current) return;
        logger.error('Error fetching secondary data:', secondaryErr);
        if (!silent) setSecondaryError('Some sections failed to load. You can retry.');
      } finally {
        if (generation === loadGenerationRef.current && !silent) {
          setSecondaryLoading(false);
        }
      }
    } catch (err) {
      if (generation !== loadGenerationRef.current) return;
      logger.error('Error fetching dashboard data:', err);
      setError(dashboardLoadErrorMessage(err));
    } finally {
      if (generation === loadGenerationRef.current) {
        setLoading(false);
        if (!silent) isInitialMount.current = false;
      }
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[50vh]">
        <div className="text-center">
          <div className="animate-spin rounded-full h-16 w-16 border-b-2 border-blue-500 mx-auto"></div>
          <p className="mt-4 text-slate-600 dark:text-slate-400">Loading dashboard...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 text-red-700 dark:text-red-400 px-4 py-3 rounded">
          {error}
          <button
            onClick={() => fetchDashboardData(false)}
            className="ml-4 rounded px-2 py-1 font-bold hover:bg-red-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500 dark:hover:bg-red-900/40"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  const averageChange = marketOverview?.averageChange ?? 0;

  return (
    <main ref={dashboardRef} className="mx-auto max-w-[1500px] px-4 py-6 sm:px-6 xl:px-8">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="mb-2 flex items-center gap-2 text-xs font-bold uppercase tracking-[0.16em] text-emerald-800 dark:text-emerald-300">
            <BoltIcon className="h-4 w-4" />
            Market snapshot
          </div>
          <h1 className="text-3xl font-extrabold tracking-[-0.04em] text-slate-950 dark:text-white">
            Market overview
          </h1>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
            Price moves from the latest saved market analysis.
          </p>
        </div>
        <ExportButton
          stocks={[]}
          captureRef={dashboardRef}
          formats={['png', 'pdf']}
          label="Dashboard"
        />
      </div>

      <Summary embedded refreshKey={refreshKey} />

      <section
        className="card mt-5 overflow-hidden p-5 sm:p-6"
        aria-labelledby="market-pulse-title"
      >
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h2
              id="market-pulse-title"
              className="text-lg font-extrabold text-slate-950 dark:text-white"
            >
              Market pulse
            </h2>
            <p className="mt-1 text-xs text-slate-600 dark:text-slate-400">
              Stocks with the largest price moves.
            </p>
          </div>
        </div>

        <div className="mt-3">
          {secondaryLoading ? (
            <div className="h-[270px] animate-pulse rounded-lg bg-slate-100 dark:bg-[#122235]" />
          ) : (
            <MarketPulseChart gainers={gainers} losers={losers} />
          )}
        </div>

        <div className="grid grid-cols-2 gap-4 border-t border-slate-200 pt-5 md:grid-cols-4 dark:border-[#26384d]">
          <KPICard
            title="Stocks covered"
            value={marketOverview?.totalStocks || 0}
            subtitle="In this market snapshot"
          />
          <KPICard
            title="Gainers"
            value={marketOverview?.gainers || 0}
            subtitle={`Largest gain ${formatPercentage(marketOverview?.maxChange ?? 0)}`}
          />
          <KPICard
            title="Losers"
            value={marketOverview?.losers || 0}
            subtitle={`Largest drop ${formatPercentage(marketOverview?.minChange ?? 0)}`}
          />
          <KPICard
            title="Average daily change"
            value={formatPercentage(averageChange)}
            subtitle={`${marketOverview?.unchanged || 0} unchanged`}
            trend={averageChange > 0 ? 'up' : averageChange < 0 ? 'down' : 'neutral'}
          />
        </div>
      </section>

      {marketOverview?.date && (
        <ForecastPreview date={marketOverview.date} refreshKey={refreshKey} />
      )}

      <section className="mt-5 flex flex-col gap-4 rounded-xl border border-slate-300 bg-[#f3f6f9] px-5 py-4 sm:flex-row sm:items-center sm:justify-between dark:border-[#223248] dark:bg-[#0e1b2a]">
        <div className="flex items-center gap-3">
          <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-emerald-400/10 text-emerald-500">
            <BellAlertIcon className="h-5 w-5" />
          </span>
          <div>
            <h2 className="text-sm font-extrabold text-slate-950 dark:text-white">Helmtower</h2>
            <p className="text-xs text-slate-600 dark:text-slate-400">
              Keep watch on price and RSI conditions while you are away.
            </p>
          </div>
        </div>
        <a
          href="/alerts"
          className="inline-flex items-center gap-2 text-sm font-bold text-emerald-800 hover:text-emerald-700 dark:text-emerald-400 dark:hover:text-emerald-300"
        >
          Manage alerts <ArrowRightIcon className="h-4 w-4" />
        </a>
      </section>

      {secondaryError && (
        <div className="mt-5 rounded-lg border border-amber-400/30 bg-amber-400/10 px-4 py-3 text-sm text-amber-700 dark:text-amber-300">
          {secondaryError}
          <button
            onClick={() => fetchDashboardData(false)}
            className="ml-3 rounded px-2 py-1 font-bold hover:bg-amber-500/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500"
          >
            Retry
          </button>
        </div>
      )}
    </main>
  );
};

export default Dashboard;
