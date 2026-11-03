# First shadow model: ridge-return-v1

The live tracker records `ridge-return-v1` alongside the incumbent and benchmarks
on new forecast cases after the artifact's freeze time. The dashboard, recommendations,
alerts and customer-facing targets continue to use `heuristic-v1`.

## Model and fixed recipe

This small ridge regression learns a market-wide offset and how strongly to use
the incumbent's predicted return. It uses the existing rounded projection origin
price and midpoint, which are available before the outcome. It needs no extra
provider requests, runtime training, new service or ML framework.

For origin price `P` and incumbent midpoint `H`:

```text
x = clip((H / P - 1) / 0.05, -1, 1)
predicted_return = clip(intercept + slope * x, -0.05, 0.05)
midpoint = P * (1 + predicted_return)
```

Training minimizes mean squared five-session return error plus
`1.0 * (intercept² + slope²)`. Each origin session has equal total weight;
stocks within that session share its weight. Training returns are clipped to
±20%. The feature scale, clipping limits and regularization were fixed before
running this candidate's archived diagnostic. There is no hyperparameter search.

This is a point-only candidate. It does not supply confidence scores or ranges.
Those require a separate calibration study and fresh evaluation before any
customer-facing integration that exposes them.

## Frozen artifact and historical diagnostic

The packaged artifact is
[`ridge_return_v1.json`](../src/analysis/models/ridge_return_v1.json).
It preserves coefficients, recipe, calendar/horizon, incumbent feature version,
freeze time, training sample hash, source snapshot hash and trainer fingerprint.
The ledger also freezes the prediction implementation. Changing any of these
under the same registered version fails; future fits require a new model version.

The first fit uses 1,365 earlier forecasts across eight origin sessions from the
October 30 archive. The newest five scored origin dates are reserved, and 855
otherwise earlier forecasts are purged because their outcomes were unavailable
before the earliest held-out generation. The training source is retrospective;
it is never inserted into the prospective forecast ledger.

On 856 held-out forecasts:

| Model                         | Mean absolute price error | Equal-session mean error |   Direction accuracy |
| ----------------------------- | ------------------------: | -----------------------: | -------------------: |
| Current heuristic             |                    4.038% |                   4.037% |              50.117% |
| Unchanged price               |                    3.950% |                   3.950% | 0% (flat prediction) |
| Ridge return candidate        |                    3.915% |                   3.915% |              58.995% |
| Always-up direction reference |                       N/A |                      N/A |              58.995% |

The candidate improves equal-session price error by about 3.0% versus the
incumbent and 0.9% versus unchanged price. Both fall below the frozen 5%
review floor. The fitted offset makes every bounded candidate return positive,
so its directional accuracy matches always-up. These are diagnostic results,
not evidence for replacing the model. The archive's outcomes were already
observed, and the dates/stocks have correlated, overlapping horizons.

See the [research report](../baselines/research/ridge-return-v1/README.md)
for exact splits, parameters, hashes and reproduction instructions.

## Collection and review

No model selection setting is needed. After deploying this code, new eligible
tracker cases include all five versions atomically. Existing cases and scores
remain unchanged, and the candidate is never attached to an old case.
Cases generated before the artifact was frozen cannot use the candidate.
Exact retries preserve the original enrolled versions, including cases first
recorded by the previous release; enrollment uses generation time for new cases.
The first eligible case per stock/origin remains the experiment's primary case;
a later refresh does not replace an earlier case lacking this candidate.

```bash
markethelm forecasts report --output data/forward-forecast-report.json
```

Inspect `models.ridge-return-v1` for sample counts, coverage, paired errors,
per-origin-session stability and readiness failures. The scheduled evidence
archive includes this report and the cumulative ledger. The hosted collector
and local tracker use the same model bundle.

The existing `prospective-v1` thresholds stay unchanged: at least 200 scored
paired cases across 20 origin sessions and 25 symbols, 90% mature coverage,
5% equal-session error improvement and wins on 60% of sessions against both
required references. Numeric success still requires manual review. Fresh
candidate results take roughly 25 trading sessions from first enrollment to
mature; missing provider windows or insufficient symbol coverage can extend that.

## Offline reproduction

Unset `MARKETHELM_DATABASE_URL`, download the source archive, and choose a new
output directory:

```bash
python3 scripts/train_return_candidate.py --data-dir /path/to/archive \
  --output-dir /tmp/ridge-reproduction \
  --frozen-at 2026-11-03T10:55:08.016026+00:00
```

The script uses SQLite backup to read a private, consistent source snapshot,
refuses to overwrite an existing output bundle, and writes the artifact plus
`diagnostic.json`. It never installs a model, changes the active projector or
registers forecasts. A failed/insufficient training split fails explicitly;
it does not shrink the holdout or relax the sample floors.
