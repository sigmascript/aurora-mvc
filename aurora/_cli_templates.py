"""Starter project templates used by the Aurora developer CLI.

Keep the generated output stable across CLI organization changes.
"""

_PROJECT = '''"""{name} Aurora application."""
from aurora import Aurora

app = Aurora.from_project(__file__)


def home(request):
    return "Hello from {name}!"


app.add_route("/", home)
'''

_MVC_APP = '''"""{name} MVC application."""
from aurora import Aurora
from controllers.Home import Home

app = Aurora.from_project(__file__)

app.route("/", Home)
'''

_MVC_HOME = '''"""Home page controller."""
from aurora import Controller, View


class Home(Controller):
    def get(self):
        return View("home", title="Welcome to {name}")
'''

_MVC_LAYOUT = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="description" content="Aurora MVC starter application">
    <title>{{% block title %}}Aurora MVC{{% endblock %}}</title>
    <link rel="stylesheet" href="/statics/css/site.css">
</head>
<body>
    <header class="site-header">
        <a class="brand" href="/">Aurora MVC</a>
    </header>
    <main id="main-content">
        {{% block content %}}{{% endblock %}}
    </main>
</body>
</html>
'''

_MVC_VIEW = '''{{% extends "layout.html" %}}
{{% from "components/actions.html" import action_link %}}

{{% block title %}}{{{{ title }}}}{{% endblock %}}

{{% block content %}}
    <section class="welcome" aria-labelledby="welcome-title">
        <h1 id="welcome-title">{{{{ title }}}}</h1>
        <p>Your Aurora MVC application is ready. Edit
           <code>controllers/Home.py</code> and <code>views/home.html</code>
           to build your first page.</p>
        {{{{ action_link("Aurora documentation", "https://auroramvc.com") }}}}
    </section>
{{% endblock %}}
'''


_MODULAR_APP = '''"""{name} modular MVC application."""
from aurora import Aurora
from apps.home.controllers.Home import Home
from apps.blog.controllers.Blog import Blog

app = Aurora.from_project(__file__)

app.route("/", Home)
app.route("/blog/", Blog)
'''

_MODULAR_HOME = '''"""Home page controller."""
from aurora import Controller, View


class Home(Controller):
    def get(self):
        return View("home/index", title="Welcome to {name}")
'''

_MODULAR_BLOG = '''"""Blog page controller."""
from aurora import Controller, View


class Blog(Controller):
    def get(self):
        return View("blog/index", title="Blog")
'''

_MODULAR_LAYOUT = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="description" content="Aurora modular MVC starter application">
    <title>{{% block title %}}{{{{ title }}}}{{% endblock %}}</title>
    <link rel="stylesheet" href="/statics/css/site.css">
</head>
<body>
    <header class="site-header">
        <a class="brand" href="/">Aurora MVC</a>
        {{% include "navigation.html" %}}
    </header>
    <main>
        {{% block content %}}{{% endblock %}}
    </main>
</body>
</html>
'''

_MODULAR_HOME_VIEW = '''{{% extends "layout.html" %}}

{{% block title %}}{{{{ title }}}}{{% endblock %}}

{{% block content %}}
    <h1>{{{{ title }}}}</h1>
    <p>Your modular Aurora project is ready. Edit
       <code>apps/home/controllers/Home.py</code> and
       <code>apps/home/views/index.html</code> to get started.</p>
{{% endblock %}}
'''

_MODULAR_BLOG_VIEW = '''{{% extends "layout.html" %}}
{{% from "components.html" import action_link %}}

{{% block title %}}{{{{ title }}}}{{% endblock %}}

{{% block content %}}
    <h1>{{{{ title }}}}</h1>
    <p>Your blog app is ready. Each app has its own controllers and views.</p>
    {{{{ action_link("Back to home", "/") }}}}
{{% endblock %}}
'''


# Includes and macros are native Jinja2 features: no extra Aurora runtime API.
_MVC_COMPONENTS = '''{% macro action_link(label, href) -%}
    <a class="action-link" href="{{ href }}">{{ label }}</a>
{%- endmacro %}
'''

_MODULAR_NAVIGATION = '''<nav aria-label="Main navigation">
    <a href="/">Home</a>
    <a href="/blog/">Blog</a>
</nav>
'''

_MODULAR_COMPONENTS = '''{% macro action_link(label, href) -%}
    <a class="action-link" href="{{ href }}">{{ label }}</a>
{%- endmacro %}
'''


_MVC_CSS = '''/* Aurora MVC starter: no external CSS dependencies. */
:root { color-scheme: light; font-family: system-ui, sans-serif; }
* { box-sizing: border-box; }
body { margin: 0; color: #17212b; background: #f7f9fc; line-height: 1.6; }
.site-header { padding: 1rem max(1rem, calc((100% - 56rem) / 2)); background: #fff; border-bottom: 1px solid #dce3eb; }
.brand { color: #173e73; font-weight: 700; text-decoration: none; }
main { max-width: 56rem; margin: 3rem auto; padding: 0 1rem; }
.welcome { padding: 2rem; background: #fff; border: 1px solid #dce3eb; border-radius: .75rem; }
h1 { line-height: 1.2; }
.action-link { display: inline-block; padding: .6rem 1rem; background: #173e73; color: #fff; border-radius: .4rem; text-decoration: none; }
a:focus-visible { outline: 3px solid #ffab40; outline-offset: 3px; }
@media (max-width: 40rem) { main { margin: 1.5rem auto; } .welcome { padding: 1.25rem; } }
'''

_MODULAR_CSS = '''/* Aurora modular starter: no external CSS dependencies. */
:root { color-scheme: light; font-family: system-ui, sans-serif; }
* { box-sizing: border-box; }
body { margin: 0; color: #17212b; background: #f7f9fc; line-height: 1.6; }
.site-header { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 1rem; padding: 1rem max(1rem, calc((100% - 56rem) / 2)); background: #fff; border-bottom: 1px solid #dce3eb; }
.brand { color: #173e73; font-weight: 700; text-decoration: none; }
nav { display: flex; flex-wrap: wrap; gap: 1rem; }
nav a { color: #173e73; text-decoration: none; }
main { max-width: 56rem; margin: 3rem auto; padding: 2rem; background: #fff; border: 1px solid #dce3eb; border-radius: .75rem; }
h1 { line-height: 1.2; }
.action-link { display: inline-block; padding: .6rem 1rem; background: #173e73; color: #fff; border-radius: .4rem; text-decoration: none; }
a:focus-visible { outline: 3px solid #ffab40; outline-offset: 3px; }
@media (max-width: 40rem) { main { margin: 1.5rem 1rem; padding: 1.25rem; } }
'''
