"""Delete the videos the bot posted in groups after a while (default 3 hours).

Messages are remembered in SQLite, so a restart does not forget them. A message that
is already gone (an admin deleted it) is simply dropped from the list.
"""
from __future__ import annotations

import asyncio
import time

from app.logger import log
from app.services import settings
from app.storage import db

_ready_for: object = None


def _table() -> None:
    """Create the table once per database (tests and restores swap the database)."""
    global _ready_for
    if _ready_for is db.impl:
        return
    db._run("CREATE TABLE IF NOT EXISTS auto_deletes (chat_id INTEGER, msg_id INTEGER, due REAL, "
            "PRIMARY KEY (chat_id, msg_id))")
    db._run("CREATE INDEX IF NOT EXISTS auto_deletes_due ON auto_deletes(due)")
    _ready_for = db.impl


def hours() -> float:
    try:
        return max(0.0, float(settings.get_float("GROUP_VIDEO_DELETE_HOURS", 3.0)))
    except Exception:
        return 0.0


def schedule(chat_id: int, msg_id: int, after_hours: float | None = None) -> None:
    after = hours() if after_hours is None else after_hours
    if not after or not chat_id or not msg_id:
        return
    try:
        _table()
        db._run("INSERT OR REPLACE INTO auto_deletes(chat_id, msg_id, due) VALUES(?,?,?)",
                (int(chat_id), int(msg_id), time.time() + after * 3600))
    except Exception as exc:
        log.debug("could not schedule auto delete: %s", exc)


def pending() -> int:
    try:
        _table()
        return int(db._one("SELECT COUNT(*) AS n FROM auto_deletes")["n"])
    except Exception:
        return 0


async def sweep() -> int:
    """Delete everything that is due. Returns how many messages were handled."""
    from app.runtime import bot
    _table()
    rows = db._q("SELECT chat_id, msg_id FROM auto_deletes WHERE due <= ? ORDER BY due LIMIT 200",
                 (time.time(),))
    by_chat: dict[int, list[int]] = {}
    for row in rows:
        by_chat.setdefault(int(row["chat_id"]), []).append(int(row["msg_id"]))
    done = 0
    for chat_id, ids in by_chat.items():
        try:
            await bot.delete_messages(chat_id, ids)
        except Exception as exc:
            # Kicked from the group, no permission... retrying forever helps nobody.
            log.debug("auto delete in %s failed: %s", chat_id, exc)
        for msg_id in ids:
            db._run("DELETE FROM auto_deletes WHERE chat_id=? AND msg_id=?", (chat_id, msg_id))
        done += len(ids)
        await asyncio.sleep(0.5)
    return done


async def loop() -> None:
    while True:
        try:
            await sweep()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("auto delete sweep failed: %s", exc)
        await asyncio.sleep(60)
