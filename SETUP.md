# X Trade Bot: setup

Every few minutes this bot checks a short list of X accounts for new posts and sends you two kinds of pings:

- **TRADE**: they took, added to, or exited a position, or called a specific setup ("short BTC (long puts)", "long POPCAT", "exit WIF").
- **VIEW**: they stated a new or changed opinion on the market or a sector ("bearish on alts", "bullish on crypto market"), with no position attached. A view they've already stated doesn't ping again; only a new one or a flip does.

Claude reads each post the way a long-time follower would. It handles sarcasm, posts with no ticker, chart images, quoted posts, and their first reply to their own post. When it's unsure, it takes a second look with more of their recent posts and replies before deciding. It runs on GitHub Actions, so nothing runs on your own computer.

Pings arrive on your phone through **ntfy**, a free notification app. Discord and Telegram also work if you ever want them.

**In short:** you install ntfy, make two keys (X developer and Anthropic), put them in a GitHub repo as secrets, and GitHub runs the bot on a timer.

## What's in the folder

| File | What it does |
|---|---|
| `bot.py` | The whole bot: reads X, looks up live prices, asks Claude, sends pings |
| `prompt.md` | The instructions Claude follows to read a post, plus per-account notes. This is the file you tune |
| `accounts.txt` | The handles to watch, one per line (based16z and lbattlerhino) |
| `tests/cases.json` | Example posts with the answer you expect, for tuning `prompt.md` |
| `tests/test_offline.py` | Checks the plumbing without calling any paid API |
| `.github/workflows/poll.yml` | The GitHub timer that runs the bot every 5 minutes |
| `state.json` | Created by the bot: last post seen, recent posts for context, each account's recorded views |
| `alerts.jsonl` | Created by the bot: every alert it has sent, one per line (useful later for a dashboard) |

## How a post gets read

1. Each run pulls everything new from each account: posts and replies, but not reposts.
2. **Main posts** are read by Claude. If their **first reply to their own post** is already there, both are read together as one, so a post like "this one's going to be fun" with "aped [CA]" in the reply becomes one TRADE ping.
3. If that self-reply shows up on a later run, it's read on its own with the original post as context. It only pings if it adds something the first alert didn't already say.
4. **Replies to other people** are not read for alerts, but they're kept as context for step 6.
5. Claude sees the post, any images (charts), any quoted post, live prices for any coin or contract address mentioned, the account's last 5 posts, and the views it has recorded for them.
6. **If it's unsure** (confidence a bit under the cutoff, or it says it needs more context, as with "adding here"), it takes a second look with their last 15 posts plus their last 10 replies to other people, then decides.

## Step 1: Set up ntfy on your phone (5 minutes)

ntfy is a free app that shows a notification whenever something sends a message to a "topic" you subscribe to. There's no account and no sign-up. The topic name works like a password: anyone who knows it can read your pings, so make it long and random.

1. Install **ntfy** from the App Store (iPhone) or Google Play (Android). Allow notifications when it asks.
2. Tap **+** to subscribe to a topic. Leave the server as `ntfy.sh` and type your **trades** topic name, for example `xbot-trades-` followed by 12 random letters and numbers.
3. Tap **+** again and subscribe to your **views** topic, for example `xbot-views-` followed by 12 different random characters.
4. To check it works right now: on a computer, open `https://ntfy.sh/<your trades topic>` in a browser, type anything in the message box at the bottom, and send it. It should pop up on your phone within a second or two.

Each topic shows up as its own feed in the app, so trades and views stay separate. You can set them up differently in the app (for example, a loud sound for trades and silent for views) under each topic's notification settings.

What a ping looks like:
- **Title:** `TRADE · @based16z: SHORT BTC (long puts)` or `VIEW · @lbattlerhino: BEARISH on alts`
- **Body:** Claude's one-line reason, then confidence, entry, horizon and contract address if there is one (or what their view was before, for views), then the post itself.
- **Buttons:** tap the ping or **Open post** to open it on X. Trade pings with a contract address also get a **Chart** button that opens DexScreener.
- Chart images in the post are attached, so you can see them in the notification.

**Other options (optional, can run alongside ntfy):**
- **Discord:** in a channel go to **Edit Channel** → **Integrations** → **Webhooks** → **New Webhook** → **Copy Webhook URL**. Make one for trades and one for views if you want them separate.
- **Telegram:** message **@BotFather**, send `/newbot`, and copy the token. Send your new bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the number after `"chat":{"id":`.

## Step 2: X developer account and bearer token

1. Go to [console.x.com](https://console.x.com) and sign in with any X account.
2. Accept the developer agreement. For the use case, say something like: "Personal tool that reads public posts from a few accounts and sends me a private notification. No posting, no redistribution."
3. Create an app. Copy the **Bearer Token** when it's shown, because it's only displayed once.
4. Under billing, add a card and buy a small credit balance ($5 to $10 is plenty to start). X currently gives new payers $20 in credits after you save your first card, plus a match of your first auto-recharge up to $50.

What it costs (X's [published rates](https://docs.x.com/x-api/getting-started/pricing)): **$0.005 per post or reply read** and **$0.01 per account lookup** (done once per account). The bot only reads things newer than the last one it saw, and a check that finds nothing new costs nothing. Replies count too, because the bot has to see their replies to find their self-replies. If each account makes about 20 posts and 30 replies a day, two accounts come to about 100 reads a day: **about $0.50/day, or $15/month**. X doesn't charge twice for the same post in one UTC day.

## Step 3: Anthropic API key

Go to [platform.claude.com](https://platform.claude.com), create an account, add billing, and create an API key.

Claude reads only main posts and first self-replies, not replies to other people. Per read:
- **Claude Opus 5.5** (default, best at sarcasm and implied trades): about $0.01 to $0.02, plus about half a cent per chart image. The second "unsure" look doubles that for the few posts that need it. 40 reads a day comes to roughly **$0.50 to $1/day**.
- **Claude Haiku 5.5**: about 10x cheaper, but noticeably weaker on the indirect posts. Switch with the `MODEL` variable once your test cases pass on it.

## Step 4: Make the GitHub repo

1. Create a new repo on GitHub and upload everything in this folder. Keep the `.github/workflows/poll.yml` path exactly as it is.
2. `accounts.txt` already lists based16z and lbattlerhino. Edit it anytime.

**Public or private?** This decides how often it can run for free:
- **Public repo:** GitHub Actions has no minute limit, so every 5 minutes is free. Anyone could see your code, your handle list, `state.json` and `alerts.jsonl`. Your keys stay hidden in secrets either way.
- **Private repo:** GitHub Free includes 2,000 Actions minutes a month, and each run counts as at least a minute. Every 5 minutes is about 8,600 runs, so change the cron line in `poll.yml` to `"7,37 * * * *"` (every 30 minutes, about 1,440 runs). Past the free minutes, Linux runners cost $0.006/minute, so every 5 minutes would be roughly $40/month.

## Step 5: Add the secrets

In the repo, go to **Settings → Secrets and variables → Actions**.

Under **Secrets**, add:
- `X_BEARER_TOKEN`: from step 2
- `ANTHROPIC_API_KEY`: from step 3
- `NTFY_TOPIC`: your trades topic name from step 1
- `NTFY_VIEWS_TOPIC`: your views topic name from step 1 (if you skip this, views also go to the trades topic)

Only if you use them:
- `DISCORD_WEBHOOK_URL` and `DISCORD_BIAS_WEBHOOK_URL`: trades and views webhooks
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
- `NTFY_TOKEN`: only if you later make an ntfy account (see Things to know)

Under **Variables** (all optional):
- `MODEL`: `claude-opus-5-5` (the default) or `claude-haiku-5-5`
- `MIN_CONFIDENCE`: `0.7` by default. Raise it to `0.8` if you get too many pings, or lower it to `0.6` if it misses things
- `SELF_REPLIES_PER_POST`: `1` by default (only their first reply to their own post). Set it to `3` if they tend to put the trade further down a thread

## Step 6: Test ping, then first run

1. Go to the **Actions** tab, enable workflows if asked, and open **poll**.
2. Click **Run workflow**, tick **"Only send a test TRADE and VIEW ping to your phone"**, and run it. You should get one sample ping in each ntfy topic, marked TEST PING. This checks your secrets and topics without touching X or Claude.
3. Click **Run workflow** again with the box unticked. That's the real first run.
4. The first run only takes a baseline. It saves each account's last 20 posts and replies as context and sends **no pings**, so old posts won't spam you.
5. From then on it runs on the timer. Open any run to see one line per post: which alerts it sent (or `no alert`), plus Claude's full reading, so you can see why.

If a handle is misspelled, the run log shows `@handle: fetch failed` for that account and keeps going with the others.

## Step 7: Tune it (this is where the quality comes from)

**Without a computer:** in **Actions → poll → Run workflow**, tick **"Only run tests/cases.json through Claude"**. When it finishes, open the run and the **Poll accounts and send alerts** step to see PASS or MISS for each case with Claude's reasoning. It costs about $0.30 a run and sends no pings. You can edit `tests/cases.json` and `prompt.md` right on github.com (pencil icon) and run it again.

**On a computer:** you can also run the classifier locally to test the prompt. You need Python 3.10+ and only the Anthropic key:

```
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...        # Windows PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-..."

python bot.py --test "this one is going to be fun" --reply "aped <CA>, small size" --handle lbattlerhino
python bot.py --test "👀" --image "https://pbs.twimg.com/media/....jpg"
python bot.py --test-cases
```

`--test-cases` runs every post in `tests/cases.json` and prints PASS or MISS with Claude's reasoning. Each case has a `text`, an optional `reply` (their first self-reply), and optional `history` (earlier posts) or `biases` (views already recorded). It also says what should ping:
- `"expect_trade": {"direction": "short", "asset": "BTC"}` means a TRADE ping should go out, matching those fields
- `"expect_bias": {"stance": "bearish"}` means a VIEW ping should go out
- Leave both out when it should send nothing (jokes, unrelated posts)

The loop:
1. Paste 20 or so real posts from both accounts into `cases.json`, including jokes, shitposts and sarcasm.
2. Run `--test-cases`. For each MISS, read the reason and add a line to `prompt.md`. The **Account notes** section at the bottom is the place for how each account talks: slang, how they tag positions, which jokes they repeat.
3. Repeat until it passes, then upload `prompt.md` to the repo.

The long-puts case is paraphrased from Grok's summary, so replace it with the real text. Cases can pin prices with a `prices` list so results don't drift as the market moves.

To check the plumbing without spending anything, run `python -m unittest discover -s tests`.

## Things to know

- **If something breaks:** a run that hits any error (a bad key, an empty credit balance, X being down) shows a red X in the Actions tab and GitHub emails you. Posts it couldn't read are retried on the next run, so nothing is skipped.
- **Delay:** GitHub's timer can run a few minutes late when it's busy, so pings arrive roughly 5 to 10 minutes after the post. For near-instant pings, the same `bot.py` can run in a loop on a $5/month always-on host (Railway, Fly.io) instead.
- **GitHub pauses timers on repos with no activity for 60 days.** The bot commits `state.json` whenever there's a new post, which counts as activity, so this only matters if both accounts go silent for two months.
- **Views are remembered per topic** (crypto market, btc, alts, memecoins, a ticker...) in `state.json`. To make it ping on a view again, delete that entry under `biases`.
- **Prices:** BTC, ETH, SOL, BNB, XRP, DOGE and HYPE come from Coinbase. Any `$CASHTAG` or contract address in a post is looked up on DexScreener. That's how "79k" gets compared to the live price.
- **ntfy limits:** the free ntfy.sh server limits how many messages one internet address can send. GitHub's machines share addresses with other users, so if pings ever stop arriving while the run log says `sent to ntfy`, or it shows `ntfy send failed: 429`, make a free account at [ntfy.sh](https://ntfy.sh), create an access token under **Account**, and add it as the `NTFY_TOKEN` secret. Messages then count against your account instead of the shared address. Paid plans start at $5/month if you ever need more.
- **Field names:** X's docs currently describe new `post.fields` names, while the API has long used `tweet.fields`. The bot tries the old names first and switches automatically if X rejects them. You'll see a line in the run log if that happens.
