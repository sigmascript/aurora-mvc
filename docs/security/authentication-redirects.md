# Authentication guards and safe return redirects (D1)

Aurora MVC supplies optional request guards, not a user database or a built-in login system. Your application owns credential verification, session configuration, and the authentication predicate.

## Protect a controller action

```python
from aurora import Controller
from aurora.security import login_required


def is_authenticated(request):
    return request.session.get("user_id") is not None


class NewNote(Controller):
    @login_required("auth.signin", authenticated=is_authenticated, with_next=True)
    def get(self):
        return "New note form"
```

The application must configure sessions and register the named `auth.signin` route. `with_next=True` is opt-in. An anonymous browser GET or HEAD request receives a redirect to sign-in with an encoded local return path. Unauthenticated POST/PUT/PATCH/DELETE requests fail with HTTP 403 instead of replaying the submitted request. `api=True` returns HTTP 401 instead of redirecting. For authenticated requests, the action executes normally.

`authenticated=` is required and must be a callable receiving the request. An async predicate may be used with an async controller action; a sync action cannot await an async predicate. Do not rely on a hardcoded session key inside the framework.

## Redirect authenticated visitors away from sign-in

```python
from aurora.security import login_abort

class Signin(Controller):
    @login_abort("notes.index", authenticated=is_authenticated)
    def get(self):
        return "Sign-in form"
```

Both decorators use named routes. When `request.locale` contains an active URL locale, the guard resolves the route using Aurora's locale-aware URL resolver. An unprefixed URL is not automatically rewritten using a preferred-language cookie.

## Validate `next` on both GET and POST

```python
from aurora.security import request_next, safe_next, redirect_local
from aurora import url_for

# On GET /auth/signin?next=... :
return_to = request_next(self.request, url_for("notes.index"))
# Render the sign-in form with an HTML-escaped hidden `next` field.

# After CSRF validation and successful credential verification on POST:
return_to = safe_next(form_data.get("next"), url_for("notes.index"))
# Establish / rotate the authenticated session, then:
return redirect_local(return_to)
```

The example above shows relevant statements, **not a complete controller**. Validate the submitted hidden field again; never trust it merely because the GET view already validated it. The application must validate CSRF before accepting login form data and must authenticate the user before redirecting. Do not automatically replay the original POST request.

`safe_next(value, fallback)` accepts a local absolute path or returns a validated local fallback. An invalid fallback raises `ValueError`. `request_next(request, fallback)` accepts exactly one `next` query parameter. It rejects external origins, protocol-relative paths, backslashes, control characters, fragments, and encoded separator bypass attempts. The maximum return URL length is 2048 characters.

## Behavior matrix

| Situation | Result |
|---|---|
| Authenticated protected action | Execute action |
| Anonymous GET/HEAD, browser mode | Redirect to named sign-in route |
| Anonymous state-changing request | HTTP 403 |
| Anonymous API request with `api=True` | HTTP 401 |
| Authenticated action protected by `login_abort` | Redirect to named destination |
| Invalid or duplicate `next` | Use trusted fallback |

## Security limitations

- These decorators are authorization *entry checks*, not substitutes for per-record permissions. Continue checking ownership and access inside application logic.
- They do not configure sessions, CSRF, cookies, password hashing, rate limiting, or authentication middleware.
- A redirect target may be a local application path that is not suitable for every application; consider additional allowlists where necessary.
- Reverse proxy and production deployment configurations require separate review.
- NoteFlow is maintained in a separate repository as a reference application. Aurora tests cover guard behavior, including locale-aware redirects; this documentation does not claim a complete localized sign-in application is bundled with the framework.
