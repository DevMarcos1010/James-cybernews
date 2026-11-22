"""James: a low-noise cybersecurity news briefing bot for Telegram."""

from __future__ import annotations

import asyncio
from email.utils import parsedate_to_datetime
import html
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("james")
load_dotenv()

FEEDS = [
    ("CISA", "https://www.cisa.gov/news.xml"),
    ("BleepingComputer", "https://www.bleepingcomputer.com/feed/"),
    ("Krebs on Security", "https://krebsonsecurity.com/feed/"),
    ("The Hacker News", "https://feeds.feedburner.com/TheHackersNews"),
    ("SecurityWeek", "https://www.securityweek.com/feed/"),
]
TOPICS = {
    "breaches": ["breach", "data leak", "leaked", "exposed", "compromis", "stolen data", "vazamento", "violação"],
    "ransomware": ["ransomware", "extortion", "ransom", "seized", "cryptolocker"],
    "threats": ["zero-day", "zero day", "actively exploited", "apt", "cyberattack", "campaign", "vulnerability", "exploit", "cve-"],
    "policy": ["cisa", "government", "sanction", "law", "regulation", "fine", "legislation"],
    "research": ["research", "discovered", "proof-of-concept", "security flaw", "researchers"],
}
DEFAULT_PREFERENCES = {"topics": ["threats", "breaches"], "language": "Português", "frequency": "daily"}


@dataclass
class Story:
    title: str
    link: str
    source: str
    description: str
    published: str
    uid: str
    score: int = 0


STATE_PATH = Path(os.getenv("STATE_FILE", "james_state.json"))
state: dict = {"chats": {}, "sent": []}


def load_state() -> None:
    global state
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.exception("Could not read state file; starting with empty state")


def save_state() -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def get_chat(chat_id: int) -> dict:
    key = str(chat_id)
    if key not in state["chats"]:
        state["chats"][key] = dict(DEFAULT_PREFERENCES)
        save_state()
    return state["chats"][key]


def clean_text(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", html.unescape(value or "")).strip()


def parse_feed(source: str, url: str) -> list[Story]:
    request = urllib.request.Request(url, headers={"User-Agent": "JamesCyberBriefing/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            root = ET.fromstring(response.read())
    except (OSError, ET.ParseError, urllib.error.URLError) as exc:
        log.warning("Feed %s unavailable: %s", source, exc)
        return []
    stories: list[Story] = []
    for item in root.findall(".//item"):
        title = clean_text(item.findtext("title", ""))
        link = item.findtext("link", "").strip()
        desc = clean_text(item.findtext("description", ""))
        published = item.findtext("pubDate", "")
        uid = item.findtext("guid", "") or link or title
        if title and link:
            stories.append(Story(title, link, source, desc[:1800], published, uid))
    # Atom feeds use a namespace; support the common representation too.
    if not stories:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for item in root.findall("a:entry", ns):
            title = clean_text(item.findtext("a:title", "", ns))
            link_node = item.find("a:link", ns)
            link = (link_node.get("href", "") if link_node is not None else "")
            desc = clean_text(item.findtext("a:summary", "", ns) or item.findtext("a:content", "", ns))
            published = item.findtext("a:published", "", ns) or item.findtext("a:updated", "", ns)
            if title and link:
                stories.append(Story(title, link, source, desc[:1800], published, link))
    return stories


def relevance(story: Story, preferred: list[str]) -> int:
    text = (story.title + " " + story.description).lower()
    score = 0
    for topic in preferred:
        score += sum(2 if word in story.title.lower() else 1 for word in TOPICS.get(topic, []) if word in text)
    # Give weight to unusually consequential incident language.
    score += 2 * sum(term in text for term in ("critical", "nationwide", "millions", "government", "zero-day", "zero day"))
    return score


async def fetch_stories() -> list[Story]:
    # Feed requests are blocking I/O, so keep them off Telegram's event loop.
    results = await asyncio.gather(*(asyncio.to_thread(parse_feed, name, url) for name, url in FEEDS))
    return [story for group in results for story in group]


async def write_briefing(client: AsyncOpenAI, story: Story, language: str) -> str:
    response = await client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        temperature=0.2,
        messages=[
            {"role": "system", "content": "You are James, a careful cybersecurity news journalist writing for a beginner. Use only the supplied article metadata; never invent details. If a fact is unavailable, say it is not specified in the source. Write in the requested language. Return concise HTML with exactly five labeled lines: What, When, Who, How, Impact. Explain technical terms briefly. No sensationalism."},
            {"role": "user", "content": f"Language: {language}\nPublisher: {story.source}\nHeadline: {story.title}\nPublished: {story.published}\nArticle snippet: {story.description}"},
        ],
    )
    return response.choices[0].message.content or ""


def escaped(value: str) -> str:
    return html.escape(value, quote=False)


async def deliver(chat_id: int, context: ContextTypes.DEFAULT_TYPE, manual: bool = False) -> int:
    client: AsyncOpenAI = context.application.bot_data["openai"]
    prefs = get_chat(chat_id)
    all_stories = await fetch_stories()
    sent_by_chat = state.setdefault("sent", {})
    seen = set(sent_by_chat.get(str(chat_id), []))
    minimum = int(os.getenv("MIN_RELEVANCE_SCORE", "3"))
    limit = int(os.getenv("MAX_STORIES_PER_BRIEFING", "4"))
    candidates = []
    for story in all_stories:
        story.score = relevance(story, prefs.get("topics", []))
        try:
            published = parsedate_to_datetime(story.published)
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - published.astimezone(timezone.utc)).total_seconds() > 72 * 3600:
                continue
        except (TypeError, ValueError, OverflowError):
            # If a publisher omits a parseable date, relevance and deduplication still apply.
            pass
        if story.uid not in seen and story.score >= minimum:
            candidates.append(story)
    candidates.sort(key=lambda s: (s.score, s.published), reverse=True)
    candidates = candidates[:limit]
    if not candidates:
        if manual:
            await context.bot.send_message(chat_id, "Não encontrei notícias recentes com pontuação alta para os seus temas. Vou continuar filtrando para evitar excesso de mensagens.")
        return 0
    for story in candidates:
        try:
            body = await write_briefing(client, story, prefs.get("language", "Português"))
            message = f"<b>{escaped(story.title)}</b>\n\n{body}\n\n<a href=\"{html.escape(story.link, quote=True)}\">Leia a matéria completa — {escaped(story.source)}</a>"
            await context.bot.send_message(chat_id, message, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            seen.add(story.uid)
            sent_by_chat[str(chat_id)] = list(seen)[-500:]
            save_state()
        except Exception:
            log.exception("Could not brief %s", story.link)
    return len(candidates)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    get_chat(chat_id)
    await update.message.reply_text(
        "Olá! Eu sou o James. Vou procurar notícias de cibersegurança e enviar no máximo quatro histórias relevantes por dia, em português.\n\n"
        "Seus temas iniciais: ameaças e vazamentos.\n"
        "Comandos: /topics threats,breaches,ransomware,policy,research | /language Português ou English | /now para buscar agora | /settings"
    )


async def topics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    prefs = get_chat(chat_id)
    allowed = set(TOPICS)
    raw = " ".join(context.args).lower().replace(" ", "")
    if not raw:
        await update.message.reply_text("Temas disponíveis: threats, breaches, ransomware, policy, research. Exemplo: /topics threats,breaches")
        return
    chosen = list(dict.fromkeys(t for t in raw.split(",") if t in allowed))
    if not chosen:
        await update.message.reply_text("Não reconheci esses temas. Use: threats, breaches, ransomware, policy, research.")
        return
    prefs["topics"] = chosen
    save_state()
    await update.message.reply_text("Temas atualizados: " + ", ".join(chosen))


async def language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prefs = get_chat(update.effective_chat.id)
    value = " ".join(context.args).strip()
    if not value:
        await update.message.reply_text("Use /language Português ou /language English")
        return
    prefs["language"] = value[:40]
    save_state()
    await update.message.reply_text(f"Idioma das próximas briefings: {prefs['language']}")


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prefs = get_chat(update.effective_chat.id)
    await update.message.reply_text(f"Temas: {', '.join(prefs['topics'])}\nIdioma: {prefs['language']}\nFrequência: diária (até {os.getenv('MAX_STORIES_PER_BRIEFING', '4')} notícias)")


async def now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Vou procurar as notícias relevantes mais recentes…")
    await deliver(update.effective_chat.id, context, manual=True)


async def daily_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    for chat_id in list(state.get("chats", {})):
        await deliver(int(chat_id), context)


async def post_init(application: Application) -> None:
    application.bot_data["openai"] = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    tz = ZoneInfo(os.getenv("TIMEZONE", "America/Fortaleza"))
    hour = max(0, min(23, int(os.getenv("DAILY_BRIEFING_HOUR", "8"))))
    application.job_queue.run_daily(daily_job, time=datetime.now(tz).replace(hour=hour, minute=0, second=0, microsecond=0).timetz(), name="daily-briefing")


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and OPENAI_API_KEY (see .env.example).")
    load_state()
    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("topics", topics))
    app.add_handler(CommandHandler("language", language))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CommandHandler("now", now))
    app.run_polling()


if __name__ == "__main__":
    main()
