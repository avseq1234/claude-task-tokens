#!/usr/bin/env python3
"""task_tokens.py: add up Claude Code token usage per task.

Claude Code writes every API call's usage into JSONL transcripts under
~/.claude/projects/. This script reads them, assigns each API call to a task
by its [spec/task/phase] tag, and prints totals per task.

Tag rule (first match wins):
  1. sub-agent: its description (from agent-*.meta.json), then its first prompt
  2. sub-agent without a tag: the tag of the transcript that spawned it
  3. session: its name (/rename), then its first prompt
  4. no tag at all: the session is its own task (listed under "(untagged)")

Standard library only. Python 3.9+.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

PRICES_AS_OF = "2026-11-03"

# USD per 1M tokens: (input, output, cache_read).
# Cache writes: 5-minute TTL = 1.25x input, 1-hour TTL = 2x input.
PRICES: Dict[str, Tuple[float, float, float]] = {
    "claude-fable-5-2": (8.00, 40.00, 0.20),
    "claude-mythos-5-2": (8.00, 40.00, 0.20),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    "claude-mythos-5-1": (10.00, 50.00, 0.25),
    "claude-fable-5": (10.00, 50.00, 1.00),
    "claude-mythos-5": (10.00, 50.00, 1.00),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-opus-4-7": (5.00, 25.00, 0.50),
    "claude-opus-4-6": (5.00, 25.00, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}
# Haiku 5.5 prompts above 100K tokens are billed at a higher rate.
HAIKU_55_LONG = (0.50, 2.50, 0.05)
FAST_MULTIPLIER = 2.0


def normalize_model(model: str) -> str:
    m = (model or "").lower().strip()
    m = re.sub(r"\[[^\]]*\]$", "", m)      # claude-opus-5-5[1m]
    m = re.sub(r"^(us\.|eu\.|apac\.|global\.)?anthropic\.", "", m)
    m = re.sub(r"[-@]\d{8}$", "", m)       # dated snapshots
    m = re.sub(r"-v\d+(:\d+)?$", "", m)    # bedrock -v1:0
    return m


# ---------------------------------------------------------------------------
# Tag parsing
# ---------------------------------------------------------------------------

_SEG = r"([A-Za-z0-9._-]+)"
TAG_RE = re.compile(
    r"^\s*[\[［]\s*" + _SEG + r"(?:/" + _SEG + r")?(?:/" + _SEG + r")?\s*[\]］](?!\()"
)

Tag = Tuple[str, Optional[str], Optional[str]]  # (spec, task, phase)


def parse_tag(text: Optional[str]) -> Optional[Tag]:
    """Return (spec, task, phase) when text starts with a valid tag."""
    if not text:
        return None
    m = TAG_RE.match(text)
    if not m:
        return None
    spec, task, phase = (g.lower() if g else None for g in m.groups())
    return (spec, task, phase)


_DROP_BLOCKS = re.compile(
    r"<(system-reminder|command-name|command-message|local-command-stdout|"
    r"local-command-stderr|local-command-caveat)>.*?</\1>",
    re.S,
)
_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_PASTE = re.compile(r"</?pasted_content[^>]*>")


def clean_prompt(content) -> str:
    """Turn a user message's content into the text the person typed."""
    if isinstance(content, list):
        text = "\n".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        text = content if isinstance(content, str) else ""
    text = _ARGS.sub(lambda m: m.group(1), text)
    text = _DROP_BLOCKS.sub("", text)
    text = _PASTE.sub("", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Transcript reading
# ---------------------------------------------------------------------------


@dataclass
class Request:
    rid: str
    ts: str
    model: str
    speed: Optional[str]
    input: int = 0
    cache_5m: int = 0
    cache_1h: int = 0
    cache_read: int = 0
    output: int = 0

    @property
    def cache_write(self) -> int:
        return self.cache_5m + self.cache_1h

    @property
    def total(self) -> int:
        return self.input + self.cache_write + self.cache_read + self.output

    def cost(self) -> Optional[float]:
        p = PRICES.get(normalize_model(self.model))
        if p is None:
            return None
        if normalize_model(self.model) == "claude-haiku-5-5":
            if self.input + self.cache_write + self.cache_read > 100_000:
                p = HAIKU_55_LONG
        inp, out, cr = p
        usd = (
            self.input * inp
            + self.cache_5m * inp * 1.25
            + self.cache_1h * inp * 2.0
            + self.cache_read * cr
            + self.output * out
        ) / 1_000_000
        if self.speed == "fast":
            usd *= FAST_MULTIPLIER
        return usd


@dataclass
class Transcript:
    path: str
    session_id: str
    project: str
    is_agent: bool
    agent_id: Optional[str] = None
    description: Optional[str] = None
    spawn_tool_use_id: Optional[str] = None
    first_prompt: str = ""
    session_name: Optional[str] = None
    ai_title: Optional[str] = None
    cwd: Optional[str] = None
    first_ts: str = ""
    tool_use_ids: Set[str] = field(default_factory=set)
    requests: Dict[str, Request] = field(default_factory=dict)


def _iter_jsonl(path: str) -> Iterable[dict]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(d, dict):
                    yield d
    except OSError:
        return


def _usage_request(d: dict) -> Optional[Request]:
    msg = d.get("message") or {}
    usage = msg.get("usage")
    model = msg.get("model") or ""
    if not isinstance(usage, dict) or model == "<synthetic>":
        return None
    rid = d.get("requestId") or msg.get("id") or d.get("uuid")
    if not rid:
        return None
    cw_total = int(usage.get("cache_creation_input_tokens") or 0)
    cc = usage.get("cache_creation") or {}
    c5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
    c1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
    if c5 + c1 != cw_total:  # no breakdown: treat as 5-minute writes
        c5, c1 = cw_total - c1, c1
    return Request(
        rid=str(rid),
        ts=d.get("timestamp") or "",
        model=model,
        speed=usage.get("speed"),
        input=int(usage.get("input_tokens") or 0),
        cache_5m=c5,
        cache_1h=c1,
        cache_read=int(usage.get("cache_read_input_tokens") or 0),
        output=int(usage.get("output_tokens") or 0),
    )


def read_transcript(t: Transcript) -> Transcript:
    for d in _iter_jsonl(t.path):
        typ = d.get("type")
        if not t.first_ts and d.get("timestamp"):
            t.first_ts = d["timestamp"]
        if t.cwd is None and d.get("cwd"):
            t.cwd = d["cwd"]
        if typ in ("custom-title", "agent-name"):
            name = d.get("customTitle") or d.get("agentName") or d.get("title")
            if name:
                t.session_name = name  # keep the last rename
        elif typ == "ai-title":
            t.ai_title = d.get("aiTitle") or d.get("title") or t.ai_title
        elif typ == "user" and not t.first_prompt and not d.get("isMeta"):
            if d.get("isCompactSummary"):
                continue
            text = clean_prompt((d.get("message") or {}).get("content"))
            if text:
                t.first_prompt = text
        elif typ == "assistant":
            content = (d.get("message") or {}).get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                        t.tool_use_ids.add(b["id"])
            r = _usage_request(d)
            if r is None:
                continue
            # One API call is written as several lines (one per content
            # block), each carrying the same usage. Keep one per request.
            old = t.requests.get(r.rid)
            if old is None or r.output > old.output:
                t.requests[r.rid] = r
    return t


def discover(projects_dir: str, project_filter: Optional[str] = None) -> List[List[Transcript]]:
    """Return one list per session: [main transcript, sub-agent transcripts...]."""
    sessions: List[List[Transcript]] = []
    for proj in sorted(os.listdir(projects_dir)) if os.path.isdir(projects_dir) else []:
        pdir = os.path.join(projects_dir, proj)
        if not os.path.isdir(pdir):
            continue
        for main_path in sorted(glob.glob(os.path.join(glob.escape(pdir), "*.jsonl"))):
            sid = os.path.basename(main_path)[: -len(".jsonl")]
            main = read_transcript(Transcript(main_path, sid, proj, is_agent=False))
            if project_filter and project_filter not in proj and project_filter not in (main.cwd or ""):
                continue
            group = [main]
            sdir = os.path.join(pdir, sid)
            pattern = os.path.join(glob.escape(sdir), "**", "subagents", "*.jsonl")
            for apath in sorted(glob.glob(pattern, recursive=True)):
                aid = os.path.basename(apath)[: -len(".jsonl")]
                a = Transcript(apath, sid, proj, is_agent=True, agent_id=aid)
                meta_path = apath[: -len(".jsonl")] + ".meta.json"
                try:
                    with open(meta_path, encoding="utf-8") as f:
                        meta = json.load(f)
                    a.description = meta.get("description")
                    a.spawn_tool_use_id = meta.get("toolUseId")
                except (OSError, json.JSONDecodeError):
                    pass
                group.append(read_transcript(a))
            sessions.append(group)
    sessions.sort(key=lambda g: g[0].first_ts or "")
    return sessions


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

UNTAGGED = "(untagged)"
OVERHEAD = "(overhead)"


def _session_key(main: Transcript) -> Tag:
    for text in (main.session_name, main.first_prompt):
        tag = parse_tag(text)
        if tag:
            return tag
    label = main.session_name or main.ai_title or (main.first_prompt[:40] if main.first_prompt else "")
    label = " ".join(label.split()) or "(no prompt)"
    return (UNTAGGED, f"{main.session_id[:8]} {label}", None)


def resolve_keys(group: List[Transcript]) -> Dict[str, Tag]:
    """Map each transcript path in one session to its tag."""
    main = group[0]
    keys: Dict[str, Tag] = {main.path: _session_key(main)}
    owner: Dict[str, Transcript] = {}
    for t in group:
        for tid in t.tool_use_ids:
            owner.setdefault(tid, t)

    def resolve(t: Transcript, seen: Set[str]) -> Tag:
        if t.path in keys:
            return keys[t.path]
        tag = parse_tag(t.description) or parse_tag(t.first_prompt)
        if tag is None:
            parent = owner.get(t.spawn_tool_use_id or "")
            if parent is None or parent.path in seen or parent is t:
                parent = main
            tag = resolve(parent, seen | {t.path})
        keys[t.path] = tag
        return tag

    for t in group[1:]:
        resolve(t, set())
    return keys


@dataclass
class Row:
    spec: str
    task: Optional[str]
    phase: Optional[str]
    requests: int = 0
    input: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0
    cost: float = 0.0
    unpriced: int = 0
    sessions: Set[str] = field(default_factory=set)
    agents: Set[str] = field(default_factory=set)
    models: Set[str] = field(default_factory=set)

    @property
    def total(self) -> int:
        return self.input + self.cache_write + self.cache_read + self.output

    def add(self, r: Request, session_id: str, agent_id: Optional[str]) -> None:
        self.requests += 1
        self.input += r.input
        self.cache_write += r.cache_write
        self.cache_read += r.cache_read
        self.output += r.output
        c = r.cost()
        if c is None:
            self.unpriced += 1
        else:
            self.cost += c
        self.sessions.add(session_id)
        if agent_id:
            self.agents.add(agent_id)
        if r.model:
            self.models.add(normalize_model(r.model))

    def merge(self, o: "Row") -> None:
        self.requests += o.requests
        self.input += o.input
        self.cache_write += o.cache_write
        self.cache_read += o.cache_read
        self.output += o.output
        self.cost += o.cost
        self.unpriced += o.unpriced
        self.sessions |= o.sessions
        self.agents |= o.agents
        self.models |= o.models


def collect(
    projects_dir: str,
    project_filter: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> Dict[Tag, Row]:
    rows: Dict[Tag, Row] = {}
    seen: Set[str] = set()
    for group in discover(projects_dir, project_filter):
        keys = resolve_keys(group)
        for t in group:
            key = keys[t.path]
            for r in sorted(t.requests.values(), key=lambda x: x.ts):
                # A resumed or forked session repeats earlier requests:
                # count each request id once, in the session seen first.
                if r.rid in seen:
                    continue
                seen.add(r.rid)
                if since and r.ts and r.ts < since:
                    continue
                if until and r.ts and r.ts >= until:
                    continue
                row = rows.get(key)
                if row is None:
                    row = rows[key] = Row(*key)
                row.add(r, t.session_id, t.agent_id)
    return rows


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def roll_up(rows: Dict[Tag, Row], by_phase: bool) -> List[Row]:
    """Merge phases into their task unless by_phase is set."""
    out: Dict[Tag, Row] = {}
    for (spec, task, phase), row in rows.items():
        key = (spec, task, phase if by_phase else None)
        tgt = out.get(key)
        if tgt is None:
            tgt = out[key] = Row(*key)
        tgt.merge(row)
    return list(out.values())


def allocate_overhead(rows: List[Row]) -> List[Row]:
    """Spread each spec's overhead row over its tasks, by task cost."""
    by_spec: Dict[str, List[Row]] = defaultdict(list)
    for r in rows:
        by_spec[r.spec].append(r)
    result: List[Row] = []
    for spec, items in by_spec.items():
        overhead = [r for r in items if r.task is None]
        tasks = [r for r in items if r.task is not None]
        if spec == UNTAGGED or not overhead or not tasks:
            result.extend(items)
            continue
        oh = overhead[0]
        weights = [t.cost if t.cost > 0 else t.total for t in tasks]
        wsum = sum(weights) or len(tasks)
        weights = weights if sum(weights) else [1] * len(tasks)
        for t, w in zip(tasks, weights):
            share = w / wsum
            t.input += round(oh.input * share)
            t.cache_write += round(oh.cache_write * share)
            t.cache_read += round(oh.cache_read * share)
            t.output += round(oh.output * share)
            t.cost += oh.cost * share
            t.sessions |= oh.sessions
        result.extend(tasks)
    return result


def _sort_key(r: Row):
    def natural(s: Optional[str]):
        if s is None:
            return ((0,),)
        return tuple((1, int(p)) if p.isdigit() else (2, p) for p in re.split(r"(\d+)", s) if p)

    return (r.spec == UNTAGGED, r.spec, natural(r.task), natural(r.phase))


def _width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _cut(s: str, w: int) -> str:
    if _width(s) <= w:
        return s
    out = ""
    for c in s:
        if _width(out + c) > w - 1:
            break
        out += c
    return out + "…"


def _pad(s: str, w: int, right: bool = False) -> str:
    s = _cut(s, w)
    gap = " " * (w - _width(s))
    return gap + s if right else s + gap


def _fmt_cost(r: Row) -> str:
    s = f"{r.cost:,.2f}"
    return s + ("*" if r.unpriced else "")


def print_table(rows: List[Row], by_phase: bool, out=sys.stdout) -> None:
    cols = [("spec", 14, False), ("task", 34, False)]
    if by_phase:
        cols.append(("phase", 8, False))
    cols += [
        ("reqs", 6, True), ("input", 11, True), ("cache_w", 11, True),
        ("cache_r", 13, True), ("output", 11, True), ("total", 13, True),
        ("cost_usd", 10, True),
    ]

    def line(values: List[str]) -> str:
        return "  ".join(_pad(v, w, right) for v, (_, w, right) in zip(values, cols))

    def values(r: Row, spec: str, task: str) -> List[str]:
        v = [spec, task]
        if by_phase:
            v.append(r.phase or "")
        v += [f"{r.requests:,}", f"{r.input:,}", f"{r.cache_write:,}",
              f"{r.cache_read:,}", f"{r.output:,}", f"{r.total:,}", _fmt_cost(r)]
        return v

    print(line([c[0] for c in cols]), file=out)
    print("-" * _width(line([c[0] for c in cols])), file=out)
    rows = sorted(rows, key=_sort_key)
    grand = Row("TOTAL", None, None)
    current: Optional[str] = None
    subtotal: Optional[Row] = None
    for r in rows:
        if r.spec != current:
            if subtotal is not None and current != UNTAGGED:
                print(line(values(subtotal, "", "= " + current + " subtotal")), file=out)
            current, subtotal = r.spec, Row(r.spec, None, None)
        subtotal.merge(r)
        grand.merge(r)
        task = r.task if r.task is not None else OVERHEAD
        print(line(values(r, r.spec, task)), file=out)
    if subtotal is not None and current != UNTAGGED:
        print(line(values(subtotal, "", "= " + current + " subtotal")), file=out)
    print("-" * _width(line([c[0] for c in cols])), file=out)
    print(line(values(grand, "TOTAL", "")), file=out)
    if grand.unpriced:
        print(f"\n* {grand.unpriced} request(s) use a model with no price in PRICES; "
              "their tokens are counted but their cost is not.", file=out)
    print(f"Prices as of {PRICES_AS_OF} (API list price; a Pro/Max plan does not "
          "bill per token, so read cost as the API-equivalent).", file=out)


def row_dict(r: Row) -> dict:
    return {
        "spec": r.spec,
        "task": r.task if r.task is not None else OVERHEAD,
        "phase": r.phase,
        "requests": r.requests,
        "input_tokens": r.input,
        "cache_write_tokens": r.cache_write,
        "cache_read_tokens": r.cache_read,
        "output_tokens": r.output,
        "total_tokens": r.total,
        "cost_usd": round(r.cost, 6),
        "unpriced_requests": r.unpriced,
        "sessions": sorted(r.sessions),
        "agents": len(r.agents),
        "models": sorted(r.models),
    }


def default_projects_dir() -> str:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Add up Claude Code token usage per [spec/task/phase] tag.")
    ap.add_argument("--projects-dir", default=default_projects_dir(),
                    help="transcript folder (default: ~/.claude/projects)")
    ap.add_argument("--project", help="only sessions whose project folder or cwd contains this text")
    ap.add_argument("--tag", help="only this spec or task, e.g. checkout or checkout/T3")
    ap.add_argument("--tagged-only", action="store_true", help="hide untagged sessions")
    ap.add_argument("--by-phase", action="store_true", help="split each task by phase")
    ap.add_argument("--allocate-overhead", action="store_true",
                    help="spread each spec's overhead over its tasks, by cost")
    ap.add_argument("--since", help="UTC start, inclusive (e.g. 2026-11-01)")
    ap.add_argument("--until", help="UTC end, exclusive (e.g. 2026-12-01)")
    ap.add_argument("--format", choices=["table", "csv", "json"], default="table")
    args = ap.parse_args(argv)

    if args.by_phase and args.allocate_overhead:
        ap.error("--by-phase and --allocate-overhead cannot be used together")
    if not os.path.isdir(args.projects_dir):
        print(f"No transcript folder at {args.projects_dir}", file=sys.stderr)
        return 1

    raw = collect(args.projects_dir, args.project, args.since, args.until)
    rows = roll_up(raw, args.by_phase)
    if args.tagged_only:
        rows = [r for r in rows if r.spec != UNTAGGED]
    if args.tag:
        want = parse_tag(f"[{args.tag}]")
        if want is None:
            ap.error(f"--tag {args.tag!r} is not a valid tag")
        rows = [r for r in rows
                if r.spec == want[0]
                and (want[1] is None or r.task == want[1])
                and (want[2] is None or not args.by_phase or r.phase == want[2])]
    if args.allocate_overhead:
        rows = allocate_overhead(rows)
    rows.sort(key=_sort_key)

    if args.format == "json":
        json.dump({"prices_as_of": PRICES_AS_OF, "rows": [row_dict(r) for r in rows]},
                  sys.stdout, ensure_ascii=False, indent=2)
        print()
    elif args.format == "csv":
        w = csv.writer(sys.stdout)
        keys = list(row_dict(Row("", None, None)).keys())
        w.writerow(keys)
        for r in rows:
            d = row_dict(r)
            d["sessions"] = " ".join(d["sessions"])
            d["models"] = " ".join(d["models"])
            w.writerow([d[k] for k in keys])
    else:
        if not rows:
            print("No matching usage found.")
        else:
            print_table(rows, args.by_phase)
    return 0


if __name__ == "__main__":
    sys.exit(main())
