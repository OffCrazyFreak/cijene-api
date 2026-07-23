<!-- Is this change useful to everyone, not just this fork? Then please open it
     against senko/cijene-api instead, so it reaches every deployment. -->

## Summary

<!-- What does this PR do, and why? -->

## Related issue

<!-- e.g. Closes #123, or a link to the upstream issue this implements. -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Documentation
- [ ] Refactor / chore
- [ ] Other:

## Crawler verification

<!-- Required for changes under crawler/. Remove if not applicable.
     A crawler that imports zero rows silently is worse than one that fails. -->

- Chain:
- Date crawled:
- Stores parsed / prices parsed:
- Sample of parsed rows:

## Checklist

- [ ] `uv run ruff check` and `uv run ruff format --check` pass.
- [ ] `uv run ty check` passes.
- [ ] `uv run pytest` passes.
- [ ] No secrets, credentials, or API tokens included.
- [ ] The linked issue's "Done when" checklist is ticked off, or says what is left.
- [ ] I have read the [Contributing guide](CONTRIBUTING.md).
