"""
Parser for LinkedIn's React Server Components ("Flight") wire format, as returned by
`POST flagship-web/rsc-action/actions/component`. See docs/FINDINGS.md for how this
endpoint was discovered and confirmed reachable with cookie + CSRF only (no fingerprint
headers).

The wire format is a sequence of `<key>:<json-or-import-decl>` lines. `key` "0" is always
the root. String values of the form `$<key>` or `$L<key>` are references to another line;
`$undefined` means "not resolved / absent". This module resolves those references into a
plain nested Python structure, then walks it to produce the same kind of flat, ordered
list of visible-text strings that `container.stripped_strings` gives the HTML-based
parsers in extractor.py -- so the same grouping logic can be reused for both.
"""
import json
import re

REF_RE = re.compile(r"^\$(L)?([0-9a-zA-Z]+)$")
LINE_RE = re.compile(r"^([0-9a-zA-Z]+):(.*)$")

# Keys whose values are worth descending into when walking a resolved node for visible
# text. Everything else (className, componentKey, action/triggers, tracking specs,
# image payloads, etc.) is structural/behavioral noise, not content.
DESCEND_KEYS = ("children", "textProps", "item", "initialItems", "initialContent")


def parse_table(rsc_text):
    """Split the raw response body into {key: resolved-or-raw-json-value}."""
    table = {}
    for line in rsc_text.split("\n"):
        if not line.strip():
            continue
        m = LINE_RE.match(line)
        if not m:
            continue
        key, rest = m.group(1), m.group(2)
        rest = rest.strip()
        if rest.startswith("I["):
            table[key] = {"__import__": True}
            continue
        try:
            table[key] = json.loads(rest)
        except json.JSONDecodeError:
            table[key] = None
    return table


def resolve(value, table, _seen=frozenset()):
    """Recursively replace $key / $Lkey reference strings with the referenced value."""
    if isinstance(value, str):
        if value == "$undefined":
            return None
        m = REF_RE.match(value)
        if m:
            key = m.group(2)
            if key in _seen or key not in table:
                return None
            return resolve(table[key], table, _seen | {key})
        return value
    if isinstance(value, list):
        return [resolve(v, table, _seen) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, table, _seen) for k, v in value.items()}
    return value


def resolve_root(rsc_text):
    table = parse_table(rsc_text)
    if "0" not in table:
        return None, table
    return resolve(table["0"], table), table


def walk_text(node, out):
    """Collect visible-text string leaves from a resolved node, in document order."""
    if node is None:
        return
    if isinstance(node, str):
        s = node.strip()
        if s:
            out.append(s)
        return
    if isinstance(node, dict):
        if node.get("__import__"):
            return
        for key in DESCEND_KEYS:
            if key in node:
                walk_text(node[key], out)
        return
    if isinstance(node, list):
        if len(node) == 4 and node and node[0] == "$":
            walk_text(node[3], out)
            return
        for item in node:
            walk_text(item, out)
        return


def find_sections(node, matches=None, _path=None):
    """Find every subtree carrying an `observabilityIdentifier`, keyed by that
    identifier's trailing component name (e.g. "educationTopLevelSection").
    A single component response can bundle several unrelated sections; this locates
    each one's own subtree so it can be extracted independently, instead of assuming
    one response == one section."""
    if matches is None:
        matches = {}
    if isinstance(node, dict):
        oid = node.get("observabilityIdentifier")
        if isinstance(oid, str):
            name = oid.rsplit(".", 1)[-1]
            # Prefer children content, not the wrapper dict itself, as the section root.
            matches.setdefault(name, node.get("children", node))
        for v in node.values():
            find_sections(v, matches)
    elif isinstance(node, list):
        for item in node:
            find_sections(item, matches)
    return matches


def find_render_payloads(node, out=None):
    """Collect every {rootUrl, imageRenditions} image payload in the tree, in order."""
    if out is None:
        out = []
    if isinstance(node, dict):
        if "rootUrl" in node and "imageRenditions" in node:
            out.append(node)
        for v in node.values():
            find_render_payloads(v, out)
    elif isinstance(node, list):
        for item in node:
            find_render_payloads(item, out)
    return out


def image_url_from_payload(payload, prefer_width=400):
    renditions = payload.get("imageRenditions") or []
    if not renditions:
        return None
    best = min(renditions, key=lambda r: abs(r.get("width", 0) - prefer_width))
    root = payload.get("rootUrl", "")
    suffix = best.get("suffixUrl", "")
    return root + suffix if root and suffix else None


def section_lines(section_node, drop_first_n=0):
    """Flatten a section subtree (as returned by find_sections) into a stripped-text
    line list, matching the shape extractor.py's grouping helpers expect."""
    out = []
    walk_text(section_node, out)
    lines = [l for l in out if l]
    return lines[drop_first_n:] if drop_first_n else lines


def section_logo_urls(section_node):
    payloads = find_render_payloads(section_node)
    return [u for u in (image_url_from_payload(p) for p in payloads) if u]


def find_first_key(node, key):
    """First value found anywhere under `key`, depth-first."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for v in node.values():
            found = find_first_key(v, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = find_first_key(item, key)
            if found is not None:
                return found
    return None


def collection_item_lines(section_node):
    """For a section rendered as an entity collection (e.g. Skills), return one
    stripped-text line list per item, with item boundaries preserved via the
    collection's own `initialItems` structure. More robust than flat-text delimiter
    grouping: the HTML-era Skills parser used the literal "Endorse" button text as its
    only per-skill boundary marker -- which lives under a button's own `text` prop, a
    key this module doesn't walk into for text extraction (to avoid pulling in every
    other button/link label as noise), so it never reaches the flat line list at all."""
    items = find_first_key(section_node, "initialItems")
    if not items:
        return []
    result = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        out = []
        walk_text(entry.get("item"), out)
        if out:
            result.append(out)
    return result


def find_component_nodes(node, key_prefix, _seen=None, _out=None):
    """Find every dict carrying a `componentKey` starting with `key_prefix`, in document
    order, one per unique key (the same componentKey typically repeats at an outer
    wrapper and its inner render call -- only the first/outer occurrence is kept, and
    its subtree isn't descended into further, so nested duplicates aren't re-matched).
    Used as an item-boundary detector for sections that aren't wrapped in an
    `initialItems` collection at all -- e.g. a short Skills list is sometimes rendered
    as bare sibling elements instead, one per `com.linkedin.sdui.profile.skill(...)`
    componentKey -- which is more robust than relying on any particular wrapper shape."""
    if _seen is None:
        _seen = set()
    if _out is None:
        _out = []
    if isinstance(node, dict):
        ck = node.get("componentKey")
        if isinstance(ck, str) and ck.startswith(key_prefix) and ck not in _seen:
            _seen.add(ck)
            _out.append(node)
            return _out
        for v in node.values():
            find_component_nodes(v, key_prefix, _seen, _out)
    elif isinstance(node, list):
        for item in node:
            find_component_nodes(item, key_prefix, _seen, _out)
    return _out


def component_item_lines(section_node, key_prefix):
    """Like collection_item_lines(), but keyed on componentKey prefix instead of an
    `initialItems` wrapper -- see find_component_nodes()."""
    nodes = find_component_nodes(section_node, key_prefix)
    result = []
    for node in nodes:
        out = []
        walk_text(node, out)
        if out:
            result.append(out)
    return result
