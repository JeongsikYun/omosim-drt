# Contributing

Use a separate checkout/branch for changes. Never edit the source tree or caches used by an active experiment.

Keep operating scenarios in configuration files. Changes to candidate filters, passenger choice, objective functions, zone count or routing semantics are model changes; describe them and preserve before/after evidence. Do not call a modified solver Runtime-v2 without identifying the differences.

Before proposing a change:

```bash
python scripts/test_choice_rules.py
python scripts/test_travel_cache.py
python -m unittest discover -s tests
python scripts/smoke_test.py
```

For changes to the optimiser/global rescue, also run `python scripts/test_quality_portfolio.py`. Preserve the recorded seed, settings, geographic definition and data/routing identities. Large-scale research validation is separate from the synthetic software tests.

Do not commit population data, output folders, DB/WAL files, graph caches, credentials or compiled dependencies. Use small synthetic examples when reporting an issue. See LICENSE_STATUS.md for the current distribution-licence status.
