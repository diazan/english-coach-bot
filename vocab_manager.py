"""
vocab_manager.py — Vocabulary spaced repetition manager
for the English Coach Telegram bot.

DynamoDB table: english-coach-vocabulary
  PK: word_key  (str, lowercase word, e.g. "however")
  Attributes:
    word            str   — original capitalisation
    definition      str   — short meaning / usage note
    example         str   — example sentence (from session or generated)
    source_date     str   — ISO date the word was first seen (YYYY-MM-DD)
    status          str   — "pending" | "practiced" | "mastered"
    easiness        float — SM-2 E-factor (starts at 2.5)
    interval        int   — days until next review
    repetitions     int   — consecutive correct reviews
    next_review     str   — ISO date of next scheduled review
    last_reviewed   str   — ISO date of last review (or None)
    correct_streak  int   — current consecutive correct answers
    total_reviews   int
    total_correct   int

Active list cap: MAX_ACTIVE_WORDS = 6
  New words are only pulled from the pending pool once the active list
  (status == "practiced" and not yet mastered) has fewer than the cap.

SM-2 quality scores used internally:
  5 — perfect recall        → interval grows fast
  4 — correct with effort   → interval grows normally
  3 — correct with hints    → interval resets to 1 day
  0 — incorrect             → interval resets, E-factor drops
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

# ── Constants ────────────────────────────────────────────────────────────────

TABLE_NAME = "english-coach-vocabulary"
MAX_ACTIVE_WORDS = 6          # max words in "practiced" (non-mastered) state
MIN_EASINESS = 1.3            # SM-2 lower bound for E-factor
DEFAULT_EASINESS = 2.5
MASTERY_THRESHOLD = 5         # repetitions with quality ≥ 4 to reach "mastered"
MASTERY_MIN_INTERVAL = 21     # days — also required for mastery


# ── DynamoDB helpers ─────────────────────────────────────────────────────────

def _table():
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    return dynamodb.Table(TABLE_NAME)


def _today() -> str:
    """Return today's date as YYYY-MM-DD (Colombia UTC-5)."""
    from datetime import timezone, timedelta
    colombia_tz = timezone(timedelta(hours=-5))
    return datetime.now(colombia_tz).date().isoformat()


def _to_float(value) -> float:
    """Convert Decimal or str to float safely."""
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


# ── SM-2 core ────────────────────────────────────────────────────────────────

def _sm2_next(repetitions: int, interval: int, easiness: float, quality: int
              ) -> tuple[int, int, float]:
    """
    Pure SM-2 calculation.
    Returns (new_repetitions, new_interval, new_easiness).
    quality: 0–5
    """
    if quality < 3:
        # incorrect or very difficult — reset
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


# ── Word management ──────────────────────────────────────────────────────────

def add_word(word: str, definition: str, example: str = "",
             source_date: Optional[str] = None) -> dict:
    """
    Add a new word to the vocabulary table with status='pending'.
    If the word already exists, return the existing item without overwriting.

    Returns the item dict (new or existing).
    """
    table = _table()
    word_key = word.strip().lower()
    today = source_date or _today()

    # Check for existing word first
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
        "next_review": today,          # available for review immediately
        "last_reviewed": None,
        "correct_streak": 0,
        "total_reviews": 0,
        "total_correct": 0,
    }

    try:
        table.put_item(
            Item=item,
            ConditionExpression=Attr("word_key").not_exists(),
        )
        logger.info("Added new word '%s'.", word_key)
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        # Race condition — word was inserted between our check and put
        resp = table.get_item(Key={"word_key": word_key})
        return resp.get("Item", item)
    except Exception as exc:
        logger.error("Error adding word '%s': %s", word_key, exc)
        raise

    return item


def get_word(word_key: str) -> Optional[dict]:
    """Fetch a single word item by its key."""
    try:
        resp = _table().get_item(Key={"word_key": word_key.lower()})
        return resp.get("Item")
    except Exception as exc:
        logger.error("Error fetching word '%s': %s", word_key, exc)
        return None


def get_all_words() -> list[dict]:
    """Scan the full vocabulary table (suitable for small tables < 1 MB)."""
    try:
        resp = _table().scan()
        return resp.get("Items", [])
    except Exception as exc:
        logger.error("Error scanning vocabulary table: %s", exc)
        return []


# ── Active list management ───────────────────────────────────────────────────

def get_active_words() -> list[dict]:
    """Return all words with status='practiced' (the 'active' review list)."""
    all_words = get_all_words()
    return [w for w in all_words if w.get("status") == "practiced"]


def get_due_words(today: Optional[str] = None) -> list[dict]:
    """
    Return active (practiced) words whose next_review date is today or earlier.
    """
    today = today or _today()
    return [
        w for w in get_active_words()
        if (w.get("next_review") or "9999-99-99") <= today
    ]


def get_pending_words() -> list[dict]:
    """Return words not yet activated (status='pending')."""
    all_words = get_all_words()
    return [w for w in all_words if w.get("status") == "pending"]


def get_mastered_words() -> list[dict]:
    """Return words that have been mastered."""
    all_words = get_all_words()
    return [w for w in all_words if w.get("status") == "mastered"]


def activate_pending_words() -> list[dict]:
    """
    Move words from 'pending' → 'practiced' to fill the active list
    up to MAX_ACTIVE_WORDS.  Words are activated oldest-first (source_date).
    Returns the list of newly activated words.
    """
    active = get_active_words()
    slots = MAX_ACTIVE_WORDS - len(active)
    if slots <= 0:
        return []

    pending = sorted(
        get_pending_words(),
        key=lambda w: w.get("source_date", ""),
    )
    newly_activated = []

    for word in pending[:slots]:
        wk = word["word_key"]
        try:
            _table().update_item(
                Key={"word_key": wk},
                UpdateExpression="SET #st = :practiced, next_review = :today",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":practiced": "practiced",
                    ":today": _today(),
                },
            )
            word["status"] = "practiced"
            newly_activated.append(word)
            logger.info("Activated word '%s'.", wk)
        except Exception as exc:
            logger.error("Error activating word '%s': %s", wk, exc)

    return newly_activated


# ── Review recording ─────────────────────────────────────────────────────────

def record_review(word_key: str, quality: int) -> dict:
    """
    Record a review for a word using SM-2 and update the DynamoDB item.

    quality: 0–5
      5 = perfect recall
      4 = correct, slight hesitation
      3 = correct with effort / hint needed
      0–2 = incorrect (all treated as 0 for interval reset)

    Returns the updated item dict.
    """
    word_key = word_key.lower()
    item = get_word(word_key)
    if not item:
        raise ValueError(f"Word '{word_key}' not found in vocabulary table.")

    quality = max(0, min(5, quality))  # clamp

    repetitions = int(item.get("repetitions", 0))
    interval = int(item.get("interval", 1))
    easiness = _to_float(item.get("easiness", DEFAULT_EASINESS))
    correct_streak = int(item.get("correct_streak", 0))
    total_reviews = int(item.get("total_reviews", 0))
    total_correct = int(item.get("total_correct", 0))

    new_rep, new_interval, new_ease = _sm2_next(
        repetitions, interval, easiness, quality
    )

    today = _today()
    next_review = (
        date.fromisoformat(today) + timedelta(days=new_interval)
    ).isoformat()

    is_correct = quality >= 3
    new_streak = correct_streak + 1 if is_correct else 0
    new_total_correct = total_correct + (1 if is_correct else 0)

    # Determine new status
    current_status = item.get("status", "practiced")
    new_status = current_status
    if (
        current_status == "practiced"
        and new_rep >= MASTERY_THRESHOLD
        and new_interval >= MASTERY_MIN_INTERVAL
        and is_correct
    ):
        new_status = "mastered"
        logger.info("Word '%s' has been MASTERED!", word_key)

    try:
        _table().update_item(
            Key={"word_key": word_key},
            UpdateExpression=(
                "SET #st = :status, "
                "easiness = :ease, "
                "#iv = :interval, "
                "repetitions = :reps, "
                "next_review = :next_review, "
                "last_reviewed = :today, "
                "correct_streak = :streak, "
                "total_reviews = :total_reviews, "
                "total_correct = :total_correct"
            ),
            ExpressionAttributeNames={
                "#st": "status",
                "#iv": "interval",
            },
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
    except Exception as exc:
        logger.error("Error updating review for '%s': %s", word_key, exc)
        raise

    # If a word was just mastered, try to activate a pending replacement
    if new_status == "mastered" and current_status != "mastered":
        activate_pending_words()

    return {
        **item,
        "status": new_status,
        "easiness": new_ease,
        "interval": new_interval,
        "repetitions": new_rep,
        "next_review": next_review,
        "last_reviewed": today,
        "correct_streak": new_streak,
        "total_reviews": total_reviews + 1,
        "total_correct": new_total_correct,
    }


# ── Ingestion from S3 sessions ───────────────────────────────────────────────

def ingest_from_session(session_data: dict) -> list[str]:
    """
    Extract vocabulary_suggestions from a session JSON object and add any
    new words to the vocabulary table.

    session_data is the parsed JSON from S3 (the full session dict).
    Returns a list of word_keys that were newly added.
    """
    suggestions = session_data.get("vocabulary_suggestions", [])
    added = []

    for entry in suggestions:
        if isinstance(entry, str):
            # Legacy format: just the word string
            word = entry
            definition = ""
            example = ""
        elif isinstance(entry, dict):
            word = entry.get("word", "").strip()
            definition = entry.get("definition", "").strip()
            example = entry.get("example", "").strip()
        else:
            continue

        if not word:
            continue

        word_key = word.lower()
        existing = get_word(word_key)
        if existing:
            continue   # already tracked

        try:
            add_word(
                word=word,
                definition=definition or f"(see session for context)",
                example=example,
                source_date=session_data.get("date", _today()),
            )
            added.append(word_key)
        except Exception as exc:
            logger.warning("Could not add word '%s': %s", word, exc)

    # After ingestion, try to fill the active list
    if added:
        activate_pending_words()

    return added


# ── Stats helpers ─────────────────────────────────────────────────────────────

def get_weekly_stats(days: int = 7) -> dict:
    """
    Return a summary dict for the /vocabulario stats command.
    """
    all_words = get_all_words()
    today = date.fromisoformat(_today())
    cutoff = (today - timedelta(days=days)).isoformat()

    reviewed_this_week = [
        w for w in all_words
        if (w.get("last_reviewed") or "") >= cutoff
    ]

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
        round(sum(accuracy_rates) / len(accuracy_rates) * 100)
        if accuracy_rates else None
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
            if w.get("status") == "mastered"
            and (w.get("last_reviewed") or "") >= cutoff
        ],
    }


def format_stats_message(days: int = 7) -> str:
    """Return a Telegram-ready stats message for /vocabulario stats."""
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
        words = ", ".join(
            w.get("word", w["word_key"]) for w in stats["recently_mastered"]
        )
        lines += ["", f"🏆 Newly mastered: _{words}_"]

    if stats["due_today"] > 0:
        lines += ["", "👉 Use /vocabulario to start your review!"]
    elif sc.get("practiced", 0) == 0 and sc.get("pending", 0) > 0:
        lines += ["", "👉 Use /vocabulario to start learning new words!"]
    else:
        lines += ["", "✨ All caught up for today!"]

    return "\n".join(lines)


def format_word_card(item: dict, show_answer: bool = False) -> str:
    """
    Format a single vocabulary word for display in Telegram.
    Used during drill sessions.
    """
    word = item.get("word", item.get("word_key", "?"))
    definition = item.get("definition", "")
    example = item.get("example", "")
    status = item.get("status", "")
    interval = int(item.get("interval", 1))
    reps = int(item.get("repetitions", 0))

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

    lines.append(f"\n📊 Reviews: {reps}  |  Next in: {interval}d")
    if status == "mastered":
        lines.append("✅ *MASTERED*")

    return "\n".join(lines)


# ── Quick test (run directly) ─────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Smoke-test: add a word, record a review, print stats
    print("Adding test word...")
    item = add_word(
        word="however",
        definition="used to introduce a contrast or exception",
        example="I wanted to go; however, it was raining.",
    )
    print(f"  → {item['word_key']} | status={item['status']}")

    print("Recording a correct review (quality=4)...")
    updated = record_review("however", quality=4)
    print(f"  → interval={updated['interval']}d | reps={updated['repetitions']}")

    print("\nStats:")
    print(format_stats_message())