import builtins
from collections.abc import MutableMapping
from typing import Any

import pytest
from fastapi import FastAPI, WebSocket

import fastapi_faults.router as router_module
from fastapi_faults import FaultRegistry
from fastapi_faults.router import (
    _effective_websocket_metadata,
    _effective_websocket_routes,
    _WebSocketMetadata,
)


async def receive() -> dict[str, Any]:
    return {"type": "websocket.disconnect"}


async def send(_message: MutableMapping[str, Any]) -> None:
    return None


def make_websocket(app: object, path: str = "/events") -> WebSocket:
    return WebSocket(
        {"type": "websocket", "path": path, "app": app},
        receive,
        send,
    )


def make_metadata() -> _WebSocketMetadata:
    return _WebSocketMetadata(
        registry=FaultRegistry(faults=[]),
        handshake_raises=(),
        closes=(),
    )


def test_effective_metadata_returns_original_without_fastapi_app() -> None:
    metadata = make_metadata()

    assert _effective_websocket_metadata(make_websocket(object()), metadata) is metadata


def test_effective_metadata_returns_original_when_no_route_matches() -> None:
    app = FastAPI()
    metadata = make_metadata()

    assert _effective_websocket_metadata(make_websocket(app), metadata) is metadata


def test_effective_metadata_returns_original_for_inconsistent_route_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    metadata = make_metadata()
    monkeypatch.setattr(
        router_module,
        "_effective_websocket_routes",
        lambda _routes: [object()],
    )

    assert _effective_websocket_metadata(make_websocket(app), metadata) is metadata


def test_websocket_route_iterator_supports_fastapi_without_route_contexts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()

    @app.websocket("/events")
    async def endpoint(websocket: WebSocket) -> None:
        del websocket

    original_import = builtins.__import__

    def import_without_route_contexts(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "fastapi.routing" and "iter_route_contexts" in fromlist:
            raise ImportError
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_route_contexts)

    assert _effective_websocket_routes(app.routes) == [app.routes[-1]]
