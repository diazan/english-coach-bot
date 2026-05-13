import html as html_lib
import json
import os
import logging
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import boto3
from boto3.dynamodb.conditions import Key, Attr
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
COLOMBIA_TZ = timezone(timedelta(hours=-5))

# ─────────────────────────────────────────
# Config
# ─────────────────────────────────────────

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
AWS_REGION     = os.getenv("AWS_REGION")
S3_BUCKET      = os.getenv("S3_BUCKET")
DYNAMODB_TABLE = os.getenv("DYNAMODB_TABLE")
DYNAMODB_ERRORS_TABLE = os.getenv("DYNAMODB_ERRORS_TABLE")
AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT        = int(os.getenv("PORT", 8080))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def e(text) -> str:
    """Escape text for Telegram HTML mode."""
    return html_lib.escape(str(text))


# ─────────────────────────────────────────
# AWS clients
# ─────────────────────────────────────────

s3 = boto3.client(
    "s3",
    region_name=AWS_REGION,
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
)

dynamodb = boto3.resource(
    "dynamodb",
    region_name=AWS_REGION,
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
)

table = dynamodb.Table(DYNAMODB_TABLE)
errors_table = dynamodb.Table(DYNAMODB_ERRORS_TABLE)

# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────

def calculate_overall_score(scores: dict) -> float:
    total = sum(v["score"] * v["weight"] for v in scores.values())
    if total <= 10:
        total = total * 10

    # Amplify penalty for chronic errors (3+ occurrences)
    try:
        today = datetime.now(COLOMBIA_TZ).strftime("%Y-%m-%d")
        since = (datetime.now(COLOMBIA_TZ) - timedelta(days=30)).strftime("%Y-%m-%d")
        result = errors_table.scan(
            FilterExpression=Attr("occurrences").gte(3) & Attr("last_seen").gte(since)
        )
        chronic = result.get("Items", [])
        penalty = min(len(chronic) * 1.5, 15)  # max -15 points
        total   = max(0, total - penalty)
    except Exception:
        pass  # never break scoring if tracker fails

    return round(total, 1)


def store_session(data: dict) -> None:
    session_id = data["session_id"]
    date       = data["date"]

    s3_key = f"sessions/{date}/{session_id}.json"
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=s3_key,
        Body=json.dumps(data, ensure_ascii=False, indent=2),
        ContentType="application/json",
    )

    item = {
        "session_id":    session_id,
        "date":          date,
        "topic":         data.get("topic", "—"),
        "overall_score": str(data.get("overall_score", 0)),
        "word_count":    data.get("word_count", 0),
        "message_count": data.get("message_count", 0),
        "model_used":    data.get("model_used", "—"),
        "s3_key":        s3_key,
    }

    for cat, val in data.get("scores", {}).items():
        item[f"score_{cat}"] = str(val["score"])

    errors = data.get("errors", [])
    real_errors = errors
    item["errors_critical"]            = sum(1 for err in real_errors if err.get("severity") == "critical")
    item["errors_moderate"]            = sum(1 for err in real_errors if err.get("severity") == "moderate")
    item["errors_minor"]               = sum(1 for err in real_errors if err.get("severity") == "minor")
    item["spanish_interference_count"] = sum(1 for err in real_errors if err.get("spanish_interference"))
    item["vocab_suggestions"]          = len(data.get("vocabulary_suggestions", []))

    table.put_item(Item=item)
    update_error_tracker(real_errors)

def update_error_tracker(errors: list) -> None:
    """Upsert each error into english-coach-errors using SM-2 spaced repetition."""
    today = datetime.now(COLOMBIA_TZ).strftime("%Y-%m-%d")

    for err in errors:
        error_type = err.get("type", "unknown").lower().replace(" ", "_")
        rule       = err.get("rule", "unknown").lower().replace(" ", "_")
        error_key  = f"{error_type}|{rule}"

        try:
            result = errors_table.get_item(Key={"error_key": error_key})
            existing = result.get("Item")
        except Exception:
            existing = None

        if existing:
            occurrences  = int(existing.get("occurrences", 1)) + 1
            ease_factor  = float(existing.get("ease_factor", 2.5))
            interval     = int(existing.get("interval_days", 1))

            # SM-2: every new occurrence resets interval growth
            if occurrences <= 2:
                interval = 1
            elif occurrences == 3:
                interval = 3
            else:
                interval = round(interval * ease_factor)
                ease_factor = max(1.3, ease_factor - 0.2)

            examples = existing.get("examples", [])
            examples.append({
                "original":   err.get("original", "—"),
                "correction": err.get("correction", "—"),
                "date":       today,
            })
            examples = examples[-3:]  # keep last 3 only

        else:
            occurrences = 1
            ease_factor = 2.5
            interval    = 1
            examples    = [{
                "original":   err.get("original", "—"),
                "correction": err.get("correction", "—"),
                "date":       today,
            }]

        next_review = (
            datetime.now(COLOMBIA_TZ) + timedelta(days=interval)
        ).strftime("%Y-%m-%d")

        errors_table.put_item(Item={
            "error_key":    error_key,
            "error_type":   err.get("type", "unknown"),
            "rule":         err.get("rule", "unknown"),
            "severity":     err.get("severity", "minor"),
            "occurrences":  occurrences,
            "interval_days": interval,
            "ease_factor":  str(ease_factor),
            "last_seen":    today,
            "next_review":  next_review,
            "examples":     examples,
            "spanish_interference": err.get("spanish_interference", False),
        })
def fetch_due_errors(limit: int = 3) -> list:
    """Return errors due for review today, sorted by occurrences descending."""
    today = datetime.now(COLOMBIA_TZ).strftime("%Y-%m-%d")
    try:
        result = errors_table.scan(
            FilterExpression=Attr("next_review").lte(today)
        )
        items = result.get("Items", [])
        items.sort(key=lambda x: int(x.get("occurrences", 1)), reverse=True)
        return items[:limit]
    except Exception:
        return []

def get_chronic_errors_summary(limit: int = 5) -> str:
    """Return a plain-text summary of top chronic errors for prompt injection."""
    try:
        result = errors_table.scan(
            FilterExpression=Attr("occurrences").gte(2)
        )
        items = result.get("Items", [])
        items.sort(key=lambda x: int(x.get("occurrences", 1)), reverse=True)
        top = items[:limit]
    except Exception:
        return ""

    if not top:
        return ""

    lines = ["The student has these recurring errors — watch for them actively:\n"]
    for item in top:
        examples   = item.get("examples", [])
        correction = examples[-1]["correction"] if examples else "—"
        lines.append(
            f"- {item['rule']} ({item['occurrences']}x): "
            f"correct form is → {correction}"
        )
    return "\n".join(lines)

def fetch_recent_sessions(days: int = 30) -> list:
    since = (datetime.now(COLOMBIA_TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    result = table.scan(FilterExpression=Attr("date").gte(since))
    items  = result.get("Items", [])
    return sorted(items, key=lambda x: x["date"])


def fetch_sessions_for_date(date_str: str) -> list:
    result = table.query(
        IndexName="date-index",
        KeyConditionExpression=Key("date").eq(date_str)
    )
    return result.get("Items", [])


def load_errors_from_sessions(sessions: list) -> list:
    all_errors = []
    for s in sessions:
        try:
            obj  = s3.get_object(Bucket=S3_BUCKET, Key=s["s3_key"])
            data = json.loads(obj["Body"].read())
            for i, err in enumerate(data.get("errors", [])):
                err["_session_id"] = s["session_id"]
                err["_s3_key"]     = s["s3_key"]
                err["_date"]       = s["date"]
                err["_index"]      = i
                all_errors.append(err)
        except Exception:
            continue
    return all_errors


def group_errors_by_rule(errors: list) -> list:
    """Group errors by rule, count occurrences, keep worst severity."""
    severity_order = {"critical": 0, "moderate": 1, "minor": 2}
    groups = {}
    for err in errors:
        rule = err.get("rule", "unknown")
        if rule not in groups:
            groups[rule] = {
                "rule":                rule,
                "count":               0,
                "severity":            err.get("severity", "minor"),
                "spanish_interference": err.get("spanish_interference", False),
                "sample_original":     err.get("original", "—"),
                "sample_correction":   err.get("correction", "—"),
            }
        groups[rule]["count"] += 1
        # Keep worst severity
        current = severity_order.get(groups[rule]["severity"], 2)
        new     = severity_order.get(err.get("severity", "minor"), 2)
        if new < current:
            groups[rule]["severity"]          = err.get("severity")
            groups[rule]["sample_original"]   = err.get("original", "—")
            groups[rule]["sample_correction"] = err.get("correction", "—")

    sorted_groups = sorted(
        groups.values(),
        key=lambda x: (severity_order.get(x["severity"], 2), -x["count"])
    )
    return sorted_groups


def format_grouped_errors(grouped: list, title: str) -> str:
    if not grouped:
        return "No errors recorded. 🎉"

    lines = [f"🔍 <b>{e(title)}</b>\n"]
    for i, g in enumerate(grouped[:10], 1):
        severity = g["severity"]
        icon     = "🔴" if severity == "critical" else ("🟡" if severity == "moderate" else "🟢")
        spanish  = " 🇪🇸" if g["spanish_interference"] else ""
        count    = f" <i>({g['count']}x)</i>" if g["count"] > 1 else ""
        lines.append(
            f"{i}. {icon}{spanish} <b>{e(g['rule'])}</b>{count}\n"
            f"   ✗ <i>{e(g['sample_original'])}</i>\n"
            f"   ✓ {e(g['sample_correction'])}\n"
        )
    return "\n".join(lines)


def format_individual_errors(errors: list, title: str, numbered: bool = False) -> str:
    if not errors:
        return "No errors recorded. 🎉"

    severity_order = {"critical": 0, "moderate": 1, "minor": 2}
    errors_sorted = sorted(errors, key=lambda x: severity_order.get(x.get("severity", "minor"), 2))

    lines = [f"🔍 <b>{e(title)}</b>\n"]
    for i, err in enumerate(errors_sorted[:15], 1):
        severity = err.get("severity", "minor")
        icon     = "🔴" if severity == "critical" else ("🟡" if severity == "moderate" else "🟢")
        spanish  = " 🇪🇸" if err.get("spanish_interference") else ""
        prefix   = f"{i}. " if numbered else ""
        lines.append(
            f"{prefix}{icon}{spanish} <b>{e(err.get('rule', '—'))}</b>\n"
            f"   ✗ <i>{e(err.get('original', '—'))}</i>\n"
            f"   ✓ {e(err.get('correction', '—'))}\n"
        )
    return "\n".join(lines)


def build_report(sessions: list, period_label: str) -> str:
    if not sessions:
        return f"No sessions found in the last {period_label}."

    scores    = [float(s["overall_score"]) for s in sessions]
    avg_score = round(sum(scores) / len(scores), 1)
    best      = max(scores)
    worst     = min(scores)
    trend     = "📈 improving" if scores[-1] > scores[0] else ("📉 declining" if scores[-1] < scores[0] else "➡️ stable")

    total_errors   = sum(int(s.get("errors_critical", 0)) + int(s.get("errors_moderate", 0)) + int(s.get("errors_minor", 0)) for s in sessions)
    spanish_errors = sum(int(s.get("spanish_interference_count", 0)) for s in sessions)
    vocab_total    = sum(int(s.get("vocab_suggestions", 0)) for s in sessions)
    total_words    = sum(int(s.get("word_count", 0)) for s in sessions)

    lines = [
        f"📊 <b>English Coach Report — {e(period_label)}</b>",
        "",
        f"<b>Sessions:</b> {len(sessions)}",
        f"<b>Total words written:</b> {total_words:,}",
        "",
        f"<b>Average score:</b> {avg_score}/100",
        f"<b>Best session:</b> {best}/100",
        f"<b>Worst session:</b> {worst}/100",
        f"<b>Trend:</b> {trend}",
        "",
        f"<b>Errors detected:</b> {total_errors}",
        f"<b>Spanish interference:</b> {spanish_errors}",
        f"<b>Vocabulary suggestions:</b> {vocab_total}",
        "",
        "<b>Sessions breakdown:</b>",
    ]

    for s in sessions:
        score      = float(s["overall_score"])
        emoji_icon = "🟢" if score >= 80 else ("🟡" if score >= 65 else "🔴")
        lines.append(f"  {emoji_icon} {e(s['date'])} — {score}/100 — <i>{e(s.get('topic', '—'))}</i>")

    return "\n".join(lines)


# ─────────────────────────────────────────
# Command handlers
# ─────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 <b>English Coach Bot</b>\n\n"
        "Paste your session JSON here and I'll store it.\n\n"
        "<b>Commands:</b>\n"
        "/reporte — last 30 days report\n"
        "/semana — last 7 days report\n"
        "/errores — top 10 most frequent errors\n"
        "/errores hoy — today's errors (numbered)\n"
        "/errores semana — this week's top 10 errors\n"
        "/vocabulario — pending vocabulary words\n"
        "/ejercicio — drill errors due for review today\n"
        "/ayuda — show this message",
        parse_mode="HTML",
    )


async def cmd_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_reporte(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Generating report...")
    sessions = fetch_recent_sessions(days=30)
    report   = build_report(sessions, "last 30 days")
    await update.message.reply_text(report, parse_mode="HTML")


async def cmd_semana(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Generating report...")
    sessions = fetch_recent_sessions(days=7)
    report   = build_report(sessions, "last 7 days")
    await update.message.reply_text(report, parse_mode="HTML")


async def cmd_vocabulario(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Fetching vocabulary...")
    sessions = fetch_recent_sessions(days=60)

    if not sessions:
        await update.message.reply_text("No sessions found in the last 60 days.")
        return

    pending_words = []
    for s in sessions[-10:]:
        try:
            obj  = s3.get_object(Bucket=S3_BUCKET, Key=s["s3_key"])
            data = json.loads(obj["Body"].read())
            for v in data.get("vocabulary_suggestions", []):
                if v.get("status") == "pending":
                    pending_words.append(v)
        except Exception:
            continue

    if not pending_words:
        await update.message.reply_text("No pending vocabulary words. Keep it up! 🎉")
        return

    lines = ["📚 <b>Pending vocabulary words:</b>\n"]
    for v in pending_words[:15]:
        options = ", ".join(f"<code>{e(w)}</code>" for w in v.get("better_options", []))
        lines.append(
            f"• Instead of <b>{e(v['word_used'])}</b> → {options}\n"
            f"  <i>{e(v.get('example', ''))}</i>\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_errores(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /errores          → top 10 grouped by rule, all time (60 days)
    /errores hoy      → today's errors, numbered
    /errores semana   → top 10 grouped, last 7 days
    """
    args = " ".join(context.args).strip().lower() if context.args else ""

    await update.message.reply_text("⏳ Analyzing errors...")

    if args == "hoy":
        from datetime import timezone, timedelta
        today = datetime.now(COLOMBIA_TZ).strftime("%Y-%m-%d")
        logger.info(f"Buscando sesiones para fecha: {today}")
        sessions = fetch_sessions_for_date(today)
        logger.info(f"Sesiones encontradas: {len(sessions)} — items: {sessions}")
        if not sessions:
            await update.message.reply_text("No sessions found for today.")
            return
        errors = load_errors_from_sessions(sessions)
        
        msg = format_individual_errors(errors, f"Today's errors ({today})", numbered=True)

    elif args == "semana":
        sessions = fetch_recent_sessions(days=7)
        if not sessions:
            await update.message.reply_text("No sessions found this week.")
            return
        errors  = load_errors_from_sessions(sessions)
        grouped = group_errors_by_rule(errors)
        msg     = format_grouped_errors(grouped, "Top errors — last 7 days")

    else:
        sessions = fetch_recent_sessions(days=60)
        if not sessions:
            await update.message.reply_text("No sessions found in the last 60 days.")
            return
        errors  = load_errors_from_sessions(sessions)
        grouped = group_errors_by_rule(errors)
        msg     = format_grouped_errors(grouped, "Top errors — last 60 days")
        

    await update.message.reply_text(msg, parse_mode="HTML")


# ─────────────────────────────────────────
# Core: process session
# ─────────────────────────────────────────

async def cmd_ejercicio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate a mini-exercise for errors due for review today."""
    await update.message.reply_text("⏳ Preparing your exercise...")

    due = fetch_due_errors(limit=3)

    if not due:
        await update.message.reply_text(
            "✅ No errors due for review today. You're up to date!\n\n"
            "Start your session when ready."
        )
        return

    lines = ["🎯 <b>Before your session — quick drill</b>\n"]
    lines.append("Fix these sentences and reply with your corrections:\n")

    for i, err in enumerate(due, 1):
        examples  = err.get("examples", [])
        original  = examples[-1]["original"] if examples else "—"
        times     = int(err.get("occurrences", 1))
        rule      = err.get("rule", "—")
        lines.append(
            f"{i}. <i>{e(original)}</i>\n"
            f"   <b>Rule:</b> {e(rule)} "
            f"<b>· Seen:</b> {times}x\n"
        )

    lines.append("\nReply with your corrected sentences when ready.")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def process_session_data(data: dict, update: Update) -> None:
    required = ["session_id", "date", "scores", "overall_score"]
    missing  = [f for f in required if f not in data]
    if missing:
        await update.message.reply_text(
            f"❌ Invalid JSON. Missing fields: {', '.join(missing)}\n"
            "Make sure you used the correct evaluation prompt."
        )
        return

    data["overall_score"] = calculate_overall_score(data["scores"])

    try:
        store_session(data)
    except Exception as ex:
        logger.exception("Error saving to AWS")
        await update.message.reply_text(f"❌ Error saving to AWS: {e(str(ex))}")
        return

    errors     = data.get("errors", [])
    real_errors = errors
    critical   = sum(1 for err in real_errors if err.get("severity") == "critical")
    moderate   = sum(1 for err in real_errors if err.get("severity") == "moderate")
    minor      = sum(1 for err in real_errors if err.get("severity") == "minor")
    spanish    = sum(1 for err in real_errors if err.get("spanish_interference"))
    vocab      = len(data.get("vocabulary_suggestions", []))

    score      = data["overall_score"]
    emoji_icon = "🟢" if score >= 80 else ("🟡" if score >= 65 else "🔴")
    trend      = data.get("session_trend", "—")

    strengths_html = "\n".join(f"  ✅ {e(s)}" for s in data.get("strengths", []))
    recs_html      = "\n".join(f"  💡 {e(r)}" for r in data.get("recommendations", []))
    focus          = data.get("focus_for_next_session", "—")

    msg = (
        f"{emoji_icon} <b>Session saved!</b>\n\n"
        f"<b>Score:</b> {score}/100  |  <b>Trend:</b> {e(trend)}\n"
        f"<b>Topic:</b> {e(data.get('topic', '—'))}\n"
        f"<b>Words written:</b> {e(data.get('word_count', 0))}\n\n"
        f"<b>Errors:</b> 🔴 {critical} critical  🟡 {moderate} moderate  🟢 {minor} minor\n"
        f"<b>Spanish interference:</b> {spanish}\n"
        f"<b>Vocabulary suggestions:</b> {vocab}\n\n"
        f"<b>Strengths:</b>\n{strengths_html}\n\n"
        f"<b>Recommendations:</b>\n{recs_html}\n\n"
        f"<b>Focus for next session:</b> {e(focus)}"
    )

    await update.message.reply_text(msg, parse_mode="HTML")


# ─────────────────────────────────────────
# Message handlers
# ─────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()

    if not text.startswith("{"):
        await update.message.reply_text(
            "I didn't understand that. Use a command like /start, "
            "or paste your session JSON directly here."
        )
        return

    await update.message.reply_text("⏳ Processing your session...")
    chronic_summary = get_chronic_errors_summary()
    if chronic_summary:
        await update.message.reply_text(
            f"📌 <b>Chronic errors loaded into context:</b>\n\n"
            f"<code>{e(chronic_summary)}</code>",
            parse_mode="HTML"
        )

    try:
        data = json.loads(text)
    except json.JSONDecodeError as ex:
        await update.message.reply_text(f"❌ Invalid JSON: {e(str(ex))}")
        return

    await process_session_data(data, update)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document

    if not doc.file_name.endswith(".json"):
        await update.message.reply_text("⚠️ Please send a .json file.")
        return

    await update.message.reply_text("⏳ Processing your session...")

    try:
        file       = await context.bot.get_file(doc.file_id)
        file_bytes = await file.download_as_bytearray()
        data       = json.loads(file_bytes.decode("utf-8"))
    except Exception as ex:
        await update.message.reply_text(f"❌ Could not read the file: {e(str(ex))}")
        return

    await process_session_data(data, update)


# ─────────────────────────────────────────
# Main
# ─────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass


def main() -> None:
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("ayuda",       cmd_ayuda))
    app.add_handler(CommandHandler("reporte",     cmd_reporte))
    app.add_handler(CommandHandler("semana",      cmd_semana))
    app.add_handler(CommandHandler("vocabulario", cmd_vocabulario))
    app.add_handler(CommandHandler("errores",     cmd_errores))
    app.add_handler(CommandHandler("ejercicio",   cmd_ejercicio))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot running with webhook...")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TELEGRAM_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}",
    )


if __name__ == "__main__":
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main()