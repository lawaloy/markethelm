import { getLogger } from '../../utils/logger';
import React, { useEffect, useRef, useState } from 'react';
import { ArrowDownTrayIcon } from '@heroicons/react/24/outline';
import { exportToCsv, exportToPng, exportToPdf } from '../../utils/exportUtils';
import type { Opportunity } from '../../types';

const logger = getLogger('components/common/ExportButton');

interface ExportButtonProps {
  /** For CSV: the data to export */
  stocks?: Opportunity[];
  /** For PNG/PDF: ref to the element to capture */
  captureRef?: React.RefObject<HTMLElement | null>;
  /** Which formats to show */
  formats?: ('csv' | 'png' | 'pdf')[];
  /** What is being exported (e.g. "Stock data", "Dashboard", "Summary") - shown in dropdown */
  label?: string;
  className?: string;
}

const ExportButton: React.FC<ExportButtonProps> = ({
  stocks = [],
  captureRef,
  formats = ['csv', 'png', 'pdf'],
  label = 'Data',
  className = '',
}) => {
  const [open, setOpen] = useState(false);
  const [exporting, setExporting] = useState<string | null>(null);
  /** Sync guard: disabled attrs alone cannot block double-clicks before re-render. */
  const exportingRef = useRef(false);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const handleExport = async (format: 'csv' | 'png' | 'pdf') => {
    if (exportingRef.current) return;
    exportingRef.current = true;
    setExporting(format);
    try {
      if (format === 'csv' && stocks.length > 0) {
        exportToCsv(stocks);
      } else if ((format === 'png' || format === 'pdf') && captureRef?.current) {
        if (format === 'png') {
          await exportToPng(captureRef.current);
        } else {
          await exportToPdf(captureRef.current);
        }
      }
    } catch (err) {
      logger.error('Export failed:', err);
    } finally {
      exportingRef.current = false;
      if (mountedRef.current) {
        setExporting(null);
        setOpen(false);
      }
    }
  };

  return (
    <div className={`relative ${className}`}>
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-semibold text-slate-700 transition hover:border-slate-400 hover:bg-slate-50 dark:border-[#31435b] dark:bg-[#101f30] dark:text-slate-200 dark:hover:border-[#455c78] dark:hover:bg-[#15283d]"
        title={`Export ${label}`}
      >
        <ArrowDownTrayIcon className="h-5 w-5" />
        <span className="hidden sm:inline">Export</span>
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} aria-hidden="true" />
          <div className="absolute right-0 z-20 mt-1 w-56 rounded-lg border border-slate-200 bg-white py-1 shadow-xl dark:border-[#31435b] dark:bg-[#101f30]">
            {formats.includes('csv') && (
              <button
                onClick={() => handleExport('csv')}
                disabled={stocks.length === 0 || exporting !== null}
                className="w-full px-4 py-2 text-left text-sm text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-700 disabled:opacity-50"
              >
                {exporting === 'csv' ? 'Exporting...' : `${label} as CSV`}
              </button>
            )}
            {formats.includes('png') && (
              <button
                onClick={() => handleExport('png')}
                disabled={!captureRef?.current || exporting !== null}
                className="w-full px-4 py-2 text-left text-sm text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-700 disabled:opacity-50"
              >
                {exporting === 'png' ? 'Exporting...' : `${label} as image (PNG)`}
              </button>
            )}
            {formats.includes('pdf') && (
              <button
                onClick={() => handleExport('pdf')}
                disabled={!captureRef?.current || exporting !== null}
                className="w-full px-4 py-2 text-left text-sm text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-700 disabled:opacity-50"
              >
                {exporting === 'pdf' ? 'Exporting...' : `${label} as PDF`}
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
};

export default ExportButton;
