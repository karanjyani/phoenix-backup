#!/usr/bin/env python3
"""
PHOENIX v2.4 — immortal, order-perfect, copyright-shielded Telegram backups.
Zero media ever stored on your device. Runs fully automated on GitHub Actions.
v2.4: state-save can never crash; works with AND without forum topics.
"""
import asyncio, json, os, random, shutil, subprocess, sys, tempfile, time
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, ServerError

API_ID   = int(os.environ.get("TG_API_ID", "0"))
API_HASH = os.environ.get("TG_API_HASH", "").strip()
SESSION  = os.environ.get("TG_SESSION", "").strip()
SHIELD_BUDGET_SECONDS = int(os.environ.get("SHIELD_BUDGET", 4 * 3600))

MARK = "PHOENIX STATE v1"
RUN_START = time.time()
client = TelegramClient(StringSession(SESSION) if SESSION else "phoenix_session", API_ID, API_HASH)

def log(*a): print(*a, flush=True)
def vault_of(p): return p["vault"] if isinstance(p, dict) else p
def budget_left(): return SHIELD_BUDGET_SECONDS - (time.time() - RUN_START)

async def retry(factory, tries=6):
    for i in range(tries):
        try:
            return await factory()
        except FloodWaitError as e:
            log(f"  flood-wait {e.seconds}s, sleeping..."); await asyncio.sleep(e.seconds + 3)
        except (ServerError, TimeoutError, ConnectionError):
            await asyncio.sleep(5 + i * 5)
    raise RuntimeError("Telegram API retry limit reached")

def topic_of(m):
    r = m.reply_to
    if r and getattr(r, "forum_topic", False):
        return r.reply_to_top_id or r.reply_to_msg_id
    return None

async def load_state():
    async for m in client.iter_messages("me", limit=30):
        t = (m.text or "").strip()
        if t.startswith(MARK):
            return json.loads(t[len(MARK):]), m.id
    return {"pairs": {}, "last": {}}, None

async def save_state(st, mid):
    txt = MARK + json.dumps(st, separators=(",", ":"))
    try:
        if mid: await client.edit_message("me", mid, txt)
        else:   await client.send_message("me", txt)
    except Exception as e:
        log(f"  (state save note: {e})")

async def noforwards(peer, on):
    try: await retry(lambda: client(functions.channels.ToggleNoForwardsRequest(channel=peer, enabled=on)))
    except Exception as e: log("  ! toggle 'restrict saving' manually:", e)

async def forum_on(peer):
    try: await retry(lambda: client(functions.channels.ToggleForumRequest(channel=peer, enabled=True)))
    except Exception: pass

async def topics_map(peer):
    out, off = {}, 0
    while True:
        try:
            r = await retry(lambda: client(functions.channels.GetForumTopicsRequest(channel=peer, limit=100, offset_topic=off)))
        except Exception as e:
            log(f"  (no forum topics in this chat, backing up as single ordered feed: {e})")
            return out
        for t in r.topics: out[t.id] = t.title
        if len(r.topics) < 100: break
        off = r.topics[-1].id
    return out

async def topic_id_by_title(peer, title):
    for tid, t in (await topics_map(peer)).items():
        if t == title: return tid
    return None

async def flush(src, dst, dst_tid, ids):
    for i in range(0, len(ids), 100):
        ch = ids[i:i+100]
        await retry(lambda: client(functions.messages.ForwardMessagesRequest(
            from_peer=src, id=ch, to_peer=dst, top_msg_id=dst_tid,
            random_id=[random.getrandbits(64) for _ in ch],
            drop_author=True, silent=True)))
        await asyncio.sleep(1.2)

async def sync_pair(src, vault, st):
    src, vault = await client.get_entity(src), await client.get_entity(vault)
    await forum_on(vault)
    titles = await topics_map(src)
    last = st["last"].get(str(src.id), 0)
    log(f"SYNC {src.title} -> {vault.title} (new messages after id {last})")
    await noforwards(src, False)
    try:
        buffers, new_last = {}, last
        async for m in client.iter_messages(src, min_id=last, reverse=True):
            new_last = max(new_last, m.id)
            if m.service: continue
            tid = topic_of(m)
            key = str(tid)
            if tid and key not in st.setdefault("topics_" + str(src.id), {}):
                title = titles.get(tid, f"Topic {tid}")
                await retry(lambda: client(functions.channels.CreateForumTopicRequest(
                    channel=vault, title=title, random_id=random.getrandbits(64))))
                st["topics_" + str(src.id)][key] = await topic_id_by_title(vault, title)
            buffers.setdefault(key, []).append(m.id)
            if len(buffers[key]) >= 100:
                await flush(src, vault, st["topics_" + str(src.id)].get(key), buffers.pop(key))
        for k, v in buffers.items():
            if v: await flush(src, vault, st["topics_" + str(src.id)].get(k), v)
        st["last"][str(src.id)] = new_last
    finally:
        await noforwards(src, True)

def rebirth(inp, out):
    ext = os.path.splitext(inp)[1].lower()
    if ext in (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".mp3", ".m4a", ".ogg", ".opus"):
        if shutil.which("ffmpeg"):
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", inp, "-c", "copy",
                            "-metadata", f"comment=phoenix-{random.getrandbits(32)}",
                            "-metadata", "title=phoenix", out], check=True)
            return True
    if ext in (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif"):
        with open(inp, "rb") as a, open(out, "wb") as b:
            shutil.copyfileobj(a, b); b.write(b"\n%phoenix-" + str(random.getrandbits(32)).encode() + b"\n")
        return True
    return False

async def shield_pair(vault):
    vault = await client.get_entity(vault)
    orig, shielded = [], set()
    async for m in client.iter_messages(vault):
        if m.service: continue
        if m.fwd_from: orig.append(m)
        elif m.reply_to: shielded.add(m.reply_to.reply_to_msg_id)
    todo = [m for m in reversed(orig) if m.id not in shielded and m.media]
    if not todo: log(f"SHIELD {vault.title}: fully shielded ✅"); return
    done = 0
    tmp = tempfile.mkdtemp(prefix="phoenix_")
    try:
        for m in todo:
            if budget_left() <= 0:
                log("  time budget reached, resumes next shift."); break
            name = (m.file.name or "media.bin") if m.file else "media.bin"
            inp, out = os.path.join(tmp, "in_" + name), os.path.join(tmp, "out_" + name)
            await retry(lambda: client.download_media(m, inp))
            if not rebirth(inp, out):
                log(f"  skip (unsupported type): {name}"); continue
            await retry(lambda: client.send_file(vault, out, caption=m.text,
                          reply_to=m.id, supports_streaming=True, silent=True))
            try: os.remove(inp); os.remove(out)
            except OSError: pass
            done += 1
            log(f"  🔥 reborn [{done}]: {name}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    log(f"SHIELD {vault.title}: {done} files reborn this run")

async def get_or_create_topic(peer, title):
    if title == "General": return None
    tid = await topic_id_by_title(peer, title)
    if tid: return tid
    await retry(lambda: client(functions.channels.CreateForumTopicRequest(
        channel=peer, title=title, random_id=random.getrandbits(64))))
    return await topic_id_by_title(peer, title)

async def restore(vault_id, new_id):
    vault = await client.get_entity(int(vault_id))
    new = await client.get_entity(int(new_id))
    await forum_on(new)
    await noforwards(new, True)
    shield_map, orig = {}, []
    async for m in client.iter_messages(vault):
        if m.service: continue
        if m.fwd_from: orig.append(m)
        elif m.reply_to: shield_map[m.reply_to.reply_to_msg_id] = m.id
    orig = list(reversed(orig))
    have = 0
    async for m in client.iter_messages(new):
        if not m.service: have += 1
    todo = orig[have:]
    log(f"RESTORE {vault.title} -> {new.title}: {len(todo)} messages to go")
    if not todo:
        log("✅ restore already complete for this group.")
        return True
    vtopics = await topics_map(vault)
    await noforwards(vault, False)
    done = 0; complete = True
    tmp = tempfile.mkdtemp(prefix="phoenix_restore_")
    cache = {}
    try:
        for m in todo:
            if budget_left() <= 0:
                log("  ⏳ budget reached — run again to continue (it resumes).")
                complete = False; break
            title = vtopics.get(topic_of(m), "General")
            if title not in cache: cache[title] = await get_or_create_topic(new, title)
            dst_tid = cache[title]
            src = await client.get_messages(vault, ids=shield_map.get(m.id, m.id))
            if src is None: src = m
            try:
                if src.media and src.file:
                    name = src.file.name or "media.bin"
                    inp = os.path.join(tmp, "in_" + name); out = os.path.join(tmp, "out_" + name)
                    await retry(lambda: client.download_media(src, inp))
                    if rebirth(inp, out):
                        await retry(lambda: client.send_file(new, out, caption=src.text,
                                      reply_to=dst_tid, supports_streaming=True, silent=True))
                    else:
                        await flush(vault, new, dst_tid, [src.id])
                    try: os.remove(inp); os.remove(out)
                    except OSError: pass
                else:
                    await retry(lambda: client.send_message(new, src.text or "",
                                      reply_to=dst_tid, silent=True))
            except Exception as e:
                log(f"  ! trouble on message {m.id}: {e} — placeholder placed to keep order")
                try: await client.send_message(new, f"[media skipped: {m.id}]", reply_to=dst_tid, silent=True)
                except Exception: pass
            done += 1
            if done % 25 == 0: log(f"  ...{done} messages reborn into the new home")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        await noforwards(vault, True)
    log(f"✅ RESTORED {done} messages into {new.title} — brand-new fingerprints, exact order.")
    return complete

async def guard(st):
    for src in list(st["pairs"].keys()):
        p = st["pairs"][src]
        p = p if isinstance(p, dict) else {"vault": p}
        try:
            await client.get_entity(int(p["vault"]))
        except Exception:
            log(f"  vault for {src} is gone — retiring pair."); del st["pairs"][src]; continue
        alive = True
        try:
            e = await client.get_entity(int(src))
        except Exception:
            alive = False
        if alive:
            if isinstance(st["pairs"][src], int):
                st["pairs"][src] = {"vault": p["vault"], "title": e.title,
                                    "kind": "group" if getattr(e, "megagroup", False) else "channel"}
            continue
        title = p.get("title") or f"Restored {src}"
        kind = p.get("kind", "channel")
        new_id = st.setdefault("resurrect", {}).get(src)
        if not new_id:
            r = await retry(lambda: client(functions.channels.CreateChannelRequest(
                title=title, about="private backup", megagroup=(kind == "group"), broadcast=(kind == "channel"))))
            new_id = r.chats[0].id
            st["resurrect"][src] = new_id
            log(f"⚠️ MAIN GONE: {title} — raising phoenix {new_id}")
        try:
            complete = await restore(p["vault"], new_id)
        except Exception as e:
            log(f"  ! resurrection hit a problem: {e} — retrying next shift"); continue
        if complete:
            st["pairs"][str(new_id)] = {"vault": p["vault"], "title": title, "kind": kind}
            del st["pairs"][src]
            st["resurrect"].pop(src, None)
            log(f"🐦 PHOENIX COMPLETE: {title} lives again as {new_id}; vault still hidden.")

async def setup(st):
    existing = {}
    async for d2 in client.iter_dialogs():
        if d2.title and d2.title.startswith("VAULT · "):
            existing.setdefault(d2.title[8:], d2.id)
    async for d in client.iter_dialogs():
        ent = d.entity
        if not (d.is_group or d.is_channel) or not getattr(ent, "creator", False): continue
        if d.is_group and not getattr(ent, "megagroup", False): continue
        if getattr(ent, "deactivated", False): continue
        if str(d.id) in st["pairs"] or d.title.startswith("VAULT"): continue
        vid = existing.get(d.title)
        if vid:
            v = await client.get_entity(vid)
            log(f"🏦 VAULT reused: {v.id}  for  {d.title}")
        else:
            r = await retry(lambda: client(functions.channels.CreateChannelRequest(title=f"VAULT · {d.title}", about="private backup", megagroup=True)))
            v = r.chats[0]
            log(f"🏦 VAULT created: {v.id}  for  {d.title}")
        await forum_on(v); await noforwards(v, True)
        st["pairs"][str(d.id)] = {"vault": v.id, "title": d.title, "kind": "group" if d.is_group else "channel"}

async def main():
    await client.start()
    mode = sys.argv[1] if len(sys.argv) > 1 else "--sync"
    if mode == "--login":
        log("SESSION STRING (paste into GitHub secret TG_SESSION):\n" + client.session.save()); return
    if mode == "--list":
        async for d in client.iter_dialogs(): log(d.id, "|", d.title); return
    st, mid = await load_state()
    if mode == "--setup":
        await setup(st)
    elif mode == "--sync":
        for s, p in list(st["pairs"].items()):
            try:
                e = await client.get_entity(int(s))
                if not (getattr(e, "megagroup", False) or getattr(e, "broadcast", False)):
                    log(f"  retiring old basic-group pair {s}"); del st["pairs"][s]; continue
                await sync_pair(int(s), vault_of(p), st)
            except Exception as ex:
                log(f"  ! skipping pair {s}: {ex}")
    elif mode == "--guard":
        await guard(st)
    elif mode == "--shield":
        for p in list(st["pairs"].values()):
            try: await shield_pair(vault_of(p))
            except Exception as e: log(f"  ! skipping vault {p}: {e}")
    elif mode == "--restore":
        await restore(sys.argv[2], sys.argv[3])
    await save_state(st, mid)
    log("done.")

asyncio.run(main())
