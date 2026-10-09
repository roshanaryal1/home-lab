"""The static project website in site/ keeps its promises (#363).

No scripts, no remote resources, a Content Security Policy in the head, every relative
link pointing at a file that exists, one h1 and no em dash. The page is parsed with the
standard library only.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
INDEX = SITE / "index.html"
STYLE = SITE / "style.css"
EM_DASH = chr(0x2014)
REPO = "https://github.com/roshanaryal1/home-lab/"
URL_ATTRS = ("href", "src", "srcset", "action", "formaction", "data", "poster", "background")


class _Page(HTMLParser):
    """Every start tag with its attributes, and the ids on the page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        self.tags.append((tag, values))
        if "id" in values:
            self.ids.add(values["id"])


def _parse() -> _Page:
    page = _Page()
    page.feed(INDEX.read_text(encoding="utf-8"))
    page.close()
    return page


def test_the_csp_meta_tag_denies_everything_by_default() -> None:
    page = _parse()
    policies = [attrs for tag, attrs in page.tags
                if tag == "meta"
                and attrs.get("http-equiv", "").lower() == "content-security-policy"]
    assert len(policies) == 1, "the head needs exactly one Content-Security-Policy meta tag"
    assert "default-src 'none'" in policies[0]["content"]
    assert "style-src 'self'" in policies[0]["content"]


def test_there_is_no_script_element() -> None:
    assert not [tag for tag, _ in _parse().tags if tag == "script"]


def test_no_element_loads_from_another_host() -> None:
    problems = []
    for tag, attrs in _parse().tags:
        for name in URL_ATTRS:
            if name not in attrs:
                continue
            value = attrs[name]
            scheme = urlsplit(value).scheme
            # A link is not a request, so a plain https link is allowed.
            if tag == "a" and scheme == "https":
                continue
            if scheme or value.startswith("//"):
                problems.append(f"<{tag} {name}={value!r}>")
    assert not problems, f"resources or links that are not relative: {problems}"


def test_every_relative_link_and_source_points_at_a_file_under_site() -> None:
    page = _parse()
    missing = []
    for tag, attrs in page.tags:
        for name in URL_ATTRS:
            value = attrs.get(name)
            if not value or urlsplit(value).scheme or value.startswith("//"):
                continue
            path, _, fragment = value.partition("#")
            if not path:
                if fragment and fragment not in page.ids:
                    missing.append(f"{value} (no element with that id)")
                continue
            target = (SITE / path).resolve()
            if not (target.is_relative_to(SITE.resolve()) and target.is_file()):
                missing.append(f"<{tag} {name}={value!r}>")
    assert not missing, f"relative links that point at nothing under site/: {missing}"


def test_the_stylesheet_is_linked_relatively_and_loads_nothing_remote() -> None:
    styles = [attrs for tag, attrs in _parse().tags
              if tag == "link" and attrs.get("rel") == "stylesheet"]
    assert [s["href"] for s in styles] == ["style.css"]
    css = STYLE.read_text(encoding="utf-8")
    assert "@import" not in css and "url(" not in css
    assert "http:" not in css and "https:" not in css


def test_no_inline_styles_or_event_handlers() -> None:
    # The policy blocks both, so a page that uses them would render wrongly.
    tags = _parse().tags
    offenders = [f"<{tag} {name}>" for tag, attrs in tags for name in attrs
                 if name == "style" or name.startswith("on")]
    assert not offenders
    assert not [tag for tag, _ in tags if tag == "style"]


def test_repository_links_point_at_files_that_exist() -> None:
    missing = []
    for _, attrs in _parse().tags:
        href = attrs.get("href", "")
        if not href.startswith(REPO):
            continue
        for kind in ("blob/main/", "tree/main/"):
            if href.startswith(REPO + kind):
                path = href[len(REPO + kind):].partition("#")[0]
                if not (ROOT / path).exists():
                    missing.append(href)
    assert not missing, f"repository links to files that are not in this repo: {missing}"


def test_the_page_has_exactly_one_h1() -> None:
    assert [tag for tag, _ in _parse().tags].count("h1") == 1


def test_the_html_element_declares_english() -> None:
    roots = [attrs for tag, attrs in _parse().tags if tag == "html"]
    assert roots and roots[0].get("lang") == "en"


def test_no_em_dash_in_the_page_or_its_stylesheet() -> None:
    for path in (INDEX, STYLE):
        assert EM_DASH not in path.read_text(encoding="utf-8"), path.name
