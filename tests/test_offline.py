"""Offline checks of the plumbing: no X, Claude, Discord or price calls leave the machine.

  python -m unittest discover -s tests
"""

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot  # noqa: E402

UID = "123"


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
    t = {"present": False, "asset": "", "chain": "", "contract_address": "", "direction": "long", "structure": "",
         "entry": "", "horizon": "", "source": "inferred", "confidence": 0.0, "reason": ""}
    b = {"present": False, "scope": "", "stance": "neutral", "change": "new", "horizon": "", "confidence": 0.0, "reason": ""}
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
        self.env = {"X_BEARER_TOKEN": "x", "DISCORD_WEBHOOK_URL": "https://discord.test/trades",
                    "DISCORD_BIAS_WEBHOOK_URL": "https://discord.test/views"}
        self.patches = [
            mock.patch.object(bot, "STATE_PATH", self.tmp / "state.json"),
            mock.patch.object(bot, "ALERTS_PATH", self.tmp / "alerts.jsonl"),
            mock.patch.object(bot, "ACCOUNTS_PATH", self.tmp / "accounts.txt"),
            mock.patch.object(bot, "price_context", lambda text: ["BTC: $84,000.00"]),
            mock.patch.dict(os.environ, self.env, clear=False),
        ]
        for p in self.patches:
            p.start()
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
        acct.update(user_id=UID, last_id=last_id, posts=list(posts), threads=threads or {}, biases=biases or {})
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
        self.assertEqual(self.run_poll(old), 0)
        self.assertEqual(self.sent, [], "first run must not alert on old posts")
        st = self.state()
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
        env = {"NTFY_TOPIC": "my-secret-topic", "TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42"}

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
        self.assertEqual(ntfy["click"], "https://x.com/based16z/status/2")
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


class UnitTest(unittest.TestCase):
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
