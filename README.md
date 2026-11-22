# James — Telegram cybersecurity briefings

James watches a small set of public cybersecurity news feeds, scores stories against each subscriber's chosen topics, and sends a short Portuguese or English briefing. Each briefing covers **what, when, who, how, and impact**, with a link to the original article. It sends at most four matching stories per day and ignores stories older than 72 hours when a feed supplies a usable publication date.

## Set up

1. Create a Telegram bot by messaging [@BotFather](https://t.me/BotFather) and copy its token.
2. Create an OpenAI API key for the article briefings.
3. Install Python 3.10 or newer, then run:

   ```sh
   python -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   ```

4. Put your Telegram token and OpenAI key in `.env`. Set `TIMEZONE` and `DAILY_BRIEFING_HOUR` if you want a different delivery time.
5. Start James with `python bot.py`, then open your bot in Telegram and send `/start`.

Keep `.env` private. `james_state.json` stores chat IDs, preferences, and deduplication data on the machine running the bot. Back it up if needed; do not publish it.

## Telegram commands

- `/start` — register the chat and show the defaults.
- `/topics threats,breaches,ransomware,policy,research` — choose topics. Available topics are `threats`, `breaches`, `ransomware`, `policy`, and `research`.
- `/language Português` or `/language English` — set briefing language.
- `/settings` — show your current preferences.
- `/now` — request a briefing immediately.

James starts with **threats and breaches**, sends one daily digest (up to four stories), and writes in Portuguese. The source list and scoring rules are at the top of `bot.py` and can be adjusted as you learn what you want to follow.

## Notes

The bot uses public RSS feeds; publishers may change or remove feed URLs. Story selection uses transparent keyword matching, so it may miss stories that use unexpected wording. The language model writes the brief from the headline, date, publisher, and feed excerpt; it does not fetch or independently verify the full article. James explicitly marks facts the source excerpt does not specify. The article link is included so you can read the original reporting.
