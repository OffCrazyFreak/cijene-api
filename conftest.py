def pytest_addoption(parser):
    parser.addoption(
        "--service-test-dsn", help="Disposable PostgreSQL server for tests"
    )
