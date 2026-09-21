#!/usr/bin/env python3
"""
PHOENIX v1.0 — immortal, order-perfect, copyright-shielded Telegram backups.
Zero media ever stored on your device. Runs fully automated on GitHub Actions.
"""
import asyncio, json, os, random, shutil, subprocess, sys, tempfile, time
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, ServerError

API_ID   = int(os.environ.get("TG_API_ID", "0"))
API_HASH = os.environ.get("TG_API_HASH", "")
SESSION  = os.environ.get("TG_SESSION", "")          # string session (cloud). Empty = local login.
SHIELD_BUDGET_SECONDS = int(os.environ.get("SHIELD_BUDGET", 4 * 3600))  # per-run time cap

MARK = " PHOENIX STATE v1"
client = TelegramClient(StringSession(SESSION) if SESSION else "phoenix_session", API_ID, API_HASH)

def log(*a): print(*a, flush=True)

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

# ---------- state (lives in your own Saved Messages => no local files) ----------
async def load_state():
    async for m in client.iter_messages("me", limit=20):
        if m.text and m.text.startswith(MARK):
            return json.loads(m.text[len(MARK):]), m.id
    return {"pairs": {}, "last": {}}, None

async def save_state(st, mid):
    txt = MARK + json.dumps(st, separators=(",", ":"))
    if mid: await client.edit_message("me", mid, txt)
    else:   await client.send_message("me", txt)

# ---------- protections ----------
async def noforwards(peer, on):
    try: await retry(lambda: client(functions.channels.ToggleNoForwardsRequest(channel=peer, enabled=on)))
    except Exception as e: log("  ! toggle 'restrict saving' manually:", e)

async def forum_on(peer):
    try: await retry(lambda: client(functions.channels.ToggleForumRequest(channel=peer, enabled=True)))
    except Exception: pass

async def topics_map(peer):
    out, off = {}, 0
    while True:
        r = await retry(lambda: client(functions.channels.GetForumTopicsRequest(channel=peer, limit=100, offset_topic=off)))
        for t in r.topics: out[t.id] = t.title
        if len(r.topics) < 100: break
        off = r.topics[-1].id
    return out

async def topic_id_by_title(peer, title):
    for tid, t in (await topics_map(peer)).items():
        if t == title: return tid
    return None

# ---------- core: order-perfect forwarding ----------
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
    titles = await topics_map(src)
    last = st["last"].get(str(src.id), 0)
    log(f"SYNC {src.title} -> {vault.title} (new messages after id {last})")
    await noforwards(src, False)                       # unlock source so forwarding is allowed
    try:
        buffers, new_last = {}, last
        async for m in client.iter_messages(src, min_id=last, reverse=True):   # OLDEST -> NEWEST = exact sequence
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
        await noforwards(src, True)                    # re-lock source group

# ---------- shield: rebirth files with NEW fingerprints (temp disk only) ----------
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
    return False   # archives (zip/rar) stay forward-only; noted honestly

async def shield_pair(vault):
    vault = await client.get_entity(vault)
    orig, shielded = [], set()
    async for m in client.iter_messages(vault):
        if m.service: continue
        if m.fwd_from: orig.append(m)
        elif m.reply_to: shielded.add(m.reply_to.reply_to_msg_id)
    todo = [m for m in reversed(orig) if m.id not in shielded and m.media]   # oldest first
    if not todo: log(f"SHIELD {vault.title}: fully shielded ✅"); return
    t0 = time.time(); done = 0
    tmp = tempfile.mkdtemp(prefix="phoenix_")
    try:
        for m in todo:
            if time.time() - t0 > SHIELD_BUDGET_SECONDS:
                log("  time budget reached, resumes tomorrow."); break
            name = (m.file.name or "media.bin") if m.file else "media.bin"
            inp, out = os.path.join(tmp, "in_" + name), os.path.join(tmp, "out_" + name)
            await retry(lambda: client.download_media(m, inp))               # streams to TEMP only
            if not rebirth(inp, out):
                log(f"  skip (unsupported type): {name}"); continue
            await retry(lambda: client.send_file(vault, out, caption=m.text,
                          reply_to=m.id, supports_streaming=True, silent=True))
            os.remove(inp); os.remove(out); done += 1
            log(f"  🔥 reborn [{done}]: {name}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)        # temp disk wiped. always.
    log(f"SHIELD {vault.title}: {done} files reborn this run")

# ---------- restore: the phoenix button ----------
async def restore(vault_id, new_id):
    vault = await client.get_entity(int(vault_id))
    new = await client.get_entity(int(new_id))
    await forum_on(new)
    shield_map, orig = {}, []
    async for m in client.iter_messages(vault):
        if m.service: continue
        if m.fwd_from: orig.append(m)
        elif m.reply_to: shield_map[m.reply_to.reply_to_msg_id] = m.id
    vtopics = await topics_map(vault)
    await noforwards(vault, False)
    try:
        buffers, cache = {}, {}
        for m in reversed(orig):                                        # oldest -> newest = exact sequence
            tid = topic_of(m); key = str(tid); title = vtopics.get(tid, "General")
            if key not in cache:
                if tid:
                    await retry(lambda: client(functions.channels.CreateForumTopicRequest(
                        channel=new, title=title, random_id=random.getrandbits(64))))
                    cache[key] = await topic_id_by_title(new, title)
                else: cache[key] = None
            buffers.setdefault(key, []).append(shield_map.get(m.id, m.id))
            if len(buffers[key]) >= 100:
                await flush(vault, new, cache[key], buffers.pop(key))
        for k, v in buffers.items():
            if v: await flush(vault, new, cache[k], v)
    finally:
        await noforwards(vault, True)
    log(f"✅ RESTORED into {new.title} — topics & sequence identical.")

# ---------- setup & misc ----------
async def setup(st):
    async for d in client.iter_dialogs():
        ent = d.entity
        if not (d.is_group or d.is_channel) or not getattr(ent, "creator", False): continue
        if str(d.id) in st["pairs"] or d.title.startswith("VAULT"): continue
        r = await retry(lambda: client(functions.channels.CreateChannelRequest(title=f"VAULT · {d.title}", broadcast=True)))
        v = r.chats[0]
        await forum_on(v); await noforwards(v, True)
        st["pairs"][str(d.id)] = v.id
        log(f"🏦 VAULT created: {v.id}  for  {d.title}")

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
        for s, v in st["pairs"].items(): await sync_pair(int(s), v, st)
    elif mode == "--shield":
        for v in st["pairs"].values(): await shield_pair(v)
    elif mode == "--restore":
        await restore(sys.argv[2], sys.argv[3])
    await save_state(st, mid)
    log("done.")

asyncio.run(main())
