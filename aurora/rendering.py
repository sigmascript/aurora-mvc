from jinja2 import (
    ChoiceLoader,
    Environment,
    FileSystemLoader,
    PrefixLoader,
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
        loader = FileSystemLoader(views_directory)
        if view_namespaces:
            loader = ChoiceLoader(
                [
                    PrefixLoader(
                        {name: FileSystemLoader(path) for name, path in view_namespaces.items()}
                    ),
                    loader,
                ]
            )
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
        source = self.environment.get_template(name)
        return source.render(**view.context)
