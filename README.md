<div align="center">
  <img src="https://raw.githubusercontent.com/lawaloy/markethelm/main/docs/assets/readme/markethelm-mark.png" alt="MarketHelm ship's helm and market chart logo" width="160">
  <h1>MarketHelm</h1>
  <p><strong>Market intelligence for informed decisions.</strong></p>
  <p>
    <a href="https://github.com/lawaloy/markethelm/actions/workflows/python-app.yml"><img src="https://github.com/lawaloy/markethelm/actions/workflows/python-app.yml/badge.svg?branch=main" alt="CI status"></a>
    <a href="https://pypi.org/project/markethelm/"><img src="https://img.shields.io/pypi/v/markethelm?logo=pypi&logoColor=white" alt="PyPI version"></a>
    <a href="https://github.com/lawaloy/markethelm/blob/main/LICENSE"><img src="https://img.shields.io/github/license/lawaloy/markethelm" alt="MIT license"></a>
  </p>
  <p>
    <a href="#purpose-and-direction">Our direction</a> ·
    <a href="#quick-start">Quick start</a> ·
    <a href="https://github.com/lawaloy/markethelm/blob/main/docs/README.md">Indexes</a>
  </p>
</div>

MarketHelm is building an open-source market intelligence platform that connects
market data, explainable signals, AI-assisted insight, and observed outcomes.
The aim is to help people understand markets, evaluate opportunities, and make
reasoned decisions.

## Purpose and direction

**Mission:** Turn market data into explainable insight that helps people evaluate
opportunities and make informed decisions.

**Vision:** A hosted product where users can follow markets, receive explained
recommendations and private alerts, and eventually execute through broker
integrations with risk limits and an audit trail.

The direction is **monitor → interpret → suggest → validate → act**. Current
priorities are projection evidence, hosted readiness, live data, and richer
alerts. ML, fundamentals, news context, and controlled trade execution are
longer-term goals in the [product roadmap](https://github.com/lawaloy/markethelm/blob/main/docs/PROJECT_STATUS.md#product-direction).

## From market data to inference

The current release provides:

| Capability            | What you can investigate                                                                                                             |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| Market context        | Movers, volume, and performance across stocks drawn from major indices.                                                              |
| Explained projections | Price targets five NYSE trading sessions ahead, with momentum, risk, confidence scores, and the reasoning behind each outlook.       |
| AI market briefs      | Natural-language summaries of observed market moves and index performance.                                                           |
| Outcome tracking      | Saved projections compared with exact target-session closes, including directional accuracy, price error, and target-range coverage. |
| Helmtower watches     | Price, RSI, and combined alert rules, with email, Slack, and Discord delivery.                                                       |

Price targets and confidence scores use technical heuristics; the optional
language model writes the market brief. The
[projection methodology](https://github.com/lawaloy/markethelm/blob/main/docs/STOCK_PROJECTIONS.md)
and [project status](https://github.com/lawaloy/markethelm/blob/main/docs/PROJECT_STATUS.md)
document the current model, validation evidence, and work ahead.

<p align="center">
  <a href="https://raw.githubusercontent.com/lawaloy/markethelm/main/docs/assets/readme/markethelm-hero.png">
    <picture>
      <source srcset="https://raw.githubusercontent.com/lawaloy/markethelm/main/docs/assets/readme/markethelm-hero-loop.webp" type="image/webp">
      <img src="https://raw.githubusercontent.com/lawaloy/markethelm/main/docs/assets/readme/markethelm-hero.png" alt="MarketHelm dashboard showing a market brief, gainers and decliners, and a Helmtower price watch" width="900">
    </picture>
  </a>
</p>

## Quick start

To run your own instance, use Python 3.12 or newer and a
[Finnhub API key](https://finnhub.io/register) for live market data.

<details>
<summary>Create and activate a virtual environment</summary>

On macOS or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

On Windows PowerShell:

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
```

</details>

Install the published package, which includes the dashboard:

```bash
pip install markethelm
```

Create `.env` in your working directory:

```text
FINNHUB_API_KEY=your-api-key-here
DATA_DIR=./data
```

From that same directory, collect market data and start the dashboard:

```bash
markethelm
markethelm-web
```

Open <http://localhost:8000>. The CLI, dashboard, and API share the saved market
data in `DATA_DIR`; the dashboard's market views need an initial fetch.

For AI-generated briefs, install `pip install "markethelm[ai]"` and set
`OPENAI_API_KEY` in `.env`. With no key, the brief uses a template.

<details>
<summary>Run from a source checkout</summary>

Use the Node.js version in [`.nvmrc`](https://github.com/lawaloy/markethelm/blob/main/.nvmrc), then install and build:

```bash
pip install -e .
cd dashboard/frontend
npm ci
npm run build
cd ../..
markethelm-web
```

For hot reload, see the [dashboard development guide](https://github.com/lawaloy/markethelm/blob/main/dashboard/README.md).

</details>

<a id="projection-validation"></a>

## Check an outlook against its outcome

Once saved projections have reached their target sessions, evaluate them with:

```bash
markethelm backtest --data-dir data --days 365 --output data/backtest.json
```

The [validation guide](https://github.com/lawaloy/markethelm/blob/main/docs/USAGE.md#projection-backtesting)
explains the metrics, missing-data handling, and requirements for a real-data
baseline. The [projection guide](https://github.com/lawaloy/markethelm/blob/main/docs/STOCK_PROJECTIONS.md)
explains how targets and scores are calculated.

## Documentation

| Topic                                | Guide                                                                                    |
| ------------------------------------ | ---------------------------------------------------------------------------------------- |
| Commands, reports, and backtesting   | [Usage](https://github.com/lawaloy/markethelm/blob/main/docs/USAGE.md)                   |
| Market data, optional AI, and alerts | [Configuration](https://github.com/lawaloy/markethelm/blob/main/docs/CONFIGURATION.md)   |
| Components and data flow             | [Architecture](https://github.com/lawaloy/markethelm/blob/main/docs/ARCHITECTURE.md)     |
| Persistence and self-hosting         | [Deployment](https://github.com/lawaloy/markethelm/blob/main/docs/DEPLOYMENT.md)         |
| Capabilities, evidence, and roadmap  | [Project status](https://github.com/lawaloy/markethelm/blob/main/docs/PROJECT_STATUS.md) |
| Development and pull requests        | [Contributing](https://github.com/lawaloy/markethelm/blob/main/CONTRIBUTING.md)          |

## License

[MIT](https://github.com/lawaloy/markethelm/blob/main/LICENSE).
