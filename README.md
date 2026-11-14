# Halal stock trading bot

Opens and closes day trades on halal stocks through Alpaca. Paper trading (fake money) by default.

## Connect it (once)

1. Create a free account at https://alpaca.markets and open the **Paper Trading** dashboard.
2. Click **Generate New Keys** (API Keys box). Copy the key and the secret.
3. In this folder, copy `.env.example` to `.env` and paste the two values in. Keep `ALPACA_PAPER=true`.
4. Test it:

```bash
python3 bot.py check
```

It should print `Connected to Alpaca - PAPER account ...`.

## Use it

```bash
python3 bot.py screen          # halal verdict for every watchlist stock
python3 bot.py run --dry-run   # watch what it would do, no orders
python3 bot.py run             # trade the paper account (leave it running 9:30-16:00 New York time)
python3 bot.py status          # account, positions, today's trades
python3 bot.py close AAPL      # close one position now
python3 bot.py close-all       # close everything the bot opened
```

The bot must keep running until 15:55 New York time to close open trades. If you stop it early, run `close-all`.

## What it trades

- Only stocks on `watchlist` in `config.json` that pass the halal screen (`halal.py`):
  business activity (no banks, insurance, alcohol, tobacco, gambling, pork, entertainment media, weapons)
  and AAOIFI-style ratios (debt < 30 %, cash + interest-bearing holdings < 30 % of market cap, interest income < 5 % of revenue).
- Long only, cash only (no margin), shares only, flat by the close.
- A stock that cannot be verified is never traded. Use `always_block` / `always_allow` in `config.json` to overrule the screen.

The screen is an automated approximation from SEC filings, not a fatwa. Check it against a certified screener (Zoya, Musaffa) or your scholar.

## Settings (`config.json`)

- `risk`: budget, max loss per trade ($50), max daily loss ($150), max trades a day (3), entry window, flat-by time.
- `strategy`: stop and target rule for the opening-range breakout.
- `safety`: stop trading after 2 losses in a row, after a weekly loss limit, or when market breadth is weak.

## Real money

Only after the paper account has run well for weeks: put live keys in `.env`, set `ALPACA_PAPER=false`, and start with `python3 bot.py run --live`. Both are required.

## Files

- `bot.py` the bot, `halal.py` the screen, `broker.py` the Alpaca connection.
- `.claude/skills/` the skills it uses (MIT licensed): day-trader-skill (scanner, exit, budget, risk) and claude-trading-skills (market-breadth-analyzer).
- `data/` log (`bot.log`), trade journal (`trades.csv`), halal cache.

Not financial advice. Most day traders lose money.
