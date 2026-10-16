# Multilingual applications

Aurora MVC supports optional locale-aware routing. Applications choose their supported locales and default locale explicitly; multilingual behavior is not automatically enabled for every project.

## Try the maintained example

From the Aurora MVC repository root, after installing the development dependencies:

```bash
cd examples/multilingual
aurora run --no-reload
```

The example configures `locales=["en", "ku", "ar"]`, `default_locale="en"`, and `locale_cookie_name="preferred_language"` on `Aurora`.

Try these routes:

| Path | Expected behavior |
| --- | --- |
| `/` | Redirects to `/en/` with HTTP 302 (example middleware) |
| `/en/`, `/ku/`, `/ar/` | Displays the active URL locale |
| `/en/articles/123` | Displays article `123` in the `en` locale context |
| `/es/` | Returns HTTP 404 because `es` is unsupported |

The application registers ordinary routes (`/` and `/articles/{id}`); Aurora handles the configured locale prefixes. The root redirect is implemented by **example-specific middleware**, not a universal framework redirect policy.

## Locale selection and cookies

In the example, the URL is authoritative when it includes a supported locale. A preference cookie is optional and is not an authentication session. No language-switching endpoint or translation catalog is supplied by this example.

## Generator support

The CLI currently provides `aurora new NAME`, `aurora new NAME --mvc`, and `aurora new NAME --modular`. There is no `--multilingual` flag. The repository example was adapted from a minimal generated project; see [Project templates and examples](project-templates.md).
