import { expect, test } from '../fixtures';

test('a real failed API request emits a scoped browser error and renders recovery guidance', async ({
  page,
}) => {
  const loggedErrors: string[] = [];
  const uncaughtErrors: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'error' && message.text().includes('[ERROR] pages/Dashboard:')) {
      loggedErrors.push(message.text());
    }
  });
  page.on('pageerror', (error) => uncaughtErrors.push(error.message));

  // Send the dashboard request to an absent API route on the real backend.
  // This exercises Axios, the component catch path, and the browser logger;
  // neither the HTTP response nor the logger is mocked.
  await page.route('**/api/market/overview', (route) =>
    route.continue({
      url: new URL('/api/market/logging-failure-probe', route.request().url()).toString(),
    }),
  );
  const failedResponse = page.waitForResponse((response) =>
    response.url().endsWith('/api/market/logging-failure-probe'),
  );
  await page.goto('/');
  expect((await failedResponse).status()).toBe(404);
  await expect(page.getByText(/No data yet/)).toBeVisible();
  await expect.poll(() => loggedErrors.length).toBe(1);
  expect(loggedErrors[0]).toMatch(
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z \[ERROR\] pages\/Dashboard: Error fetching dashboard data:/,
  );
  expect(uncaughtErrors).toEqual([]);
});
