### Fixed
- `Budgets(clock=...)` ages the default in-memory counters by that clock instead of the wall clock, so a budget built with a past or future clock no longer reads back as zero spend once the real time passes the window's expiry.
