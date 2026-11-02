"""Aggregations for the dashboard. Days are grouped in the server's local time zone."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone

from .config import Config
from .db import connect, rows


def window_start(days: int) -> datetime:
    """Local midnight `days - 1` days ago, so days=7 means today plus the six before it."""
    today = datetime.now().astimezone().date()
    start_local = datetime.combine(today - timedelta(days=max(days, 1) - 1), time.min).astimezone()
    return start_local.astimezone(timezone.utc)


def local_day(ts_iso: str) -> str:
    return datetime.fromisoformat(ts_iso).astimezone().date().isoformat()


def day_list(days: int) -> list[str]:
    start = window_start(days).astimezone().date()
    return [(start + timedelta(days=i)).isoformat() for i in range(max(days, 1))]


def model_family(cfg: Config, model_id: str) -> str:
    mid = model_id.lower()
    for name, price in cfg.pricing.models.items():
        if price.match.lower() in mid:
            return name
    return model_id


def _since(days: int) -> str:
    return window_start(days).isoformat(timespec="milliseconds")


def summary(cfg: Config, days: int) -> dict:
    since = _since(days)
    with connect() as conn:
        reqs = rows(conn.execute(
            """SELECT model_key, provider, status, fell_back, task_type, complexity, reasoning, route_mode, effort, think,
                      input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                      latency_ms, cost_usd, avoided_usd
               FROM requests WHERE created_at >= ?""",
            (since,),
        ))
        cc = rows(conn.execute(
            """SELECT source, model, session_id, input_tokens, output_tokens,
                      cache_read_tokens, cache_write_tokens, cost_usd
               FROM cc_usage WHERE ts >= ?""",
            (since,),
        ))

    ok = [r for r in reqs if r["status"] == "ok"]
    local = [r for r in ok if r["provider"] == "ollama"]
    claude = [r for r in ok if r["provider"] == "claude_cli"]

    def tok_in(r: dict) -> int:
        return r["input_tokens"] + r["cache_read_tokens"] + r["cache_write_tokens"]

    by_model: dict[str, dict] = {}
    for r in reqs:
        key = r["model_key"] or "(failed)"
        b = by_model.setdefault(key, {
            "model_key": key, "label": cfg.label(r["model_key"]) or "Failed", "provider": r["provider"],
            "requests": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0,
            "cost_usd": 0.0, "avoided_usd": 0.0, "_lat": [],
            "efforts": defaultdict(int), "thinking": 0, "think_known": 0,
        })
        b["requests"] += 1
        if r["status"] != "ok":
            b["errors"] += 1
            continue
        if r["provider"] == "ollama":
            if r["think"] is not None:
                b["think_known"] += 1
                b["thinking"] += r["think"]
        else:
            b["efforts"][r["effort"] or "default"] += 1
        b["input_tokens"] += tok_in(r)
        b["output_tokens"] += r["output_tokens"]
        b["cost_usd"] += r["cost_usd"]
        b["avoided_usd"] += r["avoided_usd"]
        if r["latency_ms"] is not None:
            b["_lat"].append(r["latency_ms"])
    for b in by_model.values():
        lat = sorted(b.pop("_lat"))
        b["median_latency_ms"] = lat[len(lat) // 2] if lat else None
        b["efforts"] = dict(b["efforts"])

    task_types: dict[str, dict] = {}
    complexity = [0, 0, 0, 0, 0]
    reasoning = [0, 0, 0, 0, 0]
    for r in ok:
        if r["reasoning"]:
            reasoning[r["reasoning"] - 1] += 1
        if r["route_mode"] != "auto":
            continue
        t = task_types.setdefault(r["task_type"] or "other", {"task_type": r["task_type"] or "other", "total": 0, "models": defaultdict(int)})
        t["total"] += 1
        t["models"][r["model_key"]] += 1
        if r["complexity"]:
            complexity[r["complexity"] - 1] += 1

    cc_sources: dict[str, dict] = {}
    cc_models: dict[str, dict] = {}
    for r in cc:
        s = cc_sources.setdefault(r["source"], {
            "messages": 0, "sessions": set(), "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0,
        })
        s["messages"] += 1
        if r["session_id"]:
            s["sessions"].add(r["session_id"])
        for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd"):
            s[k] += r[k]
        if r["source"] == "interactive":
            fam = model_family(cfg, r["model"])
            m = cc_models.setdefault(fam, {"model": fam, "messages": 0, "output_tokens": 0, "cost_usd": 0.0})
            m["messages"] += 1
            m["output_tokens"] += r["output_tokens"]
            m["cost_usd"] += r["cost_usd"]
    for s in cc_sources.values():
        s["sessions"] = len(s["sessions"])
    empty_cc = {"messages": 0, "sessions": 0, "input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0}

    return {
        "days": days,
        "since": since,
        "requests": {
            "total": len(reqs),
            "ok": len(ok),
            "errors": len(reqs) - len(ok),
            "fallbacks": sum(r["fell_back"] for r in reqs),
            "local": len(local),
            "claude": len(claude),
            "local_share": (len(local) / len(ok)) if ok else None,
        },
        "tokens": {
            "local_in": sum(tok_in(r) for r in local),
            "local_out": sum(r["output_tokens"] for r in local),
            "claude_in": sum(tok_in(r) for r in claude),
            "claude_out": sum(r["output_tokens"] for r in claude),
        },
        "cost": {
            "claude_api_equivalent_usd": sum(r["cost_usd"] for r in claude),
            "avoided_usd": sum(r["avoided_usd"] for r in local),
        },
        "by_model": sorted(by_model.values(), key=lambda b: -b["requests"]),
        "by_task_type": sorted(
            ({**t, "models": dict(t["models"])} for t in task_types.values()), key=lambda t: -t["total"]
        ),
        "complexity": complexity,
        "reasoning": reasoning,
        "claude_code": {
            "interactive": cc_sources.get("interactive", empty_cc),
            "router": cc_sources.get("router", empty_cc),
            "by_model": sorted(cc_models.values(), key=lambda m: -m["cost_usd"]),
        },
    }


def timeseries(cfg: Config, days: int) -> dict:
    since = _since(days)
    dates = day_list(days)
    with connect() as conn:
        reqs = rows(conn.execute(
            """SELECT created_at, model_key, provider, input_tokens, output_tokens,
                      cache_read_tokens, cache_write_tokens
               FROM requests WHERE created_at >= ? AND status = 'ok'""",
            (since,),
        ))
        cc = rows(conn.execute(
            "SELECT ts, model, cost_usd FROM cc_usage WHERE ts >= ? AND source = 'interactive'",
            (since,),
        ))

    tokens = {d: defaultdict(int) for d in dates}
    counts = {d: {"local": 0, "claude": 0} for d in dates}
    router_keys: list[str] = []
    for r in reqs:
        d = local_day(r["created_at"])
        if d not in tokens:
            continue
        k = r["model_key"]
        if k not in router_keys:
            router_keys.append(k)
        tokens[d][k] += r["input_tokens"] + r["output_tokens"] + r["cache_read_tokens"] + r["cache_write_tokens"]
        counts[d]["local" if r["provider"] == "ollama" else "claude"] += 1

    cc_cost = {d: defaultdict(float) for d in dates}
    cc_keys: list[str] = []
    for r in cc:
        d = local_day(r["ts"])
        if d not in cc_cost:
            continue
        fam = model_family(cfg, r["model"])
        if fam not in cc_keys:
            cc_keys.append(fam)
        cc_cost[d][fam] += r["cost_usd"]

    return {
        "dates": dates,
        "router_keys": router_keys,
        "router_labels": {k: cfg.label(k) for k in router_keys},
        "router_tokens": [{"date": d, **{k: tokens[d].get(k, 0) for k in router_keys}} for d in dates],
        "router_requests": [{"date": d, **counts[d]} for d in dates],
        "cc_keys": cc_keys,
        "cc_cost": [{"date": d, **{k: round(cc_cost[d].get(k, 0.0), 4) for k in cc_keys}} for d in dates],
    }
