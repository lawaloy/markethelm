# Troubleshooting & FAQ

---

<a id="common-issues"></a>
<details open>
<summary><b>Common issues</b></summary>

<a id="api-key-required"></a>
<details>
<summary><b>API key required</b></summary>

- Ensure a `.env` file exists with `FINNHUB_API_KEY=your-key`.
- Copy the key exactly from your Finnhub dashboard, with no spaces, quotes, or line breaks.
- Restart your terminal after creating `.env`.

</details>

<a id="rate-limit-exceeded-429"></a>
<details>
<summary><b>Rate limit exceeded (429)</b></summary>

- The tool has built-in retry logic.
- If it happens frequently:
  - Lower `top_n` in `config/filters.json`; see [CONFIGURATION.md](CONFIGURATION.md).
  - Wait 5–10 minutes between runs.
  - Consider upgrading to a paid Finnhub tier.

</details>

<a id="no-data-fetched"></a>
<details>
<summary><b>No data fetched</b></summary>

- Check your internet connection.
- Verify the Finnhub API key is valid.
- Check `logs/markethelm_errors_*.log` for details.
- Check [Finnhub API status](https://finnhub.io/status).

</details>

<a id="logs-not-showing"></a>
<details>
<summary><b>Logs not showing</b></summary>

- Logs are in the `logs/` folder (created automatically).
- Console shows INFO level; files show DEBUG level.

---

</details>

</details>

<a id="faq"></a>
<details>
<summary><b>FAQ</b></summary>

**Is this free?**  
Yes. Finnhub's free tier is sufficient for daily tracking.

**Can I track other stocks?**  
Yes. Edit `config/exchanges.json` to add symbols or change indices; see [CONFIGURATION.md](CONFIGURATION.md).

**What if I miss a day?**  
Rerun the tracker. Each run is independent; data is saved with the date.

**Can I backtest strategies?**  
MarketHelm can validate its saved projections with `markethelm backtest`. It is
not a general trading-strategy simulator, and it needs saved quotes and projections in the configured data store (`market_bars.sqlite`).

**Is my data private?**  
Yes. Data stays on your machine. API keys never leave your environment.

---

</details>

## Getting help

- Check `logs/markethelm_errors_*.log` for error details.
- Open a [GitHub issue](https://github.com/lawaloy/markethelm/issues) with log excerpts.
- Review [USAGE.md](USAGE.md) and [DEPLOYMENT.md](DEPLOYMENT.md) for setup questions.
