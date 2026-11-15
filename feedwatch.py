"""FeedWatch — a release & news monitoring digest agent built on OpenMinion 0.1.

FeedWatch watches a small set of public RSS/Atom feeds (by default the
OpenMinion and Ollama GitHub release feeds). On each run it:

1. fetches every configured feed through an OpenMinion tool
   (``fetch_new_items``) that de-duplicates items against a local state
   file, so only items published since the previous run are reported;
2. asks the agent (OpenMinion ``Agent`` with a Pydantic ``output_type``)
   to turn the new items into a validated, structured digest; and
3. renders that digest to a Markdown file under ``digests/`` and commits
   the seen-item ids to the state file, which makes the workflow safely
   repeatable: running it twice in a row reports "nothing new" the
   second time.

The model is interchangeable through OpenMinion's provider config
(``openminion setup``). The reference runs in this repository use a local
Ollama model, so the whole workflow runs with no API key and no cost;
``--provider-notes`` in the README explains how to point the same code at
Cortensor Portal or any other configured provider.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from openminion import Agent, AgentOutputValidationError, ProviderError, tool

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_FEEDS: dict[str, str] = {
    "openminion-releases": "https://github.com/OpenMinion/openminion/releases.atom",
    "ollama-releases": "https://github.com/ollama/ollama/releases.atom",
    "hn-frontpage": "https://news.ycombinator.com/rss",
}

STATE_PATH = Path(".feedwatch/state.json")
DIGEST_DIR = Path("digests")
USER_AGENT = "feedwatch/0.1 (OpenMinion hackathon demo)"
FETCH_TIMEOUT_SECONDS = 20

# Run-scoped stash filled by the fetch tool and consumed by main() after a
# successful agent run. Keeping the "pending" ids out of the state file
# until the digest exists means a failed run does not lose items.
_PENDING: dict[str, list[dict[str, Any]]] = {}
_FETCHED_THIS_RUN: dict[str, str] = {}
_FEEDS: dict[str, str] = {}
_MAX_ITEMS = 8
def prepare_network_env() -> None:
    """Sanitize bracketed IPv6 entries in ``no_proxy``/``NO_PROXY``.

    Some sandboxed environments export a ``no_proxy`` list containing
    bracketed IPv6 hosts, e.g. ``[::1]`` or ``[fd8b:...::1]``. httpx
    (used by OpenMinion's provider adapters) misparses those entries
    when building its environment proxy mounts and raises
    ``InvalidURL: Invalid port: ':1]'`` before any request — even for a
    localhost provider that the proxy should bypass. urllib (used for
    the feed fetches) accepts the original value, and the proxy
    variables themselves are fine, so the minimal fix is to unbracket
    the IPv6 entries in this process's copy of ``no_proxy``/``NO_PROXY``
    and leave everything else untouched. Environments without bracketed
    entries are not modified.
    """
    bracketed = re.compile(r"^\[(?P<host>[^\]]+)\](?::\d+)?$")
    for var in ("no_proxy", "NO_PROXY"):
        value = os.environ.get(var)
        if not value:
            continue
        entries = [e.strip() for e in value.split(",") if e.strip()]
        fixed = []
        changed = False
        for entry in entries:
            match = bracketed.match(entry)
            if match:
                fixed.append(match.group("host"))
                changed = True
            else:
                fixed.append(entry)
        if changed:
            os.environ[var] = ",".join(fixed)
            print(f"[feedwatch] note: unbracketed IPv6 entries in {var} "
                  "for httpx compatibility", file=sys.stderr)


# ---------------------------------------------------------------------------
# Feed fetching / parsing (plain stdlib; the network work lives in the tool)
# ---------------------------------------------------------------------------

def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _child_text(elem: ET.Element, names: list[str]) -> str:
    for child in elem:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag in names:
            if tag == "link":
                href = child.get("href")
                if href:
                    return href
            if child.text:
                return child.text.strip()
    return ""


def parse_feed(xml_bytes: bytes) -> list[dict[str, str]]:
    """Parse an RSS 2.0 or Atom feed into a flat list of item dicts."""
    root = ET.fromstring(xml_bytes)
    items: list[dict[str, str]] = []
    for elem in root.iter():
        tag = elem.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        title = _child_text(elem, ["title"])
        link = _child_text(elem, ["link", "guid"])
        published = _child_text(elem, ["pubDate", "published", "updated", "date"])
        summary = _child_text(elem, ["description", "summary", "content"])
        item_id = _child_text(elem, ["guid", "id"]) or link
        if not title or not item_id:
            continue
        items.append(
            {
                "id": item_id,
                "title": title,
                "link": link,
                "published": published,
                "summary": _strip_html(summary)[:240],
            }
        )
    return items


def load_state() -> dict[str, list[str]]:
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {k: list(v) for k, v in data.items()}
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return {}


def save_state(state: dict[str, list[str]]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    trimmed = {k: v[-500:] for k, v in state.items()}
    STATE_PATH.write_text(json.dumps(trimmed, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# OpenMinion tool: the agent fetches its own inputs through this
# ---------------------------------------------------------------------------

@tool(min_scope="READ_ONLY", idempotent=True)
def fetch_new_items(feed_name: str) -> str:
    """Fetch one watched feed and return only the items not seen before.

    Call this exactly once per feed name. Returns a JSON array of new
    items (title, link, published, summary). An empty array means the
    feed has nothing new since the previous run.
    """
    if feed_name in _FETCHED_THIS_RUN:
        return _FETCHED_THIS_RUN[feed_name]
    url = _FEEDS.get(feed_name)
    if url is None:
        return json.dumps({"error": f"unknown feed '{feed_name}'",
                           "known_feeds": sorted(_FEEDS)})
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as resp:
        items = parse_feed(resp.read())
    seen = set(load_state().get(feed_name, []))
    fresh = [it for it in items if it["id"] not in seen][:_MAX_ITEMS]
    _PENDING[feed_name] = fresh
    payload = json.dumps(fresh, ensure_ascii=False)
    _FETCHED_THIS_RUN[feed_name] = payload
    return payload


# ---------------------------------------------------------------------------
# Structured output contract
# ---------------------------------------------------------------------------

class Highlight(BaseModel):
    title: str
    source: str
    category: str
    summary: str


class Digest(BaseModel):
    headline: str
    overview: str
    highlights: list[Highlight]


# ---------------------------------------------------------------------------
# Digest rendering (deterministic; links come from the fetched items,
# never from the model, so they cannot be hallucinated)
# ---------------------------------------------------------------------------

def render_markdown(digest: Digest, when: datetime) -> str:
    by_title: dict[str, dict[str, Any]] = {}
    for items in _PENDING.values():
        for it in items:
            by_title[it["title"]] = it
    lines = [
        f"# {digest.headline}",
        "",
        f"_FeedWatch digest — {when.strftime('%Y-%m-%d %H:%M UTC')}_",
        "",
        digest.overview,
        "",
    ]
    if digest.highlights:
        lines.append("## Highlights")
        lines.append("")
        for h in digest.highlights:
            lines.append(f"### {h.title}")
            lines.append(f"- Source: {h.source} · Category: {h.category}")
            item = by_title.get(h.title)
            if item and item["link"]:
                lines.append(f"- Link: {item['link']}")
            if item and item["published"]:
                lines.append(f"- Published: {item['published']}")
            lines.append(f"- Summary: {h.summary}")
            lines.append("")
    total = sum(len(v) for v in _PENDING.values())
    lines.append(f"## All new items ({total})")
    lines.append("")
    for feed_name, items in _PENDING.items():
        for it in items:
            lines.append(f"- [{feed_name}] {it['title']} — {it['link']}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def build_fetch_prompt(feed_names: list[str]) -> str:
    names = ", ".join(feed_names)
    return (
        f"Call fetch_new_items once for EACH of these feeds: {names}. "
        "After the tool results come back, reply with exactly one short "
        "line saying how many items were new in total. Do not summarize "
        "the items yet."
    )


def build_digest_prompt() -> str:
    items = [
        {"feed": feed_name, **{k: it[k] for k in ("title", "published", "summary")}}
        for feed_name, fetched in _PENDING.items()
        for it in fetched
    ]
    return (
        "Here are the new feed items collected in this session as JSON:\n"
        + json.dumps(items, ensure_ascii=False)
        + "\nAnswer with ONLY a JSON object with string fields 'headline' "
        "and 'overview' and a list field 'highlights'. Each highlight is "
        "an object with string fields 'title' (copied exactly from an "
        "item), 'source' (the feed name), 'category' (one of: release, "
        "news, security, tooling, other) and 'summary' (one sentence). "
        "Cover the most important items, at most 8 highlights. If the "
        "list is NOT empty, highlights must contain at least 2 entries "
        "and the headline must say how many new items there are. If the "
        "list is empty, set headline to 'Nothing new since the last "
        "run', overview to one sentence, and highlights to an empty list."
    )


def enforce_digest_invariants(digest: Digest) -> Digest:
    """Program-side guardrails on top of the model's structured output.

    A small local model can return a self-contradictory digest: run 1
    produced the headline 'Nothing new since the last run' together with
    4 fetched items and no highlights. The facts that must never be
    wrong — whether anything is new, and that real items get
    highlighted — are therefore enforced here, in code. Highlights whose
    title does not match a fetched item are dropped (their links could
    not be rendered anyway), and if the model highlights nothing, the
    first items are highlighted with their own feed summaries.
    """
    items = [it for fetched in _PENDING.values() for it in fetched]
    if not items:
        digest.headline = "Nothing new since the last run"
        digest.highlights = []
        return digest
    known_titles = {it["title"] for it in items}
    digest.highlights = [h for h in digest.highlights if h.title in known_titles]
    if not digest.highlights:
        digest.highlights = [
            Highlight(
                title=it["title"],
                source=feed_name,
                category="other",
                summary=(it["summary"] or "New item in this feed.")[:200],
            )
            for feed_name, fetched in _PENDING.items()
            for it in fetched[:3]
        ][:3]
    feeds = {name for name, fetched in _PENDING.items() if fetched}
    generated = f"{len(items)} new item(s) across {len(feeds)} feed(s)"
    if "nothing new" in digest.headline.lower():
        digest.headline = generated
    else:
        # A headline that states a count must state the right one
        # (run 2's model wrote '8 new items' for 4 fetched items).
        counts = [int(n) for n in re.findall(r"\d+", digest.headline)]
        if counts and len(items) not in counts:
            digest.headline = generated
    return digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", default="feedwatch",
                        help="Configured OpenMinion agent id (default: feedwatch)")
    parser.add_argument("--session", default="",
                        help="OpenMinion session id (default: a fresh "
                             "feedwatch-<UTC timestamp> per run; see "
                             "NOTES.md for why runs do not share a session)")
    parser.add_argument("--feed", action="append", default=[],
                        metavar="NAME=URL",
                        help="Add or override a feed; repeatable")
    parser.add_argument("--only", default="",
                        help="Comma-separated subset of feed names to watch")
    parser.add_argument("--max-items", type=int, default=8,
                        help="Max new items per feed passed to the model")
    parser.add_argument("--timeout", type=float, default=900,
                        help="Agent turn timeout in seconds (local models "
                             "can be slow; default 900)")
    parser.add_argument("--reset-state", action="store_true",
                        help="Forget all previously seen items first")
    args = parser.parse_args()

    global _FEEDS, _MAX_ITEMS
    _FEEDS = dict(DEFAULT_FEEDS)
    for spec in args.feed:
        name, _, url = spec.partition("=")
        if name and url:
            _FEEDS[name.strip()] = url.strip()
    if args.only:
        wanted = {n.strip() for n in args.only.split(",") if n.strip()}
        _FEEDS = {k: v for k, v in _FEEDS.items() if k in wanted}
    _MAX_ITEMS = max(1, args.max_items)

    if args.reset_state and STATE_PATH.exists():
        STATE_PATH.unlink()

    prepare_network_env()

    config_path = Path(".openminion/agents.json")
    agent_kwargs: dict[str, Any] = {}
    if config_path.exists():
        agent_kwargs["config_path"] = str(config_path)

    instructions = (
        "You are FeedWatch, a monitoring agent. You report only items "
        "returned by your fetch_new_items tool; never invent items, "
        "titles, or links. Keep summaries factual and to one sentence."
    )
    feed_names = sorted(_FEEDS)
    session_id = args.session or (
        "feedwatch-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    try:
        # Turn 1 — the agent fetches its own inputs through the tool.
        with Agent(
            agent_id=args.agent,
            instructions=instructions,
            tools=[fetch_new_items],
            forced_tools=["fetch_new_items"],
            session_id=session_id,
            **agent_kwargs,
        ) as agent:
            fetch_result = agent.run(
                build_fetch_prompt(feed_names),
                timeout_seconds=args.timeout,
            )
        fetch_stats = fetch_result.stats or {}
        print(f"[feedwatch] fetch turn: tool_calls="
              f"{fetch_stats.get('tool_calls')} llm_calls="
              f"{fetch_stats.get('llm_calls')} text="
              f"{fetch_result.text.strip()[:120]!r}", file=sys.stderr)
        if not fetch_stats.get("tool_calls"):
            # Without tool calls there is no fetched data; anything the
            # model produced would be invented. Fail loudly instead of
            # writing a fabricated digest.
            print("error: the agent made no tool calls, so no feed data "
                  "was fetched; refusing to write a digest",
                  file=sys.stderr)
            return 1
        # Turn 2 — same session: turn the fetched items into a validated
        # structured digest.
        with Agent(
            agent_id=args.agent,
            instructions=instructions,
            session_id=session_id,
            output_type=Digest,
            **agent_kwargs,
        ) as agent:
            result = agent.run(
                build_digest_prompt(),
                timeout_seconds=args.timeout,
            )
    except AgentOutputValidationError as exc:
        print("error: model output did not match the Digest schema",
              file=sys.stderr)
        print(f"raw model output: {exc.raw_text}", file=sys.stderr)
        return 2
    except ProviderError as exc:
        print(f"error: provider failed: {exc.code}: {exc.message}",
              file=sys.stderr)
        return 1

    digest = enforce_digest_invariants(result.output)
    when = datetime.now(timezone.utc)
    markdown = render_markdown(digest, when)
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DIGEST_DIR / f"digest-{when.strftime('%Y%m%dT%H%M%SZ')}.md"
    out_path.write_text(markdown, encoding="utf-8")

    # The digest exists, so the pending items are now officially "seen".
    state = load_state()
    for feed_name, items in _PENDING.items():
        state.setdefault(feed_name, []).extend(it["id"] for it in items)
    save_state(state)

    print(markdown)
    print(f"[feedwatch] digest written to {out_path}")
    print(f"[feedwatch] session={result.session_id} "
          f"thread={result.thread_id} run_state={result.run_state}")
    stats = result.stats or {}
    if stats:
        print(f"[feedwatch] stats={json.dumps(stats, default=str)[:400]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
