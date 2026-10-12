import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
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

function Harness({ symbols = ['AAPL'] }: { symbols?: string[] }) {
  const {
    apiReady,
    symbolPrices,
    fetchPricesFor,
    pricingPending,
    quotesUnavailable,
    attemptedPrices,
  } = useSymbolPrices();

  return (
    <div>
      <span data-testid="ready">{apiReady ? 'ready' : 'loading'}</span>
      <span data-testid="unavailable">{quotesUnavailable ? 'yes' : 'no'}</span>
      <span data-testid="prices">{JSON.stringify(symbolPrices)}</span>
      <span data-testid="pending">{pricingPending.size}</span>
      <span data-testid="attempted">{[...attemptedPrices].sort().join(',')}</span>
      <button
        type="button"
        data-testid="fetch"
        onClick={() => {
          void fetchPricesFor(symbols);
        }}
      >
        fetch
      </button>
    </div>
  );
}

describe('useSymbolPrices unmount and dirty quotes', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(api.get).mockReset();
    vi.mocked(alertsApi.getQuotes).mockReset();
    vi.mocked(api.get).mockResolvedValue({ data: { ok: true } });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it('ignores a late getQuotes response after unmount', async () => {
    let resolveQuotes: ((value: { data: { prices: Record<string, number> } }) => void) | undefined;
    vi.mocked(alertsApi.getQuotes).mockImplementation(
      () =>
        new Promise<{ data: { prices: Record<string, number> } }>((resolve) => {
          resolveQuotes = resolve;
        }) as never,
    );

    render(<Harness />);

    await waitFor(() => {
      expect(screen.getByTestId('ready').textContent).toBe('ready');
    });

    await act(async () => {
      screen.getByTestId('fetch').click();
      await vi.advanceTimersByTimeAsync(400);
    });

    await waitFor(() => {
      expect(alertsApi.getQuotes).toHaveBeenCalled();
    });
    expect(screen.getByTestId('pending').textContent).toBe('1');

    cleanup();

    await act(async () => {
      resolveQuotes?.({ data: { prices: { AAPL: 190.5 } } });
      await Promise.resolve();
      await Promise.resolve();
    });

    // Remount a fresh harness — stale response must not leak prices/pending.
    render(<Harness />);
    await waitFor(() => {
      expect(screen.getByTestId('ready').textContent).toBe('ready');
    });
    expect(JSON.parse(screen.getByTestId('prices').textContent || '{}')).toEqual({});
    expect(screen.getByTestId('pending').textContent).toBe('0');
    expect(screen.getByTestId('attempted').textContent).toBe('');
  });

  it('drops non-finite quote prices and cools the symbol down', async () => {
    // Partial AxiosResponse is enough for this hook; cast like other API mocks.
    vi.mocked(alertsApi.getQuotes).mockResolvedValue({
      data: { prices: { AAPL: Number.POSITIVE_INFINITY, MSFT: 400 } },
    } as never);

    render(<Harness symbols={['AAPL', 'MSFT']} />);

    await waitFor(() => {
      expect(screen.getByTestId('ready').textContent).toBe('ready');
    });

    await act(async () => {
      screen.getByTestId('fetch').click();
      await vi.advanceTimersByTimeAsync(400);
    });

    await waitFor(() => {
      expect(alertsApi.getQuotes).toHaveBeenCalled();
    });

    await waitFor(() => {
      expect(JSON.parse(screen.getByTestId('prices').textContent || '{}')).toEqual({
        MSFT: 400,
      });
    });
    expect(screen.getByTestId('pending').textContent).toBe('0');

    // AAPL cooled down after non-finite quote — no immediate retry.
    const calls = vi.mocked(alertsApi.getQuotes).mock.calls.length;
    await act(async () => {
      screen.getByTestId('fetch').click();
      await vi.advanceTimersByTimeAsync(400);
    });
    expect(vi.mocked(alertsApi.getQuotes).mock.calls.length).toBe(calls);
  });
});
