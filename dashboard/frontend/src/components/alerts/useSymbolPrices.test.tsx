import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import axios from 'axios';
import { useSymbolPrices } from './useSymbolPrices';

vi.mock('../../services/api', () => ({
  default: {
    get: vi.fn(),
  },
  alertsApi: {
    getQuotes: vi.fn(),
  },
}));

import api, { alertsApi } from '../../services/api';

function ProbeHarness({ symbols }: { symbols?: string[] }) {
  const {
    apiReady,
    quotesUnavailable,
    liveQuotesConfigured,
    symbolPrices,
    quoteMeta,
    fetchPricesFor,
    retryPriceFor,
    pricingPending,
    attemptedPrices,
    mergePrices,
  } = useSymbolPrices();

  return (
    <div>
      <span data-testid="ready">{apiReady ? 'ready' : 'loading'}</span>
      <span data-testid="unavailable">{quotesUnavailable ? 'yes' : 'no'}</span>
      <span data-testid="configured">{liveQuotesConfigured ? 'yes' : 'no'}</span>
      <span data-testid="prices">{JSON.stringify(symbolPrices)}</span>
      <span data-testid="meta">{JSON.stringify(quoteMeta)}</span>
      <span data-testid="pending">{pricingPending.size}</span>
      <span data-testid="attempted">{[...attemptedPrices].sort().join(',')}</span>
      <button
        type="button"
        data-testid="retry"
        onClick={() => {
          void retryPriceFor('AAPL');
        }}
      >
        retry
      </button>
      <button
        type="button"
        data-testid="fetch"
        onClick={() => {
          void fetchPricesFor(symbols ?? ['AAPL']);
        }}
      >
        fetch
      </button>
      <button
        type="button"
        data-testid="fetch-both"
        onClick={() => {
          void fetchPricesFor(['AAPL', 'MSFT']);
        }}
      >
        fetch-both
      </button>
      <button
        type="button"
        data-testid="merge-aapl"
        onClick={() => {
          mergePrices({ AAPL: 180.5 });
        }}
      >
        merge-aapl
      </button>
      <button
        type="button"
        data-testid="merge-empty"
        onClick={() => {
          mergePrices({});
        }}
      >
        merge-empty
      </button>
      <button
        type="button"
        data-testid="merge-zero"
        onClick={() => {
          mergePrices({ AAPL: 0 });
        }}
      >
        merge-zero
      </button>
    </div>
  );
}

describe('useSymbolPrices', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(api.get).mockReset();
    vi.mocked(alertsApi.getQuotes).mockReset();
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('marks apiReady after a healthy probe and clears fail state', async () => {
    vi.mocked(api.get).mockResolvedValueOnce({ data: { ok: true } });

    render(<ProbeHarness />);

    await waitFor(() => {
      expect(screen.getByTestId('ready').textContent).toBe('ready');
    });
    expect(screen.getByTestId('unavailable').textContent).toBe('no');
    expect(api.get).toHaveBeenCalledWith('/api/alerts/health');
  });

  it('sets quotesUnavailable when health probe is not ok', async () => {
    vi.mocked(api.get).mockResolvedValueOnce({ data: { ok: false } });

    render(<ProbeHarness />);

    await waitFor(() => {
      expect(screen.getByTestId('ready').textContent).toBe('ready');
    });
    expect(screen.getByTestId('unavailable').textContent).toBe('yes');
  });

  it('does not attempt live quotes without a server-side market-data key', async () => {
    vi.mocked(api.get).mockResolvedValueOnce({
      data: { ok: true, live_quotes_configured: false },
    });
    render(<ProbeHarness />);
    await waitFor(() => expect(screen.getByTestId('ready').textContent).toBe('ready'));
    expect(screen.getByTestId('configured').textContent).toBe('no');
    expect(screen.getByTestId('unavailable').textContent).toBe('yes');
    await act(async () => screen.getByTestId('fetch').click());
    expect(alertsApi.getQuotes).not.toHaveBeenCalled();
  });

  it('retains quote provenance and lets a missing price be retried', async () => {
    vi.mocked(api.get).mockResolvedValueOnce({ data: { ok: true } });
    vi.mocked(alertsApi.getQuotes)
      .mockResolvedValueOnce({ data: { prices: {} } } as never)
      .mockResolvedValueOnce({
        data: {
          prices: { AAPL: 150 },
          quote_meta: { AAPL: { source: 'lookup', as_of: null } },
        },
      } as never);
    render(<ProbeHarness />);
    await waitFor(() => expect(screen.getByTestId('ready').textContent).toBe('ready'));
    await act(async () => screen.getByTestId('fetch').click());
    await waitFor(() => expect(screen.getByTestId('attempted').textContent).toBe('AAPL'));
    await act(async () => screen.getByTestId('retry').click());
    await waitFor(() => expect(alertsApi.getQuotes).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByTestId('prices').textContent).toContain('150'));
    expect(screen.getByTestId('meta').textContent).toContain('lookup');
  });

  it('sets quotesUnavailable when health probe throws', async () => {
    vi.mocked(api.get).mockRejectedValueOnce(new Error('network'));

    render(<ProbeHarness />);

    await waitFor(() => {
      expect(screen.getByTestId('ready').textContent).toBe('ready');
    });
    expect(screen.getByTestId('unavailable').textContent).toBe('yes');
  });

  it('does not fetch quotes while unavailable', async () => {
    vi.mocked(api.get).mockRejectedValueOnce(new Error('down'));

    render(<ProbeHarness />);

    await waitFor(() => {
      expect(screen.getByTestId('unavailable').textContent).toBe('yes');
    });

    await act(async () => {
      screen.getByTestId('fetch').click();
    });

    expect(alertsApi.getQuotes).not.toHaveBeenCalled();
  });

  it('does not fetch quotes while the health probe is still in flight', async () => {
    // AlertsSettings can call fetchPricesFor as soon as the picker mounts.
    // A getQuotes before /api/alerts/health resolves can 405 a stale backend
    // and permanently lock quotesUnavailable, or burn Finnhub before we know
    // the quotes route exists.
    let resolveHealth: ((value: { data: { ok: boolean } }) => void) | undefined;
    vi.mocked(api.get).mockImplementation(
      () =>
        new Promise<{ data: { ok: boolean } }>((resolve) => {
          resolveHealth = resolve;
        }) as never,
    );
    vi.mocked(alertsApi.getQuotes).mockResolvedValue({
      data: { prices: { AAPL: 190.5 } },
    } as never);

    render(<ProbeHarness />);

    expect(screen.getByTestId('ready').textContent).toBe('loading');

    await act(async () => {
      screen.getByTestId('fetch').click();
      await vi.advanceTimersByTimeAsync(400);
    });
    expect(alertsApi.getQuotes).not.toHaveBeenCalled();

    await act(async () => {
      resolveHealth?.({ data: { ok: true } });
      await Promise.resolve();
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(screen.getByTestId('ready').textContent).toBe('ready');
    });
    // The in-flight click is a no-op — it must not queue a fetch after ready.
    expect(alertsApi.getQuotes).not.toHaveBeenCalled();
    expect(screen.getByTestId('unavailable').textContent).toBe('no');

    await act(async () => {
      screen.getByTestId('fetch').click();
      await vi.advanceTimersByTimeAsync(400);
    });

    await waitFor(() => {
      expect(alertsApi.getQuotes).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(JSON.parse(screen.getByTestId('prices').textContent || '{}')).toEqual({
        AAPL: 190.5,
      });
    });
  });

  it('cools down a missing quote symbol for FAILED_RETRY_MS', async () => {
    vi.mocked(api.get).mockResolvedValueOnce({ data: { ok: true } });
    // Partial AxiosResponse is enough for this hook; cast like other API mocks.
    vi.mocked(alertsApi.getQuotes).mockResolvedValue({
      data: { prices: {} },
    } as never);

    render(<ProbeHarness symbols={['MSFT']} />);

    await waitFor(() => {
      expect(screen.getByTestId('ready').textContent).toBe('ready');
    });

    await act(async () => {
      screen.getByTestId('fetch').click();
    });

    await waitFor(() => {
      expect(alertsApi.getQuotes).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(screen.getByTestId('attempted').textContent).toBe('MSFT');
    });

    await act(async () => {
      screen.getByTestId('fetch').click();
    });
    // Still inside the 45s fail cooldown — no second request.
    expect(alertsApi.getQuotes).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(45_000);
    });

    await act(async () => {
      screen.getByTestId('fetch').click();
    });

    await waitFor(() => {
      expect(alertsApi.getQuotes).toHaveBeenCalledTimes(2);
    });
  });

  it('stops further batches when quotes endpoint returns 405', async () => {
    vi.mocked(api.get).mockResolvedValueOnce({ data: { ok: true } });
    const err = new Error('Method Not Allowed') as Error & {
      isAxiosError?: boolean;
      response?: { status: number };
    };
    err.isAxiosError = true;
    err.response = { status: 405 };
    vi.spyOn(axios, 'isAxiosError').mockReturnValue(true);
    vi.mocked(alertsApi.getQuotes).mockRejectedValueOnce(err);

    render(<ProbeHarness symbols={['AAPL', 'MSFT']} />);

    await waitFor(() => {
      expect(screen.getByTestId('ready').textContent).toBe('ready');
    });

    await act(async () => {
      screen.getByTestId('fetch').click();
    });

    await waitFor(() => {
      expect(screen.getByTestId('unavailable').textContent).toBe('yes');
    });
    expect(JSON.parse(screen.getByTestId('prices').textContent || '{}')).toEqual({});
    await waitFor(() => {
      expect(screen.getByTestId('attempted').textContent).toBe('AAPL,MSFT');
    });
  });

  it('does not lock quotesUnavailable on a 429 so a later fetch can retry', async () => {
    // 405 means the quotes route is absent and the picker should stop. 429 is
    // transient rate-limiting; treating it like 405 would hide live prices
    // until a full reload.
    vi.mocked(api.get).mockResolvedValueOnce({ data: { ok: true } });
    const err = new Error('Too Many Requests') as Error & {
      isAxiosError?: boolean;
      response?: { status: number };
    };
    err.isAxiosError = true;
    err.response = { status: 429 };
    vi.spyOn(axios, 'isAxiosError').mockReturnValue(true);
    vi.mocked(alertsApi.getQuotes)
      .mockRejectedValueOnce(err)
      .mockResolvedValueOnce({ data: { prices: { AAPL: 190.5 } } } as never);

    render(<ProbeHarness />);

    await waitFor(() => {
      expect(screen.getByTestId('ready').textContent).toBe('ready');
    });

    await act(async () => {
      screen.getByTestId('fetch').click();
      await vi.advanceTimersByTimeAsync(400);
    });

    await waitFor(() => {
      expect(alertsApi.getQuotes).toHaveBeenCalledTimes(1);
    });
    expect(screen.getByTestId('unavailable').textContent).toBe('no');
    expect(JSON.parse(screen.getByTestId('prices').textContent || '{}')).toEqual({});
    expect(screen.getByTestId('pending').textContent).toBe('0');

    await act(async () => {
      screen.getByTestId('fetch').click();
      await vi.advanceTimersByTimeAsync(400);
    });

    await waitFor(() => {
      expect(alertsApi.getQuotes).toHaveBeenCalledTimes(2);
    });
    await waitFor(() => {
      expect(JSON.parse(screen.getByTestId('prices').textContent || '{}')).toEqual({
        AAPL: 190.5,
      });
    });
    expect(screen.getByTestId('unavailable').textContent).toBe('no');
  });

  it('does not start a second getQuotes while the first request for that symbol is in flight', async () => {
    vi.mocked(api.get).mockResolvedValueOnce({ data: { ok: true } });
    let resolveQuotes: ((value: { data: { prices: Record<string, number> } }) => void) | undefined;
    vi.mocked(alertsApi.getQuotes).mockImplementation(
      () =>
        new Promise<{ data: { prices: Record<string, number> } }>((resolve) => {
          resolveQuotes = resolve;
        }) as never,
    );

    render(<ProbeHarness />);

    await waitFor(() => {
      expect(screen.getByTestId('ready').textContent).toBe('ready');
    });

    await act(async () => {
      screen.getByTestId('fetch').click();
      await vi.advanceTimersByTimeAsync(400);
    });

    await waitFor(() => {
      expect(alertsApi.getQuotes).toHaveBeenCalledTimes(1);
    });
    expect(screen.getByTestId('pending').textContent).toBe('1');

    await act(async () => {
      screen.getByTestId('fetch').click();
      await vi.advanceTimersByTimeAsync(400);
    });
    expect(alertsApi.getQuotes).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveQuotes?.({ data: { prices: { AAPL: 190.5 } } });
      await Promise.resolve();
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(JSON.parse(screen.getByTestId('prices').textContent || '{}')).toEqual({
        AAPL: 190.5,
      });
    });
    expect(screen.getByTestId('pending').textContent).toBe('0');
  });

  it('does not refetch catalog prices and only quotes the missing symbol', async () => {
    // AlertsSettings mergePrices() seeds saved catalog quotes. Treating those
    // as still-missing would burn Finnhub quota on every picker open, and an
    // empty merge (no saved prices) must not wipe quotes already on screen.
    vi.mocked(api.get).mockResolvedValueOnce({ data: { ok: true } });
    vi.mocked(alertsApi.getQuotes).mockResolvedValueOnce({
      data: { prices: { MSFT: 415.25 } },
    } as never);

    render(<ProbeHarness />);

    await waitFor(() => {
      expect(screen.getByTestId('ready').textContent).toBe('ready');
    });

    await act(async () => {
      screen.getByTestId('merge-aapl').click();
    });
    expect(JSON.parse(screen.getByTestId('prices').textContent || '{}')).toEqual({
      AAPL: 180.5,
    });

    await act(async () => {
      screen.getByTestId('merge-empty').click();
    });
    expect(JSON.parse(screen.getByTestId('prices').textContent || '{}')).toEqual({
      AAPL: 180.5,
    });

    await act(async () => {
      screen.getByTestId('fetch').click();
      await vi.advanceTimersByTimeAsync(400);
    });
    expect(alertsApi.getQuotes).not.toHaveBeenCalled();

    await act(async () => {
      screen.getByTestId('fetch-both').click();
      await vi.advanceTimersByTimeAsync(400);
    });

    await waitFor(() => {
      expect(alertsApi.getQuotes).toHaveBeenCalledTimes(1);
    });
    expect(alertsApi.getQuotes).toHaveBeenCalledWith(['MSFT']);
    await waitFor(() => {
      expect(JSON.parse(screen.getByTestId('prices').textContent || '{}')).toEqual({
        AAPL: 180.5,
        MSFT: 415.25,
      });
    });
  });

  it('trims, uppercases, and drops blanks so duplicate watches hit getQuotes once', async () => {
    // AlertsSettings can pass padded/mixed-case watch symbols. Sending blanks or
    // the same ticker twice would burn the shared quote route without adding prices.
    vi.mocked(api.get).mockResolvedValueOnce({ data: { ok: true } });
    vi.mocked(alertsApi.getQuotes).mockResolvedValue({
      data: { prices: { AAPL: 190.5 } },
    } as never);

    render(<ProbeHarness symbols={['  aapl  ', 'AAPL', '', '   ']} />);

    await waitFor(() => {
      expect(screen.getByTestId('ready').textContent).toBe('ready');
    });

    await act(async () => {
      screen.getByTestId('fetch').click();
      await vi.advanceTimersByTimeAsync(400);
    });

    await waitFor(() => {
      expect(alertsApi.getQuotes).toHaveBeenCalledTimes(1);
    });
    expect(alertsApi.getQuotes).toHaveBeenCalledWith(['AAPL']);
    await waitFor(() => {
      expect(JSON.parse(screen.getByTestId('prices').textContent || '{}')).toEqual({
        AAPL: 190.5,
      });
    });
  });

  it('treats a catalog price of 0 as present and merges a live 0 quote', async () => {
    // Saved closes can be 0 (halt / after a reverse split). `if (!price)` would
    // treat that as missing, refetch AAPL forever, and drop a live 0 from
    // getQuotes the same way Inf/NaN must be dropped.
    vi.mocked(api.get).mockResolvedValueOnce({ data: { ok: true } });
    vi.mocked(alertsApi.getQuotes).mockResolvedValueOnce({
      data: { prices: { MSFT: 0 } },
    } as never);

    render(<ProbeHarness />);

    await waitFor(() => {
      expect(screen.getByTestId('ready').textContent).toBe('ready');
    });

    await act(async () => {
      screen.getByTestId('merge-zero').click();
    });
    expect(JSON.parse(screen.getByTestId('prices').textContent || '{}')).toEqual({
      AAPL: 0,
    });

    await act(async () => {
      screen.getByTestId('fetch').click();
      await vi.advanceTimersByTimeAsync(400);
    });
    expect(alertsApi.getQuotes).not.toHaveBeenCalled();

    await act(async () => {
      screen.getByTestId('fetch-both').click();
      await vi.advanceTimersByTimeAsync(400);
    });

    await waitFor(() => {
      expect(alertsApi.getQuotes).toHaveBeenCalledTimes(1);
    });
    expect(alertsApi.getQuotes).toHaveBeenCalledWith(['MSFT']);
    await waitFor(() => {
      expect(JSON.parse(screen.getByTestId('prices').textContent || '{}')).toEqual({
        AAPL: 0,
        MSFT: 0,
      });
    });
    expect(screen.getByTestId('unavailable').textContent).toBe('no');
  });

  it('drops Inf/NaN quote values so they cannot seed prices and block refetch', async () => {
    // JSON.stringify turns Inf/NaN into null, and formatQuotePrice treats both as
    // missing. If fetchPricesFor merged them, the picker would show "—" forever
    // because pricesRef already has a key and isFetchBlocked would never retry.
    vi.mocked(api.get).mockResolvedValueOnce({ data: { ok: true } });
    vi.mocked(alertsApi.getQuotes)
      .mockResolvedValueOnce({
        data: {
          prices: {
            AAPL: 190.5,
            MSFT: Number.POSITIVE_INFINITY,
            GOOG: Number.NaN,
          },
        },
      } as never)
      .mockResolvedValueOnce({
        data: { prices: { MSFT: 415.25, GOOG: 140.0 } },
      } as never);

    render(<ProbeHarness symbols={['AAPL', 'MSFT', 'GOOG']} />);

    await waitFor(() => {
      expect(screen.getByTestId('ready').textContent).toBe('ready');
    });

    await act(async () => {
      screen.getByTestId('fetch').click();
      await vi.advanceTimersByTimeAsync(400);
    });

    await waitFor(() => {
      expect(alertsApi.getQuotes).toHaveBeenCalledTimes(1);
    });
    expect(alertsApi.getQuotes).toHaveBeenCalledWith(['AAPL', 'MSFT', 'GOOG']);
    await waitFor(() => {
      expect(JSON.parse(screen.getByTestId('prices').textContent || '{}')).toEqual({
        AAPL: 190.5,
      });
    });
    expect(screen.getByTestId('unavailable').textContent).toBe('no');

    await act(async () => {
      screen.getByTestId('fetch').click();
      await vi.advanceTimersByTimeAsync(400);
    });
    // Inf/NaN symbols share the 45s fail cooldown — do not burn Finnhub again.
    expect(alertsApi.getQuotes).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(45_000);
    });

    await act(async () => {
      screen.getByTestId('fetch').click();
      await vi.advanceTimersByTimeAsync(400);
    });

    await waitFor(() => {
      expect(alertsApi.getQuotes).toHaveBeenCalledTimes(2);
    });
    expect(alertsApi.getQuotes).toHaveBeenLastCalledWith(['MSFT', 'GOOG']);
    await waitFor(() => {
      expect(JSON.parse(screen.getByTestId('prices').textContent || '{}')).toEqual({
        AAPL: 190.5,
        MSFT: 415.25,
        GOOG: 140.0,
      });
    });
    expect(screen.getByTestId('unavailable').textContent).toBe('no');
  });
});
