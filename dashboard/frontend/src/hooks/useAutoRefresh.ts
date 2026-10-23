import { getLogger } from '../utils/logger';
import { useState, useEffect, useCallback } from 'react';
import api from '../services/api';

const logger = getLogger('hooks/useAutoRefresh');

interface AutoRefreshConfig {
  checkInterval?: number; // How often to check for new data (ms)
  enabled?: boolean;
}

interface RefreshState {
  lastUpdate: Date | null;
  nextScheduled: Date | null;
  shouldRefresh: boolean;
  isChecking: boolean;
}

const SCHEDULED_TIMES = ['09:00', '12:00', '15:00']; // Market hours updates

export const useAutoRefresh = (config: AutoRefreshConfig = {}) => {
  const { checkInterval = 120000, enabled = true } = config; // Default: check every 2 min
  const [state, setState] = useState<RefreshState>({
    lastUpdate: null,
    nextScheduled: null,
    shouldRefresh: false,
    isChecking: false,
  });

  const getNextScheduledTime = useCallback((): Date | null => {
    const now = new Date();
    const dayOfWeek = now.getDay();

    // Only on weekdays (1-5)
    if (dayOfWeek === 0 || dayOfWeek === 6) {
      return null; // Weekend - no scheduled updates
    }

    for (const time of SCHEDULED_TIMES) {
      const [hours, minutes] = time.split(':').map(Number);
      const scheduledTime = new Date();
      scheduledTime.setHours(hours, minutes, 0, 0);

      if (scheduledTime > now) {
        return scheduledTime;
      }
    }

    // All times passed today, next is tomorrow 9am
    const tomorrow = new Date(now);
    tomorrow.setDate(tomorrow.getDate() + 1);
    tomorrow.setHours(9, 0, 0, 0);
    return tomorrow;
  }, []);

  const checkForNewData = useCallback(async () => {
    if (!enabled || state.isChecking) return;

    setState((prev) => ({ ...prev, isChecking: true }));

    try {
      const response = await api.get('/api/market/overview');
      const dataDate = response.data.date;

      // Check if we have new data
      const currentDate = new Date().toISOString().split('T')[0];
      if (dataDate === currentDate) {
        const lastFetch = localStorage.getItem('lastDataFetch');
        const lastFetchDate = lastFetch ? new Date(lastFetch) : null;
        const now = new Date();

        // If we haven't seen this data yet, or it's been > 2 hours
        if (!lastFetch || now.getTime() - (lastFetchDate?.getTime() || 0) > 7200000) {
          localStorage.setItem('lastDataFetch', now.toISOString());
          setState((prev) => ({
            ...prev,
            shouldRefresh: true,
            lastUpdate: now,
            isChecking: false,
          }));
          return;
        }
      }

      setState((prev) => ({
        ...prev,
        isChecking: false,
        nextScheduled: getNextScheduledTime(),
      }));
    } catch (error) {
      logger.error('Auto-refresh check failed:', error);
      setState((prev) => ({ ...prev, isChecking: false }));
    }
  }, [enabled, state.isChecking, getNextScheduledTime]);

  useEffect(() => {
    if (!enabled) return;

    // Initial check
    checkForNewData();

    // Set up interval
    const interval = setInterval(checkForNewData, checkInterval);

    // Update next scheduled time every minute
    const scheduleInterval = setInterval(() => {
      setState((prev) => ({
        ...prev,
        nextScheduled: getNextScheduledTime(),
      }));
    }, 60000);

    return () => {
      clearInterval(interval);
      clearInterval(scheduleInterval);
    };
  }, [enabled, checkInterval, checkForNewData, getNextScheduledTime]);

  const resetRefreshFlag = useCallback(() => {
    setState((prev) => ({ ...prev, shouldRefresh: false }));
  }, []);

  return {
    ...state,
    resetRefreshFlag,
  };
};
