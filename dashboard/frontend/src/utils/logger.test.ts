import { afterEach, describe, expect, it, vi } from 'vitest';
import { getLogger } from './logger';

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
  vi.useRealTimers();
});

describe('shared logger', () => {
  it('includes a UTC timestamp, level, and scope while preserving error details', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-22T12:00:00Z'));
    const output = vi.spyOn(console, 'error').mockImplementation(() => {});
    const error = new Error('unavailable');
    getLogger('services/api').error('Request failed', error, { status: 503 });
    expect(output).toHaveBeenCalledExactlyOnceWith(
      '2026-10-22T12:00:00.000Z [ERROR] services/api: Request failed',
      error,
      { status: 503 },
    );
  });

  it('filters below the configured level and routes warnings to console.warn', () => {
    vi.stubEnv('VITE_LOG_LEVEL', 'WARNING');
    const debug = vi.spyOn(console, 'debug').mockImplementation(() => {});
    const info = vi.spyOn(console, 'info').mockImplementation(() => {});
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const logger = getLogger('test');
    logger.debug('hidden');
    logger.info('hidden');
    logger.warning('visible');
    expect(debug).not.toHaveBeenCalled();
    expect(info).not.toHaveBeenCalled();
    expect(warning).toHaveBeenCalledOnce();
  });

  it('defaults invalid levels to INFO and preserves critical severity', () => {
    vi.stubEnv('VITE_LOG_LEVEL', 'invalid');
    const debug = vi.spyOn(console, 'debug').mockImplementation(() => {});
    const info = vi.spyOn(console, 'info').mockImplementation(() => {});
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    const logger = getLogger('test');
    logger.debug('hidden');
    logger.info('visible');
    logger.critical('fatal');
    expect(debug).not.toHaveBeenCalled();
    expect(info).toHaveBeenCalledOnce();
    expect(error).toHaveBeenCalledWith(expect.stringContaining('[CRITICAL] test: fatal'));
  });
});
