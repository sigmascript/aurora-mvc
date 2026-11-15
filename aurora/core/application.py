import inspect
import sqlite3
from datetime import timezone
from types import MappingProxyType
from urllib.parse import quote

from aurora.core.configuration import (
    validate_locale_cookie,
    validate_locales,
    validate_runtime_settings,
    validate_session_settings,
    validate_static_settings,
)
from aurora.core.controller import Controller
from aurora.core.http_exception import HTTPException
from aurora.core.request import ClientDisconnect, Request
from aurora.core.response import Response
from aurora.core.router import Router
from aurora.core.static_files import serve_static_file
from aurora.rendering import ViewRenderer
from aurora.view import View
from aurora.security.sessions import (
    SessionConflict,
    SessionManager,
    SessionStorageError,
)
from aurora.urls import _current_application, with_query


class Application:
    """Register routes and serve HTTP requests as an ASGI application.

    Supply ``views_directory`` to enable handlers to return View objects.
    Optional sessions, static files, and localized routes are configured
    through constructor keyword arguments.
    """

    def __init__(
        self,
        views_directory=None,
        *,
        sessions=False,
        session_settings=None,
        view_context=None,
        view_headers=None,
        static_directory=None,
        static_url="/statics",
        environment="production",
        statics_directory=None,
        view_namespaces=None,
        database_work=None,
        database_registry=None,
        max_request_body_bytes=None,
        locales=None,
        default_locale=None,
        locale_cookie_name=None,
        locale_entry_policy="explicit",
    ):
        # Resolve compatible session forms before validating dependent options.
        settings = validate_session_settings(sessions, session_settings)
        validate_runtime_settings(
            environment, database_work, database_registry, max_request_body_bytes
        )
        self.locales, self.default_locale = validate_locales(locales, default_locale)
        validate_locale_cookie(locale_cookie_name, self.locales, settings)
        if locale_entry_policy not in ("explicit", "default", "remembered") or not isinstance(locale_entry_policy, str):
            raise ValueError("locale_entry_policy must be explicit, default, or remembered")
        if locale_entry_policy != "explicit" and self.locales is None:
            raise ValueError("Locale entry redirects require configured locales")
        if locale_entry_policy == "remembered" and locale_cookie_name is None:
            raise ValueError("Remembered locale policy requires locale_cookie_name")
        self.locale_entry_policy = locale_entry_policy

        # Store validated settings and construct the core services.
        self.locale_cookie_name = locale_cookie_name
        self.max_request_body_bytes = max_request_body_bytes
        self.database_work = database_work
        self.database_registry = database_registry
        self._lifespan_started = False
        self._lifespan_stopped = False
        self.environment = environment
        # Defaults are inert; only from_config opts into project JSON settings.
        self.settings = MappingProxyType({})
        self.constants = MappingProxyType({})
        self.timezone = timezone.utc
        self.static_url, self.static_directory = validate_static_settings(
            static_directory, statics_directory, static_url
        )
        self.session_manager = SessionManager(settings) if settings is not None else None
        self.router = Router()
        self._middleware = []
        self._error_handlers = {}
        self._startup_hooks = []
        self._shutdown_hooks = []
        self.renderer = None

        if view_context is not None and not callable(view_context):
            raise TypeError("view_context must be callable")

        self.view_context = view_context
        self.view_headers = dict(view_headers or {})

        # Templates are optional for API-only applications.
        if views_directory is not None or view_namespaces:
            self.renderer = ViewRenderer(views_directory, view_namespaces=view_namespaces)
            self.renderer.environment.globals["url_for"] = self.url_for
            self.renderer.environment.globals["with_query"] = with_query
            self.renderer.environment.globals["static_url"] = self.static_url_for
            self.renderer.environment.globals["constants"] = self.constants

    @classmethod
    def from_config(cls, source, **constructor_options):
        """Create an application from validated JSON and explicit Python options.

        Explicit constructor options cannot override keys also provided by JSON.
        This prevents silent precedence changes in existing applications.
        """
        from aurora.core.app_settings import load_application_settings

        configuration = load_application_settings(source)
        overlap = set(constructor_options).intersection(configuration.constructor_options)
        if "sessions" in configuration.constructor_options:
            overlap.update(set(constructor_options) & {"sessions", "session_settings"})

        if overlap:
            raise ValueError("Application options supplied in both JSON and Python: "
                             + ", ".join(sorted(overlap)))

        options = {**configuration.constructor_options, **constructor_options}
        app = cls(**options)
        app.constants = configuration.constants
        if app.renderer is not None:
            app.renderer.environment.globals["constants"] = app.constants
        app.timezone = configuration.tzinfo
        app.settings = MappingProxyType({
            "name": configuration.name,
            "timezone": configuration.timezone_name,
            "constants": configuration.constants,
        })
        return app

    @classmethod
    def from_project(cls, entry_file, **constructor_options):
        """Load conventional project resources relative to the given entry file.

        Project paths are independent of the shell working directory. Existing
        applications using from_config and explicit resource paths are unchanged.
        """
        from pathlib import Path

        root = Path(entry_file).resolve().parent
        config = root / "aurora_config.json"
        views = root / "views"
        apps = root / "apps"
        namespaces = {}

        if apps.is_dir() and not apps.is_symlink():
            for app_dir in sorted(apps.iterdir()):
                if (app_dir.is_dir() and not app_dir.is_symlink()
                        and app_dir.name.isidentifier() and not app_dir.name.startswith("_")):
                    candidate = app_dir / "views"

                    if candidate.is_dir() and not candidate.is_symlink():
                        namespaces[app_dir.name] = candidate

        options = dict(constructor_options)

        if views.is_dir() and not views.is_symlink():
            options.setdefault("views_directory", views)

        if namespaces:
            options.setdefault("view_namespaces", namespaces)

        from aurora.core.app_settings import load_application_settings

        settings = load_application_settings(config)
        static_folder = root / (settings.statics or "statics")

        if static_folder.is_dir() and not static_folder.is_symlink():
            options.setdefault("statics_directory", static_folder)

        return cls.from_config(config, **options)

    # --- Middleware and route registration ---

    def add_middleware(self, middleware):
        """Register an async HTTP middleware callable (request, call_next).

        Middleware runs after session loading and before session saving.
        Registration order is outermost-first. Static files and lifespan
        messages are outside this HTTP middleware boundary.
        """

        if not callable(middleware):
            raise TypeError("middleware must be callable")

        if not inspect.iscoroutinefunction(middleware):
            raise TypeError("middleware must be an async callable")

        self._middleware.append(middleware)
        return middleware

    async def _run_http_middleware(self, request):
        async def dispatch(index):
            if index == len(self._middleware):
                return await self._handle_request(request)

            next_called = False

            async def call_next(next_request):
                nonlocal next_called

                if next_request is not request:
                    raise ValueError("call_next must receive the original request")

                if next_called:
                    raise RuntimeError("call_next may only be called once per request")

                next_called = True
                return await dispatch(index + 1)

            result = await self._middleware[index](request, call_next)

            if not isinstance(result, Response):
                raise TypeError("HTTP middleware must return a Response")

            return result

        return await dispatch(0)

    def add_error_handler(self, status_code, handler):
        """Register a (request, HTTPException) handler for an HTTP error status."""
        if (isinstance(status_code, bool) or not isinstance(status_code, int)
                or not 400 <= status_code <= 599):
            raise ValueError("HTTP error status must be an integer from 400 to 599")
        if not callable(handler):
            raise TypeError("HTTP error handler must be callable")
        self._error_handlers[status_code] = handler
        return handler

    def error(self, status_code):
        """Decorate a synchronous or asynchronous HTTP error handler."""
        def register(handler):
            return self.add_error_handler(status_code, handler)
        return register

    async def _http_error(self, request, status_code, detail, headers=None):
        """Render one custom HTTP error, falling back safely on failure."""
        fallback = Response(detail, status_code=status_code, headers=headers)
        handler = self._error_handlers.get(status_code)
        if handler is None:
            return fallback
        token = _current_application.set(self)
        try:
            result = handler(request, HTTPException(status_code, detail))
            if inspect.isawaitable(result):
                result = await result
            response = await self._response_from_result(request, result)
            response.status_code = status_code
            if headers:
                required = {name.lower() for name in headers}
                response.headers = {
                    **{key: value for key, value in response.headers.items()
                       if isinstance(key, str) and key.lower() not in required},
                    **headers,
                }
            response.prepare()  # Broken custom responses must fail before sending.
            return response
        except Exception:
            return Response("Internal Server Error", status_code=500)
        finally:
            _current_application.reset(token)

    def add_route(self, path, handler, methods=None, *, name=None):
        """Register and return a route for a sync or async handler.

        The handler receives the request and route parameters as keyword
        arguments. It may return a Response, a View, or response content.
        """
        return self.router.add(path=path, handler=handler, methods=methods, name=name)

    def route(self, path, controller, *, name=None):
        """Register a method-based Controller using its implemented HTTP verbs.

        Example: app.route("/signin", Signin) dispatches GET to Signin.get
        and POST to Signin.post. The existing add_route API stays available.
        """

        if not isinstance(controller, type) or not issubclass(controller, Controller):
            raise TypeError("route() expects a Controller subclass")

        methods = controller.http_methods()

        if not methods:
            raise ValueError("Controller must implement at least one HTTP method")

        route = self.add_route(path, controller.dispatch, methods=methods, name=name)
        route.method_controller = True
        return route

    def static_url_for(self, path):
        """Build a URL for a deliberate public asset in the configured static root.

        This does not check whether the file exists or grant access to it.
        """
        if not isinstance(path, str):
            raise TypeError("Static asset path must be a relative string")
        parts = path.split("/")
        if not parts or any(
            not part or part in {".", ".."} or part.startswith(".")
            or any(char in part for char in ("\\", "%", "?", "#", ":", "\x00"))
            or any(ord(char) < 32 or ord(char) == 127 for char in part)
            for part in parts
        ):
            raise ValueError("Unsafe static asset path")
        return self.static_url + "/" + "/".join(quote(part, safe="-._~") for part in parts)

    # --- URL and locale helpers ---

    def url_for(self, name, **params):
        """Build a relative URL for a named route."""
        return self.router.url_for(name, **params)

    def localized_url_for(self, name, *, language, **params):
        """Build a locale-prefixed named URL without reserving route parameter names."""

        if self.locales is None:
            raise RuntimeError("Multilingual routing is not configured")

        if not isinstance(language, str) or language not in self.locales:
            raise ValueError("Unsupported locale")

        path = self.url_for(name, **params)
        return f"/{language}" + ("" if path == "/" else path)

    def remember_locale(self, response, language, *, secure=False, max_age=31536000):
        """Explicitly persist a validated locale preference; never change URL locale.

        Use secure=True for HTTPS deployments. No session or database is required.
        """

        if self.locale_cookie_name is None:
            raise RuntimeError("Locale preference cookie is not configured")

        if not isinstance(response, Response):
            raise TypeError("response must be a Response")

        if not isinstance(language, str) or language not in self.locales:
            raise ValueError("Unsupported locale")

        response.set_cookie(
            self.locale_cookie_name, language, path="/", max_age=max_age,
            secure=secure, httponly=True, samesite="Lax",
        )
        return response

    # --- ASGI request lifecycle ---

    async def __call__(self, scope, receive, send):
        """Handle an ASGI HTTP request; ignore other scope types."""

        if scope["type"] == "lifespan":
            await self._handle_lifespan(receive, send)
            return

        if scope["type"] != "http":
            return

        if (
            self.static_directory is not None
            and scope.get("path", "").startswith(self.static_url + "/")
        ):
            await serve_static_file(
                scope, send, root=self.static_directory, url_prefix=self.static_url
            )
            return

        # Static requests bypass sessions and application middleware.
        request = Request(scope, receive, max_body_bytes=self.max_request_body_bytes)

        if self.locale_cookie_name is not None:
            candidate = request.cookies.get(self.locale_cookie_name)
            request.preferred_locale = candidate if candidate in self.locales else None

        if self.locales is not None:
            # The raw request.path remains intact for logging and application use.
            # Matching uses a request-local stripped path; no shared router mutation.
            parts = request.path.split("/", 2)

            if len(parts) >= 2 and parts[1] in self.locales:
                request.locale = parts[1]
                request.routing_path = "/" + parts[2] if len(parts) == 3 else "/"
            else:
                request.locale = None
                request.routing_path = None

        # Load session state before middleware, and save it after dispatch.
        try:
            if self.session_manager is not None:
                await self.session_manager.load(request)

            response = await self._run_http_middleware(request)
            if (self.locale_entry_policy == "remembered"
                    and request.locale is not None
                    and request.preferred_locale != request.locale
                    and request.method in {"GET", "HEAD"}
                    and 200 <= response.status_code < 300
                    and self.router.resolve(request)[0] is not None
                    and not request.routing_path.startswith("/api/")):
                self.remember_locale(
                    response, request.locale, secure=scope.get("scheme") == "https"
                )
                response.headers = {
                    **response.headers,
                    "Cache-Control": "private, no-store",
                    "Vary": "Cookie",
                }

            if self.session_manager is not None:
                await self.session_manager.save(request, response)
        except ClientDisconnect:
            # A disconnected peer cannot receive an HTTP response.
            return
        except (SessionConflict, SessionStorageError, OSError, sqlite3.Error):
            response = Response("Session Unavailable", status_code=503)
        except HTTPException as exc:
            response = await self._http_error(request, exc.status_code, exc.detail)
        except Exception:
            # Unexpected details are not exposed, even through custom 500 handlers.
            response = await self._http_error(request, 500, "Internal Server Error")

        # Prepare before sending headers: serialization failures can still
        # become a clean 500, but transport failures must never be retried.
        try:
            response.prepare()
        except Exception:
            response = Response("Internal Server Error", status_code=500)

        if request.method == "HEAD":
            # Preserve GET-equivalent metadata while suppressing the payload.
            # The wrapper also supports custom Response.__call__ overrides.
            async def send_head(message):
                if message.get("type") == "http.response.body":
                    message = {**message, "body": b""}

                await send(message)

            await response(send_head)
        else:
            await response(send)

    def on_startup(self, callback):
        """Register an async callback for ASGI startup (registration order)."""
        return self._register_lifespan_hook(self._startup_hooks, callback)

    def on_shutdown(self, callback):
        """Register an async callback for ASGI shutdown (reverse registration order)."""
        return self._register_lifespan_hook(self._shutdown_hooks, callback)

    def _register_lifespan_hook(self, hooks, callback):
        if not inspect.iscoroutinefunction(callback):
            raise TypeError("Lifespan hooks must be async callables")
        if self._lifespan_started or self._lifespan_stopped:
            raise RuntimeError("Lifespan hooks must be registered before startup")
        hooks.append(callback)
        return callback

    # --- ASGI lifespan ---

    async def _handle_lifespan(self, receive, send):
        """Run application hooks and drain accepted work before engine disposal.

        Hooks belong to each ASGI worker lifespan, not to application construction.
        If startup or shutdown fails, ASGI receives a failure rather than a
        misleading completion signal. A failed drain never disposes engines.
        """
        while True:
            message = await receive()
            kind = message.get("type")

            if kind == "lifespan.startup":
                if self._lifespan_started or self._lifespan_stopped:
                    await send({"type": "lifespan.startup.failed", "message": "Application already started or stopped"})
                    return
                try:
                    for callback in self._startup_hooks:
                        await callback()
                except BaseException:
                    await send({"type": "lifespan.startup.failed", "message": "Application startup hook failed"})
                    return
                self._lifespan_started = True
                await send({"type": "lifespan.startup.complete"})
            elif kind == "lifespan.shutdown":
                if self._lifespan_stopped:
                    await send({"type": "lifespan.shutdown.failed", "message": "Application already stopped"})
                    return
                try:
                    for callback in reversed(self._shutdown_hooks):
                        await callback()
                    if self.database_work is not None:
                        await self.database_work.aclose()
                        self.database_registry.close()
                    self._lifespan_stopped = True
                except BaseException:
                    await send({"type": "lifespan.shutdown.failed", "message": "Application shutdown failed"})
                    return
                await send({"type": "lifespan.shutdown.complete"})
                return
            else:
                raise RuntimeError("Unexpected ASGI lifespan message")

    # --- Route dispatch and view rendering ---

    async def _handle_request(self, request: Request) -> Response:
        """Resolve a route and convert its handler result into a response."""
        route, params = self.router.resolve(request)

        if route is None:
            if (self.locale_entry_policy != "explicit" and request.locale is None
                    and request.method in {"GET", "HEAD"}
                    and request.path != "/api" and not request.path.startswith("/api/")
                    and any(registered.pattern.fullmatch(request.path)
                            and ("GET" in registered.methods or "HEAD" in registered.methods)
                            for registered in self.router.routes)):
                # A redirect is only available for a genuine localized route;
                # explicit prefixes and unrelated paths never enter here.
                language = (request.preferred_locale
                            if self.locale_entry_policy == "remembered"
                            else None) or self.default_locale
                raw_query = request.scope.get("query_string", b"")
                if (not isinstance(raw_query, bytes) or not raw_query.isascii()
                        or any(byte <= 32 or byte >= 127 or byte == 35 for byte in raw_query)):
                    return await self._http_error(request, 400, "Invalid query string")
                if raw_query:
                    _ = request.query_params  # Validate percent escapes and UTF-8.
                suffix = ("?" + raw_query.decode("ascii")) if raw_query else ""
                headers = {"Location": f"/{language}{request.path}{suffix}"}
                if self.locale_entry_policy == "remembered":
                    headers.update({"Cache-Control": "private, no-store", "Vary": "Cookie"})
                return Response("", status_code=302, headers=headers)

            # A known path with an unsupported verb is 405 for *all* route
            # styles. Reflect registered methods, including implicit HEAD.
            allowed = set()
            if request.routing_path is not None:
                for registered in self.router.routes:
                    if registered.pattern.fullmatch(request.routing_path):
                        allowed.update(registered.methods)

            if allowed:
                if "GET" in allowed:
                    allowed.add("HEAD")
                return await self._http_error(
                    request, 405, "Method Not Allowed",
                    headers={"Allow": ", ".join(sorted(allowed))},
                )

            return await self._http_error(request, 404, "Not Found")

        # Keep request-local helpers available through template rendering.
        # Always restore the previous context, including on exceptions.
        token = _current_application.set(self)

        try:
            result = route.handler(request, **params)

            if inspect.isawaitable(result):
                result = await result

            return await self._response_from_result(request, result)
        finally:
            _current_application.reset(token)

    async def _response_from_result(self, request, result):
        if isinstance(result, Response):
            return result

        if isinstance(result, View):
            if self.renderer is None:
                raise RuntimeError("View rendering is not configured")

            # Context providers are opt-in, run per request, and cannot override
            # values supplied explicitly by the controller.
            if self.view_context is not None:
                provided = self.view_context(request)

                if inspect.isawaitable(provided):
                    provided = await provided

                if not isinstance(provided, dict):
                    raise TypeError("view_context must return a dictionary")

                context = {**provided, **result.context}
                result = View(result.name, context, status_code=result.status_code,
                              headers={**self.view_headers, **result.headers})

            # Request-local template navigation; never mutate shared Jinja globals.
            # Explicit controller context retains precedence.
            if self.locales is not None:
                def localized_url_for(name, *, language=None, **params):
                    target = request.locale if language is None else language

                    if target is None:
                        raise ValueError("No active URL locale")

                    return self.localized_url_for(name, language=target, **params)

                context = dict(result.context)
                context.setdefault("locale", request.locale)
                context.setdefault("localized_url_for", localized_url_for)
                result = View(result.name, context, status_code=result.status_code,
                              headers=result.headers)

            html = self.renderer.render(result)
            return Response(html, status_code=result.status_code,
                            content_type="text/html; charset=utf-8",
                            headers={**self.view_headers, **result.headers})

        return Response(result)
