"""F03 URL query generation and template presentation boundaries."""

import json

import pytest

from aurora import Aurora, View, with_query
from aurora._cli_scaffolding import create_project
from aurora.rendering import ViewRenderer
from aurora.urls import url_for


def test_query_builder_preserves_path_and_existing_query_and_fragment():
    assert with_query('/articles/', {'page': 2, 'category': 'tech news'}) == '/articles/?page=2&category=tech+news'
    assert with_query('/articles/?sort=new#top', [('page', '2')]) == '/articles/?sort=new&page=2#top'
    assert with_query('/articles/#top', {}) == '/articles/#top'
    assert with_query('/articles/?', [('q', '')]) == '/articles/?q='


def test_repeated_query_keys_and_unicode_encoding():
    assert with_query('/search', [('tag', 'a'), ('tag', 'b'), ('q', 'café')]) == '/search?tag=a&tag=b&q=caf%C3%A9'
    assert with_query('/search', {'tag': ['a', 'b']}) == '/search?tag=a&tag=b'
    assert with_query('/search', {'active': True, 'offset': 0}) == '/search?active=true&offset=0'


@pytest.mark.parametrize('fields', [{'page': None}, {'x': {'y': 'z'}}, [(123, 'v')], 'raw', [('x', object())]])
def test_query_builder_refuses_ambiguous_inputs(fields):
    with pytest.raises(TypeError):
        with_query('/a', fields)


def test_query_builder_rejects_nonfinite_numbers():
    with pytest.raises(ValueError, match="finite"):
        with_query("/a", {"page": float("nan")})


def test_query_builder_does_not_reserve_named_route_parameters():
    app = Aurora()
    app.add_route('/articles/{query}/{page}/{params}/', lambda request, **kw: kw, name='detail')
    path = app.url_for('detail', query='a', page='2', params='z')
    assert with_query(path, [('page', 3)]) == '/articles/a/2/z/?page=3'
    with pytest.raises(RuntimeError, match='active request'):
        url_for('detail', query='a', page='2', params='z')


def test_config_constants_visible_in_templates_but_not_mutable(tmp_path):
    root = create_project('newsroom', directory=tmp_path, mvc=True)
    config = root / 'aurora_config.json'
    data = json.loads(config.read_text())
    data['constants'] = {'SITE_NAME': 'Aurora News'}
    config.write_text(json.dumps(data))
    (root / 'views' / 'brand.html').write_text('{{ constants.SITE_NAME }} | {{ with_query("/posts/", {"page": 2}) }}')
    app = Aurora.from_project(root / 'app.py')
    assert app.renderer.render(View('brand')) == 'Aurora News | /posts/?page=2'
    assert app.renderer.render(View('brand', constants={'SITE_NAME': 'Override'})) == 'Override | /posts/?page=2'
    with pytest.raises(TypeError):
        app.constants['SITE_NAME'] = 'Changed'
    assert 'request' not in app.renderer.environment.globals
    assert 'session' not in app.renderer.environment.globals


def test_missing_view_error_identifies_name_and_namespace_without_paths(tmp_path):
    shared = tmp_path / 'views'
    shared.mkdir()
    renderer = ViewRenderer(shared)
    with pytest.raises(LookupError, match='blog/missing.html.*namespace: blog') as exc:
        renderer.render(View('blog/missing'))
    assert str(tmp_path) not in str(exc.value)
