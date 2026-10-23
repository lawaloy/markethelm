import { getLogger } from '../../utils/logger';
import React, { useEffect, useState } from 'react';
import { Dialog, Transition } from '@headlessui/react';
import { Fragment } from 'react';
import { XMarkIcon } from '@heroicons/react/24/outline';
import { stocksApi } from '../../services/api';
import {
  formatPrice,
  formatPercentage,
  formatVolume,
  getCompanyName,
  getRecommendationColor,
  getRiskColor,
  getTrendIcon,
} from '../../utils/formatters';
import type { StockDetail } from '../../types';

const logger = getLogger('components/modals/StockDetailModal');

interface StockDetailModalProps {
  symbol: string;
  isOpen: boolean;
  onClose: () => void;
}

const StockDetailModal: React.FC<StockDetailModalProps> = ({ symbol, isOpen, onClose }) => {
  const [stockDetail, setStockDetail] = useState<StockDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!isOpen || !symbol) {
      return;
    }

    let cancelled = false;

    const fetchStockDetail = async () => {
      setLoading(true);
      setError(null);
      setStockDetail(null);
      try {
        const response = await stocksApi.getDetail(symbol);
        if (cancelled) return;
        setStockDetail(response.data);
      } catch (err) {
        if (cancelled) return;
        setError('Failed to load stock details');
        logger.error('Failed to load stock detail', err);
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    void fetchStockDetail();
    return () => {
      cancelled = true;
    };
  }, [isOpen, symbol]);

  return (
    <Transition appear show={isOpen} as={Fragment}>
      <Dialog as="div" className="fixed inset-0 z-50 overflow-y-auto" onClose={onClose}>
        <Transition.Child
          as={Fragment}
          enter="ease-out duration-300"
          enterFrom="opacity-0"
          enterTo="opacity-100"
          leave="ease-in duration-200"
          leaveFrom="opacity-100"
          leaveTo="opacity-0"
        >
          <div className="fixed inset-0 bg-slate-950/70 backdrop-blur-sm" />
        </Transition.Child>

        <div className="relative min-h-full overflow-y-auto overscroll-contain">
          <div className="flex min-h-full items-center justify-center p-2 text-center sm:p-4">
            <Transition.Child
              as={Fragment}
              enter="ease-out duration-300"
              enterFrom="opacity-0 scale-95"
              enterTo="opacity-100 scale-100"
              leave="ease-in duration-200"
              leaveFrom="opacity-100 scale-100"
              leaveTo="opacity-0 scale-95"
            >
              <Dialog.Panel className="max-h-[calc(100dvh-1rem)] w-full max-w-3xl transform overflow-y-auto rounded-xl border border-slate-300 bg-[#f3f6f9] p-4 text-left align-middle text-slate-950 shadow-2xl transition-all sm:max-h-[calc(100dvh-2rem)] sm:p-5 dark:border-[#31435b] dark:bg-[#0e1b2a] dark:text-slate-100">
                <div className="mb-4 flex items-start justify-between gap-4">
                  <Dialog.Title
                    as="h3"
                    className="min-w-0 text-xl font-extrabold tracking-[-0.025em] text-slate-950 sm:text-2xl dark:text-white"
                  >
                    {loading
                      ? 'Loading...'
                      : stockDetail
                        ? `${stockDetail.symbol} - ${getCompanyName(stockDetail.symbol, stockDetail.name)}`
                        : symbol}
                  </Dialog.Title>
                  <button
                    type="button"
                    onClick={onClose}
                    className="-mr-1 -mt-1 inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-lg text-slate-600 transition hover:bg-slate-200 hover:text-slate-950 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 focus-visible:ring-offset-2 focus-visible:ring-offset-[#f3f6f9] dark:text-slate-300 dark:hover:bg-[#1a2b3f] dark:hover:text-white dark:focus-visible:ring-offset-[#0e1b2a]"
                    aria-label="Close stock details"
                  >
                    <XMarkIcon className="h-6 w-6" />
                  </button>
                </div>

                {loading && (
                  <div className="flex justify-center py-8">
                    <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-500"></div>
                  </div>
                )}

                {error && (
                  <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 text-red-700 dark:text-red-400 px-4 py-3 rounded">
                    {error}
                  </div>
                )}

                {!loading && !error && stockDetail && (
                  <div className="space-y-4">
                    {/* Current Price */}
                    <div>
                      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                        <span className="font-data text-2xl font-medium text-slate-950 sm:text-3xl dark:text-white">
                          {formatPrice(stockDetail.currentData.price)}
                        </span>
                        <span
                          className={`text-lg font-medium ${
                            stockDetail.currentData.changePercent >= 0
                              ? 'text-green-800 dark:text-green-400'
                              : 'text-red-600 dark:text-red-400'
                          }`}
                        >
                          {formatPercentage(stockDetail.currentData.changePercent)}(
                          {formatPrice(Math.abs(stockDetail.currentData.change))})
                        </span>
                      </div>
                    </div>

                    {/* Projection */}
                    {stockDetail.projection && (
                      <div className="rounded-xl border border-slate-200 bg-slate-100/80 p-4 dark:border-[#26384d] dark:bg-[#122235]">
                        <h4 className="mb-3 text-sm font-bold text-slate-700 dark:text-slate-300">
                          5-Day Projection
                        </h4>
                        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                          <div>
                            <p className="text-sm text-slate-600 dark:text-slate-400">
                              Target Price
                            </p>
                            <p className="font-data text-xl font-medium text-slate-950 dark:text-white">
                              {formatPrice(stockDetail.projection.targetPrice)}
                            </p>
                            <p
                              className={`text-sm ${
                                stockDetail.projection.expectedChange >= 0
                                  ? 'text-green-800 dark:text-green-400'
                                  : 'text-red-600 dark:text-red-400'
                              }`}
                            >
                              {formatPercentage(stockDetail.projection.expectedChange)}
                            </p>
                          </div>
                          <div className="flex flex-wrap items-start gap-4">
                            <div>
                              <p className="text-sm text-slate-600 dark:text-slate-400">
                                Recommendation
                              </p>
                              <span
                                className={`inline-block mt-1 badge ${getRecommendationColor(stockDetail.projection.recommendation)}`}
                              >
                                {stockDetail.projection.recommendation}
                              </span>
                            </div>
                            <div>
                              <p className="text-sm text-slate-600 dark:text-slate-400">Risk</p>
                              <span
                                className={`inline-block mt-1 badge ${getRiskColor(stockDetail.projection.risk)}`}
                              >
                                {stockDetail.projection.risk}
                              </span>
                            </div>
                          </div>
                        </div>
                        {Number.isFinite(stockDetail.projection.confidence) && (
                          <div className="mt-3">
                            <p className="text-sm text-slate-600 dark:text-slate-400">
                              Confidence: {stockDetail.projection.confidence}%
                            </p>
                            <div
                              className="mt-2 h-2 w-full overflow-hidden rounded-full bg-slate-300 dark:bg-slate-600"
                              role="progressbar"
                              aria-label="Projection confidence"
                              aria-valuemin={0}
                              aria-valuemax={100}
                              aria-valuenow={Math.min(
                                100,
                                Math.max(0, stockDetail.projection.confidence),
                              )}
                            >
                              <div
                                className="bg-blue-500 h-2 rounded-full"
                                style={{
                                  width: `${Math.min(100, Math.max(0, stockDetail.projection.confidence))}%`,
                                }}
                              />
                            </div>
                          </div>
                        )}
                        <div className="mt-4 flex items-center gap-2">
                          <span className="text-xl" aria-hidden="true">
                            {getTrendIcon(stockDetail.projection.trend)}
                          </span>
                          <span className="text-sm font-medium">
                            {stockDetail.projection.trend}
                          </span>
                        </div>
                      </div>
                    )}

                    {/* Key Metrics */}
                    <div>
                      <h4 className="mb-3 text-sm font-bold text-slate-700 dark:text-slate-300">
                        Key Metrics
                      </h4>
                      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                        <div>
                          <p className="text-sm text-slate-600 dark:text-slate-400">Volume</p>
                          <p className="font-data text-lg font-medium text-slate-950 dark:text-white">
                            {formatVolume(stockDetail.currentData.volume)}
                          </p>
                        </div>
                        {stockDetail.currentData.marketCap && (
                          <div>
                            <p className="text-sm text-slate-600 dark:text-slate-400">Market Cap</p>
                            <p className="font-data text-lg font-medium text-slate-950 dark:text-white">
                              {formatVolume(stockDetail.currentData.marketCap)}
                            </p>
                          </div>
                        )}
                        {Number.isFinite(stockDetail.technical?.momentum) && (
                          <div>
                            <p className="text-sm text-slate-600 dark:text-slate-400">Momentum</p>
                            <p className="font-data text-lg font-medium text-slate-950 dark:text-white">
                              {(stockDetail.technical?.momentum as number).toFixed(1)}
                            </p>
                          </div>
                        )}
                        {Number.isFinite(stockDetail.technical?.volatility) && (
                          <div>
                            <p className="text-sm text-slate-600 dark:text-slate-400">Volatility</p>
                            <p className="font-data text-lg font-medium text-slate-950 dark:text-white">
                              {(stockDetail.technical?.volatility as number).toFixed(1)}%
                            </p>
                          </div>
                        )}
                      </div>
                    </div>

                    <div className="flex justify-end border-t border-slate-200 pt-4 dark:border-[#26384d]">
                      <button onClick={onClose} className="btn-primary min-h-11 min-w-24">
                        Close
                      </button>
                    </div>
                  </div>
                )}
              </Dialog.Panel>
            </Transition.Child>
          </div>
        </div>
      </Dialog>
    </Transition>
  );
};

export default StockDetailModal;
