# Project website

The site is one page and one stylesheet in `site/`: `site/index.html` and `site/style.css`.
It is plain HTML5 with no build step. It presents the project using only the documents in
this repository. The page says what home-lab does, what has been measured, what is not built,
and how to install it.

## Preview locally

From the repository root, run:

```sh
python3 -m http.server --directory site 8000
```

Then open http://localhost:8000 in a browser. Stop the server with Ctrl+C.

## Rules

- No JavaScript, anywhere on the page.
- No web fonts, no images from another host, no analytics and no trackers. The page makes
  no external request of any kind.
- Links to repository documents are plain links to the GitHub pages for those files. They
  are absolute URLs of the form `https://github.com/roshanaryal1/home-lab/blob/main/<path>`,
  so they still work when the site is hosted elsewhere.
- Every fact on the page links to the document it comes from. A fact that no document
  supports is left out.
- The Content Security Policy meta tag in the head blocks everything except the site's own
  stylesheet. The page sets no inline styles and no event handlers, because the policy
  blocks both.
- Text uses plain short sentences, with no em dash and no semicolons.

## Tests

`tests/test_site.py` parses `site/index.html` with the standard library and checks the
policy meta tag, the absence of scripts and remote resources, that every relative link
points at a file under `site/`, that repository links point at files in this repository,
that the page has one h1 and declares `lang="en"`, and that neither site file contains an em
dash.

## Publishing

`.github/workflows/pages.yml` publishes `site/` to GitHub Pages on every push to `main`
that changes `site/` or the workflow, and when run by hand. Its actions are pinned by
commit hash. Until the owner switches it on, both of its jobs are skipped, not failed, so
nothing is published. To switch it on, the owner:

1. Opens the repository's Settings, then Pages, and sets the source to GitHub Actions.
2. Opens Settings, then Secrets and variables, then Actions, then Variables, and adds a
   repository variable named `PAGES_ENABLED` with the value `true`.
3. Runs the `pages` workflow once from the Actions tab.

The page is then at the address Pages shows. To take it down, set `PAGES_ENABLED` to
`false` and unpublish the site in Settings, then Pages.
