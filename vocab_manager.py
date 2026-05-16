"""
vocab_manager.py — Vocabulary spaced repetition manager
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Attr, Key

logger = logging.getLogger(__name__)

TABLE_NAME = "english-coach-vocabulary"
MAX_ACTIVE_WORDS = 6
MIN_EASINESS = 1.3
DEFAULT_EASINESS = 2.5
MASTERY_THRESHOLD = 5
MASTERY_MIN_INTERVAL = 21


def _table():
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    return dynamodb.Table(TABLE_NAME)


def _today() -> str:
    from datetime import timezone, timedelta
    colombia_tz = timezone(timedelta(hours=-5))
    return datetime.now(colombia_tz).date().isoformat()


def _to_float(value) -> float:
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _sm2_next(repetitions, interval, easiness, quality):
    if quality < 3:
        new_rep = 0
        new_interval = 1
    else:
        new_rep = repetitions + 1
        if repetitions == 0:
            new_interval = 1
        elif repetitions == 1:
            new_interval = 6
        else:
            new_interval = round(interval * easiness)

    new_ease = easiness + 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)
    new_ease = max(MIN_EASINESS, round(new_ease, 4))
    return new_rep, new_interval, new_ease


def add_word(word, definition, example="", source_date=None):
    table = _table()
    word_key = word.strip().lower()
    today = source_date or _today()

    try:
        resp = table.get_item(Key={"word_key": word_key})
        if "Item" in resp:
            logger.info("Word '%s' already exists, skipping.", word_key)
            return resp["Item"]
    except Exception as exc:
        logger.error("Error checking existing word '%s': %s", word_key, exc)
        raise

    item = {
        "word_key": word_key,
        "word": word.strip(),
        "definition": definition.strip(),
        "example": example.strip(),
        "source_date": today,
        "status": "pending",
        "easiness": Decimal(str(DEFAULT_EASINESS)),
        "interval": 1,
        "repetitions": 0,
        "next_review": today,
        "last_reviewed": None,
        "correct_streak": 0,
        "total_reviews": 0,
        "total_correct": 0,
    }

    try:
        table.put_item(Item=item, ConditionExpression=Attr("word_key").not_exists())
        logger.info("Added new word '%s'.", word_key)
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        resp = table.get_item(Key={"word_key": word_key})
        return resp.get("Item", item)
    except Exception as exc:
        logger.error("Error adding word '%s': %s", word_key, exc)
        raise

    return item


def get_word(word_key):
    try:
        resp = _table().get_item(Key={"word_key": word_key.lower()})
        return resp.get("Item")
    except Exception as exc:
        logger.error("Error fetching word '%s': %s", word_key, exc)
        return None


def get_all_words():
    try:
        resp = _table().scan()
        return resp.get("Items", [])
    except Exception as exc:
        logger.error("Error scanning vocabulary table: %s", exc)
        return []


def get_active_words():
    return [w for w in get_all_words() if w.get("status") == "practiced"]


def get_due_words(today=None):
    today = today or _today()
    return [
        w for w in get_active_words()
        if (w.get("next_review") or "9999-99-99") <= today
    ]


def get_pending_words():
    return [w for w in get_all_words() if w.get("status") == "pending"]


def get_mastered_words():
    return [w for w in get_all_words() if w.get("status") == "mastered"]


def activate_pending_words():
    active = get_active_words()
    slots = MAX_ACTIVE_WORDS - len(active)
    if slots <= 0:
        return []

    pending = sorted(get_pending_words(), key=lambda w: w.get("source_date", ""))
    newly_activated = []

    for word in pending[:slots]:
        wk = word["word_key"]
        try:
            _table().update_item(
                Key={"word_key": wk},
                UpdateExpression="SET #st = :practiced, next_review = :today",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":practiced": "practiced", ":today": _today()},
            )
            word["status"] = "practiced"
            newly_activated.append(word)
        except Exception as exc:
            logger.error("Error activating word '%s': %s", wk, exc)

    return newly_activated


def record_review(word_key, quality):
    word_key = word_key.lower()
    item = get_word(word_key)
    if not item:
        raise ValueError(f"Word '{word_key}' not found.")

    quality = max(0, min(5, quality))
    repetitions = int(item.get("repetitions", 0))
    interval = int(item.get("interval", 1))
    easiness = _to_float(item.get("easiness", DEFAULT_EASINESS))
    correct_streak = int(item.get("correct_streak", 0))
    total_reviews = int(item.get("total_reviews", 0))
    total_correct = int(item.get("total_correct", 0))

    new_rep, new_interval, new_ease = _sm2_next(repetitions, interval, easiness, quality)

    today = _today()
    next_review = (date.fromisoformat(today) + timedelta(days=new_interval)).isoformat()
    is_correct = quality >= 3
    new_streak = correct_streak + 1 if is_correct else 0
    new_total_correct = total_correct + (1 if is_correct else 0)

    current_status = item.get("status", "practiced")
    new_status = current_status
    if (
        current_status == "practiced"
        and new_rep >= MASTERY_THRESHOLD
        and new_interval >= MASTERY_MIN_INTERVAL
        and is_correct
    ):
        new_status = "mastered"

    _table().update_item(
        Key={"word_key": word_key},
        UpdateExpression=(
            "SET #st = :status, easiness = :ease, #iv = :interval, "
            "repetitions = :reps, next_review = :next_review, "
            "last_reviewed = :today, correct_streak = :streak, "
            "total_reviews = :total_reviews, total_correct = :total_correct"
        ),
        ExpressionAttributeNames={"#st": "status", "#iv": "interval"},
        ExpressionAttributeValues={
            ":status": new_status,
            ":ease": Decimal(str(new_ease)),
            ":interval": new_interval,
            ":reps": new_rep,
            ":next_review": next_review,
            ":today": today,
            ":streak": new_streak,
            ":total_reviews": total_reviews + 1,
            ":total_correct": new_total_correct,
        },
    )

    if new_status == "mastered" and current_status != "mastered":
        activate_pending_words()

    return {**item, "status": new_status, "easiness": new_ease, "interval": new_interval,
            "repetitions": new_rep, "next_review": next_review, "last_reviewed": today,
            "correct_streak": new_streak, "total_reviews": total_reviews + 1,
            "total_correct": new_total_correct}




def ingest_from_session(session_data: dict) -> list[str]:
    """
    Extract vocabulary_suggestions from a session JSON object and add any
    new words to the vocabulary table.

    Supports three entry formats:

    Format A — coach output (word_used / better_options):
        {
            "word_used": "amazing things",
            "better_options": ["meaningful initiatives", "valuable projects"],
            "example": "I have been channeling my time into meaningful initiatives.",
            "status": "pending"
        }
        Each better_option becomes a separate word to learn.
        definition = 'Better alternative for "amazing things"'

    Format B — explicit definition format:
        {"word": "however", "definition": "...", "example": "..."}

    Format C — legacy string: "however"

    Returns a list of word_keys that were newly added.
    """
    suggestions = session_data.get("vocabulary_suggestions", [])
    session_date = session_data.get("date", _today())
    added = []

    for entry in suggestions:
        if isinstance(entry, str):
            # Format C
            word = entry.strip()
            if not word:
                continue
            existing = get_word(word.lower())
            if not existing:
                try:
                    add_word(word=word, definition="", example="", source_date=session_date)
                    added.append(word.lower())
                except Exception as exc:
                    logger.warning("Could not add word '%s': %s", word, exc)

        elif isinstance(entry, dict):
            if "word_used" in entry:
                # Format A: word_used / better_options
                word_used = entry.get("word_used", "").strip()
                better    = entry.get("better_options", [])
                example   = entry.get("example", "").strip()

                if not better:
                    continue

                for option in better:
                    option = option.strip()
                    if not option:
                        continue
                    word_key = option.lower()
                    if get_word(word_key):
                        continue
                    definition = f'Better alternative for "{word_used}"'
                    try:
                        add_word(
                            word=option,
                            definition=definition,
                            example=example,
                            source_date=session_date,
                        )
                        added.append(word_key)
                        logger.info("Added vocab word '%s' (replaces: %s)", option, word_used)
                    except Exception as exc:
                        logger.warning("Could not add word '%s': %s", option, exc)

            else:
                # Format B: explicit word/definition
                word       = entry.get("word", "").strip()
                definition = entry.get("definition", "").strip()
                example    = entry.get("example", "").strip()
                if not word:
                    continue
                if get_word(word.lower()):
                    continue
                try:
                    add_word(word=word, definition=definition, example=example, source_date=session_date)
                    added.append(word.lower())
                except Exception as exc:
                    logger.warning("Could not add word '%s': %s", word, exc)

    # After ingestion, try to fill the active list
    if added:
        activate_pending_words()

    return added


def get_weekly_stats(days: int = 7) -> dict:
    all_words = get_all_words()
    today = date.fromisoformat(_today())
    cutoff = (today - timedelta(days=days)).isoformat()

    reviewed_this_week = [w for w in all_words if (w.get("last_reviewed") or "") >= cutoff]
    status_counts = {"pending": 0, "practiced": 0, "mastered": 0}
    for w in all_words:
        s = w.get("status", "pending")
        status_counts[s] = status_counts.get(s, 0) + 1

    due_today = [
        w for w in all_words
        if w.get("status") == "practiced"
        and (w.get("next_review") or "9999-99-99") <= today.isoformat()
    ]

    accuracy_rates = []
    for w in reviewed_this_week:
        tr = int(w.get("total_reviews", 0))
        tc = int(w.get("total_correct", 0))
        if tr > 0:
            accuracy_rates.append(tc / tr)

    avg_accuracy = (
        round(sum(accuracy_rates) / len(accuracy_rates) * 100) if accuracy_rates else None
    )

    return {
        "total_words": len(all_words),
        "status_counts": status_counts,
        "reviewed_this_week": len(reviewed_this_week),
        "due_today": len(due_today),
        "due_words": due_today,
        "avg_accuracy_pct": avg_accuracy,
        "recently_mastered": [
            w for w in all_words
            if w.get("status") == "mastered" and (w.get("last_reviewed") or "") >= cutoff
        ],
    }


def format_stats_message(days: int = 7) -> str:
    stats = get_weekly_stats(days)
    sc = stats["status_counts"]
    lines = [
        f"📚 *Vocabulary Report — last {days} days*",
        "",
        f"🗂 Total words tracked: *{stats['total_words']}*",
        f"  • Pending (not started): {sc.get('pending', 0)}",
        f"  • Practicing: {sc.get('practiced', 0)}",
        f"  • Mastered ✅: {sc.get('mastered', 0)}",
        "",
        f"🔄 Reviewed this week: *{stats['reviewed_this_week']}*",
        f"📅 Due for review today: *{stats['due_today']}*",
    ]

    if stats["avg_accuracy_pct"] is not None:
        lines.append(f"🎯 Accuracy (this week): *{stats['avg_accuracy_pct']}%*")

    if stats["recently_mastered"]:
        words = ", ".join(w.get("word", w["word_key"]) for w in stats["recently_mastered"])
        lines += ["", f"🏆 Newly mastered: _{words}_"]

    if stats["due_today"] > 0:
        lines += ["", "👉 Use /vocabulario to start your review!"]
    elif sc.get("practiced", 0) == 0 and sc.get("pending", 0) > 0:
        lines += ["", "👉 Use /vocabulario to start learning new words!"]
    else:
        lines += ["", "✨ All caught up for today!"]

    return "\n".join(lines)


def format_word_card(item: dict, show_answer: bool = False) -> str:
    word       = item.get("word", item.get("word_key", "?"))
    definition = item.get("definition", "")
    example    = item.get("example", "")
    status     = item.get("status", "")
    interval   = int(item.get("interval", 1))
    reps       = int(item.get("repetitions", 0))

    if not show_answer:
        return (
            f"📝 *{word}*\n\n"
            f"What does this word mean? How would you use it?\n\n"
            f"_(tap a button when ready to see the answer)_"
        )

    lines = [f"📝 *{word}*"]
    if definition:
        lines.append(f"\n📖 {definition}")
    if example:
        lines.append(f"\n💬 _{example}_")

    status_label = {
        "pending": "🔵 Pending",
        "practiced": "🟡 Practicing",
        "mastered": "✅ Mastered",
    }.get(status, "")
    lines.append(f"\n📊 Reviews: {reps}  |  Next in: {interval}d  |  {status_label}")

    return "\n".join(lines)