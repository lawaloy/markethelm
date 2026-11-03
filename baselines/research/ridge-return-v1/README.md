# Frozen ridge-return-v1 diagnostic

This is retrospective research for a shadow candidate, with no model promotion.
The [candidate documentation](../../../docs/SHADOW_MODEL.md) explains the fixed
recipe, deployment behavior and review requirements.

Source: the October 30, 2026 archive from
[workflow run 41156746107](https://github.com/lawaloy/markethelm/actions/runs/41156746107),
artifact `projection-forward-archive` (ID `13025626473`). Original download hashes
are recorded in the [source manifest](../projection-2026-10-30/source.json).
The training artifact records the hash of the private SQLite backup actually read;
SQLite backup encodings can differ between platforms.

The artifact was frozen at `2026-11-03T10:55:08.016026+00:00` with coefficients
`[0.0040154534590833854, 0.0012852793079429626]`. The fit uses 1,365 cases from
September 29–October 8, purges 855 unavailable earlier outcomes, and reserves
October 16, 19, 20, 21 and 22 (856 cases) for diagnosis. The full split, training
fingerprint, equal-session comparisons and per-session errors are in
[diagnostic.json](diagnostic.json); the packaged artifact is
[ridge_return_v1.json](../../../src/analysis/models/ridge_return_v1.json).

Candidate mean absolute price error is 3.915%, versus 4.038% for the incumbent
and 3.950% for unchanged price. Equal-session improvement is 3.024% and 0.884%
respectively, below the frozen 5% review floor. Its direction is always up,
matching the always-up reference at 58.995%. No confidence or ranges are supplied.

The recipe was fixed before this candidate's diagnostic. These outcomes had
already been observed in prior research, so the comparison is not a fresh test.
The candidate remains shadow-only regardless of this result; new prospective
cases are necessary for any promotion review. Reproduce using the command in
[SHADOW_MODEL.md](../../../docs/SHADOW_MODEL.md#offline-reproduction).
