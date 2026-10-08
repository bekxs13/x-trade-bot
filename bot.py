"""Watch a few X accounts and ping you when one of them posts a trade or a new market view.

Run once per schedule tick (GitHub Actions cron). State lives in state.json, sent alerts in alerts.jsonl.

  python bot.py                      poll every account once and send alerts
  python bot.py --dry-run            poll and classify, print alerts instead of sending
  python bot.py --test "post text"   classify one pasted post (needs only ANTHROPIC_API_KEY)
       [--reply "their own reply"] [--handle based16z] [--image URL]
  python bot.py --test-cases         run tests/cases.json against the model and show misses
  python bot.py --test-notify        send a sample TRADE and VIEW ping to your notifiers
"""

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import requests

ROOT = Path(__file__).parent
STATE_PATH = ROOT / "state.json"
ALERTS_PATH = ROOT / "alerts.jsonl"
ACCOUNTS_PATH = ROOT / "accounts.txt"
PROMPT_PATH = ROOT / "prompt.md"
CASES_PATH = ROOT / "tests" / "cases.json"

X_API = "https://api.x.com/2"
MODEL = os.environ.get("MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("EFFORT", "low")
MIN_CONFIDENCE = float(os.environ.get("MIN_CONFIDENCE", "0.7"))
# A first read this far under MIN_CONFIDENCE (or one that asks for more context) gets a second, deeper look.
UNSURE_MARGIN = float(os.environ.get("UNSURE_MARGIN", "0.25"))
CONTEXT_POSTS = int(os.environ.get("CONTEXT_POSTS", "5"))  # own posts shown on the first read
DEEP_CONTEXT_POSTS = int(os.environ.get("DEEP_CONTEXT_POSTS", "15"))  # own posts shown on the deeper look
DEEP_CONTEXT_REPLIES = int(os.environ.get("DEEP_CONTEXT_REPLIES", "10"))  # replies to others on the deeper look
SELF_REPLIES_PER_POST = int(os.environ.get("SELF_REPLIES_PER_POST", "1"))
BASELINE_POSTS = 20
KEEP_POSTS, KEEP_REPLIES, KEEP_THREADS = 20, 15, 100
MAX_IMAGES = 4
# Models that accept server-side refusal fallbacks. Haiku does not.
FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")

# Coins priced from Coinbase when a post names them without a cashtag.
MAJORS = {
    "BTC": ["btc", "bitcoin"],
    "ETH": ["eth", "ether", "ethereum"],
    "SOL": ["sol", "solana"],
    "BNB": ["bnb"],
    "XRP": ["xrp"],
    "DOGE": ["doge"],
    "HYPE": ["hyperliquid"],
}

TRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "present": {"type": "boolean"},
        "asset": {"type": "string", "description": "Ticker or name, empty if unknown"},
        "chain": {"type": "string", "description": "solana, base, eth, bnb, other, or empty"},
        "contract_address": {"type": "string"},
        "direction": {"type": "string", "enum": ["long", "short", "exit"]},
        "structure": {"type": "string", "description": "spot, perp, long puts, long calls, short puts, short calls, or unknown"},
        "entry": {"type": "string", "description": "Entry price or level if given, else empty"},
        "horizon": {"type": "string"},
        "source": {"type": "string", "enum": ["disclosed", "stated", "inferred"]},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["present", "asset", "chain", "contract_address", "direction", "structure", "entry",
                 "horizon", "source", "confidence", "reason"],
    "additionalProperties": False,
}
BIAS_SCHEMA = {
    "type": "object",
    "properties": {
        "present": {"type": "boolean"},
        "scope": {"type": "string", "description": "Short label: crypto market, btc, alts, memecoins, a ticker..."},
        "stance": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
        "change": {"type": "string", "enum": ["new", "changed", "restated"]},
        "horizon": {"type": "string"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["present", "scope", "stance", "change", "horizon", "confidence", "reason"],
    "additionalProperties": False,
}
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "trade": TRADE_SCHEMA,
        "bias": BIAS_SCHEMA,
        "needs_more_context": {"type": "boolean"},
        "more_context_reason": {"type": "string"},
    },
    "required": ["trade", "bias", "needs_more_context", "more_context_reason"],
    "additionalProperties": False,
}


# ---------- state ----------

def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"accounts": {}}


def save_state(state, heartbeat=timedelta(hours=1)):
    # updated_at shows on the dashboard that the bot is alive. It's refreshed when anything changed, or
    # hourly on quiet runs, so a quiet run doesn't make a commit every 5 minutes.
    now = datetime.now(timezone.utc)
    old = load_state()
    prev = old.pop("updated_at", "")
    state.pop("updated_at", None)
    same = json.dumps(old, sort_keys=True) == json.dumps(state, sort_keys=True)
    if same and prev and now - datetime.fromisoformat(prev) < heartbeat:
        state["updated_at"] = prev
    else:
        state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def load_accounts():
    handles = []
    for line in ACCOUNTS_PATH.read_text().splitlines():
        line = line.split("#", 1)[0].strip().lstrip("@")
        if line:
            handles.append(line)
    return handles


def new_account(handle):
    # posts: their main posts and self-replies. replies: their replies to other people (context only).
    # threads: per main post, how many self-replies were handled and which alerts already went out.
    # biases: their last recorded view per scope, so view pings only fire on a new or changed view.
    # positions: trades they're in, per asset, from TRADE alerts; an exit alert closes one.
    return {"handle": handle, "posts": [], "replies": [], "threads": {}, "biases": {}, "positions": {}}


def slim(p):
    return {"id": p["id"], "created_at": p.get("created_at", ""), "text": p["text"]}


# ---------- X API ----------

class XClient:
    # X's current docs show "post" names (post.fields, note_post) while the API has long used
    # "tweet" names. Try the long-standing names first and switch once if X rejects them.
    TWEET_FIELDS = {
        "tweet.fields": "created_at,note_tweet,referenced_tweets,conversation_id,in_reply_to_user_id,attachments",
        "expansions": "attachments.media_keys",
        "media.fields": "type,url,preview_image_url",
    }
    POST_FIELDS = {
        "post.fields": "created_at,note_post,referenced_posts,conversation_id,in_reply_to_user_id,attachments",
        "expansions": "attachments.media_keys",
        "media.fields": "type,url,preview_image_url",
    }

    def __init__(self, bearer):
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {bearer}"
        self.naming = "tweet"

    def _get(self, path, params):
        r = self.s.get(f"{X_API}{path}", params=params, timeout=30)
        if r.status_code == 429:
            reset = int(r.headers.get("x-rate-limit-reset", time.time() + 60))
            raise RuntimeError(f"X rate limit hit, resets at {datetime.fromtimestamp(reset, timezone.utc)}")
        return r

    def _get_posts(self, path, params):
        fields = self.TWEET_FIELDS if self.naming == "tweet" else self.POST_FIELDS
        r = self._get(path, {**params, **fields})
        if r.status_code == 400 and self.naming == "tweet":
            print(f"X rejected tweet.* field names ({r.text[:200]}), retrying with post.* names", file=sys.stderr)
            self.naming = "post"
            return self._get_posts(path, params)
        r.raise_for_status()
        return parse_posts(r.json())

    def user_id(self, handle):
        r = self._get(f"/users/by/username/{handle}", {})
        r.raise_for_status()
        return r.json()["data"]["id"]

    def timeline(self, user_id, since_id=None):
        """New posts and replies (not reposts), oldest first. Replies are needed to spot self-replies."""
        params = {"exclude": "retweets", "max_results": BASELINE_POSTS}
        if since_id:
            params.update(since_id=since_id, max_results=100)
        return self._get_posts(f"/users/{user_id}/tweets", params)

    def lookup(self, ids):
        """Fetch specific posts (quoted posts, parents of self-replies) by id."""
        if not ids:
            return {}
        posts = self._get_posts("/tweets", {"ids": ",".join(sorted(ids)[:100])})
        return {p["id"]: p for p in posts}


def full_text(post):
    note = post.get("note_tweet") or post.get("note_post") or {}
    return note.get("text") or post.get("text", "")


def parse_posts(body):
    includes = body.get("includes", {})
    media = {m["media_key"]: m for m in includes.get("media", [])}
    out = []
    for p in body.get("data", []):
        refs = {r["type"]: r["id"] for r in (p.get("referenced_tweets") or p.get("referenced_posts") or [])}
        images = []
        for key in (p.get("attachments") or {}).get("media_keys", []):
            m = media.get(key, {})
            url = m.get("url") or m.get("preview_image_url")
            if url:
                images.append(url)
        out.append({
            "id": p["id"],
            "text": full_text(p),
            "created_at": p.get("created_at", ""),
            "conversation_id": p.get("conversation_id", p["id"]),
            "in_reply_to_user_id": p.get("in_reply_to_user_id", ""),
            "reply_to_id": refs.get("replied_to", ""),
            "quoted_id": refs.get("quoted", ""),
            "images": images,
        })
    return sorted(out, key=lambda p: int(p["id"]))


# ---------- prices ----------

CASHTAG_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,14})\b")
EVM_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
SOL_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


def find_assets(text):
    """Return (majors, cashtags, contract addresses) mentioned in text."""
    lower = text.lower()
    majors = {sym for sym, words in MAJORS.items() if any(re.search(rf"\b{w}\b", lower) for w in words)}
    tags = {t.upper() for t in CASHTAG_RE.findall(text)}
    majors |= tags & MAJORS.keys()
    tags -= MAJORS.keys()
    addrs = set(EVM_RE.findall(text))
    addrs |= {a for a in SOL_RE.findall(text) if any(c.isdigit() for c in a) and any(c.isupper() for c in a)}
    return majors, tags, addrs


def coinbase_price(sym):
    r = requests.get(f"https://api.coinbase.com/v2/prices/{sym}-USD/spot", timeout=10)
    if r.ok:
        return float(r.json()["data"]["amount"])
    return None


def dexscreener_lookup(query):
    """Best-matching pair for a ticker or contract address. Returns a short description or None."""
    r = requests.get("https://api.dexscreener.com/latest/dex/search", params={"q": query}, timeout=10)
    if not r.ok:
        return None
    pairs = r.json().get("pairs") or []
    if query.startswith("0x") or len(query) > 30:
        pairs = [p for p in pairs if p["baseToken"]["address"].lower() == query.lower()]
    else:
        pairs = [p for p in pairs if p["baseToken"]["symbol"].upper() == query.upper()]
    if not pairs:
        return None
    best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    base = best["baseToken"]
    mcap = best.get("marketCap") or best.get("fdv")
    change = (best.get("priceChange") or {}).get("h24")
    parts = [f"{base['symbol']} ({base['name']}) on {best['chainId']}", f"price ${best.get('priceUsd')}"]
    if mcap:
        parts.append(f"mcap ${mcap:,.0f}")
    if change is not None:
        parts.append(f"24h {change}%")
    parts.append(f"CA {base['address']}")
    return ", ".join(parts)


def price_context(text):
    majors, tags, addrs = find_assets(text)
    lines = []
    for sym in sorted(majors):
        try:
            p = coinbase_price(sym)
            if p:
                lines.append(f"{sym}: ${p:,.2f}")
        except requests.RequestException:
            pass
    for q in sorted(tags) + sorted(addrs):
        try:
            info = dexscreener_lookup(q)
            if info:
                lines.append(info)
        except requests.RequestException:
            pass
    return lines


# ---------- classifier ----------
# A "unit" is what gets read together: {"main": post, "reply": their first self-reply or None, "late": bool}.
# late=True means the main post was already handled on an earlier run and only the reply is new.

_client = None


def claude():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def unit_text(unit):
    parts = []
    for key in ("main", "reply"):
        p = unit.get(key)
        if p:
            parts += [p["text"], p.get("quoted_text", "")]
    return " ".join(parts)


def unit_images(unit):
    imgs = []
    if not unit.get("late") and unit.get("main"):
        imgs += unit["main"].get("images", [])
    if unit.get("reply"):
        imgs += unit["reply"].get("images", [])
    return imgs[:MAX_IMAGES]


def build_prompt_text(handle, unit, ctx, prices, deep, first_read=None):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    main, reply = unit.get("main"), unit.get("reply")
    lines = [f"Author: @{handle}", f"Now: {now}", ""]

    def show(label, p):
        lines.append(f"{label} (posted {p.get('created_at') or 'unknown'}):")
        lines.append(p["text"])
        if p.get("quoted_text"):
            lines.append("It quotes this post:")
            lines.append(p["quoted_text"])
        lines.append("")

    if unit.get("late"):
        lines.append(f"This is @{handle}'s first reply to their own earlier post, which was already processed.")
        already = unit.get("already_alerted") or []
        lines.append("Alerts already sent for that post: " + (", ".join(already) if already else "none"))
        lines.append("Report a trade or bias only if the reply adds or changes something.")
        lines.append("")
        if main:
            show("Their earlier post", main)
        show("Their reply to it", reply)
    else:
        show("Post", main)
        if reply:
            show(f"@{handle}'s first reply to this post", reply)
    if unit_images(unit):
        lines += ["The attached images belong to these posts.", ""]

    lines.append("Live prices for assets mentioned:")
    lines += [f"- {line}" for line in prices] or ["- none found"]
    lines.append("")
    lines.append(f"@{handle}'s recorded views (scope: stance, since):")
    views = ctx.get("biases") or {}
    lines += [f"- {v['scope']}: {v['stance']} (since {v.get('since', '?')})" for v in views.values()] or ["- none recorded yet"]
    lines.append("")
    shown = {p["id"] for p in (main, reply) if p}
    posts = [h for h in ctx.get("posts", []) if h["id"] not in shown]
    posts = posts[-(DEEP_CONTEXT_POSTS if deep else CONTEXT_POSTS):]
    lines.append(f"@{handle}'s recent posts, oldest first:")
    lines += [f"- [{h.get('created_at', '')}] {h['text']}" for h in posts] or ["- none stored yet"]
    if deep:
        lines.append("")
        lines.append(f"@{handle}'s recent replies to other people, oldest first:")
        replies = ctx.get("replies", [])[-DEEP_CONTEXT_REPLIES:]
        lines += [f"- [{h.get('created_at', '')}] {h['text']}" for h in replies] or ["- none stored yet"]
        lines.append("")
        lines.append("This is a second, deeper look. Your first read was unsure:")
        lines.append(json.dumps(first_read))
        lines.append("Decide again using the extra history above.")
    return "\n".join(lines)


def call_model(text, images):
    content = [{"type": "image", "source": {"type": "url", "url": u}} for u in images]
    content.append({"type": "text", "text": text})
    kwargs = dict(
        model=MODEL,
        max_tokens=4000,
        system=PROMPT_PATH.read_text(),
        messages=[{"role": "user", "content": content}],
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": RESULT_SCHEMA}},
    )
    if MODEL in FALLBACK_MODELS:
        kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    try:
        resp = claude().beta.messages.create(**kwargs)
    except anthropic.BadRequestError:
        if not images:
            raise
        # Most likely an image URL that couldn't be fetched. Read the text alone rather than miss the post.
        return call_model(text, [])
    if resp.stop_reason == "refusal":
        return None
    return json.loads(next(b.text for b in resp.content if b.type == "text"))


def is_unsure(res):
    if res["needs_more_context"]:
        return True
    return any(
        part["present"] and MIN_CONFIDENCE - UNSURE_MARGIN <= part["confidence"] < MIN_CONFIDENCE
        for part in (res["trade"], res["bias"])
    )


def classify(handle, unit, ctx, prices=None):
    """Read a post (plus their first self-reply, if any). Takes a deeper look when unsure."""
    if prices is None:
        prices = price_context(unit_text(unit))
    images = unit_images(unit)
    res = call_model(build_prompt_text(handle, unit, ctx, prices, deep=False), images)
    if res and is_unsure(res):
        print(f"  unsure on first read ({res['more_context_reason'] or 'borderline confidence'}), taking a deeper look")
        deeper = call_model(build_prompt_text(handle, unit, ctx, prices, deep=True, first_read=res), images)
        if deeper:
            deeper["deeper_look"] = True
            res = deeper
    if res:
        res["prices"] = prices
    return res


def trade_key(t):
    return f"trade:{(t['asset'] or '?').upper()}:{t['direction']}"


def bias_key(b):
    return f"bias:{b['scope'].lower()}:{b['stance']}"


def decide(res, biases, already=()):
    """Turn a model result into alerts: [(kind, key, part, previous_view)]."""
    if not res:
        return []
    out = []
    t, b = res["trade"], res["bias"]
    trade_on = t["present"] and t["confidence"] >= MIN_CONFIDENCE
    if trade_on and trade_key(t) not in already:
        out.append(("trade", trade_key(t), t, None))
    if b["present"] and b["confidence"] >= MIN_CONFIDENCE:
        prev = biases.get(b["scope"].lower())
        changed = prev is None or prev["stance"] != b["stance"]
        same_as_trade = trade_on and b["scope"].lower() == (t["asset"] or "").lower()
        if changed and not same_as_trade and bias_key(b) not in already:
            out.append(("bias", bias_key(b), b, prev))
    return out


def record_bias(biases, b, post):
    if not (b["present"] and b["confidence"] >= MIN_CONFIDENCE):
        return
    scope = b["scope"].lower()
    prev = biases.get(scope)
    since = prev["since"] if prev and prev["stance"] == b["stance"] else (post.get("created_at") or "")[:10]
    biases[scope] = {"scope": b["scope"], "stance": b["stance"], "since": since, "post_id": post["id"],
                     "updated_at": post.get("created_at", ""), "reason": b["reason"]}


def record_position(positions, t, post, url):
    """Keep the account's open trades: long/short opens or flips a position, exit closes it."""
    key = (t["asset"] or t["contract_address"] or "?").upper()
    if t["direction"] == "exit":
        positions.pop(key, None)
        return
    positions[key] = {
        "asset": t["asset"], "direction": t["direction"], "structure": t["structure"], "entry": t["entry"],
        "horizon": t["horizon"], "chain": t["chain"], "contract_address": t["contract_address"],
        "confidence": t["confidence"], "reason": t["reason"], "since": post.get("created_at", ""),
        "post_id": post["id"], "url": url,
    }


# ---------- notifiers ----------

def alert_title(kind, handle, part):
    if kind == "trade":
        struct = f" ({part['structure']})" if part["structure"] not in ("", "spot", "unknown") else ""
        return f"TRADE · @{handle}: {part['direction'].upper()} {part['asset'] or '?'}{struct}"
    return f"VIEW · @{handle}: {part['stance'].upper()} on {part['scope']}"


def unit_post_text(unit):
    parts = []
    if unit.get("main") and not unit.get("late"):
        parts.append(unit["main"]["text"])
    if unit.get("reply"):
        parts.append(f"↳ their reply: {unit['reply']['text']}")
    return "\n\n".join(parts)


COLORS = {"long": 0x2ECC71, "short": 0xE74C3C, "exit": 0xF1C40F,
          "bullish": 0x27AE60, "bearish": 0xC0392B, "neutral": 0x95A5A6}


def discord_payload(kind, handle, unit, part, prev, prices, url):
    fields = []
    if kind == "trade":
        fields.append({"name": "Confidence", "value": f"{part['confidence']:.2f} ({part['source']})", "inline": True})
        if part["entry"]:
            fields.append({"name": "Entry", "value": part["entry"], "inline": True})
        if part["horizon"]:
            fields.append({"name": "Horizon", "value": part["horizon"], "inline": True})
        if part["contract_address"]:
            fields.append({"name": "CA", "value": f"`{part['contract_address']}` {part['chain']}".strip(), "inline": False})
    else:
        was = f"was {prev['stance']} since {prev['since']}" if prev else "first recorded view"
        fields.append({"name": "Change", "value": was, "inline": True})
        fields.append({"name": "Confidence", "value": f"{part['confidence']:.2f}", "inline": True})
        if part["horizon"]:
            fields.append({"name": "Horizon", "value": part["horizon"], "inline": True})
    if prices:
        fields.append({"name": "Prices at alert", "value": "\n".join(prices)[:1000], "inline": False})
    fields.append({"name": "Post", "value": unit_post_text(unit)[:1000] or "(no text)", "inline": False})
    color = COLORS.get(part.get("direction") or part.get("stance"), 0x95A5A6)
    return {
        "username": "X Trade Bot",
        "embeds": [{"title": alert_title(kind, handle, part)[:250], "url": url,
                    "description": part["reason"][:2000], "color": color, "fields": fields}],
    }


def send_discord(hook, payload):
    for _ in range(3):
        r = requests.post(hook, json=payload, timeout=15)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)))
            continue
        r.raise_for_status()
        return


DEXSCREENER_CHAINS = {"solana": "solana", "base": "base", "eth": "ethereum", "ethereum": "ethereum",
                      "bnb": "bsc", "bsc": "bsc"}


def alert_body(kind, part, prev, unit):
    """Plain-text body for phone notifications: the reason, the key facts, then the post."""
    facts = []
    if kind == "trade":
        facts.append(f"Confidence {part['confidence']:.2f} ({part['source']})")
        if part["entry"]:
            facts.append(f"Entry {part['entry']}")
        if part["horizon"]:
            facts.append(f"Horizon {part['horizon']}")
        if part["contract_address"]:
            facts.append(f"CA {part['contract_address']} {part['chain']}".strip())
    else:
        facts.append(f"Was {prev['stance']} since {prev['since']}" if prev else "First recorded view on this")
        facts.append(f"Confidence {part['confidence']:.2f}")
        if part["horizon"]:
            facts.append(f"Horizon {part['horizon']}")
    return f"{part['reason']}\n{' · '.join(facts)}\n\n{unit_post_text(unit)}"


def send_ntfy(kind, part, title, body, url, images):
    env = os.environ
    topic = (env.get("NTFY_VIEWS_TOPIC") if kind == "bias" else None) or env["NTFY_TOPIC"]
    up = (part.get("direction") or part.get("stance")) in ("long", "bullish")
    actions = [{"action": "view", "label": "Open post", "url": url, "clear": True}]
    chain = DEXSCREENER_CHAINS.get((part.get("chain") or "").lower())
    if kind == "trade" and part.get("contract_address") and chain:
        actions.append({"action": "view", "label": "Chart",
                        "url": f"https://dexscreener.com/{chain}/{part['contract_address']}"})
    msg = {
        "topic": topic,
        "title": title,
        "message": body.encode()[:3500].decode(errors="ignore"),  # ntfy caps messages at 4,096 bytes
        "click": url,
        "priority": 4 if kind == "trade" else 3,
        "tags": ["chart_with_upwards_trend" if up else "chart_with_downwards_trend"],
        "actions": actions,
    }
    if images:
        msg["attach"] = images[0]  # shows the chart in the notification
    headers = {"Authorization": f"Bearer {env['NTFY_TOKEN']}"} if env.get("NTFY_TOKEN") else {}
    requests.post(env.get("NTFY_SERVER") or "https://ntfy.sh", json=msg, headers=headers, timeout=15).raise_for_status()


def send_telegram(token, chat_id, title, body, url):
    text = f"<b>{html.escape(title)}</b>\n{html.escape(body[:3500])}\n\n{url}"
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
        "chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True,
    }, timeout=15).raise_for_status()


def notify(kind, handle, unit, part, prev, prices, dry_run=False, log=True):
    """Send one alert to every notifier that's configured. A failed send is logged, not fatal."""
    target = unit["reply"] if unit.get("late") else unit["main"]
    url = f"https://x.com/{handle}/status/{target['id']}"
    title = alert_title(kind, handle, part)
    body = alert_body(kind, part, prev, unit)
    payload = discord_payload(kind, handle, unit, part, prev, prices, url)
    if dry_run:
        print(f"{title}\n{body}\n")
        return
    env = os.environ
    hook = (env.get("DISCORD_BIAS_WEBHOOK_URL") if kind == "bias" else None) or env.get("DISCORD_WEBHOOK_URL")
    senders = []
    if env.get("NTFY_TOPIC"):
        senders.append(("ntfy", lambda: send_ntfy(kind, part, title, body, url, unit_images(unit))))
    if hook:
        senders.append(("discord", lambda: send_discord(hook, payload)))
    if env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
        senders.append(("telegram", lambda: send_telegram(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"], title, body, url)))
    if not senders:
        print("  no notifier configured (set NTFY_TOPIC, DISCORD_WEBHOOK_URL or TELEGRAM_*)", file=sys.stderr)
    sent = 0
    for name, send in senders:
        try:
            send()
            sent += 1
            print(f"  sent to {name}")
        except requests.RequestException as e:
            print(f"  {name} send failed: {e}", file=sys.stderr)
    if not log:
        return sent
    with ALERTS_PATH.open("a") as f:
        f.write(json.dumps({"sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "kind": kind, "handle": handle, "url": url, "post": unit_post_text(unit),
                            "prices": prices, **part}) + "\n")
    return sent


# ---------- main loop ----------

def is_self_reply(p, uid):
    return bool(p["reply_to_id"]) and p["in_reply_to_user_id"] == uid


def is_own_post(p, uid):
    return not p["reply_to_id"] or is_self_reply(p, uid)


def store(acct, posts):
    uid = acct["user_id"]
    for p in posts:
        acct["posts" if is_own_post(p, uid) else "replies"].append(slim(p))
    acct["posts"] = acct["posts"][-KEEP_POSTS:]
    acct["replies"] = acct["replies"][-KEEP_REPLIES:]
    acct["threads"] = dict(sorted(acct["threads"].items(), key=lambda kv: int(kv[0]))[-KEEP_THREADS:])


def process_account(x, handle, acct, posts, dry_run=False):
    """Classify new main posts (with their first self-reply) and late self-replies. Returns error count."""
    uid = acct["user_id"]
    by_id = {h["id"]: h for h in acct["posts"]}
    by_id.update({p["id"]: p for p in posts})
    selfs = [p for p in posts if is_self_reply(p, uid)]

    # One lookup call for quoted posts and for parents of self-replies we haven't stored.
    need = {p["quoted_id"] for p in posts if p["quoted_id"] and is_own_post(p, uid)}
    need |= {r["reply_to_id"] for r in selfs if r["reply_to_id"] not in by_id}
    try:
        fetched = x.lookup(need)
    except (requests.RequestException, RuntimeError) as e:
        print(f"@{handle}: lookup of quoted/parent posts failed: {e}", file=sys.stderr)
        fetched = {}
    for p in posts:
        if p["quoted_id"] in fetched:
            p["quoted_text"] = fetched[p["quoted_id"]]["text"]

    grouped, failed_at = set(), None
    for p in posts:
        if p["id"] in grouped:
            continue
        if not p["reply_to_id"]:
            reply = next((r for r in selfs if r["reply_to_id"] == p["id"]), None)
            if reply:
                grouped.add(reply["id"])
            unit = {"main": p, "reply": reply, "late": False}
            thread_id, already = p["id"], []
        elif is_self_reply(p, uid):
            if p["reply_to_id"] != p["conversation_id"]:
                continue  # further down their own thread, or a chain under someone else's post
            thread = acct["threads"].get(p["conversation_id"], {})
            if thread.get("self_replies", 0) >= SELF_REPLIES_PER_POST:
                continue
            already = thread.get("alerted", [])
            parent = by_id.get(p["reply_to_id"]) or fetched.get(p["reply_to_id"])
            unit = {"main": parent, "reply": p, "late": True, "already_alerted": already}
            thread_id = p["conversation_id"]
        else:
            continue  # a reply to someone else: kept as context only

        try:
            res = classify(handle, unit, acct)
        except (anthropic.APIError, json.JSONDecodeError, StopIteration) as e:
            print(f"@{handle} {p['id']}: classify failed: {e}", file=sys.stderr)
            failed_at = int(p["id"])
            break
        alerts = decide(res, acct["biases"], already)
        summary = json.dumps({k: res[k] for k in ("trade", "bias", "needs_more_context")}) if res else "model declined"
        print(f"@{handle} {p['id']}: {', '.join(a[1] for a in alerts) or 'no alert'} {summary}")
        target = unit["reply"] if unit["late"] else unit["main"]
        for kind, _key, part, prev in alerts:
            notify(kind, handle, unit, part, prev, res["prices"], dry_run)
            if kind == "trade":
                record_position(acct["positions"], part, target, f"https://x.com/{handle}/status/{target['id']}")
        if res:
            record_bias(acct["biases"], res["bias"], target)
        thread = acct["threads"].setdefault(thread_id, {"self_replies": 0, "alerted": []})
        thread["self_replies"] += 1 if unit["reply"] else 0
        thread["alerted"] += [a[1] for a in alerts]

    # Everything before a failure is done; the failed post and anything after it is retried next run.
    done = [p for p in posts if failed_at is None or int(p["id"]) < failed_at]
    store(acct, done)
    if done:
        acct["last_id"] = done[-1]["id"]
    return 0 if failed_at is None else 1


def poll(dry_run=False):
    missing = [k for k in ("X_BEARER_TOKEN", "ANTHROPIC_API_KEY") if not os.environ.get(k)]
    if missing:
        # Keeps the 5-minute schedule from failing (and emailing you) until the secrets are added.
        print(f"::warning::Skipping this run: {', '.join(missing)} not set yet. "
              "Add them under Settings > Secrets and variables > Actions.")
        return 0
    x = XClient(os.environ["X_BEARER_TOKEN"])
    state = load_state()
    accounts = state.setdefault("accounts", {})
    errors = 0
    for handle in load_accounts():
        acct = accounts.setdefault(handle.lower(), new_account(handle))
        for key, val in new_account(handle).items():
            acct.setdefault(key, val)
        try:
            if "user_id" not in acct:
                acct["user_id"] = x.user_id(handle)
            first_run = "last_id" not in acct
            posts = x.timeline(acct["user_id"], acct.get("last_id"))
        except (requests.RequestException, RuntimeError, KeyError) as e:
            print(f"@{handle}: fetch failed: {e}", file=sys.stderr)
            errors += 1
            continue
        if first_run:
            # Baseline: keep recent posts as context, don't alert on old ones.
            for p in posts:
                if not p["reply_to_id"]:
                    acct["threads"][p["id"]] = {"self_replies": 0, "alerted": []}
            store(acct, posts)
            acct["last_id"] = posts[-1]["id"] if posts else "0"
            print(f"@{handle}: baseline saved from {len(posts)} recent post(s), no alerts")
            continue
        print(f"@{handle}: {len(posts)} new post(s) and replies")
        errors += process_account(x, handle, acct, posts, dry_run)
    if not dry_run:
        save_state(state)
    return errors


# ---------- manual testing ----------

def test_post(text, pid="0", images=None, created_at=""):
    return {"id": pid, "text": text, "created_at": created_at, "images": images or [], "quoted_text": ""}


def send_test_pings():
    """Send one sample TRADE and one sample VIEW ping through every configured notifier."""
    trade = {"present": True, "asset": "BTC", "chain": "", "contract_address": "", "direction": "short",
             "structure": "long puts", "entry": "", "horizon": "2d-2w", "source": "disclosed", "confidence": 0.9,
             "reason": "TEST PING. Discloses long puts on BTC with a 2 day to 2 week horizon."}
    view = {"present": True, "scope": "alts", "stance": "bearish", "change": "changed", "horizon": "few weeks",
            "confidence": 0.8, "reason": "TEST PING. Says alts bleed for a few weeks, after being bullish."}
    unit = {"main": test_post("(Discl long puts) 2d-2w", pid="2108074591104926044"), "reply": None, "late": False}
    prev = {"stance": "bullish", "since": "2026-10-01"}
    sent = notify("trade", "based16z", unit, trade, None, ["BTC: $84,000.00"], log=False)
    sent += notify("bias", "based16z", unit, view, prev, [], log=False)
    print(f"{sent} test ping(s) sent")
    return sent


def run_test(text, handle, reply=None, image=None):
    unit = {"main": test_post(text, images=[image] if image else []),
            "reply": test_post(reply, "1") if reply else None, "late": False}
    res = classify(handle, unit, {"posts": [], "replies": [], "biases": {}})
    print(json.dumps(res, indent=2))
    alerts = decide(res, {})
    print("would send: " + (", ".join(a[1] for a in alerts) or "nothing"))


def check_case(c, res):
    """Compare a model result with a test case's expectations. Returns a list of problems."""
    alerts = {a[0]: a[2] for a in decide(res, c.get("biases", {}))}
    problems = []
    for kind in ("trade", "bias"):
        want = c.get(f"expect_{kind}", False)
        if bool(want) != (kind in alerts):
            problems.append(f"{kind}: expected {'an alert' if want else 'no alert'}")
        elif isinstance(want, dict):
            got = alerts[kind]
            for field, value in want.items():
                if value.lower() not in str(got.get(field, "")).lower():
                    problems.append(f"{kind}.{field}: expected {value!r}, got {got.get(field)!r}")
    return problems


def run_cases():
    cases = json.loads(CASES_PATH.read_text())
    misses = 0
    for c in cases:
        unit = {"main": test_post(c["text"], images=c.get("images"), created_at=c.get("created_at", "")),
                "reply": test_post(c["reply"], "1") if c.get("reply") else None, "late": False}
        ctx = {"posts": [dict(h, id=f"h{i}") for i, h in enumerate(c.get("history", []))],
               "replies": [], "biases": c.get("biases", {})}
        res = classify(c.get("handle", "someone"), unit, ctx, c.get("prices"))
        problems = check_case(c, res) if res else ["model declined"]
        misses += bool(problems)
        print(f"{'MISS' if problems else 'PASS'}  {c['name']}")
        for prob in problems:
            print(f"      {prob}")
        for kind in ("trade", "bias"):
            if res and res[kind]["present"]:
                print(f"      read {kind}: conf={res[kind]['confidence']:.2f} {res[kind]['reason']}")
    print(f"\n{len(cases) - misses}/{len(cases)} passed")
    return misses


SECRET_ENV = ("X_BEARER_TOKEN", "ANTHROPIC_API_KEY", "NTFY_TOPIC", "NTFY_VIEWS_TOPIC", "NTFY_TOKEN",
              "DISCORD_WEBHOOK_URL", "DISCORD_BIAS_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def clean_env():
    """Strip stray spaces/newlines that sneak in when pasting secrets into GitHub."""
    for key in SECRET_ENV:
        if key in os.environ:
            os.environ[key] = os.environ[key].strip()


def main():
    clean_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", metavar="TEXT")
    ap.add_argument("--reply", metavar="TEXT", help="with --test: the author's first reply to their own post")
    ap.add_argument("--image", metavar="URL", help="with --test: an image attached to the post")
    ap.add_argument("--handle", default="someone")
    ap.add_argument("--test-cases", action="store_true")
    ap.add_argument("--test-notify", action="store_true", help="send a sample trade and view ping to your notifiers")
    a = ap.parse_args()
    if a.test_notify:
        sys.exit(0 if send_test_pings() else 1)
    elif a.test:
        run_test(a.test, a.handle, a.reply, a.image)
    elif a.test_cases:
        sys.exit(1 if run_cases() else 0)
    else:
        # Any failure marks the run red in GitHub (and emails you), so a bad key or empty credit
        # balance can't go unnoticed while posts quietly pile up for retry.
        sys.exit(1 if poll(dry_run=a.dry_run) else 0)


if __name__ == "__main__":
    main()
