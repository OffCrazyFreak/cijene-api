# Contributing

Thanks for your interest in improving this service! Contributions of every kind
are welcome: bug reports, feature ideas, documentation fixes, and code.

By participating, you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md).

## Read this first: which repo do you want?

This repository is a **fork of [`senko/cijene-api`](https://github.com/senko/cijene-api)**,
maintained for [Disscount](https://disscount.me). Upstream is the home of the
project and the place almost everything should go.

| You want to | Go to |
| --- | --- |
| Fix a crawler, report bad data, request an API change | [Upstream issues](https://github.com/senko/cijene-api/issues) |
| Contribute code to the service itself | Upstream, via a PR |
| Track what Disscount needs from the API | [Cijene API Roadmap](https://github.com/users/OffCrazyFreak/projects/10) (private) |
| Report something specific to this fork | Issues here |

Filing upstream is not a formality. It is where the maintainer and the other
contributors are, and a change merged there reaches everyone consuming
`api.cijene.dev` rather than one fork.

Two things to respect when filing upstream. Croatian and English are both used,
so write in whichever the surrounding thread uses. And file one focused issue at
a time: a batch of auto-generated `Step 1..N` issues was closed as `invalid` in
2026, so a split-into-steps dump is the fastest way to be ignored.

## Development setup

Python 3.13 and [uv](https://docs.astral.sh/uv/) are required. The full setup,
including the database and crawler usage, is in the [README](../README.md) and
[`docs/crawler.md`](../docs/crawler.md).

```sh
uv sync --dev
uv run pre-commit install
cp .env.example .env
```

## Pull request workflow

1. **Branch from `main`**: `feat/filter-by-chain`, `fix/lidl-missing-brand`,
   `docs/update-crawler-guide`.
2. **Match the existing style.** `ruff` is the only formatter and linter, and
   `ty` is the type checker. Both run in `pre-commit`, so installing the hook
   above means you rarely have to think about it.
3. **Verify locally before opening the PR:**

   ```sh
   uv run ruff check
   uv run ruff format --check --diff
   uv run ty check
   uv run pytest
   ```

4. **Write clear commits** in imperative mood and reference related issue
   numbers.
5. **Open the PR** with a summary of what changed and why, a link to the related
   issue, and any migration steps. For crawler changes, say which chain and what
   changed on their site.

### Crawler changes need extra care

A crawler breaks because a retailer changed their site, so the fix is only
verifiable against live data. Say in the PR which date you ran it for, how many
stores and prices came back, and paste a couple of parsed rows. A crawler that
imports zero rows silently is worse than one that fails loudly.

### Before you request review

- [ ] `ruff`, `ty` and `pytest` all pass locally.
- [ ] No secrets, credentials, or API tokens included.
- [ ] Crawler changes verified against a real fetch.

## Review process

CI runs on every PR (`.github/workflows/ci.yml`): ruff lint, ruff format check,
`ty`, and pytest, on Linux, macOS and Windows. Windows is in the matrix because
character encoding and zip handling have broken there before, so do not dismiss
a Windows-only failure as flaky.

## Licence

This project is licensed under **AGPL-3.0**, inherited from upstream.
Contributions are accepted under the same licence.
