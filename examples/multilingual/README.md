# Multilingual Aurora example

Generated with `aurora new multilingual`, then adapted to use Aurora's locale-aware application settings.

From this directory run `aurora run --no-reload`. Open `/` (302 redirect to `/en/`), `/en/`, `/ku/`, `/ar/`, and `/en/articles/123/`. Unsupported locales such as `/es/` return 404.

Language selection uses the URL; a preference cookie is optional and not a session. No production language-switching endpoints are provided.
