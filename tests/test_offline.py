"""Offline checks of the plumbing: no X, Claude, Discord or price calls leave the machine.

  python -m unittest discover -s tests
"""

import copy
import json
import os
import struct
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot  # noqa: E402

UID = "123"
REAL_TAG_TOPICS = bot.tag_topics  # Base stubs these out so other tests' fake Claude results line up
REAL_LABEL_MARKETS = bot.label_markets


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.ok = status < 400
        self.text = json.dumps(body)
        self.headers = {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            raise bot.requests.HTTPError(str(self.status_code))


def result(trade=None, bias=None, needs_more=False):
    t = {"present": False, "asset": "", "market": "crypto", "chain": "", "contract_address": "", "direction": "long", "structure": "",
         "entry": "", "horizon": "", "source": "inferred", "confidence": 0.0, "reason": ""}
    b = {"present": False, "scope": "", "market": "crypto", "stance": "neutral", "change": "new", "horizon": "", "confidence": 0.0, "reason": ""}
    t.update(trade or {})
    b.update(bias or {})
    return {"trade": t, "bias": b, "needs_more_context": needs_more, "more_context_reason": ""}


SHORT_BTC = result(trade={"present": True, "asset": "BTC", "direction": "short", "confidence": 0.8,
                          "reason": "Says 79k would benefit him with BTC at 84k."})
NOTHING = result()


def fake_claude(*results):
    client = mock.MagicMock()
    client.beta.messages.create.side_effect = [
        SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(r))])
        for r in results
    ]
    return client


def tweet(pid, text, reply_to=None, to_user=None, conv=None, quoted=None, media=None):
    t = {"id": pid, "text": text, "created_at": f"2026-10-0{int(pid) % 9 + 1}T00:00:00Z", "conversation_id": conv or pid}
    refs = []
    if reply_to:
        refs.append({"type": "replied_to", "id": reply_to})
        t["in_reply_to_user_id"] = to_user
    if quoted:
        refs.append({"type": "quoted", "id": quoted})
    if refs:
        t["referenced_tweets"] = refs
    if media:
        t["attachments"] = {"media_keys": [media]}
    return t


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "accounts.txt").write_text("# comment\n@based16z\n")
        self.env = {"X_BEARER_TOKEN": "x", "ANTHROPIC_API_KEY": "k", "DISCORD_WEBHOOK_URL": "https://discord.test/trades",
                    "DISCORD_BIAS_WEBHOOK_URL": "https://discord.test/views"}
        self.patches = [
            mock.patch.object(bot, "STATE_PATH", self.tmp / "state.json"),
            mock.patch.object(bot, "ALERTS_PATH", self.tmp / "alerts.jsonl"),
            mock.patch.object(bot, "ACCOUNTS_PATH", self.tmp / "accounts.txt"),
            mock.patch.object(bot, "NOTES_DIR", self.tmp / "notes"),
            mock.patch.object(bot, "FEEDBACK_PATH", self.tmp / "feedback.jsonl"),
            mock.patch.object(bot, "LESSONS_DIR", self.tmp / "lessons"),
            mock.patch.object(bot, "PUSH_PATH", self.tmp / "push.json"),
            mock.patch.object(bot, "price_context", lambda text: ["BTC: $84,000.00"]),
            mock.patch.object(bot, "coinbase_price", lambda sym: None),
            mock.patch.object(bot, "dexscreener_lookup", lambda q: None),
            mock.patch.object(bot, "tag_topics", lambda handle, acct: None),  # has its own test
            mock.patch.object(bot, "label_markets", lambda state: None),  # has its own test
            mock.patch.dict(os.environ, self.env, clear=False),
        ]
        for p in self.patches:
            p.start()
        bot.PUSH_GONE.clear()
        self.sent = []
        self.post_patch = mock.patch.object(
            bot.requests, "post",
            side_effect=lambda url, json, timeout, headers=None: self.sent.append((url, json)) or FakeResp(204, {}))
        self.post_patch.start()

    def tearDown(self):
        for p in self.patches + [self.post_patch]:
            p.stop()
        bot._client = None

    def set_state(self, posts=(), threads=None, biases=None, last_id="1"):
        acct = bot.new_account("based16z")
        acct.update(user_id=UID, last_id=last_id, posts=list(posts), threads=threads or {}, biases=biases or {},
                    backfilled=True)
        (self.tmp / "state.json").write_text(json.dumps({"accounts": {"based16z": acct}}))

    def state(self):
        return json.loads((self.tmp / "state.json").read_text())["accounts"]["based16z"]

    def run_poll(self, timeline_body, *results, lookup_body=None):
        """timeline_body: dict or callable(params) -> FakeResp."""
        self.calls = []

        def get(url, params=None, timeout=None):
            self.calls.append((url, dict(params or {})))
            if "/users/by/username/" in url:
                return FakeResp(200, {"data": {"id": UID}})
            if url.endswith("/tweets") and "/users/" not in url:
                return FakeResp(200, lookup_body or {"data": []})
            return timeline_body(params) if callable(timeline_body) else FakeResp(200, timeline_body)

        bot._client = fake_claude(*results)
        with mock.patch.object(bot.requests.Session, "get", side_effect=get):
            return bot.poll()

    def prompts(self):
        return [c.kwargs["messages"][0]["content"][-1]["text"] for c in bot._client.beta.messages.create.call_args_list]


class PollTest(Base):
    def test_baseline_then_trade_alert(self):
        old = {"data": [tweet("100", "gm"), tweet("101", "lol same", reply_to="900", to_user="999", conv="900")]}
        self.assertEqual(self.run_poll(old, NOTHING), 0)  # one history read of "gm"
        self.assertEqual(self.sent, [], "first run must not alert on old posts")
        st = self.state()
        self.assertTrue(st["backfilled"])
        self.assertEqual(st["last_id"], "101")
        self.assertEqual([p["id"] for p in st["posts"]], ["100"])
        self.assertEqual([p["id"] for p in st["replies"]], ["101"])

        new = {"data": [tweet("105", "To be honest, 79k btc later this week would benefit me")]}
        self.assertEqual(self.run_poll(new, SHORT_BTC), 0)
        self.assertEqual(self.calls[-1][1]["since_id"], "101")
        self.assertEqual(len(self.sent), 1)
        url, payload = self.sent[0]
        self.assertEqual(url, "https://discord.test/trades")
        embed = payload["embeds"][0]
        self.assertIn("TRADE · @based16z: SHORT BTC", embed["title"])
        self.assertEqual(embed["url"], "https://x.com/based16z/status/105")
        self.assertIn("- [2026-10-02T00:00:00Z] gm", self.prompts()[0])
        self.assertEqual(self.state()["last_id"], "105")
        logged = [json.loads(line) for line in (self.tmp / "alerts.jsonl").read_text().splitlines()]
        self.assertEqual(logged[0]["kind"], "trade")

    def test_main_post_read_with_first_self_reply(self):
        self.set_state()
        body = {"data": [
            tweet("10", "this one is going to be fun"),
            tweet("11", "aped CA123, small size", reply_to="10", to_user=UID, conv="10"),
            tweet("12", "second self reply, ignored", reply_to="10", to_user=UID, conv="10"),
            tweet("13", "congrats ser", reply_to="500", to_user="999", conv="500"),
        ]}
        long_ = result(trade={"present": True, "asset": "POPCAT", "direction": "long", "confidence": 0.9, "reason": "aped"})
        self.run_poll(body, long_)
        self.assertEqual(bot._client.beta.messages.create.call_count, 1, "post + first reply = one read; others skipped")
        prompt = self.prompts()[0]
        self.assertIn("this one is going to be fun", prompt)
        self.assertIn("first reply to this post", prompt)
        self.assertIn("aped CA123", prompt)
        self.assertNotIn("second self reply", prompt)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1]["embeds"][0]["url"], "https://x.com/based16z/status/10")
        st = self.state()
        self.assertEqual([p["id"] for p in st["replies"]], ["13"])
        self.assertEqual(st["threads"]["10"]["self_replies"], 1)
        self.assertEqual(st["last_id"], "13")

    def test_late_self_reply_adds_trade_and_skips_duplicates(self):
        self.set_state(posts=[{"id": "10", "created_at": "", "text": "this one is going to be fun"}],
                       threads={"10": {"self_replies": 0, "alerted": []}}, last_id="10")
        body = {"data": [tweet("20", "aped CA123", reply_to="10", to_user=UID, conv="10")]}
        long_ = result(trade={"present": True, "asset": "POPCAT", "direction": "long", "confidence": 0.9, "reason": "aped"})
        self.run_poll(body, long_)
        prompt = self.prompts()[0]
        self.assertIn("first reply to their own earlier post", prompt)
        self.assertIn("Their earlier post", prompt)
        self.assertIn("this one is going to be fun", prompt)
        self.assertEqual(self.sent[0][1]["embeds"][0]["url"], "https://x.com/based16z/status/20")
        self.assertEqual(self.state()["threads"]["10"], {"self_replies": 1, "alerted": ["trade:POPCAT:long"]})

        # A second self-reply to the same post is not read (SELF_REPLIES_PER_POST=1).
        self.sent.clear()
        self.run_poll({"data": [tweet("21", "more", reply_to="10", to_user=UID, conv="10")]})
        self.assertEqual(bot._client.beta.messages.create.call_count, 0)
        self.assertEqual(self.sent, [])

    def test_late_self_reply_fetches_parent_not_in_history(self):
        self.set_state(last_id="10")
        body = {"data": [tweet("20", "long here", reply_to="7", to_user=UID, conv="7")]}
        self.run_poll(body, NOTHING, lookup_body={"data": [tweet("7", "old thesis post about $ETH")]})
        lookup = [c for c in self.calls if c[0].endswith("/2/tweets")]
        self.assertEqual(lookup[0][1]["ids"], "7")
        self.assertIn("old thesis post about $ETH", self.prompts()[0])

    def test_history_read_fills_board_without_pinging(self):
        # An account added before this feature: baseline saved, history not read yet.
        self.set_state(last_id="20", posts=[{"id": i, "created_at": "", "text": "t"} for i in ("10", "12", "13", "20")])
        st = json.loads((self.tmp / "state.json").read_text())
        del st["accounts"]["based16z"]["backfilled"]
        (self.tmp / "state.json").write_text(json.dumps(st))
        history = {"data": [
            tweet("10", "alts look cooked for weeks"),
            tweet("12", "this one is going to be fun"),
            tweet("13", "aped 7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr", reply_to="12", to_user=UID, conv="12"),
            tweet("15", "lol", reply_to="900", to_user="999", conv="900"),
            tweet("20", "took profit on the rest of my popcat"),
        ]}
        bearish_alts = result(bias=dict(present=True, scope="alts", stance="bearish", change="new", horizon="weeks",
                                        confidence=0.85, reason="says alts are cooked"))
        long_popcat = result(trade=dict(present=True, asset="POPCAT", chain="solana", contract_address="7GC",
                                        direction="long", structure="spot", entry="", horizon="", source="stated",
                                        confidence=0.9, reason="aped the CA in the first reply"))
        timeline = lambda params: FakeResp(200, history if "until_id" in params else {"data": []})
        self.assertEqual(self.run_poll(timeline, bearish_alts, long_popcat, NOTHING), 0)
        self.assertEqual(self.sent, [], "history finds must not ping")
        until = [c[1] for c in self.calls if "until_id" in c[1]]
        self.assertEqual(until[0]["until_id"], "21")
        prompts = self.prompts()
        self.assertEqual(len(prompts), 3, "three main posts read; the self-reply rides with its post")
        self.assertIn("first reply to this post (posted", prompts[1])
        self.assertNotIn("took profit", prompts[0], "no look-ahead into later posts")
        st = self.state()
        self.assertTrue(st["backfilled"])
        self.assertEqual(st["positions"]["POPCAT"]["direction"], "long")
        self.assertEqual(st["biases"]["alts"]["stance"], "bearish")
        self.assertEqual(st["threads"]["12"]["alerted"], ["trade:POPCAT:long"])
        self.assertEqual(st["reads"]["12"], "TRADE LONG POPCAT")
        self.assertEqual(st["reads"]["13"], "Read together with the post it replies to")
        self.assertEqual(st["reads"]["20"], "No call")
        logged = [json.loads(line) for line in (self.tmp / "alerts.jsonl").read_text().splitlines()]
        self.assertEqual([(a["kind"], a["backfill"]) for a in logged], [("bias", True), ("trade", True)])
        # Runs once: the next poll doesn't read history again.
        self.assertEqual(self.run_poll(timeline), 0)
        self.assertFalse([c for c in self.calls if "until_id" in c[1]])

    def test_history_read_failure_saves_nothing_and_retries(self):
        self.set_state(last_id="20")
        st = json.loads((self.tmp / "state.json").read_text())
        del st["accounts"]["based16z"]["backfilled"]
        (self.tmp / "state.json").write_text(json.dumps(st))
        history = {"data": [tweet("10", "short btc here"), tweet("12", "gm")]}
        timeline = lambda params: FakeResp(200, history if "until_id" in params else {"data": []})
        self.assertEqual(self.run_poll(timeline, SHORT_BTC), 1)  # second post: Claude fails
        st = self.state()
        self.assertNotIn("backfilled", st)
        self.assertEqual(st["positions"], {})
        self.assertFalse((self.tmp / "alerts.jsonl").exists())

    def test_correction_becomes_lesson_and_fixes_board(self):
        self.set_state(last_id="50", posts=[{"id": "40", "created_at": "", "text": "loading"}])
        (self.tmp / "feedback.jsonl").write_text(json.dumps({
            "id": "f1", "at": "t", "handle": "", "url": "https://x.com/based16z/status/40?s=20",
            "text": "he was long hype here, 'loading' means buying"}) + "\n")
        lesson = {"lesson": "When @based16z says 'loading', treat it as a long.", "applies_to": "this account"}
        long_hype = result(trade={"present": True, "asset": "HYPE", "direction": "long", "confidence": 0.85, "reason": "loading"})
        lookup = {"data": [dict(tweet("40", "loading"), author_id=UID)]}
        self.assertEqual(self.run_poll({"data": []}, lesson, long_hype, lookup_body=lookup), 0)
        self.assertEqual(self.sent, [], "corrections don't ping")
        self.assertIn("The owner's correction: he was long hype here", self.prompts()[0])
        self.assertIn("What the bot reported for it: nothing", self.prompts()[0])
        self.assertIn("Lessons from the reader's past corrections (follow them):\n- When @based16z says 'loading'",
                      self.prompts()[1], "the re-read already uses the new lesson")
        self.assertEqual((self.tmp / "lessons" / "based16z.md").read_text(),
                         "- When @based16z says 'loading', treat it as a long.\n")
        st = self.state()
        self.assertEqual(st["positions"]["HYPE"]["post_id"], "40")
        done = json.loads((self.tmp / "state.json").read_text())["feedback"]["f1"]
        self.assertEqual(done["file"], "based16z")
        self.assertEqual(done["outcome"], "Re-read the post: LONG HYPE.")
        self.assertEqual(st["reads"]["40"], "Corrected: LONG HYPE")
        logged = [json.loads(line) for line in (self.tmp / "alerts.jsonl").read_text().splitlines()]
        self.assertTrue(logged[0]["correction"])
        # Handled once.
        self.assertEqual(self.run_poll({"data": []}), 0)

    def test_correction_of_a_false_call_removes_it(self):
        self.set_state(last_id="50")
        st = json.loads((self.tmp / "state.json").read_text())
        st["accounts"]["based16z"]["positions"] = {"BTC": {"asset": "BTC", "direction": "short", "post_id": "40", "since": ""}}
        (self.tmp / "state.json").write_text(json.dumps(st))
        (self.tmp / "feedback.jsonl").write_text(json.dumps({
            "id": "f2", "handle": "based16z", "url": "https://x.com/based16z/status/40", "text": "that was a joke"}) + "\n")
        lesson = {"lesson": "Numbers followed by 'lol' are jokes.", "applies_to": "all accounts"}
        lookup = {"data": [tweet("40", "379k btc would benefit me lol")]}
        self.assertEqual(self.run_poll({"data": []}, lesson, NOTHING, lookup_body=lookup), 0)
        self.assertEqual(self.state()["positions"], {})
        self.assertEqual((self.tmp / "lessons" / "_all.md").read_text(), "- Numbers followed by 'lol' are jokes.\n")

    def test_account_notes_are_included(self):
        self.set_state()
        (self.tmp / "notes").mkdir()
        (self.tmp / "notes" / "based16z.md").write_text("tags positions with (Discl ...)\n")
        self.run_poll({"data": [tweet("30", "gm")]}, NOTHING)
        self.assertIn("Notes about @based16z:\ntags positions with (Discl ...)\n", self.prompts()[0])

    def test_quoted_post_text_is_included(self):
        self.set_state()
        body = {"data": [tweet("30", "this", quoted="8")]}
        self.run_poll(body, NOTHING, lookup_body={"data": [tweet("8", "SOL breaking out")]})
        self.assertIn("It quotes this post:\nSOL breaking out", self.prompts()[0])

    def test_unsure_gets_deeper_look(self):
        self.set_state(posts=[{"id": str(i), "created_at": "", "text": f"old post {i}"} for i in range(2, 12)])
        st = json.loads((self.tmp / "state.json").read_text())
        st["accounts"]["based16z"]["replies"] = [{"id": "50", "created_at": "", "text": "yeah im short eth too"}]
        st["accounts"]["based16z"]["last_id"] = "60"
        (self.tmp / "state.json").write_text(json.dumps(st))
        unsure = result(trade={"present": True, "asset": "ETH", "direction": "short", "confidence": 0.55}, needs_more=True)
        sure = result(trade={"present": True, "asset": "ETH", "direction": "short", "confidence": 0.85, "reason": "r"})
        self.run_poll({"data": [tweet("70", "adding here")]}, unsure, sure)
        first, second = self.prompts()
        self.assertNotIn("old post 2\n", first)  # first read shows only the last CONTEXT_POSTS
        self.assertIn("old post 11", first)
        self.assertNotIn("replies to other people", first)
        self.assertIn("old post 2", second)
        self.assertIn("yeah im short eth too", second)
        self.assertIn("second, deeper look", second)
        self.assertEqual(len(self.sent), 1)

    def test_confident_result_skips_deeper_look(self):
        self.set_state()
        self.run_poll({"data": [tweet("70", "79k would benefit me")]}, SHORT_BTC)
        self.assertEqual(bot._client.beta.messages.create.call_count, 1)

    def test_skips_cleanly_until_secrets_are_set(self):
        with mock.patch.dict(os.environ, {"X_BEARER_TOKEN": ""}):
            self.assertEqual(self.run_poll({"data": []}), 0)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.tmp / "state.json").exists())

    def test_save_state_only_bumps_timestamp_on_change_or_hourly(self):
        state = {"accounts": {"a": {"last_id": "1"}}}
        bot.save_state(state)
        first = json.loads((self.tmp / "state.json").read_text())["updated_at"]
        state = bot.load_state()
        with mock.patch.object(bot, "datetime", wraps=datetime) as dt:
            dt.now.return_value = datetime.fromisoformat(first) + timedelta(minutes=5)
            bot.save_state(state)
            self.assertEqual(bot.load_state()["updated_at"], first)  # quiet run: no change, no commit
            state["accounts"]["a"]["last_id"] = "2"
            bot.save_state(state)
            self.assertNotEqual(bot.load_state()["updated_at"], first)  # something changed
            second = bot.load_state()["updated_at"]
            dt.now.return_value = datetime.fromisoformat(second) + timedelta(minutes=61)
            bot.save_state(bot.load_state())
            self.assertNotEqual(bot.load_state()["updated_at"], second)  # hourly heartbeat

    def test_multi_asset_trade_is_one_position_each(self):
        positions = {}
        t = dict(asset="NVDA, SOXL, MU", direction="short", structure="puts", entry="", horizon="", chain="",
                 contract_address="", confidence=0.9, reason="r")
        bot.record_position(positions, t, {"id": "1", "created_at": "c"}, "u")
        self.assertEqual(sorted(positions), ["MU", "NVDA", "SOXL"])
        bot.record_position(positions, {**t, "asset": "SOXL and MU", "direction": "exit"}, {"id": "2"}, "u")
        self.assertEqual(sorted(positions), ["NVDA"])
        bot.record_position(positions, {**t, "asset": "ETH/BTC", "direction": "long"}, {"id": "3"}, "u")
        self.assertIn("ETH/BTC", positions)

    def test_positions_open_flip_and_close(self):
        self.set_state()
        long_ = result(trade={"present": True, "asset": "SOL", "direction": "long", "confidence": 0.9, "entry": "180"})
        self.run_poll({"data": [tweet("2", "long sol 180")]}, long_)
        pos = self.state()["positions"]["SOL"]
        self.assertEqual((pos["direction"], pos["entry"], pos["url"]), ("long", "180", "https://x.com/based16z/status/2"))
        self.assertIn("updated_at", json.loads((self.tmp / "state.json").read_text()))

        short = result(trade={"present": True, "asset": "sol", "direction": "short", "confidence": 0.9})
        self.run_poll({"data": [tweet("3", "flipped short sol")]}, short)
        self.assertEqual(self.state()["positions"]["SOL"]["direction"], "short")

        exit_ = result(trade={"present": True, "asset": "SOL", "direction": "exit", "confidence": 0.9})
        self.run_poll({"data": [tweet("4", "closed my sol")]}, exit_)
        self.assertEqual(self.state()["positions"], {})

    def test_exit_ping_says_when_they_got_in_and_how_it_went(self):
        self.set_state()
        self.run_poll({"data": [tweet("2", "79k btc would benefit me")]}, SHORT_BTC)
        self.assertEqual(self.state()["positions"]["BTC"]["open_price"], 84000.0)
        self.sent.clear()

        exit_ = result(trade={"present": True, "asset": "BTC", "direction": "exit", "confidence": 0.9,
                              "reason": "Says closed."})
        with mock.patch.object(bot, "price_context", lambda text: ["BTC: $80,000.00"]):
            self.run_poll({"data": [tweet("3", "closed it, thanks for the dump")]}, exit_)
        self.assertIn("open trades on record", self.prompts()[0])
        self.assertIn("- BTC: short (since 2026-10-03)", self.prompts()[0])
        embed = self.sent[0][1]["embeds"][0]
        self.assertEqual(embed["title"], "CLOSED · @based16z: BTC short")
        trade = next(f["value"] for f in embed["fields"] if f["name"] == "Trade")
        self.assertIn("opened 2026-10-03 · held 1d", trade)
        self.assertIn("$84,000.00 → $80,000.00 (+4.8% their way)", trade)
        st = self.state()
        self.assertEqual(st["positions"], {})
        self.assertEqual([(c["asset"], c["direction"], c["move"]) for c in st["closed"]], [("BTC", "short", 4.8)])
        logged = json.loads((self.tmp / "alerts.jsonl").read_text().splitlines()[-1])
        self.assertEqual(logged["ended"][0]["close_price"], 80000.0)

    def test_trim_keeps_the_trade_open_and_flip_closes_it(self):
        self.set_state()
        long_ = result(trade={"present": True, "asset": "BTC", "direction": "long", "confidence": 0.9})
        self.run_poll({"data": [tweet("2", "long btc")]}, long_)
        self.sent.clear()
        trim = result(trade={"present": True, "asset": "BTC", "direction": "trim", "confidence": 0.9})
        self.run_poll({"data": [tweet("3", "took some off")]}, trim)
        self.assertEqual(self.sent[0][1]["embeds"][0]["title"], "TRIMMED · @based16z: BTC long")
        self.assertIn("BTC", self.state()["positions"])
        self.assertEqual(self.state()["closed"], [])

        short = result(trade={"present": True, "asset": "BTC", "direction": "short", "confidence": 0.9})
        self.run_poll({"data": [tweet("4", "flipped short")]}, short)
        st = self.state()
        self.assertEqual(st["positions"]["BTC"]["direction"], "short")
        self.assertEqual([(c["direction"], c["flipped"]) for c in st["closed"]], [("long", True)])

    def test_exit_of_a_trade_never_seen_still_pings(self):
        self.set_state()
        exit_ = result(trade={"present": True, "asset": "PEPE", "direction": "exit", "confidence": 0.9})
        self.run_poll({"data": [tweet("2", "out of pepe")]}, exit_)
        self.assertEqual(self.sent[0][1]["embeds"][0]["title"], "CLOSED · @based16z: PEPE")
        trade = next(f["value"] for f in self.sent[0][1]["embeds"][0]["fields"] if f["name"] == "Trade")
        self.assertIn("entry not seen by the bot", trade)

    def test_asset_price_only_trusts_safe_matches(self):
        dex = "WIF (dogwifhat) on solana, price $1.92, mcap $1,900,000,000, CA EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
        self.assertEqual(bot.asset_price("BTC", prices=["BTC: $84,000.00"]), 84000.0)
        self.assertEqual(bot.asset_price("WIF", "solana", prices=[dex]), 1.92)
        self.assertIsNone(bot.asset_price("WIF", "base", prices=[dex]), "wrong chain")
        self.assertIsNone(bot.asset_price("NVDA", prices=[dex]), "stocks aren't priced")

    def test_low_confidence_is_not_sent(self):
        self.set_state()
        weak = result(trade={"present": True, "asset": "BTC", "direction": "short", "confidence": 0.3})
        self.run_poll({"data": [tweet("2", "hmm btc")]}, weak)
        self.assertEqual(self.sent, [])

    def test_views_ping_only_when_new_or_changed(self):
        self.set_state()
        bearish = result(bias={"present": True, "scope": "alts", "stance": "bearish", "confidence": 0.8, "reason": "r"})
        self.run_poll({"data": [tweet("2", "alts are cooked")]}, bearish)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "https://discord.test/views")
        self.assertIn("VIEW · @based16z: BEARISH on alts", self.sent[0][1]["embeds"][0]["title"])
        self.assertEqual(self.state()["biases"]["alts"]["stance"], "bearish")

        self.sent.clear()
        restated = copy.deepcopy(bearish)
        restated["bias"]["change"] = "restated"
        self.run_poll({"data": [tweet("3", "still think alts bleed")]}, restated)
        self.assertEqual(self.sent, [])
        self.assertIn("alts: bearish", self.prompts()[0])

        bullish = result(bias={"present": True, "scope": "Alts", "stance": "bullish", "change": "changed",
                               "confidence": 0.8, "reason": "r"})
        self.run_poll({"data": [tweet("4", "ok alts look good now")]}, bullish)
        self.assertEqual(len(self.sent), 1)
        fields = {f["name"]: f["value"] for f in self.sent[0][1]["embeds"][0]["fields"]}
        self.assertTrue(fields["Change"].startswith("was bearish since"))

    def test_view_on_same_asset_as_trade_is_folded_into_trade(self):
        self.set_state()
        both = result(trade={"present": True, "asset": "BTC", "direction": "short", "confidence": 0.9},
                      bias={"present": True, "scope": "btc", "stance": "bearish", "confidence": 0.9})
        self.run_poll({"data": [tweet("2", "btc weak, bought puts")]}, both)
        self.assertEqual([s[0] for s in self.sent], ["https://discord.test/trades"])

    def test_trade_and_broader_view_send_two_pings(self):
        self.set_state()
        both = result(trade={"present": True, "asset": "BTC", "direction": "short", "confidence": 0.9},
                      bias={"present": True, "scope": "crypto market", "stance": "bearish", "confidence": 0.9})
        self.run_poll({"data": [tweet("2", "whole market rolling over, short btc")]}, both)
        self.assertEqual([s[0] for s in self.sent], ["https://discord.test/trades", "https://discord.test/views"])

    def test_falls_back_to_post_field_names(self):
        self.set_state()

        def timeline(params):
            if "tweet.fields" in params:
                return FakeResp(400, {"errors": [{"message": "bad field"}]})
            return FakeResp(200, {"data": [{"id": "2", "text": "short", "conversation_id": "2",
                                            "note_post": {"text": "long version of the post"}}]})

        self.run_poll(timeline, NOTHING)
        self.assertIn("post.fields", self.calls[-1][1])
        self.assertIn("long version of the post", self.prompts()[0])

    def test_images_sent_and_dropped_if_unreadable(self):
        self.set_state()
        body = {"data": [tweet("2", "👀", media="m1")],
                "includes": {"media": [{"media_key": "m1", "type": "photo", "url": "https://pbs.twimg.com/x.jpg"}]}}
        client = mock.MagicMock()
        ok = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(NOTHING))])
        client.beta.messages.create.side_effect = [
            bot.anthropic.BadRequestError("bad image", response=mock.MagicMock(status_code=400), body=None), ok]
        with mock.patch.object(bot.requests.Session, "get", return_value=FakeResp(200, body)):
            bot._client = client
            self.assertEqual(bot.poll(), 0)
        first, second = [c.kwargs["messages"][0]["content"] for c in client.beta.messages.create.call_args_list]
        self.assertEqual(first[0], {"type": "image", "source": {"type": "url", "url": "https://pbs.twimg.com/x.jpg"}})
        self.assertEqual([b["type"] for b in second], ["text"])

    def test_claude_error_retries_same_post_next_run(self):
        self.set_state()
        client = mock.MagicMock()
        client.beta.messages.create.side_effect = bot.anthropic.APIConnectionError(request=mock.MagicMock())
        with mock.patch.object(bot.requests.Session, "get", return_value=FakeResp(200, {"data": [tweet("2", "x")]})):
            bot._client = client
            errors = bot.poll()
        self.assertEqual(errors, 1)
        self.assertEqual(self.state()["last_id"], "1")

    def test_other_notifiers_and_failures(self):
        self.set_state()
        env = {"NTFY_TOPIC": "my-secret-topic", "TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42",
               "GITHUB_REPOSITORY": "Someone/x-trade-bot"}

        def post(url, json, timeout, headers=None):
            self.sent.append((url, json))
            return FakeResp(500 if "discord" in url else 200, {})

        self.post_patch.stop()
        self.post_patch = mock.patch.object(bot.requests, "post", side_effect=post)
        self.post_patch.start()
        with mock.patch.dict(os.environ, env):
            self.assertEqual(self.run_poll({"data": [tweet("2", "79k")]}, SHORT_BTC), 0)
        urls = [s[0] for s in self.sent]
        self.assertEqual(urls, ["https://ntfy.sh", "https://discord.test/trades", "https://api.telegram.org/bottok/sendMessage"])
        ntfy = self.sent[0][1]
        self.assertEqual(ntfy["topic"], "my-secret-topic")
        # Tapping the ping opens the dashboard on their profile; the post is a button.
        self.assertEqual(ntfy["click"], "https://someone.github.io/x-trade-bot/?post=2#@based16z")
        self.assertEqual(ntfy["actions"][0]["url"], "https://x.com/based16z/status/2")
        self.assertEqual(self.state()["last_id"], "2", "a failed Discord send must not block the run")


class NtfyTest(Base):
    def setUp(self):
        super().setUp()
        self.headers = []
        self.post_patch.stop()
        self.post_patch = mock.patch.object(
            bot.requests, "post",
            side_effect=lambda url, json, timeout, headers=None: (
                self.sent.append((url, json)), self.headers.append(headers)) and FakeResp(200, {}))
        self.post_patch.start()

    def test_trades_and_views_go_to_separate_topics(self):
        env = {"NTFY_TOPIC": "xbot-trades", "NTFY_VIEWS_TOPIC": "xbot-views", "DISCORD_WEBHOOK_URL": "",
               "DISCORD_BIAS_WEBHOOK_URL": ""}
        self.set_state()
        both = result(trade={"present": True, "asset": "POPCAT", "direction": "long", "confidence": 0.9,
                             "chain": "solana", "contract_address": "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
                             "reason": "aped"},
                      bias={"present": True, "scope": "memecoins", "stance": "bullish", "confidence": 0.9, "reason": "r"})
        body = {"data": [tweet("2", "memes are back, aped", media="m1")],
                "includes": {"media": [{"media_key": "m1", "type": "photo", "url": "https://pbs.twimg.com/c.jpg"}]}}
        with mock.patch.dict(os.environ, env):
            self.run_poll(body, both)
        trade, view = self.sent[0][1], self.sent[1][1]
        self.assertEqual((trade["topic"], view["topic"]), ("xbot-trades", "xbot-views"))
        self.assertEqual(trade["priority"], 4)
        self.assertEqual(view["priority"], 3)
        self.assertEqual(trade["tags"], ["chart_with_upwards_trend"])
        self.assertIn("TRADE · @based16z: LONG POPCAT", trade["title"])
        self.assertIn("Confidence 0.90 (inferred)", trade["message"])
        self.assertIn("memes are back, aped", trade["message"])
        self.assertEqual([a["label"] for a in trade["actions"]], ["Open post", "Chart"])
        self.assertEqual(trade["actions"][1]["url"],
                         "https://dexscreener.com/solana/7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr")
        self.assertEqual(trade["attach"], "https://pbs.twimg.com/c.jpg")
        self.assertIn("First recorded view", view["message"])
        self.assertEqual(self.headers[0], {})

    def test_token_and_long_message(self):
        env = {"NTFY_TOPIC": "t", "NTFY_TOKEN": "tk_abc", "DISCORD_WEBHOOK_URL": ""}
        self.set_state()
        with mock.patch.dict(os.environ, env):
            self.run_poll({"data": [tweet("2", "🚀" * 3000)]}, SHORT_BTC)
        self.assertEqual(self.headers[0], {"Authorization": "Bearer tk_abc"})
        self.assertLessEqual(len(self.sent[0][1]["message"].encode()), 3500)

    def test_test_pings(self):
        env = {"NTFY_TOPIC": "xbot-trades", "NTFY_VIEWS_TOPIC": "xbot-views", "DISCORD_WEBHOOK_URL": "",
               "DISCORD_BIAS_WEBHOOK_URL": ""}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(bot.send_test_pings(), 2)
        self.assertEqual([s[1]["topic"] for s in self.sent], ["xbot-trades", "xbot-views"])
        self.assertIn("TEST PING", self.sent[0][1]["message"])
        self.assertFalse((self.tmp / "alerts.jsonl").exists(), "test pings are not logged as alerts")


def ua_decrypt(body, ua_key, auth):
    """What the phone does with a push: undo push_encrypt (RFC 8291) using its own private key."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes, serialization
    salt, n = body[:16], body[20]
    as_pub, ct = body[21:21 + n], body[21 + n:]
    ua_pub = ua_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    hkdf = lambda salt, ikm, info, n: HKDF(algorithm=hashes.SHA256(), length=n, salt=salt, info=info).derive(ikm)
    shared = ua_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    ikm = hkdf(auth, shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    plain = AESGCM(hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)).decrypt(
        hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12), ct, None)
    assert plain.endswith(b"\x02")
    return json.loads(plain[:-1])


class PushTest(Base):
    """Notifications from the dashboard app: phones saved in push.json get encrypted, signed pushes."""

    def setUp(self):
        super().setUp()
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        self.ec, self.ser = ec, serialization
        self.vapid = ec.generate_private_key(ec.SECP256R1())
        self.phones = {}
        devices = {}
        for name, host, views in (("iPhone", "https://web.push.apple.com/QAbc", False),
                                  ("Old phone", "https://fcm.googleapis.com/fcm/send/gone", True)):
            key, auth = ec.generate_private_key(ec.SECP256R1()), os.urandom(16)
            pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
            self.phones[host] = (key, auth)
            devices[name] = {"name": name, "views": views,
                             "sub": {"endpoint": host, "keys": {"p256dh": bot.b64u(pub), "auth": bot.b64u(auth)}}}
        (self.tmp / "push.json").write_text(json.dumps({"ntfy": False, "devices": devices}))
        self.pushes = []

        def post(url, json=None, data=None, headers=None, timeout=None):
            if url in self.phones:
                self.pushes.append((url, data, headers))
                return FakeResp(410 if "gone" in url else 201, {})
            self.sent.append((url, json))
            return FakeResp(204, {})

        self.post_patch.stop()
        self.post_patch = mock.patch.object(bot.requests, "post", side_effect=post)
        self.post_patch.start()
        scalar = self.vapid.private_numbers().private_value.to_bytes(32, "big")
        self.env_patch = mock.patch.dict(os.environ, {"VAPID_PRIVATE_KEY": bot.b64u(scalar), "NTFY_TOPIC": "my-secret-topic",
                                                      "GITHUB_REPOSITORY": "Someone/x-trade-bot"})
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        super().tearDown()

    def test_encryption_matches_the_rfc_example(self):
        as_key = self.ec.derive_private_key(int.from_bytes(bot.unb64u("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"),
                                            self.ec.SECP256R1())
        with mock.patch.object(bot.ec, "generate_private_key", lambda curve: as_key), \
                mock.patch.object(bot.os, "urandom", lambda n: bot.unb64u("DGv6ra1nlYgDCS1FRnbzlw")):
            out = bot.push_encrypt(b"When I grow up, I want to be a watermelon",
                                   "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
                                   "BTBZMqHH6r4Tts7J_aSIgg")
        self.assertEqual(bot.b64u(out), "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLo"
                         "cInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxs"
                         "j_Qulcy4a-fN")

    def test_trade_goes_to_the_app_and_skips_ntfy_when_switched_off(self):
        self.set_state()
        self.assertEqual(self.run_poll({"data": [tweet("2", "79k")]}, SHORT_BTC), 0)
        self.assertFalse([u for u, _ in self.sent if "ntfy" in u], "ntfy is switched off in push.json")
        self.assertEqual([u for u, _, _ in self.pushes], list(self.phones))
        url, body, headers = self.pushes[0]
        msg = ua_decrypt(body, *self.phones[url])
        self.assertTrue(msg["title"].startswith("TRADE · @based16z"))
        self.assertEqual(msg["url"], "https://someone.github.io/x-trade-bot/?post=2#@based16z")
        self.assertEqual(headers["Content-Encoding"], "aes128gcm")
        # Signed with the bot's key for that push service, so the saved addresses can't be used by anyone else.
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        token, key = headers["Authorization"].removeprefix("vapid t=").split(", k=")
        head, claims, sig = token.split(".")
        pub = self.ec.EllipticCurvePublicKey.from_encoded_point(self.ec.SECP256R1(), bot.unb64u(key))
        raw = bot.unb64u(sig)
        pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                   f"{head}.{claims}".encode(), self.ec.ECDSA(hashes.SHA256()))
        self.assertEqual(json.loads(bot.unb64u(claims))["aud"], "https://web.push.apple.com")
        self.assertEqual(bot.unb64u(key), self.vapid.public_key().public_bytes(
            self.ser.Encoding.X962, self.ser.PublicFormat.UncompressedPoint))
        # The phone that's gone is remembered and skipped from now on.
        state = json.loads((self.tmp / "state.json").read_text())
        self.assertEqual(state["push_gone"], ["https://fcm.googleapis.com/fcm/send/gone"])

    def test_views_respect_the_phone_setting(self):
        bot.PUSH_GONE.add("https://fcm.googleapis.com/fcm/send/gone")
        self.assertEqual(bot.push_devices("bias"), [])
        self.assertEqual([d["name"] for d in bot.push_devices("trade")], ["iPhone"])


class UnitTest(unittest.TestCase):
    def test_tradingview_links(self):
        tv = lambda **p: bot.tradingview_url(p).removeprefix("https://www.tradingview.com/chart/?symbol=")
        self.assertEqual(tv(asset="NVDA", market="stocks"), "NVDA")
        self.assertEqual(tv(asset="$hype", market="crypto"), "HYPEUSDT")
        self.assertEqual(tv(asset="FREN PET (FP)", market="crypto"), "FPUSDT")
        self.assertEqual(tv(asset="gold", market="macro"), "TVC:GOLD")
        self.assertEqual(tv(asset="NVDA, SOXL, MU", market="stocks"), "NVDA")
        self.assertEqual(bot.tradingview_url({"asset": "WIF", "contract_address": "abc"}), "", "coins with a CA use DexScreener")

    def test_secrets_are_stripped(self):
        with mock.patch.dict(os.environ, {"X_BEARER_TOKEN": "AAAA%3Dtok\n", "NTFY_TOPIC": " topic \n"}):
            bot.clean_env()
            self.assertEqual(os.environ["X_BEARER_TOKEN"], "AAAA%3Dtok")
            self.assertEqual(os.environ["NTFY_TOPIC"], "topic")

    def test_find_assets(self):
        majors, tags, addrs = bot.find_assets(
            "79k btc, also $ETH and $WIF. CA 7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr and 0x4200000000000000000000000000000000000006"
        )
        self.assertEqual(majors, {"BTC", "ETH"})
        self.assertEqual(tags, {"WIF"})
        self.assertEqual(addrs, {"7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr", "0x4200000000000000000000000000000000000006"})

    def test_fallbacks_only_on_supported_models(self):
        unit = {"main": bot.test_post("x"), "reply": None, "late": False}
        with mock.patch.object(bot, "MODEL", "claude-haiku-5-5"):
            bot._client = fake_claude(NOTHING)
            bot.classify("a", unit, {}, prices=[])
            self.assertNotIn("fallbacks", bot._client.beta.messages.create.call_args.kwargs)
        with mock.patch.object(bot, "MODEL", "claude-opus-5-5"):
            bot._client = fake_claude(NOTHING)
            bot.classify("a", unit, {}, prices=[])
            kw = bot._client.beta.messages.create.call_args.kwargs
            self.assertEqual(kw["fallbacks"], "default")
            self.assertEqual(kw["output_config"]["format"]["type"], "json_schema")
        bot._client = None

    def test_check_case(self):
        case = {"expect_trade": {"direction": "short", "asset": "BTC"}}
        self.assertEqual(bot.check_case(case, SHORT_BTC), [])
        self.assertEqual(bot.check_case({}, SHORT_BTC), ["trade: expected no alert"])


if __name__ == "__main__":
    unittest.main()


class MorningSummaryTest(Base):
    def alert(self, at, **kw):
        a = {"sent_at": at, "kind": "trade", "handle": "based16z", "direction": "long", "asset": "SOL", **kw}
        with (self.tmp / "alerts.jsonl").open("a") as f:
            f.write(json.dumps(a) + "\n")

    def test_sends_overnight_calls_once(self):
        env = {"NTFY_TOPIC": "t", "DISCORD_WEBHOOK_URL": "", "GITHUB_REPOSITORY": "Someone/x-trade-bot"}
        self.alert("2026-10-08T22:00:00+00:00")  # 6pm New York: before the night, already pinged live
        self.alert("2026-10-09T03:00:00+00:00", asset="PEPE")
        self.alert("2026-10-09T05:00:00+00:00", kind="bias", stance="bearish", scope="alts")
        self.alert("2026-10-09T06:00:00+00:00", direction="exit", asset="BTC",
                   ended=[{"asset": "BTC", "direction": "short", "move": 4.8}])
        self.alert("2026-10-09T07:00:00+00:00", backfill=True)
        state = {}
        with mock.patch.dict(os.environ, env):
            bot.morning_summary(state, now=datetime.fromisoformat("2026-10-09T11:30:00+00:00"))  # 7:30am NY
            self.assertEqual(self.sent, [])
            bot.morning_summary(state, now=datetime.fromisoformat("2026-10-09T12:05:00+00:00"))  # 8:05am NY
            bot.morning_summary(state, now=datetime.fromisoformat("2026-10-09T12:10:00+00:00"))
        self.assertEqual(len(self.sent), 1)
        msg = self.sent[0][1]
        self.assertEqual(msg["title"], "Overnight: 2 trade calls and 1 view change")
        self.assertEqual(msg["message"], "• @based16z LONG PEPE\n• @based16z now bearish on alts\n"
                                         "• @based16z closed BTC short (BTC +4.8% their way)")
        self.assertEqual(msg["click"], "https://someone.github.io/x-trade-bot/")
        self.assertEqual(state["summary_date"], "2026-10-09")

    def test_quiet_night_or_late_start_sends_nothing(self):
        self.alert("2026-10-09T03:00:00+00:00")
        state = {"summary_date": "2026-10-08"}
        with mock.patch.dict(os.environ, {"NTFY_TOPIC": "t"}):
            bot.morning_summary(state, now=datetime.fromisoformat("2026-10-09T18:00:00+00:00"))  # 2pm NY
            self.assertEqual(self.sent, [])
            self.assertEqual(state["summary_date"], "2026-10-09")
            bot.morning_summary(state, now=datetime.fromisoformat("2026-10-10T12:05:00+00:00"))
        self.assertEqual(self.sent, [])


class TagTopicsTest(Base):
    def test_marks_trading_posts_once(self):
        acct = bot.new_account("based16z")
        acct["posts"] = [{"id": "1", "text": "btc to 100k"}, {"id": "2", "text": "happy birthday mom"}]
        acct["replies"] = [{"id": "3", "text": "loaded more", "images": ["https://img/1.png"]}]
        bot._client = fake_claude({"trading_ids": ["1", "3"]})
        REAL_TAG_TOPICS("based16z", acct)
        self.assertEqual(acct["trading"], {"1": True, "2": False, "3": True})
        prompt = self.prompts()[0]
        self.assertIn("[2] happy birthday mom", prompt)
        self.assertIn("[3] loaded more [image]", prompt)
        REAL_TAG_TOPICS("based16z", acct)  # nothing new: no second call
        self.assertEqual(bot._client.beta.messages.create.call_count, 1)

    def test_failure_leaves_posts_for_next_run(self):
        acct = bot.new_account("based16z")
        acct["posts"] = [{"id": "1", "text": "btc"}]
        bot._client = fake_claude()  # no result: raises StopIteration
        REAL_TAG_TOPICS("based16z", acct)
        self.assertEqual(acct["trading"], {})


class PositionTest(unittest.TestCase):
    def test_adding_keeps_the_first_entry(self):
        positions = {}
        t = dict(asset="BTC", direction="long", structure="", entry="", horizon="", chain="", contract_address="",
                 confidence=0.9, reason="r")
        bot.record_position(positions, t, {"id": "1", "created_at": "2026-10-01T00:00:00Z"}, "u1", ["BTC: $80,000.00"])
        bot.record_position(positions, t, {"id": "2", "created_at": "2026-10-03T00:00:00Z"}, "u2", ["BTC: $90,000.00"])
        self.assertEqual((positions["BTC"]["since"], positions["BTC"]["open_price"], positions["BTC"]["url"]),
                         ("2026-10-01T00:00:00Z", 80000.0, "u2"))
        ended = bot.record_position(positions, {**t, "direction": "exit"}, {"id": "3", "created_at": "2026-10-04T00:00:00Z"},
                                    "u3", ["BTC: $88,000.00"])
        self.assertEqual(ended[0]["move"], 10.0)
        self.assertEqual(bot.ended_lines(ended), ["BTC long · opened 2026-10-01 · held 3d · $80,000.00 → $88,000.00 (+10.0% their way)"])


class LabelMarketsTest(Base):
    def test_labels_earlier_calls_once(self):
        state = {"accounts": {"m": {
            "positions": {"NVDA": {"asset": "NVDA", "direction": "short", "reason": "long puts"},
                          "STRK": {"asset": "STRK", "chain": "bnb", "market": "crypto"}},
            "biases": {"stocks/macro": {"scope": "stocks/macro", "stance": "bearish"}},
            "closed": [{"asset": "NVDA", "direction": "short"}]}}}
        (self.tmp / "alerts.jsonl").write_text(json.dumps({"kind": "trade", "asset": "NVDA"}) + "\n"
                                               + json.dumps({"kind": "bias", "scope": "alts"}) + "\n")
        bot._client = fake_claude({"labels": [{"id": "trade:nvda", "market": "stocks"},
                                              {"id": "bias:stocks/macro", "market": "stocks"},
                                              {"id": "bias:alts", "market": "crypto"}]})
        REAL_LABEL_MARKETS(state)
        acct = state["accounts"]["m"]
        self.assertEqual(acct["positions"]["NVDA"]["market"], "stocks")
        self.assertEqual(acct["closed"][0]["market"], "stocks")
        self.assertEqual(acct["biases"]["stocks/macro"]["market"], "stocks")
        logged = [json.loads(x) for x in (self.tmp / "alerts.jsonl").read_text().splitlines()]
        self.assertEqual([a["market"] for a in logged], ["stocks", "crypto"])
        prompt = self.prompts()[0]
        self.assertIn("[trade:nvda] a trade in NVDA (long puts)", prompt)
        self.assertNotIn("strk", prompt)
        self.assertTrue(state["markets_labeled"])
        REAL_LABEL_MARKETS(state)  # done: no second call
        self.assertEqual(bot._client.beta.messages.create.call_count, 1)

    def test_stock_calls_are_labelled_in_pings(self):
        self.set_state()
        nvda = result(trade={"present": True, "asset": "NVDA", "market": "stocks", "direction": "short",
                             "structure": "long puts", "confidence": 0.9})
        self.run_poll({"data": [tweet("2", "(Discl long puts) NVDA 2w")]}, nvda)
        self.assertEqual(self.sent[0][1]["embeds"][0]["title"], "TRADE · @based16z: SHORT NVDA (long puts) · stocks")
        self.assertEqual(self.state()["positions"]["NVDA"]["market"], "stocks")
