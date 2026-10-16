import inspect
import sqlite3

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
from aurora.urls import _current_application


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
    ):
        # Resolve compatible session forms before validating dependent options.
        settings = validate_session_settings(sessions, session_settings)
        validate_runtime_settings(
            environment, database_work, database_registry, max_request_body_bytes
        )
        self.locales, self.default_locale = validate_locales(locales, default_locale)
        validate_locale_cookie(locale_cookie_name, self.locales, settings)

        # Store validated settings and construct the core services.
        self.locale_cookie_name = locale_cookie_name
        self.max_request_body_bytes = max_request_body_bytes
        self.database_work = database_work
        self.database_registry = database_registry
        self._lifespan_started = False
        self._lifespan_stopped = False
        self.environment = environment
        self.static_url, self.static_directory = validate_static_settings(
            static_directory, statics_directory, static_url
        )
        self.session_manager = SessionManager(settings) if settings is not None else None
        self.router = Router()
        self._middleware = []
        self.renderer = None
        if view_context is not None and not callable(view_context):
            raise TypeError("view_context must be callable")
        self.view_context = view_context
        self.view_headers = dict(view_headers or {})

        # Templates are optional for API-only applications.
        if views_directory is not None:
            self.renderer = ViewRenderer(views_directory, view_namespaces=view_namespaces)
            self.renderer.environment.globals["url_for"] = self.url_for

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
            if self.session_manager is not None:
                await self.session_manager.save(request, response)
        except ClientDisconnect:
            # A disconnected peer cannot receive an HTTP response.
            return
        except (SessionConflict, SessionStorageError, OSError, sqlite3.Error):
            response = Response("Session Unavailable", status_code=503)
        except HTTPException as exc:
            response = Response(exc.detail, status_code=exc.status_code)
        except Exception:
            # Unexpected exception details must not be exposed to clients.
            response = Response("Internal Server Error", status_code=500)

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

    # --- ASGI lifespan ---

    async def _handle_lifespan(self, receive, send):
        """ASGI lifespan: drain accepted routed work before engine disposal.

        Shutdown failures are reported to the ASGI server; a failed drain never
        disposes potentially active connections. No database is opened at startup.
        """
        while True:
            message = await receive()
            kind = message.get("type")
            if kind == "lifespan.startup":
                if self._lifespan_stopped:
                    await send(
                        {"type": "lifespan.startup.failed", "message": "Application already stopped"}
                    )
                    return
                self._lifespan_started = True
                await send({"type": "lifespan.startup.complete"})
            elif kind == "lifespan.shutdown":
                try:
                    if self.database_work is not None:
                        await self.database_work.aclose()
                        self.database_registry.close()
                    self._lifespan_stopped = True
                except BaseException:
                    await send(
                        {"type": "lifespan.shutdown.failed", "message": "Database shutdown failed"}
                    )
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
            # Method-based endpoints distinguish an unknown URL from a
            # known URL called with an unsupported HTTP verb.
            for registered in self.router.routes:
                if (
                    getattr(registered, "method_controller", False)
                    and request.routing_path is not None
                    and registered.pattern.fullmatch(request.routing_path)
                ):
                    return Response("Method Not Allowed", status_code=405)
            return Response("Not Found", status_code=404)

        # Keep request-local helpers available through template rendering.
        # Always restore the previous context, including on exceptions.
        token = _current_application.set(self)
        try:
            result = route.handler(request, **params)
            if inspect.isawaitable(result):
                result = await result

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
        finally:
            _current_application.reset(token)
