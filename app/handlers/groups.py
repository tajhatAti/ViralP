"""One free video per explicit group /start; no unsolicited group blasting."""
import asyncio
import random
import time
from telethon import events, Button
from app import runtime
from app.runtime import bot
from app.handlers.router import route
from app.logger import log
from app.services import access, flow, forcejoin, settings, sender
from app.storage import db

_locks = {}
_wait_notice: dict[tuple, float] = {}      # (chat, user) -> last time we told them to wait


_user_last: dict[tuple, float] = {}        # (chat, user) -> when this user last got a video


ANON_ADMIN_ID = 1087968824       # "GroupAnonymousBot": what an anonymous admin looks like


async def _tell_to_wait(event, user_id, seconds: int, text: str = '') -> None:
    """Tell someone who asked too soon how long to wait.

    A button tap gets a real pop-up alert at the top of the screen. A typed `/start`
    cannot get a pop-up (Telegram only allows that for buttons), so it gets a short
    reply that deletes itself.
    """
    message = text or f'⏳ {seconds} সেকেন্ড পর আবার চেষ্টা করুন।'
    if getattr(event, 'data', None) is not None:          # a button tap → pop-up
        try:
            await event.answer(message, alert=True)
            return
        except Exception as exc:                          # already answered, etc.
            log.debug('wait pop-up failed: %s', exc)
    now = time.time()
    key = (event.chat_id, user_id)
    if now - _wait_notice.get(key, 0) < 5:                # never answer a spammer with spam
        return
    if len(_wait_notice) > 2000:
        _wait_notice.clear()
    _wait_notice[key] = now
    try:
        note = await event.reply(message + ' (বা নিচের 🎬 বাটন চাপুন)')
    except Exception as exc:
        log.debug('wait notice failed: %s', exc)
        return

    async def _cleanup():
        await asyncio.sleep(8)
        try:
            await note.delete()
        except Exception:
            pass
    asyncio.create_task(_cleanup())


def buttons(file_id=0):
    username = runtime.bot_username
    if not username:
        return []
    home = f'https://t.me/{username}'
    rows = [[Button.inline('🎬 আরেকটা ভিডিও', 'gnext')]] if file_id else []
    rows += [[Button.url('🟢 অনলাইন — বটে খুলুন', home + (f'?start=f{file_id}' if file_id else '?start=home'))],
            [Button.url('➕ নিজের গ্রুপে যোগ করুন', home + '?startgroup=promo')]]
    seen = set()
    for target in forcejoin.global_targets():
        url = forcejoin.invite_of(target)
        if url and url not in seen:
            seen.add(url)
            rows.append([Button.url('📢 চ্যানেল জয়েন করুন', url)])
    extra = settings.get_str('GROUP_PROMO_URL')
    if extra.startswith(('https://t.me/', 'http://t.me/')):
        rows.append([Button.url('👥 আমাদের গ্রুপ', extra)])
    return rows


async def promote(event, user_id, store_id=None):
    if not settings.get_bool('GROUP_PROMO_ENABLED', True):
        return
    if not user_id:
        return
    raw = (getattr(event, 'raw_text', '') or '').strip().split(maxsplit=1)
    if len(raw) > 1 and raw[1].startswith('s') and raw[1][1:].isdigit():
        store_id = int(raw[1][1:])
    # An anonymous admin posts as the group itself: not a real user, so there is no
    # membership to check and no personal cooldown — only the group gap applies.
    anonymous = user_id == ANON_ADMIN_ID or user_id == event.chat_id or user_id < 0
    privileged = anonymous or access.is_admin(user_id)

    async def gate_for(store):
        if anonymous:
            return {'missing': []}
        return await flow.gate_check(user_id, store)

    gate = await gate_for(store_id)
    if gate['missing']:
        await show_join_prompt(event, user_id, store_id, gate['missing'])
        return
    if not anonymous and db.is_banned(user_id) and not access.is_admin(user_id):
        return
    now = time.time()
    # 1) the same person must not farm videos: own cooldown, told how long to wait
    cooldown = max(10, settings.get_int('GROUP_PROMO_COOLDOWN', 60))
    own = now - _user_last.get((event.chat_id, user_id), 0)
    if own < cooldown and not privileged:
        await _tell_to_wait(event, user_id, int(cooldown - own) + 1,
                            f'⏳ আপনি এইমাত্র ভিডিও পেয়েছেন — {int(cooldown - own) + 1} সেকেন্ড পর আবার চাইতে পারবেন।')
        return
    # 2) the whole group: a short gap between two videos, whoever asks
    gap = max(0, settings.get_int('GROUP_PROMO_GAP', 10))
    key = f'group_promo_time:{event.chat_id}'
    lock = _locks.setdefault(event.chat_id, asyncio.Lock())
    last = float(db.get_meta(key, '0') or 0)
    if lock.locked() or now - last < gap:
        wait = max(1, int(gap - (now - last)) + 1) if not lock.locked() else gap
        await _tell_to_wait(event, user_id, wait,
                            f'⏳ এই মুহূর্তে অন্য একজনের ভিডিও যাচ্ছে — {wait} সেকেন্ড পর আবার চাপুন।')
        return
    async with lock:
        # Random over the whole library (the old newest-100 list kept repeating the
        # same two videos in every group).
        candidates = [row for row in db.public_gallery(limit=5000, kind='Video')
                      if (not store_id or row['store_id'] == store_id)]
        random.shuffle(candidates)
        # A per-store required channel must never be bypassed by a generic /start.
        chosen = None
        blocked = None
        previous = db.get_meta(f'group_promo_file:{event.chat_id}', '')
        candidates.sort(key=lambda r: str(r['id']) == previous)
        for row in candidates:
            store_gate = await gate_for(row['store_id'])
            if not store_gate['missing']:
                chosen = row
                break
            if blocked is None:
                blocked = (row['store_id'], store_gate['missing'])
        if chosen is None and blocked:
            await show_join_prompt(event, user_id, blocked[0], blocked[1])
            return
        if chosen is None:
            await event.respond('🎬 এখন শেয়ার করার মতো ফ্রি ভিডিও নেই। বটে সংগ্রহ দেখুন।', buttons=buttons())
            db.set_meta(key, str(time.time()))
            return
        result = await sender.deliver(event.chat_id, chosen, caption_text='', buttons=buttons(chosen['id']))
        if result.ok:
            db.set_meta(key, str(time.time()))
            _user_last[(event.chat_id, user_id)] = time.time()
            if len(_user_last) > 5000:
                _user_last.clear()
            db.set_meta(f'group_promo_file:{event.chat_id}', str(chosen['id']))
            db.log_event('group_promo', None if anonymous else user_id, chosen['store_id'], chosen['id'], str(event.chat_id))
        else:
            log.warning('Group promotion delivery failed (%s): %s', result.reason, result.detail)
            await event.respond('ভিডিওটি এখন প্রস্তুত করা যায়নি। বটে খুলে চেষ্টা করুন।', buttons=buttons(chosen['id']))


@bot.on(events.NewMessage(incoming=True, func=lambda e: bool(getattr(e, 'is_group', False)),
                          pattern=r'(?i)^/start(?:@([\w]+))?(?:\s+[^\n]*)?\s*$'))
async def group_start(event):
    addressed = event.pattern_match.group(1)
    if addressed and addressed.lower() != runtime.bot_username.lower():
        return
    await promote(event, event.sender_id)
    raise events.StopPropagation


@route('gnext')
async def group_next_video(event, _rest=''):
    """The 🎬 button under a group video: same rules as /start, but too-early taps get a pop-up."""
    if not getattr(event, 'is_group', False):
        await event.answer('এই বাটনটি গ্রুপের জন্য।', alert=True)
        return
    await promote(event, event.sender_id)
    try:
        await event.answer()                    # stop the spinner (no-op if a pop-up was shown)
    except Exception:
        pass


@bot.on(events.ChatAction())
async def added_to_group(event):
    if not getattr(event, 'user_added', False) or runtime.bot_id not in (event.user_ids or []):
        return
    if not getattr(event, 'is_group', False):
        return
    adder = await event.get_added_by()
    await promote(event, getattr(adder, 'id', None))


async def show_join_prompt(event, user_id, store_id, missing):
    rows = []
    for target in missing:
        url = forcejoin.invite_of(target)
        if url:
            rows.append([Button.url('📢 চ্যানেল জয়েন করুন', url)])
    rows.append([Button.inline('✅ জয়েন করেছি — ভিডিও দিন', f'gpj:{user_id}:{store_id or 0}')])
    if runtime.bot_username:
        rows.append([Button.url('🤖 ব্যক্তিগত চ্যাটে খুলুন', f'https://t.me/{runtime.bot_username}?start=home')])
    await event.respond(forcejoin.gate_text(missing), buttons=rows)


@route('gpj:')
async def group_join_retry(event, rest):
    parts = rest.split(':')
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        await event.answer('আবার /start দিন।', alert=True)
        return
    uid, store_id = map(int, parts)
    if uid != event.sender_id:
        await event.answer('নিজে /start দিয়ে শুরু করুন।', alert=True)
        return
    if not getattr(event, 'is_group', False):
        await event.answer('এই বাটনটি গ্রুপের জন্য।', alert=True)
        return
    await event.answer('সদস্যপদ পরীক্ষা করছি…')
    await promote(event, uid, store_id or None)
