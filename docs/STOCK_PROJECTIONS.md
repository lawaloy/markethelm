# Stock Projections Feature

<a id="overview"></a>
<details open>
<summary><b>Overview</b></summary>

The Stock Projection system analyzes current market data and generates heuristic five-session price projections with buy/sell/hold recommendations for all tracked stocks.

</details>

<a id="features"></a>
<details>
<summary><b>Features</b></summary>

<a id="1-price-projections"></a>
<details>
<summary><b>1. Price Projections</b></summary>

- **5-Day Price Targets**: Low, Mid, and High price targets
- **Expected Change**: Percentage gain/loss projection
- **Projection Date**: Target date for the projection

</details>

<a id="2-recommendations"></a>
<details>
<summary><b>2. Recommendations</b></summary>

- **STRONG BUY**: High momentum bullish stocks with strong confidence
- **BUY**: Moderate bullish momentum
- **HOLD**: Neutral or unclear direction
- **SELL**: Moderate bearish pressure
- **STRONG SELL**: Strong bearish momentum, potential decline

</details>

<a id="3-confidence-scoring"></a>
<details>
<summary><b>3. Confidence Scoring</b></summary>

- **0-100% Confidence Score**: Based on multiple factors:
  - Momentum strength
  - Volatility levels
  - Trading volume
  - Market capitalization
  - Price stability

</details>

<a id="4-risk-assessment"></a>
<details>
<summary><b>4. Risk Assessment</b></summary>

- **Low Risk**: Stable, low volatility stocks
- **Medium Risk**: Moderate volatility
- **High Risk**: High volatility, unpredictable

</details>

<a id="5-trend-classification"></a>
<details>
<summary><b>5. Trend Classification</b></summary>

- **Bullish**: Upward momentum expected
- **Bearish**: Downward pressure expected
- **Neutral**: Sideways movement expected

</details>

</details>

<a id="how-it-works"></a>
<details>
<summary><b>How It Works</b></summary>

<a id="technical-analysis-components"></a>
<details>
<summary><b>Technical Analysis Components</b></summary>

1. **Momentum Score** (-100 to +100)
   - Calculated from recent price changes
   - Positive = Bullish, Negative = Bearish
   - Magnitude = Strength of trend

2. **Volatility Score** (0 to 100)
   - Measures price stability
   - Lower = More predictable
   - Higher = More risk/uncertainty

3. **Price Target Calculation**
   - Based on momentum direction
   - Adjusted for volatility range
   - Considers mean reversion for extreme moves

4. **Confidence Calculation**
   - Higher momentum = Higher confidence
   - Lower volatility = Higher confidence
   - High volume = Higher confidence
   - Large market cap = Higher confidence

</details>

<a id="recommendation-logic"></a>
<details>
<summary><b>Recommendation Logic</b></summary>

```text
STRONG BUY:  Momentum > 40 AND Volatility < 60
BUY:         Momentum > 15
HOLD:        Momentum between -15 and +15
SELL:        Momentum < -15
STRONG SELL: Momentum < -40 AND Volatility < 60
```

</details>

</details>

<a id="output-format"></a>
<details>
<summary><b>Output Format</b></summary>

<a id="console-display"></a>
<details>
<summary><b>Console Display</b></summary>

When you run the tracker, you'll see:

```text
=================================================================
STOCK PROJECTIONS - Next 5 Days
=================================================================
Total Projections: 178
Average Confidence: 73.2%
Expected Market Move: +0.13%

Recommendation Breakdown:
  HOLD: 93
  BUY: 34
  SELL: 31
  STRONG BUY: 13
  STRONG SELL: 7

Top 5 BUY Opportunities:
  1. AMAT - Target: $275.08 (+2.3%) | Confidence: 90%
     Reason: Positive +4.6% momentum; strong momentum

  ...

Top 5 SELL Warnings:
  1. ADBE - Target: $325.35 (-2.4%) | Confidence: 90%
     Reason: Negative -4.8% pressure; strong momentum

  ...
```

</details>

<a id="saved-storage"></a>
<details>
<summary><b>Saved storage</b></summary>

<a id="1-daily-summary-daily_summaries-in-hosted-db-or-data_dirmarket_barssqlite"></a>
<details>
<summary><b>1. Daily summary (<code>daily_summaries</code> in hosted DB or <code>DATA_DIR/market_bars.sqlite</code>)</b></summary>

Logical location: `summary:YYYY-MM-DD`. Payload includes complete projection data:

```json
{
  "projections": {
    "AAPL": {
      "symbol": "AAPL",
      "current_price": 150.5,
      "target_low": 148.2,
      "target_mid": 152.3,
      "target_high": 156.4,
      "expected_change_percent": 1.2,
      "recommendation": "BUY",
      "confidence": 75,
      "trend": "Bullish",
      "momentum_score": 25.5,
      "volatility_score": 35.2,
      "risk_level": "Low",
      "reason": "Positive +2.5% momentum; moderate momentum; high volume support",
      "projection_date": "2026-01-09",
      "projection_horizon_sessions": 5,
      "projection_calendar": "XNYS",
      "generated_at": "2026-01-04T10:30:00"
    }
  },
  "projection_summary": {
    "total_projections": 178,
    "recommendations": {
      "STRONG BUY": 13,
      "BUY": 34,
      "HOLD": 93,
      "SELL": 31,
      "STRONG SELL": 7
    },
    "average_confidence": 73.2,
    "average_expected_change": 0.13
  }
}
```

</details>

<a id="2-projections-table-projections-in-the-same-db-sidecar"></a>
<details>
<summary><b>2. Projections table (<code>projections</code> in the same DB sidecar)</b></summary>

Logical location: `projections:YYYY-MM-DD`. No `projections_*.csv` / `summary_*.json` write path.

Optional human report: `data/projections_YYYY-MM-DD.md`.

</details>

</details>

</details>

<a id="usage"></a>
<details>
<summary><b>Usage</b></summary>

<a id="running-the-tracker"></a>
<details>
<summary><b>Running the Tracker</b></summary>

The projection system is automatically integrated into the daily tracker:

```bash
# Run as normal - projections are included
python main.py

# Or via console script
markethelm
```

</details>

<a id="programmatic-access"></a>
<details>
<summary><b>Programmatic Access</b></summary>

```python
from src.workflows.tracker import StockTrackerWorkflow

# Run workflow
workflow = StockTrackerWorkflow()
result = workflow.run(use_screener=True)

if result['success']:
    # Access projections
    projections = result['projections']
    projection_summary = result['projection_summary']

    # Get specific stock projection
    aapl_projection = projections.get('AAPL')
    if aapl_projection:
        print(f"AAPL Target: ${aapl_projection['target_mid']:.2f}")
        print(f"Recommendation: {aapl_projection['recommendation']}")
        print(f"Confidence: {aapl_projection['confidence']}%")
```

</details>

<a id="custom-analysis"></a>
<details>
<summary><b>Custom Analysis</b></summary>

```python
from src.analysis.projector import StockProjector

# Initialize projector
projector = StockProjector()

# Generate projections for your data
current_stocks = [
    {'symbol': 'AAPL', 'close': 150.50, 'change_percent': 2.5, ...},
    # ... more stocks
]

projections = projector.generate_projections(current_stocks)
summary = projector.generate_projection_summary(projections)
```

</details>

</details>

<a id="important-notes"></a>
<details>
<summary><b>Important Notes</b></summary>

<a id="-disclaimer"></a>
<details>
<summary><b>⚠️ Disclaimer</b></summary>

**Stock projections are for informational purposes only and should not be considered financial advice.**

- Projections are based on technical analysis of recent price action
- Past performance does not guarantee future results
- Markets are influenced by many unpredictable factors
- Always do your own research and consult with financial advisors
- Consider multiple sources before making investment decisions

</details>

<a id="limitations"></a>
<details>
<summary><b>Limitations</b></summary>

1. **Short-term focus**: 5-session projections only
2. **Technical only**: No fundamental analysis (earnings, news, etc.)
3. **Historical data**: Requires at least 1 day of data
4. **No guarantees**: Market conditions can change rapidly

</details>

<a id="best-practices"></a>
<details>
<summary><b>Best Practices</b></summary>

✅ **Use as one input among many**
✅ **Compare with other analysis tools**
✅ **Monitor confidence scores**
✅ **Consider risk levels**
✅ **Track accuracy over time**
✅ **Adjust for market conditions**

</details>

</details>

<a id="projection-accuracy-shipped"></a>
<details>
<summary><b>Projection accuracy (shipped)</b></summary>

The projection files now record a five-session **XNYS** target, rather than adding
five calendar days. Premarket and intraday runs anchor that horizon to the last
completed exchange session, while post-close runs anchor to the session that just
closed. A reusable backtester evaluates only the exact target
session, keeps missing target closes explicit, and reports absolute
error, direction accuracy, target-band coverage, and confidence calibration by
confidence and recommendation cohort:

```bash
markethelm backtest --data-dir data --days 365 --output data/backtest.json
```

The dashboard and **`GET /api/history/accuracy`** use the same evaluator and show
the headline metrics, data coverage, confidence cohorts, recommendation cohorts,
and individual sample outcomes.

A versioned scenario baseline is committed under
**`baselines/projection-v1/`**. It spans all confidence cohorts, multiple market
session anchors, correct and incorrect directions, band hits and misses, and
pending/missing/invalid rows. Verify it with:

```bash
python3 scripts/projection_baseline.py check
```

This synthetic baseline protects evaluator semantics; it is not evidence of model
quality. Confidence must remain unchanged until enough real, forward-generated
projections mature into a representative out-of-sample report.

Real outcome collection is provenance-aware: each new daily row retains Finnhub's
explicit previous close (`pc`) and provider timestamp, and maps that close to the
preceding XNYS session only on an exchange session day. Run `python3 scripts/projection_baseline.py
assess --data-dir data --days 365` to see the evidence shortfall. The gate requires
200 scored samples across 20 run dates and 25 symbols, at least 90% coverage, and
two confidence cohorts of 30 or more samples. `capture` writes a report and input
hash manifest only after all gates pass; see [USAGE.md](USAGE.md#projection-backtesting).

New live runs also preserve immutable versioned predictions and paired benchmarks.
Future exact-session outcomes produce append-only scores, with model/session
comparisons available through `markethelm forecasts report`. See
[immutable forecasts and forward evaluation](FORWARD_FORECASTS.md). This supplies
prospective evidence; it does not change the current heuristic or calibrate its
confidence for customers.

A separate frozen temporal study now compares identical outcomes against an
unchanged-price forecast and directional/confidence benchmarks. It fits cohort
confidence and a residual-based range candidate only on outcomes available before
the held-out forecasts were generated. Brier score, cohort error, interval width,
and paired daily errors supplement the headline metrics. These diagnostics do not
change application scoring; see [the protocol](USAGE.md#projection-backtesting)
and [the initial real-data study](../baselines/research/projection-2026-10-30/README.md).

</details>

<a id="future-enhancements"></a>
<details>
<summary><b>Future Enhancements</b></summary>

Potential improvements for future versions:

- [ ] Multi-timeframe projections (1-day, 5-day, 30-day)
- [ ] Preserve a representative real out-of-sample baseline, then calibrate confidence
- [ ] Machine learning model integration
- [ ] Fundamental analysis factors
- [ ] News sentiment integration
- [ ] Sector correlation analysis
- [ ] Nested compound / extra indicators and SMS/push for high-confidence
      opportunities (RSI + shallow price/RSI compounds already ship; see
      [ARCHITECTURE.md](ARCHITECTURE.md#alert-workflow))

</details>

<a id="architecture"></a>
<details>
<summary><b>Architecture</b></summary>

<a id="module-structure"></a>
<details>
<summary><b>Module Structure</b></summary>

```text
src/analysis/projector.py
├── StockProjector
│   ├── generate_projections()      # Main entry point
│   ├── _project_stock()            # Single stock projection
│   ├── _calculate_momentum()       # Momentum scoring
│   ├── _calculate_volatility()     # Volatility analysis
│   ├── _determine_trend()          # Trend classification
│   ├── _calculate_targets()        # Price target calculation
│   ├── _generate_recommendation()  # Buy/sell logic
│   ├── _calculate_confidence()     # Confidence scoring
│   └── generate_projection_summary() # Aggregate statistics
```

</details>

<a id="integration-points"></a>
<details>
<summary><b>Integration Points</b></summary>

- **Workflow**: `src/workflows/tracker.py` - Step 4: Generate projections
- **CLI**: `src/cli/commands.py` - Display projection results
- **Storage**: `src/storage/data_storage.py` - Save projections and summaries to durable storage

</details>

</details>

<a id="example-results"></a>
<details>
<summary><b>Example Results</b></summary>

Based on actual test with 197 stocks:

- **Total Projections**: 178 (90% success rate)
- **Average Confidence**: 73.2%
- **Expected Market Move**: +0.13%

<a id="top-performers-test-data"></a>
<details>
<summary><b>Top Performers (Test Data)</b></summary>

**Top BUY Opportunities:**

1. AMAT: $268.87 → $275.08 (+2.3%, 90% confidence)
2. BA: $227.77 → $233.36 (+2.5%, 90% confidence)
3. AMD: $223.47 → $228.33 (+2.2%, 90% confidence)

**Top SELL Warnings:**

1. ADBE: $333.30 → $325.35 (-2.4%, 90% confidence)
2. PLTR: $167.86 → $165.53 (-1.4%, 90% confidence)
3. INTU: $629.46 → $613.80 (-2.5%, 90% confidence)

</details>

</details>

<a id="testing"></a>
<details>
<summary><b>Testing</b></summary>

The automated tests verify deterministic formulas, calendar alignment, storage,
and outcome evaluation. Real-data research measures forecast quality separately;
the current evidence does not establish a reliable predictive advantage.

<a id="test-results"></a>
<details>
<summary><b>Test Results</b></summary>

- Tests cover projection generation and invalid-input handling.
- Recommendations and risk labels follow the configured heuristic rules.
- Confidence scores are heuristic scores, not calibrated success probabilities.
- The real-data study reports midpoint errors, direction, confidence calibration,
  and low/high range coverage against explicit benchmarks.

</details>

</details>

## Support

For questions or issues with the projection system:

1. Check projection confidence scores - low confidence = less reliable
2. Review the `reason` field for explanation of projection
3. Compare multiple days of projections for trends
4. Monitor actual vs. projected performance

---

**Doc scope:** Describes the core **`StockProjector`** pipeline. For shipping status and related APIs, see [PROJECT_STATUS.md](PROJECT_STATUS.md) and the main [README.md](../README.md).
