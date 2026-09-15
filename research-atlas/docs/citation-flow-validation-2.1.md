# Citation time atlas validation — v2.1.0

Checked on Windows, 2026-09-15. All datasets used below are synthetic, not research evidence.

## Automated checks

- Python: 1,091 passed, 1 skipped. Citation tests cover known analytical centers, paired populations, exact shared projection, zero vectors, unresolved/conflicting identifiers, duplicate edges, strict chronology, sidecar storage, render-budget independence, exports and read-only API behavior.
- JavaScript: 138 passed. Citation tests cover new-to-old direction, pulse endpoints, orbit bounds, eight-layer windows, scoped empty states, stale requests, escaped labels, paper evidence, navigation cleanup, pause, visibility and reduced motion.

## Chrome interaction checks

An isolated 90-paper corpus spanning 2020–2025 contained 172 eligible citation links. Year mode rendered all 90 points and 172 links. Checked period controls, monthly windows, pause (pulse coordinates remained unchanged), drag rotation while paused (plane coordinates changed), citation-line selection and opening the corresponding original paper. Separate synthetic-data labels remain visible. The existing landscape is a separate navigation view.

## Bounded performance smoke test

An isolated saved corpus with 20,000 papers, 30-dimensional vectors, four publication cohorts and 40,000 reference records produced exactly 30,000 valid edges. There were 15,000 matched citing papers and 10,000 unresolved outside-corpus references. Exact shared PCA fitted all 20,000 rows.

- Cold computation: 8.4745 seconds; cached response including copy: 0.0026 seconds. Storage write time was excluded.
- Display: 240 nodes and 120 actual edges, with both endpoints of every displayed edge retained. The node budget bound before the edge budget.
- Four cohort comparisons; serialized JSON 89,553 bytes. Cold and cached responses were equal.

This is one local smoke measurement, not a service-level guarantee. It does not establish a measured runtime or memory bound for 200,000 papers. Reference count, vector dimensions, machine and cache state affect cost. Rendering limits do not truncate the analytical population.
