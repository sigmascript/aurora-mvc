# Project templates and examples

Aurora MVC includes four **repository examples** under `examples/`, but the `aurora new` CLI currently exposes **three starter layouts**. This distinction matters: multilingual routing is demonstrated in a maintained example, not selected with a `--multilingual` flag.

| Example directory | Generator | Focus |
| --- | --- | --- |
| `examples/minimal` | `aurora new NAME` | A small ASGI application without templates or a database |
| `examples/mvc` | `aurora new NAME --mvc` | Controller and Jinja view |
| `examples/modular` | `aurora new NAME --modular` | Separate apps and namespaced views |
| `examples/multilingual` | Adapted from `aurora new NAME` | Locale-aware application settings and routes |

## Run a repository example

First install Aurora MVC and its development dependencies from the repository root:

```bash
python -m pip install -e '.[dev]'
```

Then change into the example directory you want to try:

```bash
cd examples/minimal
aurora run --no-reload
```

The command above assumes your shell started in the repository root. For the other examples, substitute `mvc`, `modular`, or `multilingual` for `minimal`.

## What to try

- **Minimal:** open `/`.
- **MVC:** open `/` to see controller-to-view rendering.
- **Modular:** open `/` and `/blog` to see separate applications.
- **Multilingual:** open `/` for a redirect to `/en/`; also try `/en/`, `/ku/`, `/ar/`, and `/en/articles/123`. Unsupported locales such as `/es/` return 404 in this example.

The multilingual example selects language from the URL. It does not imply that every generated Aurora project automatically enables multilingual routes.

Each example has its own README documenting its behavior. The independent **NoteFlow** reference application is maintained outside the Aurora MVC framework repository.
