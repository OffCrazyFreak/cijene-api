import asyncio
from unittest.mock import AsyncMock, Mock
from urllib.parse import unquote

import pytest
from starlette.requests import Request
from fastapi.security import HTTPAuthorizationCredentials
from service.routers import auth


@pytest.mark.parametrize(
    "raw", [b"/v1/a%3Fb/stores/", b"/v1/a%20b/stores/", b"/v1/a%0Ab/stores/"]
)
@pytest.mark.parametrize("fallback", [False, True])
def test_log_preserves_path_and_one_line(monkeypatch, raw, fallback):
    scope = {
        "type": "http",
        "method": "GET",
        "path": unquote(raw.decode()),
        "query_string": b"secret=hidden",
        "headers": [(b"user-agent", b"Example Agent\r\nforged\trow")],
        "client": ("127.0.0.1", 1),
    }
    if not fallback:
        scope["raw_path"] = raw
    log = Mock()
    user = Mock(id=1, name="test")
    monkeypatch.setattr(auth, "_key_log", log)
    monkeypatch.setattr(auth, "_lookup_user_by_token", AsyncMock(return_value=user))
    result = asyncio.run(
        auth.verify_authentication(
            Request(scope),
            HTTPAuthorizationCredentials(scheme="Bearer", credentials="token"),
        )
    )
    assert result is user
    fmt, *args = log.info.call_args.args
    line = fmt % tuple(args)
    assert line.split(" ", 4) == [
        "1",
        "GET",
        raw.decode(),
        "127.0.0.1",
        "Example Agent%0D%0Aforged%09row",
    ]
    assert "hidden" not in line
