## Summary

Describe the observable behavior changed and why.

## Testing evidence

- [ ] I identified the lowest test layer that owns this behavior.
- [ ] I added or updated a regression test, or explained below why no test is
      appropriate.
- [ ] The regression test failed for the expected reason before the production
      change and passes afterward.
- [ ] Assertions verify meaningful postconditions, not only execution or an
      HTTP status.
- [ ] I considered validation failure, rollback/cleanup, repeated or stale
      actions, secret handling, and restored UI state where applicable.
- [ ] I ran the focused test while iterating.
- [ ] I ran the complete local gate documented in `DEVELOPER.md`.

Focused command and result:

```text

```

Complete-gate result:

```text

```

## Coverage and risk

- [ ] No coverage threshold or gate was weakened.
- [ ] No coverage exclusion was added, or the exact exception and its
      independently asserted user-visible behavior are explained below.
- [ ] Platform-specific behavior was verified on its owning platform or CI job.
- [ ] No production API, unreachable state, or broad refactor was introduced
      solely to satisfy coverage.

Test or exclusion rationale, residual risk, and follow-up work:

```text

```
