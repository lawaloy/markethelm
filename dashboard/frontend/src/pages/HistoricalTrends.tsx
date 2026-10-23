import { getLogger } from '../utils/logger';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Listbox, ListboxButton, ListboxOption, ListboxOptions } from '@headlessui/react';
import { CheckIcon, ChevronDownIcon, ChevronUpIcon } from '@heroicons/react/20/solid';
import { ChartBarIcon, ClockIcon, DocumentChartBarIcon } from '@heroicons/react/24/outline';
import { historyApi, stocksApi } from '../services/api';
import LatestForecasts from '../components/tables/LatestForecasts';
import { formatDate, formatPercentage, formatPrice, getCompanyName } from '../utils/formatters';
import type {
  DailySummaryPoint,
  HistoricalPoint,
  ProjectionAccuracyResponse,
  RunProjection,
  RunProjectionsResponse,
} from '../types';

const logger = getLogger('pages/HistoricalTrends');

const DAY_OPTIONS = [7, 14, 30, 90];

interface HistoricalTrendsProps {
  refreshKey?: number;
}

function percent(value: number | null): string {
  return value == null ? '—' : `${value.toFixed(2)}%`;
}

function valueTone(value: number): string {
  if (value > 0) return 'text-emerald-700 dark:text-emerald-300';
  if (value < 0) return 'text-rose-700 dark:text-rose-300';
  return 'text-slate-700 dark:text-slate-200';
}

function Metric({
  label,
  value,
  detail,
  tone = 'text-slate-950 dark:text-white',
  onClick,
  selected = false,
}: {
  label: string;
  value: string | number;
  detail?: string;
  tone?: string;
  onClick?: () => void;
  selected?: boolean;
}) {
  const content = (
    <>
      <span className={`font-data block text-xl font-bold tabular-nums ${tone}`}>{value}</span>
      {detail && (
        <span className="mt-1 block text-xs text-slate-600 dark:text-slate-400">{detail}</span>
      )}
    </>
  );
  return (
    <div className="min-w-0">
      <dt className="text-xs font-semibold uppercase tracking-[0.12em] text-slate-600 dark:text-slate-400">
        {label}
      </dt>
      <dd className="mt-1">
        {onClick ? (
          <button
            type="button"
            onClick={onClick}
            aria-label={`View ${label.toLowerCase()} details: ${value}`}
            aria-pressed={selected}
            className={`-ml-2 min-h-11 w-full rounded-lg px-2 py-1 text-left transition-colors hover:bg-emerald-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 dark:hover:bg-emerald-950/40 ${selected ? 'bg-emerald-50 ring-1 ring-emerald-300 dark:bg-emerald-950/40 dark:ring-emerald-700' : ''}`}
          >
            {content}
            <span className="mt-1 block text-[11px] font-semibold text-emerald-800 dark:text-emerald-300">
              View details
            </span>
          </button>
        ) : (
          content
        )}
      </dd>
    </div>
  );
}

function AccuracyDetails({ accuracy }: { accuracy: ProjectionAccuracyResponse | null }) {
  const [selectedMetric, setSelectedMetric] = useState<string | null>(null);
  if (!accuracy) {
    return (
      <p className="text-sm text-slate-600 dark:text-slate-400">
        No completed projection scores are available for this range yet.
      </p>
    );
  }

  const { summary, samples, samplesTruncated } = accuracy;
  const recommendationRows = Object.entries(summary.byRecommendation);
  const confidenceRows = Object.entries(summary.byConfidenceBand);
  const explanations: Record<string, string> = {
    'Scored projections': `${summary.sampleCount} forecasts have a recorded closing price on their target date. ${summary.pendingCount} are still pending and ${summary.missingActualCount} have no closing price for that date.`,
    'Directional accuracy':
      'The share of scored projections that correctly predicted whether the stock would rise or fall.',
    'Target-band coverage':
      'The share of actual closes that landed inside the projected low-to-high target range.',
    'Evaluation coverage': `${summary.sampleCount} of ${summary.validProjectionCount} valid projections have been scored in this range.`,
    'Mean absolute error':
      'The average absolute percentage difference between the target price and the actual close. Lower is better.',
    'Median absolute error':
      'The middle absolute percentage error across scored projections. Lower is better.',
    'Mean confidence': 'The average model confidence of projections that have been scored.',
    'Calibration gap':
      'The difference between average confidence and directional accuracy. A value near zero is better aligned.',
  };

  return (
    <div className="space-y-5">
      <div>
        <h4 className="font-bold text-slate-950 dark:text-white">Validation for this range</h4>
        <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
          Forecast results for the selected period, using the closing price on each target date.
        </p>
      </div>

      <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Metric
          label="Scored projections"
          value={summary.sampleCount}
          detail={`${summary.pendingCount} pending · ${summary.missingActualCount} missing actual`}
          onClick={() => setSelectedMetric('Scored projections')}
          selected={selectedMetric === 'Scored projections'}
        />
        {[
          ['Directional accuracy', summary.directionalAccuracyPct],
          ['Target-band coverage', summary.bandCoveragePct],
          ['Evaluation coverage', summary.evaluationCoveragePct],
          ['Mean absolute error', summary.meanAbsErrorPct],
          ['Median absolute error', summary.medianAbsErrorPct],
          ['Mean confidence', summary.meanConfidence],
          ['Calibration gap', summary.calibrationGapPct],
        ].map(([label, value]) => (
          <Metric
            key={String(label)}
            label={String(label)}
            value={percent(value as number | null)}
            onClick={() => setSelectedMetric(String(label))}
            selected={selectedMetric === label}
          />
        ))}
      </dl>

      {selectedMetric && (
        <div
          role="region"
          aria-label={`${selectedMetric} details`}
          className="rounded-xl border border-emerald-200 bg-emerald-50/70 p-4 dark:border-emerald-900 dark:bg-emerald-950/20"
        >
          <h5 className="font-bold text-slate-950 dark:text-white">{selectedMetric}</h5>
          <p className="mt-1 text-sm leading-6 text-slate-700 dark:text-slate-300">
            {explanations[selectedMetric]}
          </p>
          {summary.sampleCount === 0 && selectedMetric !== 'Scored projections' && (
            <p className="mt-2 text-sm text-slate-600 dark:text-slate-400">
              No scored projections are available yet, so this metric has no result.
            </p>
          )}
          {summary.sampleCount > 0 && (
            <p className="mt-2 text-sm text-slate-600 dark:text-slate-400">
              Open Recent scores below to inspect available examples.
            </p>
          )}
        </div>
      )}

      {(recommendationRows.length > 0 || confidenceRows.length > 0) && (
        <details className="group rounded-xl border border-slate-200 bg-white/70 dark:border-slate-700 dark:bg-slate-950/25">
          <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-4 px-4 py-3 font-semibold text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 dark:text-slate-100">
            Confidence calibration by cohort
            <ChevronDownIcon className="h-5 w-5 group-open:rotate-180" aria-hidden="true" />
          </summary>
          <div className="grid gap-5 border-t border-slate-200 p-4 dark:border-slate-700 lg:grid-cols-2">
            {[
              ['Recommendation', recommendationRows],
              ['Confidence band', confidenceRows],
            ].map(([heading, rows]) => (
              <div key={String(heading)} className="min-w-0 overflow-x-auto">
                <h5 className="mb-2 text-sm font-bold text-slate-900 dark:text-slate-100">
                  {String(heading)}
                </h5>
                <table className="w-full min-w-96 text-left text-sm">
                  <thead className="text-xs uppercase tracking-wide text-slate-600 dark:text-slate-400">
                    <tr>
                      <th className="py-2 pr-3">Cohort</th>
                      <th className="py-2 pr-3">Count</th>
                      <th className="py-2 pr-3">Accuracy</th>
                      <th className="py-2">Gap</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(rows as typeof recommendationRows).map(([name, row]) => (
                      <tr key={name} className="border-t border-slate-200 dark:border-slate-700">
                        <td className="py-2 pr-3 font-medium">{name}</td>
                        <td className="py-2 pr-3">{row.count}</td>
                        <td className="py-2 pr-3">{percent(row.directionalAccuracyPct)}</td>
                        <td className="py-2">{percent(row.calibrationGapPct)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
          </div>
        </details>
      )}

      {samples.length > 0 && (
        <details className="group rounded-xl border border-slate-200 bg-white/70 dark:border-slate-700 dark:bg-slate-950/25">
          <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-4 px-4 py-3 font-semibold text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 dark:text-slate-100">
            Recent scores (newest {samples.length} of {summary.sampleCount})
            <ChevronDownIcon className="h-5 w-5 group-open:rotate-180" aria-hidden="true" />
          </summary>
          <div className="overflow-x-auto border-t border-slate-200 p-4 dark:border-slate-700">
            <table className="w-full min-w-[680px] text-left text-sm">
              <thead className="text-xs uppercase tracking-wide text-slate-600 dark:text-slate-400">
                <tr>
                  <th className="py-2 pr-4">Symbol</th>
                  <th className="py-2 pr-4">Forecast date → result date</th>
                  <th className="py-2 pr-4">Target / actual</th>
                  <th className="py-2 pr-4">Direction</th>
                  <th className="py-2 pr-4">Band</th>
                  <th className="py-2">Call</th>
                </tr>
              </thead>
              <tbody>
                {samples.map((row) => (
                  <tr
                    key={`${row.symbol}-${row.runDate}-${row.targetDate}`}
                    className="border-t border-slate-200 dark:border-slate-700"
                  >
                    <td className="py-2 pr-4 font-bold">{row.symbol}</td>
                    <td className="py-2 pr-4">
                      {formatDate(row.runDate)} → {formatDate(row.actualDate)}
                    </td>
                    <td className="py-2 pr-4">
                      {formatPrice(row.predicted)} / {formatPrice(row.actual)}
                    </td>
                    <td className="py-2 pr-4">
                      {row.directionCorrect == null
                        ? '—'
                        : row.directionCorrect
                          ? 'Correct'
                          : 'Miss'}
                    </td>
                    <td className="py-2 pr-4">
                      {row.bandHit == null ? '—' : row.bandHit ? 'Hit' : 'Miss'}
                    </td>
                    <td className="py-2">{row.recommendation}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {samplesTruncated && (
              <p className="mt-3 text-xs text-slate-600 dark:text-slate-400">
                Showing the newest available samples.
              </p>
            )}
          </div>
        </details>
      )}
    </div>
  );
}

type RunLens =
  | 'companies'
  | 'confidence'
  | 'move'
  | 'signal'
  | 'STRONG BUY'
  | 'BUY'
  | 'HOLD'
  | 'SELL'
  | 'STRONG SELL';

const RECOMMENDATIONS: RunLens[] = ['STRONG BUY', 'BUY', 'HOLD', 'SELL', 'STRONG SELL'];

function RunProjectionExplorer({
  date,
  lens,
  onSelectCompany,
}: {
  date: string;
  lens: RunLens;
  onSelectCompany: (symbol: string, date: string) => void;
}) {
  const [run, setRun] = useState<RunProjectionsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [retryKey, setRetryKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(false);
    void historyApi
      .getRunProjections(date)
      .then((response) => {
        if (!cancelled) setRun(response.data);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [date, retryKey]);

  const rows = useMemo(() => {
    if (!run) return [];
    const filtered = RECOMMENDATIONS.includes(lens)
      ? run.projections.filter((row) => row.recommendation === lens)
      : [...run.projections];
    if (lens === 'confidence') {
      filtered.sort((a, b) => (b.confidence ?? -Infinity) - (a.confidence ?? -Infinity));
    } else if (lens === 'move') {
      filtered.sort((a, b) => (b.expectedChange ?? -Infinity) - (a.expectedChange ?? -Infinity));
    } else {
      filtered.sort((a, b) => a.symbol.localeCompare(b.symbol));
    }
    return filtered;
  }, [run, lens]);

  const title =
    lens === 'confidence'
      ? 'Confidence by company'
      : lens === 'move'
        ? 'Expected move by company'
        : lens === 'signal'
          ? 'Recommendations by company'
          : RECOMMENDATIONS.includes(lens)
            ? `${lens.toLowerCase()} calls`
            : 'Companies on this date';
  const description =
    lens === 'confidence'
      ? 'Sorted from highest confidence. The figure is the average of available company scores.'
      : lens === 'move'
        ? 'Sorted from highest expected move. The figure is the average of available company moves.'
        : lens === 'signal'
          ? 'Every saved recommendation behind the market signal.'
          : RECOMMENDATIONS.includes(lens)
            ? `Companies rated ${lens.toLowerCase()} on this date.`
            : 'Saved forecasts behind the company count.';

  return (
    <section
      id={`run-details-${date}`}
      tabIndex={-1}
      aria-label={`${title} for ${formatDate(date)}`}
      className="border-t border-slate-200 bg-slate-50/80 px-5 py-5 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 dark:border-slate-700 dark:bg-slate-950/30 sm:px-6"
    >
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h4 className="text-base font-extrabold text-slate-950 dark:text-white">{title}</h4>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">{description}</p>
        </div>
        {!loading && !error && (
          <span className="rounded-full bg-slate-200 px-3 py-1 text-xs font-bold text-slate-800 dark:bg-slate-700 dark:text-slate-100">
            {rows.length} {rows.length === 1 ? 'company' : 'companies'}
          </span>
        )}
      </div>
      {loading ? (
        <p role="status" className="py-5 text-sm text-slate-600 dark:text-slate-300">
          Loading saved projections…
        </p>
      ) : error ? (
        <div
          role="alert"
          className="rounded-xl border border-amber-300 bg-amber-50 p-4 text-sm text-amber-950 dark:border-amber-700 dark:bg-amber-950/30 dark:text-amber-100"
        >
          <p>These market details could not be loaded.</p>
          <button
            type="button"
            className="mt-2 min-h-11 rounded px-2 font-bold hover:bg-emerald-500/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
            onClick={() => setRetryKey((value) => value + 1)}
          >
            Try again
          </button>
        </div>
      ) : rows.length === 0 ? (
        <p className="rounded-xl border border-slate-200 bg-white p-4 text-sm text-slate-700 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300">
          {RECOMMENDATIONS.includes(lens)
            ? 'No companies match this recommendation on this date.'
            : 'No saved company details are available for this date.'}
        </p>
      ) : (
        <>
          {run && run.projections.length < run.totalProjections && (
            <p className="mb-3 text-xs text-slate-600 dark:text-slate-400">
              Showing {run.projections.length} saved rows with valid company symbols out of{' '}
              {run.totalProjections} projections.
            </p>
          )}
          <ul className="grid gap-2">
            {rows.map((row: RunProjection, index) => (
              <li
                key={`${row.symbol}-${index}`}
                className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-[#101f30]"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p
                      className="truncate font-bold text-slate-950 dark:text-white"
                      title={row.name}
                    >
                      {row.name}{' '}
                      <span className="font-data text-sm font-medium text-slate-600 dark:text-slate-300">
                        ({row.symbol})
                      </span>
                    </p>
                    <p className="mt-1 text-xs text-slate-600 dark:text-slate-400">
                      {row.recommendation} · {row.risk} risk
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() => onSelectCompany(row.symbol, date)}
                    className="min-h-11 rounded-lg border border-slate-300 px-3 py-2 text-xs font-bold text-emerald-800 hover:bg-emerald-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 dark:border-slate-600 dark:text-emerald-300 dark:hover:bg-emerald-950/40"
                  >
                    View history
                  </button>
                </div>
                <dl className="mt-3 grid grid-cols-2 gap-3 border-t border-slate-200 pt-3 dark:border-slate-700 sm:grid-cols-4">
                  <div>
                    <dt className="text-xs text-slate-600 dark:text-slate-400">Close</dt>
                    <dd className="font-data mt-1 font-semibold text-slate-950 dark:text-white">
                      {row.currentPrice == null ? '—' : formatPrice(row.currentPrice)}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-xs text-slate-600 dark:text-slate-400">Target</dt>
                    <dd className="font-data mt-1 font-semibold text-slate-950 dark:text-white">
                      {row.targetPrice == null ? '—' : formatPrice(row.targetPrice)}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-xs text-slate-600 dark:text-slate-400">Confidence</dt>
                    <dd className="font-data mt-1 font-semibold text-slate-950 dark:text-white">
                      {row.confidence == null ? '—' : `${row.confidence.toFixed(1)}%`}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-xs text-slate-600 dark:text-slate-400">Expected move</dt>
                    <dd
                      className={`font-data mt-1 font-semibold ${row.expectedChange == null ? 'text-slate-950 dark:text-white' : valueTone(row.expectedChange)}`}
                    >
                      {row.expectedChange == null ? '—' : formatPercentage(row.expectedChange)}
                    </dd>
                  </div>
                </dl>
                {row.reason && (
                  <p className="mt-3 text-xs leading-5 text-slate-600 dark:text-slate-300">
                    {row.reason}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

function MarketRun({
  point,
  previous,
  latest,
  accuracy,
  expanded,
  onToggle,
  activeLens,
  onSelectLens,
  onSelectCompany,
}: {
  point: DailySummaryPoint;
  previous?: DailySummaryPoint;
  latest: boolean;
  accuracy: ProjectionAccuracyResponse | null;
  expanded: boolean;
  onToggle: () => void;
  activeLens: RunLens | null;
  onSelectLens: (lens: RunLens) => void;
  onSelectCompany: (symbol: string, date: string) => void;
}) {
  const confidenceDelta = previous ? point.averageConfidence - previous.averageConfidence : null;
  const moveDelta = previous ? point.expectedMarketMove - previous.expectedMarketMove : null;
  const recommendationCount =
    point.strongBuy + point.buy + point.hold + point.sell + point.strongSell;

  return (
    <article className="relative pl-10 sm:pl-14">
      <span
        className={`absolute left-[7px] top-7 h-3.5 w-3.5 rounded-full border-[3px] sm:left-[15px] ${latest ? 'border-emerald-200 bg-emerald-600 ring-4 ring-emerald-100 dark:border-emerald-950 dark:bg-emerald-300 dark:ring-emerald-900/50' : 'border-slate-200 bg-slate-500 dark:border-slate-800 dark:bg-slate-400'}`}
        aria-hidden="true"
      />
      <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-[#101f30]">
        <div className="flex flex-wrap items-center justify-between gap-3 px-5 py-4 sm:px-6">
          <div>
            <button
              type="button"
              onClick={onToggle}
              aria-expanded={expanded}
              className="flex min-h-11 items-center gap-2 rounded-lg text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
            >
              <span className="text-base font-extrabold text-slate-950 dark:text-white">
                {formatDate(point.date)}
              </span>
              {latest && (
                <span className="rounded-full bg-emerald-100 px-2.5 py-1 text-[11px] font-bold uppercase tracking-[0.12em] text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200">
                  Most recent
                </span>
              )}
              {expanded ? (
                <ChevronUpIcon className="h-5 w-5 text-slate-500" aria-hidden="true" />
              ) : (
                <ChevronDownIcon className="h-5 w-5 text-slate-500" aria-hidden="true" />
              )}
            </button>
            <p className="mt-0.5 text-sm text-slate-600 dark:text-slate-400">
              Market projection snapshot
            </p>
          </div>
          <button
            type="button"
            onClick={() => onSelectLens('companies')}
            aria-pressed={activeLens === 'companies'}
            className="min-h-11 rounded-lg border border-slate-300 px-3 py-2 text-sm font-bold text-emerald-800 hover:bg-emerald-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 dark:border-slate-600 dark:text-emerald-300 dark:hover:bg-emerald-950/40"
          >
            {point.totalProjections || recommendationCount} stocks · View companies
          </button>
        </div>

        <dl className="grid gap-5 border-t border-slate-200 px-5 py-5 dark:border-slate-700 sm:grid-cols-3 sm:px-6">
          <Metric
            label="Confidence"
            value={`${point.averageConfidence.toFixed(1)}%`}
            detail={
              confidenceDelta == null
                ? 'First day in this range'
                : `${formatPercentage(confidenceDelta, 1)} vs previous`
            }
            onClick={() => onSelectLens('confidence')}
            selected={activeLens === 'confidence'}
          />
          <Metric
            label="Expected move"
            value={formatPercentage(point.expectedMarketMove)}
            detail={
              moveDelta == null
                ? 'First day in this range'
                : `${formatPercentage(moveDelta, 2)} change`
            }
            tone={valueTone(point.expectedMarketMove)}
            onClick={() => onSelectLens('move')}
            selected={activeLens === 'move'}
          />
          <Metric
            label="Market signal"
            value={point.sentiment || 'Mixed'}
            detail={`${recommendationCount} recommendations`}
            onClick={() => onSelectLens('signal')}
            selected={activeLens === 'signal'}
          />
        </dl>

        {expanded && (
          <div className="space-y-6 border-t border-slate-200 bg-slate-50/75 px-5 py-5 dark:border-slate-700 dark:bg-slate-950/25 sm:px-6">
            <div>
              <h4 className="mb-3 text-sm font-bold text-slate-950 dark:text-white">
                Recommendation mix
              </h4>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
                {[
                  ['Strong buy', point.strongBuy, 'STRONG BUY'],
                  ['Buy', point.buy, 'BUY'],
                  ['Hold', point.hold, 'HOLD'],
                  ['Sell', point.sell, 'SELL'],
                  ['Strong sell', point.strongSell, 'STRONG SELL'],
                ].map(([label, count, lens]) => (
                  <button
                    type="button"
                    key={String(label)}
                    onClick={() => onSelectLens(lens as RunLens)}
                    aria-label={`View ${count} ${String(label).toLowerCase()} companies from ${formatDate(point.date)}`}
                    aria-pressed={activeLens === lens}
                    className={`min-h-16 rounded-xl border p-3 text-left hover:border-emerald-400 hover:bg-emerald-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 dark:hover:bg-emerald-950/40 ${activeLens === lens ? 'border-emerald-500 bg-emerald-50 dark:bg-emerald-950/40' : 'border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900/70'}`}
                  >
                    <span className="block text-xs text-slate-600 dark:text-slate-400">
                      {label}
                    </span>
                    <span className="font-data mt-1 block text-lg font-bold text-slate-950 dark:text-white">
                      {count}
                    </span>
                    <span className="mt-1 block text-[11px] font-semibold text-emerald-800 dark:text-emerald-300">
                      View companies
                    </span>
                  </button>
                ))}
              </div>
            </div>
            {activeLens && (
              <RunProjectionExplorer
                date={point.date}
                lens={activeLens}
                onSelectCompany={onSelectCompany}
              />
            )}
            {latest && <AccuracyDetails accuracy={accuracy} />}
          </div>
        )}
      </div>
    </article>
  );
}

function CompanyRun({
  point,
  previous,
  latest,
  expanded,
  onToggle,
  symbol,
}: {
  point: HistoricalPoint;
  previous?: HistoricalPoint;
  latest: boolean;
  expanded: boolean;
  onToggle: () => void;
  symbol: string;
}) {
  const [selectedMetric, setSelectedMetric] = useState<'close' | 'target' | 'model' | null>(null);
  const [savedProjection, setSavedProjection] = useState<RunProjection | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState(false);
  const [detailRetryKey, setDetailRetryKey] = useState(0);
  const fetchedKey = useRef('');
  const priceDelta = previous ? point.close - previous.close : null;
  const targetMove =
    point.projection && point.close !== 0
      ? ((point.projection.targetPrice - point.close) / point.close) * 100
      : null;

  useEffect(() => {
    const key = `${symbol}:${point.date}`;
    if (!selectedMetric || selectedMetric === 'close' || fetchedKey.current === key) return;
    let cancelled = false;
    setDetailLoading(true);
    setDetailError(false);
    setSavedProjection(null);
    void historyApi
      .getRunProjections(point.date)
      .then((response) => {
        if (!cancelled) {
          setSavedProjection(
            response.data.projections.find((row) => row.symbol === symbol) ?? null,
          );
          fetchedKey.current = key;
        }
      })
      .catch(() => {
        if (!cancelled) setDetailError(true);
      })
      .finally(() => {
        if (!cancelled) {
          setDetailLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [detailRetryKey, point.date, selectedMetric, symbol]);

  const showMetric = (metric: 'close' | 'target' | 'model') => {
    setSelectedMetric(metric);
    if (!expanded) onToggle();
  };

  return (
    <article className="relative pl-10 sm:pl-14">
      <span
        className={`absolute left-[7px] top-7 h-3.5 w-3.5 rounded-full border-[3px] sm:left-[15px] ${latest ? 'border-blue-200 bg-blue-600 ring-4 ring-blue-100 dark:border-blue-950 dark:bg-blue-300 dark:ring-blue-900/50' : 'border-slate-200 bg-slate-500 dark:border-slate-800 dark:bg-slate-400'}`}
        aria-hidden="true"
      />
      <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-[#101f30]">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={expanded}
          className="flex min-h-11 w-full items-center justify-between gap-4 px-5 py-4 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-emerald-500 sm:px-6"
        >
          <span>
            <span className="flex flex-wrap items-center gap-2">
              <span className="font-extrabold text-slate-950 dark:text-white">
                {formatDate(point.date)}
              </span>
              {latest && (
                <span className="rounded-full bg-blue-100 px-2.5 py-1 text-[11px] font-bold uppercase tracking-[0.12em] text-blue-800 dark:bg-blue-950 dark:text-blue-200">
                  Most recent
                </span>
              )}
            </span>
            <span className="mt-1 block text-sm text-slate-600 dark:text-slate-400">
              Recorded market day
            </span>
          </span>
          {expanded ? (
            <ChevronUpIcon className="h-5 w-5 shrink-0 text-slate-500" aria-hidden="true" />
          ) : (
            <ChevronDownIcon className="h-5 w-5 shrink-0 text-slate-500" aria-hidden="true" />
          )}
        </button>
        <dl className="grid gap-5 border-t border-slate-200 px-5 py-5 dark:border-slate-700 sm:grid-cols-3 sm:px-6">
          <Metric
            label="Closing price"
            value={formatPrice(point.close)}
            detail={
              priceDelta == null
                ? 'First day in this range'
                : `${priceDelta >= 0 ? '+' : ''}${formatPrice(priceDelta)} vs previous`
            }
            tone={valueTone(priceDelta ?? point.change)}
            onClick={() => showMetric('close')}
            selected={selectedMetric === 'close' && expanded}
          />
          <Metric
            label="5-trading-day target"
            value={point.projection ? formatPrice(point.projection.targetPrice) : '—'}
            detail={
              targetMove == null
                ? 'No target recorded'
                : `${formatPercentage(targetMove)} from close`
            }
            onClick={() => showMetric('target')}
            selected={selectedMetric === 'target' && expanded}
          />
          <Metric
            label="Model view"
            value={point.projection?.recommendation || 'Not recorded'}
            detail={
              point.projection?.confidence != null
                ? `${point.projection.confidence.toFixed(1)}% confidence`
                : undefined
            }
            onClick={() => showMetric('model')}
            selected={selectedMetric === 'model' && expanded}
          />
        </dl>
        {expanded && (
          <div className="border-t border-slate-200 bg-slate-50/75 px-5 py-5 dark:border-slate-700 dark:bg-slate-950/25 sm:px-6">
            {selectedMetric ? (
              <section aria-label={`${selectedMetric} details for ${formatDate(point.date)}`}>
                <h4 className="font-bold text-slate-950 dark:text-white">
                  {selectedMetric === 'close'
                    ? 'Closing price details'
                    : selectedMetric === 'target'
                      ? '5-trading-day target details'
                      : 'Model view details'}
                </h4>
                {selectedMetric === 'close' && (
                  <p className="mt-2 text-sm leading-6 text-slate-700 dark:text-slate-300">
                    The recorded close was {formatPrice(point.close)}.{' '}
                    {previous
                      ? `The previous recorded close was ${formatPrice(previous.close)}.`
                      : 'This is the first recorded day in the selected range.'}{' '}
                    Daily change: {formatPercentage(point.change)}. Volume:{' '}
                    {point.volume?.toLocaleString() ?? 'not recorded'}.
                  </p>
                )}
                {selectedMetric === 'target' && (
                  <p className="mt-2 text-sm leading-6 text-slate-700 dark:text-slate-300">
                    {point.projection
                      ? `The 5-trading-day target was ${formatPrice(point.projection.targetPrice)}, ${targetMove == null ? 'with no comparable closing price' : `${formatPercentage(targetMove)} from the recorded closing price`}.`
                      : 'No target was saved for this day.'}
                  </p>
                )}
                {selectedMetric === 'model' && (
                  <p className="mt-2 text-sm leading-6 text-slate-700 dark:text-slate-300">
                    {point.projection
                      ? `The saved recommendation was ${point.projection.recommendation || 'unrated'}${point.projection.confidence == null ? '' : ` at ${point.projection.confidence.toFixed(1)}% confidence`}.`
                      : 'No recommendation was saved for this day.'}
                  </p>
                )}
                {selectedMetric !== 'close' &&
                  (detailLoading ? (
                    <p role="status" className="mt-3 text-sm text-slate-600 dark:text-slate-400">
                      Loading saved projection details…
                    </p>
                  ) : detailError ? (
                    <div role="alert" className="mt-3 text-sm text-amber-800 dark:text-amber-200">
                      Additional saved details are unavailable for this day.{' '}
                      <button
                        type="button"
                        className="min-h-11 rounded px-2 font-bold hover:bg-emerald-500/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
                        onClick={() => setDetailRetryKey((value) => value + 1)}
                      >
                        Try again
                      </button>
                    </div>
                  ) : (
                    savedProjection && (
                      <div className="mt-3 rounded-xl border border-slate-200 bg-white p-4 text-sm text-slate-700 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300">
                        <p>
                          Saved target range:{' '}
                          {savedProjection.targetLow == null
                            ? '—'
                            : formatPrice(savedProjection.targetLow)}{' '}
                          to{' '}
                          {savedProjection.targetHigh == null
                            ? '—'
                            : formatPrice(savedProjection.targetHigh)}{' '}
                          · {savedProjection.risk} risk
                        </p>
                        {savedProjection.reason && <p className="mt-2">{savedProjection.reason}</p>}
                      </div>
                    )
                  ))}
              </section>
            ) : (
              <p className="text-sm leading-6 text-slate-700 dark:text-slate-300">
                Select a figure above for its recorded details.
              </p>
            )}
          </div>
        )}
      </div>
    </article>
  );
}

const HistoricalTrends: React.FC<HistoricalTrendsProps> = ({ refreshKey = 0 }) => {
  const [data, setData] = useState<DailySummaryPoint[]>([]);
  const [dateRange, setDateRange] = useState<{ first: string; last: string } | null>(null);
  const [days, setDays] = useState(30);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [symbolList, setSymbolList] = useState<string[]>([]);
  const [apiNames, setApiNames] = useState<Record<string, string>>({});
  const [selectedSymbol, setSelectedSymbol] = useState(
    () => new URLSearchParams(window.location.search).get('symbol')?.trim().toUpperCase() ?? '',
  );
  const [stockHistory, setStockHistory] = useState<HistoricalPoint[]>([]);
  const [stockLoading, setStockLoading] = useState(false);
  const [accuracy, setAccuracy] = useState<ProjectionAccuracyResponse | null>(null);
  const [accuracyLoading, setAccuracyLoading] = useState(true);
  const [expandedDate, setExpandedDate] = useState('');
  const [runLens, setRunLens] = useState<{ date: string; lens: RunLens } | null>(null);
  const isInitialMount = useRef(true);
  const requestedCompanyDate = useRef('');
  const focusRunDetails = useRef(false);

  useEffect(() => {
    if (!focusRunDetails.current || !runLens) return;
    const details = document.getElementById(`run-details-${runLens.date}`);
    if (details) {
      details.focus();
      details.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
      focusRunDetails.current = false;
    }
  }, [runLens]);

  const symbols = useMemo(
    () => symbolList.map((value) => ({ value, label: getCompanyName(value, apiNames[value]) })),
    [apiNames, symbolList],
  );

  const fetchData = useCallback(
    async (silent = false, cancelled?: () => boolean) => {
      if (!silent) setLoading(true);
      setError(null);
      try {
        const response = await historyApi.getSummary(days);
        if (cancelled?.()) return;
        const summaryData = [...(response.data?.data ?? [])].sort((a, b) =>
          a.date.localeCompare(b.date),
        );
        setData(summaryData);
        setExpandedDate(summaryData.length ? summaryData[summaryData.length - 1].date : '');
        setRunLens(null);
        setSymbolList(response.data?.symbols ?? []);
        setApiNames(response.data?.names ?? {});
        const first = response.data?.firstDate ?? '';
        const last = response.data?.lastDate ?? '';
        setDateRange(first && last ? { first, last } : null);
      } catch (err) {
        if (cancelled?.()) return;
        logger.error('Error fetching historical data:', err);
        if (!silent) setError('Unable to load historical data. Please try again later.');
        setData([]);
        setDateRange(null);
      } finally {
        if (!cancelled?.()) {
          setLoading(false);
          if (!silent) isInitialMount.current = false;
        }
      }
    },
    [days],
  );

  // fetchData changes identity with `days`; the refresh effect reads it through a ref so it
  // still runs only when refreshKey changes (a days change is already handled below).
  const fetchDataRef = useRef(fetchData);
  useEffect(() => {
    fetchDataRef.current = fetchData;
  });

  useEffect(() => {
    let cancelled = false;
    void fetchData(false, () => cancelled);
    return () => {
      cancelled = true;
    };
  }, [fetchData]);

  useEffect(() => {
    let cancelled = false;
    setAccuracyLoading(true);
    void historyApi
      .getAccuracy(days)
      .then((response) => {
        if (!cancelled) setAccuracy(response.data);
      })
      .catch(() => {
        if (!cancelled) setAccuracy(null);
      })
      .finally(() => {
        if (!cancelled) setAccuracyLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [days, refreshKey]);

  useEffect(() => {
    if (isInitialMount.current) return;
    let cancelled = false;
    void fetchDataRef.current(true, () => cancelled);
    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  useEffect(() => {
    if (!selectedSymbol) {
      setStockHistory([]);
      return;
    }
    let cancelled = false;
    setStockLoading(true);
    void stocksApi
      .getHistorical(selectedSymbol, days)
      .then((response) => {
        if (cancelled) return;
        const history = [...(response.data?.data ?? [])].sort((a, b) =>
          a.date.localeCompare(b.date),
        );
        setStockHistory(history);
        const requested = requestedCompanyDate.current;
        setExpandedDate(
          history.some((point) => point.date === requested)
            ? requested
            : history.length
              ? history[history.length - 1].date
              : '',
        );
        requestedCompanyDate.current = '';
      })
      .catch(() => {
        if (!cancelled) setStockHistory([]);
      })
      .finally(() => {
        if (!cancelled) setStockLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedSymbol, days]);

  if (loading) {
    return (
      <div className="flex min-h-[50vh] items-center justify-center">
        <div className="text-center">
          <div className="mx-auto h-12 w-12 animate-spin rounded-full border-2 border-slate-300 border-b-emerald-600" />
          <p className="mt-4 text-slate-600 dark:text-slate-400">Loading market history...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <main className="mx-auto max-w-7xl px-4 py-8">
        <div className="rounded-xl border border-amber-300 bg-amber-50 px-5 py-4 text-amber-950 dark:border-amber-700 dark:bg-amber-950/30 dark:text-amber-100">
          <p>{error}</p>
          <p className="mt-2 text-sm">Use Fetch New to create a fresh market snapshot.</p>
          <button
            type="button"
            onClick={() => fetchData()}
            className="mt-4 min-h-11 rounded px-2 font-bold hover:bg-emerald-500/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
          >
            Retry
          </button>
        </div>
      </main>
    );
  }

  if (data.length === 0) {
    return (
      <main className="mx-auto max-w-7xl px-4 py-8">
        <div className="rounded-2xl border border-slate-200 bg-white p-8 text-center dark:border-slate-700 dark:bg-[#101f30]">
          <DocumentChartBarIcon className="mx-auto h-9 w-9 text-slate-500" aria-hidden="true" />
          <h1 className="mt-3 text-xl font-extrabold text-slate-950 dark:text-white">
            No market history yet
          </h1>
          <p className="mt-2 text-slate-600 dark:text-slate-400">
            Use Fetch New regularly to build a projection history.
          </p>
        </div>
      </main>
    );
  }

  const selectedLabel = selectedSymbol
    ? symbols.find((item) => item.value === selectedSymbol)?.label || selectedSymbol
    : 'All companies';
  const marketRunsNewestFirst = [...data].reverse();
  const companyRunsNewestFirst = [...stockHistory].reverse();
  const latestMarket = data[data.length - 1];
  const selectRunLens = (date: string, lens: RunLens, focus = false) => {
    focusRunDetails.current = focus;
    setExpandedDate(date);
    setRunLens({ date, lens });
  };
  const selectCompanyFromRun = (symbol: string, date: string) => {
    requestedCompanyDate.current = date;
    setSelectedSymbol(symbol);
    setRunLens(null);
  };

  return (
    <main className="mx-auto max-w-[1180px] px-4 py-6 sm:px-6 xl:px-8">
      <header className="mb-7">
        <div className="mb-2 flex items-center gap-2 text-xs font-bold uppercase tracking-[0.16em] text-emerald-800 dark:text-emerald-300">
          <ClockIcon className="h-4 w-4" aria-hidden="true" />
          Projection history
        </div>
        <h1 className="text-3xl font-extrabold tracking-[-0.04em] text-slate-950 dark:text-white sm:text-4xl">
          Market history
        </h1>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-600 dark:text-slate-300">
          See how stock prices and forecasts changed over time. Select a day for more details.
        </p>
      </header>

      <section
        aria-label="Timeline filters"
        className="mb-6 grid gap-4 rounded-2xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-700 dark:bg-[#101f30] sm:grid-cols-[auto_minmax(0,1fr)_auto] sm:items-end sm:p-5"
      >
        <div>
          <label
            htmlFor="days"
            className="mb-1.5 block text-xs font-bold uppercase tracking-[0.12em] text-slate-600 dark:text-slate-400"
          >
            Time range
          </label>
          <select
            id="days"
            aria-label="Time range:"
            value={days}
            onChange={(event) => setDays(Number(event.target.value))}
            className="min-h-11 rounded-xl border border-slate-400 bg-slate-50 px-3 py-2 text-sm font-semibold text-slate-950 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 dark:border-slate-600 dark:bg-slate-950 dark:text-white"
          >
            {DAY_OPTIONS.map((value) => (
              <option key={value} value={value}>
                Last {value} days
              </option>
            ))}
          </select>
        </div>
        <div className="min-w-0">
          <span className="mb-1.5 block text-xs font-bold uppercase tracking-[0.12em] text-slate-600 dark:text-slate-400">
            Company
          </span>
          <Listbox value={selectedSymbol} onChange={setSelectedSymbol}>
            <div className="relative max-w-sm">
              <ListboxButton
                aria-label="Company"
                className="relative min-h-11 w-full rounded-xl border border-slate-400 bg-slate-50 py-2 pl-3 pr-10 text-left text-sm font-semibold text-slate-950 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 dark:border-slate-600 dark:bg-slate-950 dark:text-white"
              >
                <span className="block truncate">{selectedLabel}</span>
                <ChevronDownIcon
                  className="pointer-events-none absolute right-3 top-1/2 h-5 w-5 -translate-y-1/2 text-slate-500"
                  aria-hidden="true"
                />
              </ListboxButton>
              <ListboxOptions
                anchor="bottom start"
                className="z-50 mt-1 max-h-72 w-[var(--button-width)] min-w-72 overflow-auto rounded-xl border border-slate-300 bg-white p-1.5 text-sm text-slate-950 shadow-2xl focus:outline-none dark:border-slate-600 dark:bg-[#101f30] dark:text-white"
              >
                <ListboxOption value="">
                  {({ focus, selected }) => (
                    <div
                      className={`flex min-h-11 cursor-pointer items-center justify-between rounded-lg px-3 py-2.5 ${focus ? 'bg-emerald-100 dark:bg-emerald-900/50' : ''} ${selected ? 'font-bold text-emerald-800 dark:text-emerald-200' : ''}`}
                    >
                      <span>All companies</span>
                      {selected && <CheckIcon className="h-4 w-4" aria-hidden="true" />}
                    </div>
                  )}
                </ListboxOption>
                {symbols.map((symbol) => (
                  <ListboxOption key={symbol.value} value={symbol.value}>
                    {({ focus, selected }) => (
                      <div
                        title={`${symbol.label} (${symbol.value})`}
                        className={`flex min-h-11 cursor-pointer items-center justify-between gap-3 rounded-lg px-3 py-2.5 ${focus ? 'bg-emerald-100 dark:bg-emerald-900/50' : ''} ${selected ? 'font-bold text-emerald-800 dark:text-emerald-200' : ''}`}
                      >
                        <span className="min-w-0 truncate">
                          {symbol.label} ({symbol.value})
                        </span>
                        {selected && <CheckIcon className="h-4 w-4 shrink-0" aria-hidden="true" />}
                      </div>
                    )}
                  </ListboxOption>
                ))}
              </ListboxOptions>
            </div>
          </Listbox>
        </div>
        {dateRange && (
          <p className="font-data text-sm text-slate-600 dark:text-slate-300">
            {formatDate(dateRange.first)} – {formatDate(dateRange.last)}
          </p>
        )}
      </section>

      {!selectedSymbol && (
        <section
          aria-label="Latest market day"
          className="mb-7 grid gap-5 rounded-2xl border border-emerald-200 bg-emerald-50/70 p-5 dark:border-emerald-900 dark:bg-emerald-950/20 sm:grid-cols-[1fr_auto] sm:items-center"
        >
          <div>
            <p className="text-xs font-bold uppercase tracking-[0.14em] text-emerald-800 dark:text-emerald-300">
              Market snapshot
            </p>
            <h2 className="mt-1 text-xl font-extrabold text-slate-950 dark:text-white">
              <button
                type="button"
                onClick={() => selectRunLens(latestMarket.date, 'companies', true)}
                className="rounded-lg px-2 py-1 text-left font-bold text-emerald-800 hover:bg-emerald-500/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 dark:text-emerald-300"
              >
                {latestMarket.totalProjections ||
                  latestMarket.strongBuy +
                    latestMarket.buy +
                    latestMarket.hold +
                    latestMarket.sell +
                    latestMarket.strongSell}{' '}
                projections
              </button>{' '}
              recorded on {formatDate(latestMarket.date)}
            </h2>
            <p className="mt-2 text-sm text-slate-700 dark:text-slate-300">
              Confidence was{' '}
              <button
                type="button"
                onClick={() => selectRunLens(latestMarket.date, 'confidence', true)}
                className="min-h-11 rounded-md px-2 font-bold text-emerald-800 hover:bg-emerald-500/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 dark:text-emerald-300"
              >
                {latestMarket.averageConfidence.toFixed(1)}%
              </button>{' '}
              with an expected market move of{' '}
              <button
                type="button"
                onClick={() => selectRunLens(latestMarket.date, 'move', true)}
                className="min-h-11 rounded-md px-2 font-bold text-emerald-800 hover:bg-emerald-500/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 dark:text-emerald-300"
              >
                {formatPercentage(latestMarket.expectedMarketMove)}
              </button>
              .
            </p>
          </div>
          <ChartBarIcon
            className="hidden h-10 w-10 text-emerald-700 dark:text-emerald-300 sm:block"
            aria-hidden="true"
          />
        </section>
      )}

      {!selectedSymbol && <LatestForecasts refreshKey={refreshKey} />}

      <section aria-labelledby="timeline-heading">
        <div className="mb-4 flex items-end justify-between gap-4">
          <div>
            <h2
              id="timeline-heading"
              className="text-xl font-extrabold tracking-[-0.02em] text-slate-950 dark:text-white"
            >
              {selectedSymbol ? `${selectedLabel} history` : 'Past market days'}
            </h2>
            <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
              Newest first · select a day for more details
            </p>
          </div>
        </div>
        {stockLoading ? (
          <div className="flex min-h-48 items-center justify-center">
            <div
              className="h-10 w-10 animate-spin rounded-full border-2 border-slate-300 border-b-emerald-600"
              aria-label="Loading company history"
            />
          </div>
        ) : selectedSymbol && companyRunsNewestFirst.length === 0 ? (
          <div className="rounded-2xl border border-slate-200 bg-white p-8 text-center text-slate-600 dark:border-slate-700 dark:bg-[#101f30] dark:text-slate-300">
            No price history for <strong>{selectedSymbol}</strong> in this range.
          </div>
        ) : (
          <div className="relative space-y-4 before:absolute before:bottom-7 before:left-[13px] before:top-7 before:w-px before:bg-slate-300 dark:before:bg-slate-700 sm:before:left-[21px]">
            {selectedSymbol
              ? companyRunsNewestFirst.map((point, index) => (
                  <CompanyRun
                    key={point.date}
                    point={point}
                    previous={
                      stockHistory[stockHistory.findIndex((item) => item.date === point.date) - 1]
                    }
                    latest={index === 0}
                    expanded={expandedDate === point.date}
                    onToggle={() => setExpandedDate(expandedDate === point.date ? '' : point.date)}
                    symbol={selectedSymbol}
                  />
                ))
              : marketRunsNewestFirst.map((point, index) => (
                  <MarketRun
                    key={point.date}
                    point={point}
                    previous={data[data.findIndex((item) => item.date === point.date) - 1]}
                    latest={index === 0}
                    accuracy={accuracyLoading ? null : accuracy}
                    expanded={expandedDate === point.date}
                    onToggle={() => setExpandedDate(expandedDate === point.date ? '' : point.date)}
                    activeLens={runLens?.date === point.date ? runLens.lens : null}
                    onSelectLens={(lens) => selectRunLens(point.date, lens)}
                    onSelectCompany={selectCompanyFromRun}
                  />
                ))}
          </div>
        )}
      </section>
    </main>
  );
};

export default HistoricalTrends;
