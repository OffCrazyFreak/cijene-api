# Security Policy

## Where to report

Please **do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.**

This repository is a fork of
[`senko/cijene-api`](https://github.com/senko/cijene-api), so **which repo you
report to depends on where the flaw lives**:

- **In the service or crawler code** (anything you can see in this tree): report
  it upstream, privately, via
  [senko/cijene-api security advisories](https://github.com/senko/cijene-api/security/advisories/new).
  A fix there protects every deployment, including `api.cijene.dev`.
- **In how Disscount deploys or consumes this code**, or in something we changed
  in this fork: report it to us via
  [Report a vulnerability](https://github.com/OffCrazyFreak/cijene-api/security/advisories/new)
  or by email at info@disscount.me.

If you are not sure which applies, report it to us and we will forward it
upstream with credit to you.

## What to include

- The type of issue (for example: SQL injection, API key leakage, SSRF in a
  crawler fetch, path traversal in archive import).
- The affected component (a crawler module, an API route, the import pipeline)
  and the commit you tested against.
- Steps to reproduce, and a proof of concept if possible.
- The potential impact.

## What to expect

- We aim to acknowledge your report within a few days.
- We will keep you informed as we investigate and work on a fix.
- Please give us reasonable time to release a fix before any public disclosure.

We appreciate responsible disclosure and will credit reporters who wish to be
acknowledged.
