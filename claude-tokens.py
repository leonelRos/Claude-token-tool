#!/usr/bin/env python3
"""
claude-tokens — analyse token usage from Claude Code transcripts.

Reads ~/.claude/projects/**/*.jsonl and aggregates token usage and cost
across multiple time windows: all-time, recent, today, and current session.

Usage:
  claude-tokens                       # dashboard: all-time + recent + today + current session
  claude-tokens --window today        # zoom into one window
  claude-tokens --window recent --recent-days 14
  claude-tokens --by prompt --top 10  # priciest prompts
  claude-tokens --by session
  claude-tokens --by project
  claude-tokens --since 2026-06-01 --until 2026-06-18
  claude-tokens --project amctv-la --model opus
  claude-tokens --json                # machine-readable output
  claude-tokens --no-cost             # hide cost columns
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# # Approximate per-million-token prices (USD). Adjust freely.
# # Order: (input, output, cache_write_5m, cache_write_1h, cache_read)
# PRICING = {
#     "opus-4":   (15.0, 75.0, 18.75, 30.0, 1.50),
#     "sonnet-4": (3.0,  15.0, 3.75,  6.0,  0.30),
#     "haiku-4":  (0.80, 4.0,  1.0,   1.6,  0.08),
#     "fable":    (3.0,  15.0, 3.75,  6.0,  0.30),
# }

# Approximate per-million-token prices (USD). Adjust freely.
# Order: (input, output, cache_write_5m, cache_write_1h, cache_read)
# Current pricing for different models 2026
PRICING = {
    "opus-4":   (5.0,  25.0, 6.25,  10.0, 0.50),   # Opus 4.6 / 4.7 / 4.8
    "sonnet-4": (3.0,  15.0, 3.75,  6.0,  0.30),   # Sonnet 4.6
    "haiku-4":  (1.0,  5.0,  1.25,  2.0,  0.10),   # Haiku 4.5
    "fable":    (10.0, 50.0, 12.5,  20.0, 1.00),   # Fable 5
}

def price_for(model: str):
    m = (model or "").lower()
    if "opus" in m:   return PRICING["opus-4"]
    if "sonnet" in m: return PRICING["sonnet-4"]
    if "haiku" in m:  return PRICING["haiku-4"]
    if "fable" in m:  return PRICING["fable"]
    return None

def cost_components(model: str, totals: dict) -> dict:
    """Return per-component USD cost; zeros if unknown model."""
    p = price_for(model)
    if not p:
        return {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0, "total": 0.0, "known": False}
    p_in, p_out, p_cw5, p_cw1, p_cr = p
    cw5 = totals.get("cache_write_5m", 0)
    cw1 = totals.get("cache_write_1h", 0)
    cw_total = totals.get("cache_write", 0)
    if cw5 or cw1:
        cw_cost = (cw5 * p_cw5 + cw1 * p_cw1) / 1_000_000
    else:
        cw_cost = (cw_total * p_cw5) / 1_000_000
    in_cost  = totals.get("input", 0)      * p_in  / 1_000_000
    out_cost = totals.get("output", 0)     * p_out / 1_000_000
    cr_cost  = totals.get("cache_read", 0) * p_cr  / 1_000_000
    return {
        "input": in_cost,
        "output": out_cost,
        "cache_read": cr_cost,
        "cache_write": cw_cost,
        "total": in_cost + out_cost + cr_cost + cw_cost,
        "known": True,
    }

def estimate_cost(model: str, totals: dict) -> float | None:
    c = cost_components(model, totals)
    return c["total"] if c["known"] else None

def parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None

def empty_totals() -> dict:
    return {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
        "cache_write_5m": 0,
        "cache_write_1h": 0,
        "messages": 0,
    }

def add_totals(dst: dict, src: dict) -> None:
    for k, v in src.items():
        dst[k] = dst.get(k, 0) + v

def extract_usage(record: dict) -> tuple[str, dict] | None:
    if record.get("type") != "assistant":
        return None
    msg = record.get("message") or {}
    if not isinstance(msg, dict):
        return None
    model = msg.get("model") or "?"
    if model == "<synthetic>":
        return None
    u = msg.get("usage") or {}
    inp = u.get("input_tokens", 0) or 0
    out = u.get("output_tokens", 0) or 0
    cr  = u.get("cache_read_input_tokens", 0) or 0
    cw  = u.get("cache_creation_input_tokens", 0) or 0
    cc  = u.get("cache_creation") or {}
    cw5 = (cc.get("ephemeral_5m_input_tokens", 0) or 0) if isinstance(cc, dict) else 0
    cw1 = (cc.get("ephemeral_1h_input_tokens", 0) or 0) if isinstance(cc, dict) else 0
    if inp + out + cr + cw == 0:
        return None
    return model, {
        "input": inp,
        "output": out,
        "cache_read": cr,
        "cache_write": cw,
        "cache_write_5m": cw5,
        "cache_write_1h": cw1,
        "messages": 1,
    }

def extract_user_prompt(record: dict) -> str | None:
    if record.get("type") != "user":
        return None
    if record.get("isMeta") or record.get("isSidechain"):
        return None
    msg = record.get("message") or {}
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text", ""))
            elif isinstance(p, str):
                parts.append(p)
        text = "\n".join(parts)
    else:
        return None
    text = (text or "").strip()
    if not text or text.startswith("<") or text.startswith("["):
        return None
    return text

def iter_records(path: Path):
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return

def project_label(jsonl_path: Path) -> str:
    return jsonl_path.parent.name

def find_current_session(files: list[Path]) -> str | None:
    """The session whose .jsonl was most recently modified."""
    if not files:
        return None
    latest = max(files, key=lambda p: p.stat().st_mtime)
    return latest.stem

def collect_events(args) -> list[dict]:
    """One event per assistant message with token usage, plus its preceding prompt."""
    events: list[dict] = []
    files = sorted(PROJECTS_DIR.rglob("*.jsonl"))
    if args.project:
        files = [f for f in files if args.project.lower() in project_label(f).lower()]

    for path in files:
        proj = project_label(path)
        session = path.stem
        if args.session and args.session not in session:
            continue

        current_prompt = None
        current_prompt_ts = None

        for rec in iter_records(path):
            ts = parse_iso(rec.get("timestamp", ""))

            prompt = extract_user_prompt(rec)
            if prompt is not None:
                current_prompt = prompt
                current_prompt_ts = rec.get("timestamp")
                continue

            usage = extract_usage(rec)
            if usage is None:
                continue
            model, totals = usage
            if args.model and args.model.lower() not in model.lower():
                continue
            events.append({
                "timestamp": ts,
                "ts_raw": rec.get("timestamp"),
                "project": proj,
                "session": session,
                "model": model,
                "prompt": current_prompt,
                "prompt_ts": current_prompt_ts,
                "totals": totals,
            })
    return events

def filter_events(events: list[dict], window: str, recent_days: int, current_session: str | None,
                  since: datetime | None, until: datetime | None) -> list[dict]:
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    recent_start = now - timedelta(days=recent_days)

    out = []
    for e in events:
        ts = e["timestamp"]
        if since and ts and ts < since: continue
        if until and ts and ts > until: continue
        if window == "today":
            if not ts or ts < today_start: continue
        elif window == "recent":
            if not ts or ts < recent_start: continue
        elif window == "current":
            if e["session"] != current_session: continue
        out.append(e)
    return out

def aggregate(events: list[dict]) -> dict:
    by_model = defaultdict(empty_totals)
    by_session = defaultdict(empty_totals)
    by_project = defaultdict(empty_totals)
    by_prompt_map: dict[tuple, dict] = {}
    grand = empty_totals()

    for e in events:
        t = e["totals"]
        add_totals(by_model[e["model"]], t)
        add_totals(by_session[f"{e['project']}/{e['session']}"], t)
        add_totals(by_project[e["project"]], t)
        add_totals(grand, t)
        if e["prompt"]:
            key = (e["session"], e["prompt_ts"], e["prompt"])
            if key not in by_prompt_map:
                by_prompt_map[key] = {
                    "timestamp": e["prompt_ts"],
                    "project": e["project"],
                    "session": e["session"],
                    "prompt": e["prompt"],
                    "model": e["model"],
                    **empty_totals(),
                }
            add_totals(by_prompt_map[key], t)
            by_prompt_map[key]["model"] = e["model"]

    return {
        "by_model": by_model,
        "by_session": by_session,
        "by_project": by_project,
        "by_prompt": list(by_prompt_map.values()),
        "grand": grand,
    }

def grand_cost(by_model: dict) -> dict:
    """Sum cost components across all models."""
    total = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0, "total": 0.0}
    for model, t in by_model.items():
        c = cost_components(model, t)
        for k in total:
            total[k] += c[k]
    return total

def fmt_int(n: int) -> str:
    return f"{n:,}"

def fmt_cost(c) -> str:
    if c is None: return "—"
    return f"${c:,.2f}" if abs(c) >= 0.01 or c == 0 else f"${c:,.4f}"

def truncate(s: str, n: int) -> str:
    s = s.replace("\n", " ").replace("\r", " ")
    return s if len(s) <= n else s[: n - 1] + "…"

def render_table(rows, headers, aligns):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    def fmt_row(row):
        out = []
        for i, cell in enumerate(row):
            s = str(cell)
            out.append(s.rjust(widths[i]) if aligns[i] == "r" else s.ljust(widths[i]))
        return "  ".join(out)
    print(fmt_row(headers))
    print(fmt_row(["-" * w for w in widths]))
    for row in rows:
        print(fmt_row(row))

def render_models_block(by_model: dict, show_cost: bool, top: int | None):
    items = sorted(by_model.items(), key=lambda kv: kv[1].get("output", 0), reverse=True)
    if top:
        items = items[:top]
    headers = ["model", "msgs", "input", "output", "cache_r", "cache_w", "total_in"]
    aligns = ["l", "r", "r", "r", "r", "r", "r"]
    if show_cost:
        headers += ["cost_in", "cost_out", "cost_cr", "cost_cw", "cost"]
        aligns += ["r", "r", "r", "r", "r"]
    rows = []
    for name, t in items:
        total_in = t["input"] + t["cache_read"] + t["cache_write"]
        row = [
            truncate(name, 40),
            fmt_int(t["messages"]),
            fmt_int(t["input"]),
            fmt_int(t["output"]),
            fmt_int(t["cache_read"]),
            fmt_int(t["cache_write"]),
            fmt_int(total_in),
        ]
        if show_cost:
            c = cost_components(name, t)
            if c["known"]:
                row += [fmt_cost(c["input"]), fmt_cost(c["output"]), fmt_cost(c["cache_read"]),
                        fmt_cost(c["cache_write"]), fmt_cost(c["total"])]
            else:
                row += ["—", "—", "—", "—", "—"]
        rows.append(row)
    render_table(rows, headers, aligns)

def render_grand_block(grand: dict, by_model: dict, show_cost: bool, indent: str = "  "):
    total_in = grand["input"] + grand["cache_read"] + grand["cache_write"]
    print(f"{indent}assistant messages : {fmt_int(grand['messages'])}")
    print(f"{indent}input tokens       : {fmt_int(grand['input'])}")
    print(f"{indent}output tokens      : {fmt_int(grand['output'])}")
    print(f"{indent}cache-read tokens  : {fmt_int(grand['cache_read'])}")
    print(f"{indent}cache-write tokens : {fmt_int(grand['cache_write'])}")
    print(f"{indent}total input-side   : {fmt_int(total_in)}")
    print(f"{indent}total tokens       : {fmt_int(total_in + grand['output'])}")
    if show_cost:
        gc = grand_cost(by_model)
        print(f"{indent}cost — input       : {fmt_cost(gc['input'])}")
        print(f"{indent}cost — output      : {fmt_cost(gc['output'])}")
        print(f"{indent}cost — cache read  : {fmt_cost(gc['cache_read'])}")
        print(f"{indent}cost — cache write : {fmt_cost(gc['cache_write'])}")
        print(f"{indent}cost — total       : {fmt_cost(gc['total'])}")

def render_prompts(prompts, top, show_cost):
    print()
    title = "By prompt"
    print(title)
    print("=" * len(title))
    prompts = sorted(prompts, key=lambda p: p.get("output", 0), reverse=True)
    if top:
        prompts = prompts[:top]
    headers = ["timestamp", "model", "msgs", "input", "output", "cache_r", "cache_w"]
    aligns = ["l", "l", "r", "r", "r", "r", "r"]
    if show_cost:
        headers.append("cost"); aligns.append("r")
    headers.append("prompt"); aligns.append("l")
    rows = []
    for p in prompts:
        ts = (p.get("timestamp") or "")[:19].replace("T", " ")
        row = [
            ts,
            truncate(p["model"], 40),
            fmt_int(p["messages"]),
            fmt_int(p["input"]),
            fmt_int(p["output"]),
            fmt_int(p["cache_read"]),
            fmt_int(p["cache_write"]),
        ]
        if show_cost:
            row.append(fmt_cost(estimate_cost(p["model"], p)))
        row.append(truncate(p["prompt"], 80))
        rows.append(row)
    render_table(rows, headers, aligns)

def render_window_section(label: str, events: list[dict], show_cost: bool, top: int | None,
                          subtitle_extra: str = ""):
    print()
    title = f"▌ {label}"
    if subtitle_extra:
        title += f"  ({subtitle_extra})"
    print(title)
    print("─" * max(40, len(title)))
    if not events:
        print("  (no usage in this window)")
        return
    agg = aggregate(events)
    render_models_block(agg["by_model"], show_cost, top)
    print()
    render_grand_block(agg["grand"], agg["by_model"], show_cost)

def main():
    ap = argparse.ArgumentParser(
        description="Analyse Claude Code token usage across time windows.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--window", choices=["dashboard", "all", "today", "recent", "current"],
                    default="dashboard",
                    help="time window: dashboard (default, shows all four), all-time, today, recent, or current session")
    ap.add_argument("--recent-days", type=int, default=7,
                    help="size of the 'recent' window in days (default 7)")
    ap.add_argument("--by", choices=["model", "session", "project", "prompt"], default=None,
                    help="alternate grouping; overrides the dashboard layout")
    ap.add_argument("--since", help="ISO date/datetime lower bound (UTC)")
    ap.add_argument("--until", help="ISO date/datetime upper bound (UTC)")
    ap.add_argument("--project", help="filter by project dir substring")
    ap.add_argument("--session", help="filter by session UUID substring")
    ap.add_argument("--model", help="filter by model name substring (e.g. opus, sonnet, haiku)")
    ap.add_argument("--top", type=int, help="limit rows in tables")
    ap.add_argument("--no-cost", action="store_true", help="hide USD cost columns")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of formatted output")
    args = ap.parse_args()

    if not PROJECTS_DIR.exists():
        print(f"No transcripts directory at {PROJECTS_DIR}", file=sys.stderr)
        sys.exit(1)

    show_cost = not args.no_cost
    since = parse_iso(args.since) if args.since else None
    until = parse_iso(args.until) if args.until else None

    files = sorted(PROJECTS_DIR.rglob("*.jsonl"))
    current_session = find_current_session(files)
    events_all = collect_events(args)
    file_count = len({e["session"] for e in events_all}) or len(files)

    if args.json:
        windows = {}
        for w in ("all", "today", "recent", "current"):
            evs = filter_events(events_all, w, args.recent_days, current_session, since, until)
            agg = aggregate(evs)
            windows[w] = {
                "events": len(evs),
                "grand": agg["grand"],
                "by_model": agg["by_model"],
                "cost": grand_cost(agg["by_model"]) if show_cost else None,
            }
        print(json.dumps({
            "files_scanned": file_count,
            "current_session": current_session,
            "recent_days": args.recent_days,
            "windows": windows,
        }, indent=2, default=str))
        return

    print(f"Scanned {file_count} session(s) under {PROJECTS_DIR}")
    if current_session:
        print(f"Current session : {current_session}")

    # Alternate groupings short-circuit the windowed dashboard.
    if args.by:
        # Pick the slice based on --window (default: all).
        win = "all" if args.window == "dashboard" else args.window
        evs = filter_events(events_all, win, args.recent_days, current_session, since, until)
        agg = aggregate(evs)
        title_window = {"all": "All time", "today": "Today",
                        "recent": f"Last {args.recent_days} days",
                        "current": f"Current session ({current_session or '?'})"}[win]
        print(f"\nWindow: {title_window}\n")
        if args.by == "model":
            render_models_block(agg["by_model"], show_cost, args.top)
        elif args.by == "project":
            _render_named("By project", agg["by_project"], show_cost, args.top, model_lookup=False)
        elif args.by == "session":
            _render_named("By session", agg["by_session"], show_cost, args.top, model_lookup=False)
        elif args.by == "prompt":
            render_prompts(agg["by_prompt"], args.top, show_cost)
        print()
        render_grand_block(agg["grand"], agg["by_model"], show_cost)
        return

    # Dashboard or single-window view.
    if args.window == "dashboard":
        render_window_section("All time",
            filter_events(events_all, "all", args.recent_days, current_session, since, until),
            show_cost, args.top)
        render_window_section(f"Last {args.recent_days} days",
            filter_events(events_all, "recent", args.recent_days, current_session, since, until),
            show_cost, args.top)
        render_window_section("Today (UTC)",
            filter_events(events_all, "today", args.recent_days, current_session, since, until),
            show_cost, args.top)
        render_window_section("Current session",
            filter_events(events_all, "current", args.recent_days, current_session, since, until),
            show_cost, args.top, subtitle_extra=current_session or "—")
    else:
        labels = {"all": "All time", "today": "Today (UTC)",
                  "recent": f"Last {args.recent_days} days",
                  "current": "Current session"}
        extra = current_session or "—" if args.window == "current" else ""
        render_window_section(
            labels[args.window],
            filter_events(events_all, args.window, args.recent_days, current_session, since, until),
            show_cost, args.top, subtitle_extra=extra,
        )

def _render_named(title: str, mapping: dict, show_cost: bool, top: int | None, model_lookup: bool):
    print(title)
    print("=" * len(title))
    items = sorted(mapping.items(), key=lambda kv: kv[1].get("output", 0), reverse=True)
    if top:
        items = items[:top]
    headers = ["name", "msgs", "input", "output", "cache_r", "cache_w", "total_in"]
    aligns = ["l", "r", "r", "r", "r", "r", "r"]
    rows = []
    for name, t in items:
        total_in = t["input"] + t["cache_read"] + t["cache_write"]
        rows.append([
            truncate(name, 60),
            fmt_int(t["messages"]),
            fmt_int(t["input"]),
            fmt_int(t["output"]),
            fmt_int(t["cache_read"]),
            fmt_int(t["cache_write"]),
            fmt_int(total_in),
        ])
    render_table(rows, headers, aligns)

if __name__ == "__main__":
    main()
