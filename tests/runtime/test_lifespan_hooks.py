"""F02.4 async application-owned lifecycle hooks and ASGI truthfulness."""

import asyncio

import pytest

from aurora.core.application import Application


def lifespan(app, *types):
    sent = []
    received = iter({"type": t} for t in types)

    async def receive():
        return next(received)

    async def send(message):
        sent.append(message)

    asyncio.run(app({"type": "lifespan"}, receive, send))
    return sent


def test_async_hooks_execute_only_during_lifespan_and_in_resource_order():
    app = Application()
    events = []

    @app.on_startup
    async def load_first():
        events.append("load-first")

    @app.on_startup
    async def load_second():
        events.append("load-second")

    @app.on_shutdown
    async def release_first():
        events.append("release-first")

    @app.on_shutdown
    async def release_second():
        events.append("release-second")

    assert events == []
    messages = lifespan(app, "lifespan.startup", "lifespan.shutdown")
    assert [m["type"] for m in messages] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]
    assert events == ["load-first", "load-second", "release-second", "release-first"]


def test_startup_failure_never_reports_success_or_leaks_exception_detail():
    app = Application()

    @app.on_startup
    async def broken():
        raise RuntimeError("private-credential")

    messages = lifespan(app, "lifespan.startup")
    assert messages == [
        {"type": "lifespan.startup.failed", "message": "Application startup hook failed"}
    ]
    assert not app._lifespan_started


def test_shutdown_failure_reports_failure_before_closing_owned_work():
    events = []

    class Work:
        async def aclose(self):
            events.append("work-drained")

    class Registry:
        def close(self):
            events.append("registry-closed")

    app = Application()
    # Inject lifecycle doubles after construction; production constructors validate real types.
    app.database_work = Work()
    app.database_registry = Registry()

    @app.on_shutdown
    async def broken():
        raise RuntimeError("private-token")

    messages = lifespan(app, "lifespan.startup", "lifespan.shutdown")
    assert [m["type"] for m in messages] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.failed",
    ]
    assert messages[-1]["message"] == "Application shutdown failed"
    assert not events


def test_shutdown_hooks_run_before_guarded_database_drain():
    events = []

    class Work:
        async def aclose(self):
            events.append("drain")

    class Registry:
        def close(self):
            events.append("dispose")

    app = Application()
    # Inject lifecycle doubles after construction; production constructors validate real types.
    app.database_work = Work()
    app.database_registry = Registry()

    @app.on_shutdown
    async def release():
        events.append("hook")

    lifespan(app, "lifespan.startup", "lifespan.shutdown")
    assert events == ["hook", "drain", "dispose"]


def test_hook_registration_requires_async_and_prestartup():
    app = Application()
    with pytest.raises(TypeError):
        app.on_startup(lambda: None)
    with pytest.raises(TypeError):
        app.on_shutdown(lambda: None)
    lifespan(app, "lifespan.startup", "lifespan.shutdown")

    async def callback():
        pass

    with pytest.raises(RuntimeError):
        app.on_startup(callback)
    with pytest.raises(RuntimeError):
        app.on_shutdown(callback)
    assert lifespan(app, "lifespan.startup")[0]["type"] == "lifespan.startup.failed"
