# PostgreSQL startup and service tests

Run `bash scripts/test-service.sh` from the repository root with Docker available and `postgres:17-alpine` already present. The script builds the development target using the frozen lock, runs the full pytest suite with Python 3.13 and PostgreSQL 17, and removes its containers, network and labeled images on exit. Pass pytest arguments to select tests, for example `bash scripts/test-service.sh tests/service -v`.

The network is internal, no host ports are published, and the database uses bounded temporary storage. Test data contains authored rows and temporary random credentials. No source archive downloads or production connections are needed. Builds still need package registry access. The Python base is pinned by digest; operating system package mirrors are not a frozen snapshot.

For an existing disposable PostgreSQL server, run `uv run --frozen pytest tests/service --service-test-dsn=postgresql://test-user@localhost/test-admin` with your disposable server address. This is an explicit test command option; no new environment variable is required. The database account must be allowed to create/drop databases and install extensions. Each test creates a unique database and drops it in cleanup. Never point this input at a production server. Without it, service integration tests are explicitly skipped; crawler and barcode tests still run.

`database` yields a connected `PostgresDatabase` through an async context manager. `service_client` yields an HTTPX ASGI client inside the real application lifespan, using the provided database and the real bearer authentication dependency. It clears test authentication cache entries before and after use. Tests use `asyncio.run`, so no additional pytest plugin is required.

## Bootstrap contract

`create_tables()` reapplies `psql.sql` on each startup. The immutable unaccent wrapper is replaced before the expression index is created or reused. Both the extension function and its dictionary are resolved using the installed extension namespace, quoted as identifiers, and qualified explicitly. The wrapper has a catalog-only search path so callers cannot redirect these lookups through their own search path. An existing unaccent extension in another schema is retained.

The same bootstrap operation upgrades the old wrapper, so this change needs no migration runner or separate operator command. No applied migration file is rewritten. It does not relocate or drop extensions, change importer behavior, alter API limits, repair arbitrary pre-existing index corruption or change authentication caching. As before, bootstrap requires a trusted owner connection and an application schema protected from untrusted DDL. This change addresses caller search paths inside the wrapper; it does not make all bootstrap statements safe under an untrusted deployment search path.

The tests verify fresh and repeated bootstrap, replacement of the old function body, hostile function/dictionary shadowing, a non-public extension namespace, an expression-index query plan and real authenticated HTTP serialization. Later import, publication and pagination tests can reuse these fixtures without adding another test runtime.

## Review regressions

Cleanup tests preserve both the latest raw-price date and the latest statistics-backed API date per chain, including imports with no statistics, older statistics, a raw date after the cutoff, and the cutoff boundary. Dry-run counts must equal actual deletions. Only test VACUUM execution is replaced.

API key log paths use the escaped ASGI `raw_path`, or quote the decoded ASGI path when it is absent. Query strings are excluded. Control characters and field whitespace are percent-escaped; ordinary spaces remain valid in the final user-agent field. Source behavior was checked in [Uvicorn 0.34.2](https://github.com/encode/uvicorn/blob/0.34.2/uvicorn/protocols/http/h11_impl.py) and [Starlette 0.46.2](https://github.com/encode/starlette/blob/0.46.2/starlette/datastructures.py). Authentication and cache policies are unchanged.
