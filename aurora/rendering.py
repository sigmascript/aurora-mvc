from jinja2 import (
    ChoiceLoader,
    Environment,
    FileSystemLoader,
    PrefixLoader,
    TemplateNotFound,
    select_autoescape,
)

from aurora.view import View


class ViewRenderer:
    """Render Views with Jinja2 view files from the configured directory.

    Autoescaping is enabled for HTML, HTM, and XML view-file extensions.
    Explicit namespaces take precedence over the ordinary view directory.
    """

    def __init__(self, views_directory, *, view_namespaces=None):
        # Namespace lookup is explicit; legacy filesystem view roots still work.
        loaders = []

        if view_namespaces:
            loaders.append(
                PrefixLoader(
                    {name: FileSystemLoader(path) for name, path in view_namespaces.items()}
                )
            )

        if views_directory is not None:
            loaders.append(FileSystemLoader(views_directory))

        if not loaders:
            raise ValueError("A view directory or app view namespaces are required")

        loader = ChoiceLoader(loaders) if len(loaders) > 1 else loaders[0]

        self.environment = Environment(
            loader=loader,
            autoescape=select_autoescape(["html", "htm", "xml"]),
        )

    def render(self, view: View) -> str:
        """Render a View's view and context; other inputs raise TypeError."""

        if not isinstance(view, View):
            raise TypeError("ViewRenderer.render() expects a View")

        # An explicit extension wins; extensionless names default to HTML.
        name = view.name if "." in view.name.rsplit("/", 1)[-1] else f"{view.name}.html"
        try:
            source = self.environment.get_template(name)
        except TemplateNotFound as error:
            namespace = name.split("/", 1)[0] if "/" in name else "shared"
            raise LookupError(
                f"Template {name!r} was not found (namespace: {namespace})"
            ) from error
        return source.render(**view.context)
