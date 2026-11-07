"""The content worker: everything that parses a fetched page, in its own process.

The server runs `python -I -m mcp_server_fetch._worker` once per fetch, writes one
JSON request to its stdin and reads one JSON result from its stdout, killing it
(and anything it started) when it overruns. `run()` is the whole job as a pure
function: the process entry point and the in-process test seam both call it.
"""

import functools
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator, Sequence
from typing import Any
from urllib.parse import urljoin, urlsplit
from xml.etree.ElementTree import Element

import html5lib
import markdownify
import readabilipy
import readabilipy.simple_tree
from html5lib.constants import namespaces, voidElements
from html5lib.serializer import HTMLSerializer
from html5lib.treewalkers.base import TreeWalker

from . import _textsan
from .title import with_page_title

_HTML = "{" + namespaces["html"] + "}"  # the etree tag prefix of HTML elements
_FAILED = "<error>Page failed to be simplified from HTML</error>"
_PY_RESERVE = 5.0  # seconds kept back for the pure-Python fallback after Node
_NODE_MAX_OUTPUT = 64 * 1024 * 1024
_PROBE_TIMEOUT = 5.0
_DEFAULT_BUDGET = 30.0  # extract_content_from_html (in-process callers)
_MEMORY_LIMIT = 1536 * 1024 * 1024  # Linux RLIMIT_DATA for the worker and its Node
_NO_WINDOW: dict[str, Any] = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
)
# Never shown by a browser (template/noscript: scripting on; iframe: its own document;
# noembed/noframes: fallbacks no current browser renders; rp: ruby fallback).
_STRIP = frozenset(
    {"script", "style", "template", "noscript", "iframe", "noembed", "noframes", "rp"}
)
# Hidden page furniture expected to stay hidden: never a hydration container (#3878).
_CHROME_TAGS = frozenset({"nav", "header", "footer", "aside", "menu", "dialog"})
_CHROME_ROLES = frozenset(
    {
        "navigation",
        "banner",
        "contentinfo",
        "complementary",
        "menu",
        "menubar",
        "dialog",
        "alertdialog",
    }
)
_PROVISION = "uv run --locked python scripts/provision_node_deps.py"
_STAMP = ".fetch-node-deps.sha256"  # written by _PROVISION next to node_modules
_READABILITY_JS = """
const nm = %s;
const {JSDOM} = require(nm + '/jsdom'), {Readability} = require(nm + '/@mozilla/readability');
const html = require('fs').readFileSync(0, 'utf8').trim();
const a = new Readability(new JSDOM(html).window.document).parse();
process.stdout.write(JSON.stringify(a && a.content || ''));
"""


def _which_node() -> str | None:
    """Node from an absolute PATH entry only: never the working directory or a relative
    entry, and never a .cmd/.bat shim."""
    name = "node.exe" if sys.platform == "win32" else "node"
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        if sys.platform == "win32":
            # Require both an absolute path and a drive/UNC anchor: `C:tools` and
            # `\tools` are drive-relative/root-relative, so Windows would resolve
            # them against the current drive/cwd.
            if not (os.path.isabs(entry) and os.path.splitdrive(entry)[0]):
                continue
        elif not os.path.isabs(entry):
            continue
        path = os.path.join(entry, name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _provisioned(javascript: str) -> bool:
    """Whether node_modules is still what scripts/provision_node_deps.py installed:
    the sha256 of npm's hidden lockfile equals the stamp the script wrote after `npm ci`
    and its probe succeeded. A missing stamp or lockfile is a mismatch."""
    try:
        with open(
            os.path.join(javascript, "node_modules", ".package-lock.json"), "rb"
        ) as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        with open(os.path.join(javascript, _STAMP), encoding="ascii") as f:
            stamp = f.read()
    except (OSError, ValueError):  # ValueError: a stamp that is not ASCII
        return False
    return stamp == digest + "\n"


@functools.cache
def find_node() -> tuple[str, ...] | None:
    """Once per process, at server start. Never installs anything."""
    node = _which_node()
    javascript = os.path.join(os.path.dirname(readabilipy.__file__), "javascript")
    modules = os.path.join(javascript, "node_modules")
    if node is None or not os.path.isdir(modules):
        return None
    if not _provisioned(javascript):
        print(
            "mcp-server-fetch: readabilipy's javascript/node_modules is not the provisioned "
            f"Readability.js install; using the pure-Python extractor. Run `{_PROVISION}`.",
            file=sys.stderr,
        )
        return None
    argv = (
        node,
        "--max-old-space-size=512",
        "-e",
        _READABILITY_JS % json.dumps(modules),
    )
    try:
        done = subprocess.run(
            argv,
            input=b"<p>probe</p>",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT,
            check=False,
            **_NO_WINDOW,
        )
        return (
            argv
            if done.returncode == 0 and isinstance(json.loads(done.stdout), str)
            else None
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _without_aria_hidden(html: str) -> str:
    """`aria-hidden` is accessibility metadata, not visual hiding.

    Readability.js treats it as hidden and drops the subtree, so remove the
    attribute before Node sees the page; the server's own pruner already
    ignores it.
    """
    root = _parse(html)
    for el in root.iter():
        el.attrib.pop("aria-hidden", None)
    return _serialize(root)


def _node_readability(html: str, node: Sequence[str], timeout: float) -> str | None:
    try:
        payload = _without_aria_hidden(html).encode("utf-8", "replace")
        proc = subprocess.Popen[bytes](
            list(node),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            **_NO_WINDOW,
        )
        try:
            output, _ = proc.communicate(payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            # A descendant can keep stdout open after Node dies. Do not drain
            # that pipe: the enclosing worker's process tree is terminated on
            # every exit, after the Python fallback has produced its result.
            proc.kill()
            proc.wait(timeout=_PROBE_TIMEOUT)
            return None
        finally:
            if sys.platform != "win32":
                # POSIX communicate leaves pipes open on timeout. Close our
                # handles without draining a descendant's inherited stdout.
                # On Windows its reader thread owns and closes stdout; closing
                # it here could block on that thread's buffered-reader lock.
                for pipe in (proc.stdin, proc.stdout):
                    if pipe is not None:
                        pipe.close()
        if proc.returncode != 0 or len(output) > _NODE_MAX_OUTPUT:
            return None
        result = json.loads(output)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return result if isinstance(result, str) else None


def _flatten_details(html: str) -> str:
    """Keep visible summary/body text when readabilipy's extractor sees <details>.

    Its pure-Python simplified tree drops the element wholesale. Flattening to
    div/p preserves the always-visible summary and the body for Markdown output.
    """
    if "<details" not in html.lower() and "<summary" not in html.lower():
        return html
    root = _parse(html)
    changed = False
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        name = el.tag.rpartition("}")[2].lower()
        if name == "details":
            el.tag = f"{_HTML}div"
            changed = True
        elif name == "summary":
            el.tag = f"{_HTML}p"
            changed = True
    return _serialize(root) if changed else html


def _python_readability(html: str) -> str:
    try:
        return str(
            readabilipy.simple_tree.simple_tree_from_html_string(_flatten_details(html))
        )
    except IndexError:
        # readabilipy's pure-Python extractor wraps its simplified tree with
        # `Tag.append`, which beautifulsoup4 4.13 implements as
        # `self.insert(...)[0]`. A page that simplifies to nothing (an SPA
        # shell, an empty body) inserts nothing, so that index raises (#5158).
        # Under beautifulsoup4 <= 4.12 the same page comes back as an empty
        # tree, reported by the caller as this same failure.
        return ""


def _readability_content(
    html: str, node: Sequence[str] | None, deadline: float, node_timeout: float
) -> str:
    if node:
        t = min(node_timeout, deadline - time.monotonic() - _PY_RESERVE)
        if t >= 1 and (c := _node_readability(html, node, t)) is not None:
            return c
    return _python_readability(html)


def _parse(html: str) -> Element:
    return html5lib.parse(html, treebuilder="etree", scripting=True)


def _tokens(root: Element) -> Iterator[dict[str, Any]]:
    tw = TreeWalker(None)
    stack: list[Any] = [("e", root)]
    while stack:
        kind, x = stack.pop()
        if kind == "t":
            yield from tw.text(x)  # pyright: ignore[reportGeneralTypeIssues]
            continue
        if kind == "c":
            yield tw.endTag(*x)
            continue
        if callable(x.tag):  # comment
            yield tw.comment(x.text)
            continue
        ns, name = x.tag[1:].split("}") if x.tag[0] == "{" else (None, x.tag)
        attrs = {
            (tuple(a[1:].split("}")) if a[0] == "{" else (None, a)): v
            for a, v in x.attrib.items()
        }
        if ns == namespaces["html"] and name in voidElements:
            yield from tw.emptyTag(ns, name, attrs, len(x) > 0)  # pyright: ignore[reportGeneralTypeIssues]
            continue
        yield tw.startTag(ns, name, attrs)
        stack.append(("c", (ns, name)))
        for child in reversed(x):
            if child.tail:
                stack.append(("t", child.tail))
            stack.append(("e", child))
        if x.text:
            stack.append(("t", x.text))


def _serialize(root: Element) -> str:
    # Not html5lib's treewalker: its getParentNode is O(siblings) per step (O(n^2) overall).
    serializer = HTMLSerializer(omit_optional_tags=False, quote_attr_values="always")
    return serializer.render(_tokens(root))


def _text_of(el: Element) -> str:
    parts: list[str] = []
    stack: list[Any] = [el]
    while stack:
        x = stack.pop()
        if isinstance(x, str):
            parts.append(x)
            continue
        if callable(x.tag):
            continue
        for child in reversed(x):
            if child.tail:
                stack.append(child.tail)
            stack.append(child)
        if x.text:
            stack.append(x.text)
    return " ".join(parts)


def _text_len(el: Element) -> int:
    return len(" ".join(_text_of(el).split()))


_CSS_ESCAPE = re.compile(r"\\(?:([0-9a-fA-F]{1,6})[ \t\n\r\f]?|(.))", re.DOTALL)


def _css_unescape(text: str) -> str:
    """CSS escapes as a browser reads them: `n\\6f ne` is `none` (CSS Syntax 4.3.7)."""

    def one(m: re.Match[str]) -> str:
        if m.group(2) is not None:
            return m.group(2)
        code = int(m.group(1), 16)
        # CSS Syntax: a whitespace code point escaped on its own is U+FFFD, not
        # the whitespace character (so `none\9` is not silently `none`).
        if code in (0x09, 0x0A, 0x0C, 0x0D, 0x20):
            return "\ufffd"
        return (
            chr(code)
            if 0 < code <= 0x10FFFF and not 0xD800 <= code < 0xE000
            else "\ufffd"
        )

    return _CSS_ESCAPE.sub(one, text)


_HIDING_VALUES = {
    "display": {"none"},
    "visibility": {"hidden", "collapse"},
    "content-visibility": {"hidden"},
}


def _strip_css_comments(style: str) -> str:
    # Python 3.11.4's possessive-repeat engine mishandles empty comments.
    # Find delimiters directly, retaining linear work and unterminated-comment behavior.
    parts: list[str] = []
    start = 0
    while (opening := style.find("/*", start)) != -1:
        parts.append(style[start:opening])
        closing = style.find("*/", opening + 2)
        if closing == -1:
            return "".join(parts)
        start = closing + 2
    parts.append(style[start:])
    return "".join(parts)


def _declarations(style: str) -> list[tuple[str, str, bool]]:
    """(property, value, important) for each inline declaration, in source order."""
    out = []
    for raw in _strip_css_comments(style).split(";"):
        prop, sep, value = _css_unescape(raw).partition(":")
        if not sep:
            continue
        prop = prop.strip().lower()
        value = value.strip().lower()
        important_match = re.search(r"!\s*important\s*$", value)
        important = important_match is not None
        if important_match is not None:
            value = value[: important_match.start()].strip()
        out.append((prop, value, important))
    return out


def _computed_style(style: str) -> dict[str, str]:
    """The effective value of each inline property: important wins, then last wins."""
    values: dict[str, tuple[str, bool]] = {}
    for prop, value, important in _declarations(style):
        previous = values.get(prop)
        if previous is None or important or not previous[1]:
            values[prop] = (value, important)
    return {prop: value for prop, (value, _) in values.items()}


def _hiding_cause(name: str, attrs: dict[str, str]) -> str | None:
    """Why a browser would hide this element from its own attributes, if it would.

    `aria-hidden` is accessibility metadata, not a visual-hiding signal, and is
    deliberately ignored. Inline declarations follow the cascade:
    important wins over normal, later wins within a priority.
    """
    if "hidden" in attrs:
        return "attribute"
    if name == "dialog" and "open" not in attrs:
        return "dialog"
    style = _computed_style(attrs.get("style", ""))
    for prop, hidden_values in _HIDING_VALUES.items():
        if style.get(prop) in hidden_values:
            return prop
    return None


def _is_hidden(name: str, attrs: dict[str, str]) -> bool:
    """Whether a browser hides this element by its own attributes."""
    return _hiding_cause(name, attrs) is not None


def _explicitly_visible(attrs: dict[str, str]) -> bool:
    return _computed_style(attrs.get("style", "")).get("visibility") == "visible"


def _unhide(el: Element) -> None:
    el.attrib.pop("hidden", None)
    if "style" in el.attrib:
        kept = []
        for raw in _strip_css_comments(el.attrib["style"]).split(";"):
            prop = _css_unescape(raw).partition(":")[0].strip().lower()
            if prop not in _HIDING_VALUES:
                kept.append(raw)
        el.set("style", ";".join(kept))


def _prune(
    root: Element, *, remove: bool, unhide: frozenset[int] = frozenset()
) -> tuple[int, list[tuple[int, int]]]:
    """Walk the tree once, iteratively.

    Every outermost hidden element (no hidden ancestor) gets the next number in
    document order, identical across parses of the same page, so a second parse can
    unhide exactly the elements a first parse found. With `remove`, comments, never-
    rendered elements (`_STRIP`, a `<title>` outside `<head>`), hidden elements and
    tooltip `title` attributes of links and images are removed. Returns (removed
    count, candidates): the count is of removed things that held text; a candidate is
    (number, text length) of an outermost hidden element that is not page furniture."""
    removed, candidates, seq = 0, [], 0
    root_cause = _hiding_cause("html", root.attrib)
    if remove and root_cause is not None and root_cause != "visibility":
        root.clear()
        return 1, candidates
    stack = [(root, False, False)]  # element, inside chrome, inside a hidden element
    while stack:
        el, chrome, nested = stack.pop()
        parent = el.tag.rpartition("}")[2].lower()
        kept: list[Element] = []
        prev: Element | None = None
        changed = False
        for ch in list(el):
            number = -1
            if callable(ch.tag):  # a comment: never shown, removed in remove mode
                strip, hidden, counted, in_chrome = True, False, 0, chrome
            else:
                name = ch.tag.rpartition("}")[2].lower()
                in_chrome = (
                    chrome or name in _CHROME_TAGS or ch.get("role") in _CHROME_ROLES
                )
                strip = name in _STRIP or (name == "title" and parent != "head")
                cause = None if strip else _hiding_cause(name, ch.attrib)
                # visibility can be re-shown by a descendant, so its text is
                # cleared by the iterative visibility pass after hard-hidden
                # subtrees are gone. display/content-visibility/attribute/dialog
                # remove the whole subtree.
                hidden = cause is not None and cause != "visibility"
                if cause is not None and not hidden and not nested and not remove:
                    number, seq = seq, seq + 1
                    if number in unhide:
                        _unhide(ch)
                    elif not in_chrome:
                        candidates.append((number, _text_len(ch)))
                if hidden and not nested:
                    number, seq = seq, seq + 1
                    if number in unhide:
                        _unhide(ch)
                    elif not in_chrome:
                        candidates.append((number, _text_len(ch)))
                quiet = name in ("script", "style")
                counted = int(((strip and not quiet) or hidden) and _text_len(ch) > 0)
                if (
                    remove
                    and name in ("a", "img")
                    and ch.attrib.pop("title", "").strip()
                ):
                    removed += 1  # a tooltip: text a reader does not see
            if remove and (strip or (hidden and number not in unhide)):
                removed += counted
                if ch.tail:
                    if prev is None:
                        el.text = (el.text or "") + ch.tail
                    else:
                        prev.tail = (prev.tail or "") + ch.tail
                changed = True
                continue
            kept.append(ch)
            prev = ch
            if not callable(ch.tag):
                stack.append((ch, in_chrome, nested or hidden))
        if changed:
            el[:] = kept
    if remove:
        removed += _strip_visibility_text(root)
    return removed, candidates


def _strip_visibility_text(root: Element) -> int:
    """Remove text hidden by inherited `visibility`, keeping visible descendants.

    Iterative: deeply nested pages must not recurse. A descendant with
    `visibility:visible` clears the inherited state for its own subtree, but the
    parent's own text and each child's tail remain hidden.
    """
    children = {el: [ch for ch in el if not callable(ch.tag)] for el in root.iter()}
    parents = {ch: el for el, kids in children.items() for ch in kids}
    order: list[Element] = []
    walk: list[Element] = [root]
    while walk:
        el = walk.pop()
        order.append(el)
        walk.extend(children.get(el, ()))
    has_visible: dict[Element, bool] = {}
    for el in reversed(order):
        has_visible[el] = any(
            _explicitly_visible(ch.attrib) or has_visible[ch]
            for ch in children.get(el, ())
        )

    removed = 0
    vis_stack: list[tuple[Element, bool]] = [(root, False)]
    while vis_stack:
        el, inherited = vis_stack.pop()
        style = _computed_style(el.attrib.get("style", ""))
        visibility = style.get("visibility")
        hidden = (
            False
            if visibility == "visible"
            else inherited or visibility in ("hidden", "collapse")
        )
        if hidden:
            parent = parents.get(el)
            if parent is not None and not has_visible[el]:
                index = list(parent).index(el)
                if index:
                    previous = parent[index - 1]
                    previous.tail = (previous.tail or "") + (el.tail or "")
                else:
                    parent.text = (parent.text or "") + (el.tail or "")
                parent.remove(el)
                removed += 1
                continue
            if el.text:
                removed += 1
                el.text = None
            for ch in el:
                if ch.tail:
                    removed += 1
                    ch.tail = None
        for ch in children.get(el, ()):
            vis_stack.append((ch, hidden))
    return removed


_TEXT_LINK = re.compile(r"\[([^\[\]]{0,300}+)\]\((https?://[^)\s\[]{1,2048}+)\)")
_MAX_LINKS, _MAX_LINK_TEXT, _MAX_HREF, _MAX_LINKS_SECTION = 100, 80, 2048, 20_000


def _dom_links(root: Element, page_url: str) -> Iterator[tuple[str, str]]:
    """(absolute http(s) URL, link text) for every HTML <a href> in document order."""
    for a in root.iter(_HTML + "a"):  # C-implemented, iterative
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        try:
            url = urljoin(page_url, href)
            scheme = urlsplit(url).scheme
        except ValueError:  # an unterminated IPv6 literal and the like
            continue
        if scheme in ("http", "https"):
            yield url, _text_of(a)


def text_links(text: str) -> Iterator[tuple[str, str]]:
    """Markdown-style [text](https://...) links in plain text (non-HTML bodies)."""
    for m in _TEXT_LINK.finditer(text):
        yield m.group(2), m.group(1)


def links_section(
    pairs: Iterable[tuple[str, str]], *, sanitize: bool
) -> tuple[str, dict[str, int]]:
    """The "Links on this page" block and what sanitizing its text found.

    At most 100 lines and 20,000 characters; listed lines are deduplicated, and once
    the list is full the rest are only counted, so memory stays bounded. A
    URL that is too long, not printable, or holds `<`, `>` or a space is skipped; the
    rest are CommonMark angle-bracket destinations, so no character needs encoding. Link text is escaped so it cannot close its brackets."""
    seen: set[str] = set()
    lines: list[str] = []
    reports: list[dict[str, int]] = []
    size = omitted = 0
    for url, text in pairs:
        if len(lines) == _MAX_LINKS:
            omitted += 1
            continue
        if (
            len(url) > _MAX_HREF
            or not url.isprintable()
            or any(c in url for c in "<>\\ ")
        ):
            continue
        if sanitize:
            text, report = _textsan.clean_inline(text)
            url, swapped = _textsan.neutralize(url, alter=False)
            report["neutralized"] += swapped
        else:
            text, report = " ".join(_textsan.strip_controls(text).split()), {}
        text = (
            text[:_MAX_LINK_TEXT]
            .replace("\\", "\\\\")
            .replace("[", "\\[")
            .replace("]", "\\]")
        )
        line = f"- [{text}](<{url}>)" if text else f"- <{url}>"
        if line in seen:
            continue
        if size + len(line) + 1 > _MAX_LINKS_SECTION:
            omitted += 1
            continue
        seen.add(line)
        lines.append(line)
        reports.append(report)
        size += len(line) + 1
    section = ""
    if lines:
        section = "\n\nLinks on this page:\n" + "\n".join(lines)
        section += f"\n({omitted} more omitted)" if omitted else ""
    return section, _textsan.merged(*reports) if sanitize else {}


def run(request: dict[str, Any]) -> dict[str, Any]:
    """The worker's whole job (see the module docstring)."""
    deadline = time.monotonic() + request["budget"]
    html, node, node_timeout = request["html"], request["node"], request["node_timeout"]
    sanitize, page_url = request["sanitize"], request["page_url"]
    if request["mode"] == "links":
        section, report = links_section(
            _dom_links(_parse(html), page_url), sanitize=sanitize
        )
        return {"content": "", "links": section, "report": report}
    if request["mode"] != "markdown":
        raise ValueError(f"unknown mode {request['mode']!r}")
    removed, candidates, root = 0, [], None
    if sanitize:  # hidden and never-rendered elements go before any extractor
        root = _parse(html)
        removed = _prune(root, remove=True)[0]
        source = _serialize(root)
    else:
        source = html
        if node or request["links"]:
            root = _parse(html)
        if node and root is not None:
            # sanitize=false only: Readability.js drops hidden nodes, so a hidden
            # server-rendered container may hold the real page (#3878).
            candidates = _prune(root, remove=False)[1]
    try:
        content_html = _readability_content(source, node, deadline, node_timeout)
        if candidates:
            extracted = _text_len(_parse(content_html))
            unhide = frozenset(n for n, length in candidates if length > extracted)
            if unhide:
                again = _parse(html)
                _prune(again, remove=False, unhide=unhide)
                second = _readability_content(
                    _serialize(again), node, deadline, node_timeout
                )
                if _text_len(_parse(second)) > extracted:
                    content_html = second
        markdown = (
            markdownify.markdownify(content_html, heading_style=markdownify.ATX)
            if content_html
            else ""
        )
        if markdown.strip():
            markdown = with_page_title(source, markdown)
    except RecursionError:
        # readabilipy and markdownify both walk the tree recursively, so a
        # page nested deeper than Python's recursion limit (a few thousand
        # tags, seen in the wild from generated markup) raises instead of
        # simplifying. Report it like any other page the extractor cannot
        # handle, on the tool and the prompt alike, rather than letting an
        # unexpected exception cross the MCP boundary.
        markdown = ""
    if sanitize:
        markdown, report = _textsan.sanitize_text(markdown, "markdown")
        report["removed"] += removed
    else:
        markdown, report = _textsan.strip_controls(markdown), {}
    section = ""
    if request["links"] and root is not None:
        section, link_report = links_section(
            _dom_links(root, page_url), sanitize=sanitize
        )
        report = _textsan.merged(report, link_report) if sanitize else {}
    # Without Node, readabilipy's pure-Python extractor never returns empty
    # content: an empty page comes back as "<div></div>", which converts to an
    # empty string. Report that as the same simplification failure the Node
    # path reports, rather than letting it reach pagination as an exhausted page.
    return {
        "content": markdown if markdown.strip() else _FAILED,
        "links": section,
        "report": report,
    }


def extract_content_from_html(html: str, use_readability_js: bool = True) -> str:
    """Extract and convert HTML content to Markdown format, sanitized.

    Runs in this process; the server runs the same `run()` in a worker process.

    Args:
        html: Raw HTML content to process
        use_readability_js: Ask for Readability.js under Node when Node works;
            False always uses the pure-Python extractor (#4199).

    Returns:
        Simplified markdown version of the content
    """
    request = {
        "mode": "markdown",
        "html": html,
        "page_url": "",
        "links": False,
        "sanitize": True,
        "node": find_node() if use_readability_js else None,
        "budget": _DEFAULT_BUDGET,
        "node_timeout": 20.0,
    }
    return run(request)["content"]


def _limit_self(budget: float) -> None:
    """POSIX ceilings for this worker, inherited by Node.

    RLIMIT_CPU is the backstop for a worker orphaned by a killed server
    (macOS has no PDEATHSIG). No core dumps of page data. Memory: Linux
    enforces RLIMIT_DATA; macOS does not, and relies on the 5 MiB body cap and
    the 4-worker bound. Windows: the parent enforces the wall timeout;
    its Job Object enforces memory and process-tree containment."""
    if sys.platform == "win32":
        return

    import resource

    def lower(kind: int, value: int) -> None:
        soft, hard = resource.getrlimit(kind)
        # Never loosen an operator's tighter limit: min against both current values.
        candidates = [value]
        if soft != resource.RLIM_INFINITY:
            candidates.append(soft)
        if hard != resource.RLIM_INFINITY:
            candidates.append(hard)
        resource.setrlimit(kind, (min(candidates), hard))

    lower(resource.RLIMIT_CPU, int(budget) + 5)
    lower(resource.RLIMIT_CORE, 0)
    if sys.platform == "linux":
        lower(resource.RLIMIT_DATA, _MEMORY_LIMIT)


def main() -> None:
    result = os.fdopen(os.dup(1), "wb")
    os.dup2(
        2, 1
    )  # stray prints (readabilipy, libraries, children) go to stderr, which the server discards
    request = json.loads(sys.stdin.buffer.read())
    if sys.platform != "win32":
        _limit_self(request["budget"])
    with result:
        result.write(json.dumps(run(request)).encode("ascii"))


if __name__ == "__main__":
    main()
