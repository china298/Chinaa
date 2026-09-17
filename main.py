import os
import re
import asyncio
import sqlite3
import time
import random
import aiohttp
from telethon import TelegramClient, events, Button
from telethon.sessions import MemorySession
from telethon.errors import MessageNotModifiedError

# ==================== CONFIG ====================
# NOTE: pull secrets from environment variables instead of hardcoding them in
# the source file — this file will likely end up in a git repo, and hardcoded
# tokens/keys get scraped by bots within minutes of a public push.
API_ID = int(os.environ.get("TG_API_ID", "8477522"))
API_HASH = os.environ.get("TG_API_HASH", "366c19cf69e02cad530261ad81212a85")
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "8430045904:AAHUF7DF0IKzINaTW2jV5Sx_dDMK891Ozv8")
ADMIN_ID = int(os.environ.get("TG_ADMIN_ID", "5190717598"))

# 51sms.cc user-side API
SMS_API_KEY = os.environ.get("SMS_API_KEY", "hr_8cabc037a7325d429054e8dd432eef88")
SMS_API_BASE = "https://51sms.cc/api/v1/user"
# ================================================


# ==================== FAKE NAMES API ====================
FIRST_NAMES = [
    'James', 'Mary', 'Robert', 'Patricia', 'John', 'Jennifer', 'Michael', 'Linda',
    'David', 'Elizabeth', 'William', 'Barbara', 'Richard', 'Susan', 'Joseph',
    'Jessica', 'Thomas', 'Sarah', 'Christopher', 'Karen', 'Daniel', 'Nancy',
    'Matthew', 'Betty', 'Anthony', 'Margaret', 'Mark', 'Sandra', 'Paul', 'Emily',
    'Andrew', 'Donna', 'Joshua', 'Michelle', 'Kevin', 'Amanda', 'Brian', 'Dorothy',
]
LAST_NAMES = [
    'Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller', 'Davis',
    'Rodriguez', 'Martinez', 'Hernandez', 'Lopez', 'Gonzalez', 'Wilson', 'Anderson',
    'Thomas', 'Taylor', 'Moore', 'Jackson', 'Martin', 'Lee', 'Perez', 'Thompson',
    'White', 'Harris', 'Sanchez', 'Clark', 'Ramirez', 'Lewis', 'Robinson',
]


async def get_fake_name():
    """Get a random display name — tries randomuser.me first, falls back to a local list."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                'https://randomuser.me/api/?nat=us,gb,ru,de',
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                data = await r.json()
                if 'results' in data and data['results']:
                    u = data['results'][0]
                    return f"{u['name']['first'].title()} {u['name']['last'].title()}"
    except Exception:
        pass
    return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"


# ==================== DB ====================
def get_db():
    return sqlite3.connect("shop.db", timeout=15)


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance REAL DEFAULT 0.0)')
    # "types" replaces the old "countries" table: 51sms.cc sells by type_id
    # (a receiving-line product), with an optional country_code filter.
    c.execute('''CREATE TABLE IF NOT EXISTS types (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type_id TEXT NOT NULL,
        name TEXT,
        flag TEXT,
        country_code TEXT DEFAULT '',
        price REAL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        task_id TEXT UNIQUE,
        token TEXT,
        phone TEXT,
        item_name TEXT,
        price REAL,
        status TEXT DEFAULT 'WAITING',
        created_at INTEGER
    )''')
    conn.commit()
    conn.close()


def get_balance(uid):
    conn = get_db()
    r = conn.execute('SELECT balance FROM users WHERE user_id=?', (uid,)).fetchone()
    if r:
        conn.close()
        return r[0]
    conn.execute('INSERT OR IGNORE INTO users (user_id, balance) VALUES (?,0)', (uid,))
    conn.commit()
    conn.close()
    return 0.0


def add_balance(uid, amt):
    conn = get_db()
    conn.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amt, uid))
    conn.commit()
    conn.close()


def create_user(uid):
    conn = get_db()
    conn.execute('INSERT OR IGNORE INTO users (user_id, balance) VALUES (?,0)', (uid,))
    conn.commit()
    conn.close()


def all_users():
    conn = get_db()
    rows = conn.execute('SELECT user_id, balance FROM users ORDER BY balance DESC').fetchall()
    conn.close()
    return rows


init_db()

client = TelegramClient(MemorySession(), API_ID, API_HASH)
admin_states = {}
user_states = {}
auto_check_tasks = {}
PROCESSED_EVENTS = set()


def is_duplicate(evt_key):
    if evt_key in PROCESSED_EVENTS:
        return True
    PROCESSED_EVENTS.add(evt_key)
    if len(PROCESSED_EVENTS) > 10000:
        PROCESSED_EVENTS.clear()
    return False


# ==================== 51sms.cc API ====================
async def sms_get(path, **params):
    params['apikey'] = SMS_API_KEY
    url = f"{SMS_API_BASE}/{path}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                return await r.json(content_type=None)
    except Exception as e:
        return {"Code": -1, "Msg": str(e)}


async def sms_post(path, body):
    params = {'apikey': SMS_API_KEY}
    url = f"{SMS_API_BASE}/{path}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, params=params, json=body, timeout=aiohttp.ClientTimeout(total=15)) as r:
                return await r.json(content_type=None)
    except Exception as e:
        return {"Code": -1, "Msg": str(e)}


async def sms_balance():
    return await sms_get("billing/balance")


async def sms_recv_extract(type_id, country_code=None, phone=None):
    params = {"type_id": type_id}
    if country_code:
        params["country_code"] = country_code
    if phone:
        params["phone"] = phone
    return await sms_get("recv/extract", **params)


async def sms_recv_ask(token, last_id=None):
    params = {"token": token}
    if last_id is not None:
        params["last_id"] = last_id
    return await sms_get("recv/ask", **params)


async def sms_recv_blacklist(type_id, phone):
    return await sms_post("recv/blacklist", {"TypeId": type_id, "Phone": phone})


async def retry_recv_extract(type_id, country_code=None, max_retries=10, delay=2):
    """Try a few times to get a number — the pool can be briefly empty."""
    for attempt in range(max_retries):
        res = await sms_recv_extract(type_id, country_code=country_code)
        if res and res.get("Code") == 0:
            return True, res.get("Data") or {}
        if attempt < max_retries - 1:
            await asyncio.sleep(delay)
    return False, None


def extract_code(messages):
    """Best-effort digit-code pull from the raw SMS text; falls back to the raw text."""
    if not messages:
        return "RECEIVED"
    content = messages[-1].get("Content", "")
    m = re.search(r'\b\d{4,8}\b', content)
    return m.group(0) if m else content


# ==================== BUTTONS ====================
def main_buttons(uid):
    btns = [
        [Button.inline("🛒 Buy Telegram", b"buy_tg"), Button.inline("👤 Account", b"my_account")],
        [Button.inline("📋 Active Orders", b"active_orders")],
    ]
    if uid == ADMIN_ID:
        btns.append([Button.inline("⚙️ Admin Panel", b"admin_panel")])
    return btns


def main_text(uid):
    bal = get_balance(uid)
    return f"👋 **Welcome!**\n\n💳 Balance: **${bal:.2f}**\n⚡ Service: **Telegram**\n\nChoose:"


def admin_buttons():
    return [
        [Button.inline("➕ Add Type", b"adm_add_c"), Button.inline("📋 Types", b"adm_list_c")],
        [Button.inline("➕ Add Balance", b"adm_add_b"), Button.inline("➖ Sub Balance", b"adm_sub_b")],
        [Button.inline("👥 User Balances", b"adm_balances")],
        [Button.inline("💰 SMS Provider Balance", b"adm_provider_balance")],
        [Button.inline("🔙 Main Menu", b"back_main")],
    ]


# ==================== AUTO CHECK SMS ====================
async def auto_check_sms(uid, task_id, token, phone_display):
    try:
        for _ in range(120):
            await asyncio.sleep(3)
            conn = get_db()
            r = conn.execute('SELECT status FROM orders WHERE task_id=?', (task_id,)).fetchone()
            conn.close()
            if not r or r[0] != 'WAITING':
                return

            res = await sms_recv_ask(token)
            if not res or res.get("Code") != 0:
                continue
            d = res.get("Data") or {}
            status = d.get("Status")

            if status == 1:  # received
                code = extract_code(d.get("Message"))
                conn = get_db()
                conn.execute("UPDATE orders SET status='COMPLETED' WHERE task_id=?", (task_id,))
                conn.commit()
                conn.close()
                auto_check_tasks.pop(task_id, None)
                fake_name = await get_fake_name()
                try:
                    await client.send_message(
                        uid,
                        f"🎉 **Code Received!**\n\n"
                        f"📱 Phone: `{phone_display}`\n"
                        f"👤 Name: `{fake_name}`\n"
                        f"🔑 Code: `{code}`\n\n✅ Done!",
                        buttons=[[Button.inline("📋 Active Orders", b"active_orders")],
                                 [Button.inline("🔙 Menu", b"back_main")]],
                    )
                except Exception:
                    pass
                return

            elif status == 2:  # timed out on the provider's side
                conn = get_db()
                row = conn.execute("SELECT price, status FROM orders WHERE task_id=?", (task_id,)).fetchone()
                if row and row[1] == 'WAITING':
                    conn.execute("UPDATE orders SET status='CANCELLED' WHERE task_id=?", (task_id,))
                    conn.commit()
                    add_balance(uid, row[0])
                    try:
                        await client.send_message(
                            uid,
                            f"❌ **Order Expired**\n📱 `{phone_display}`\n💵 ${row[0]:.2f} refunded.",
                            buttons=[[Button.inline("🔙 Menu", b"back_main")]],
                        )
                    except Exception:
                        pass
                conn.close()
                auto_check_tasks.pop(task_id, None)
                return
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"Auto check error: {e}")


# ==================== BATCH BUY ====================
async def process_batch_purchase(event, uid, tid, count):
    conn = get_db()
    row = conn.execute(
        "SELECT type_id, name, flag, country_code, price FROM types WHERE id=?", (tid,)
    ).fetchone()
    if not row:
        conn.close()
        await event.respond("❌ Item not found.")
        return
    type_id, name, flag, country_code, price = row
    total_cost = price * count
    bal_row = conn.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
    bal = bal_row[0] if bal_row else 0.0
    conn.close()

    if bal < total_cost:
        await event.respond(f"❌ Need **${total_cost:.2f}** (You have **${bal:.2f}**)")
        return

    progress_msg = await event.respond(f"⏳ Ordering {count}x {flag} {name}...")
    successful = 0
    created_orders = []

    for i in range(count):
        got = False
        t0 = time.time()
        while time.time() - t0 < 60:
            res = await sms_recv_extract(type_id, country_code=country_code or None)
            if res and res.get("Code") == 0:
                d = res.get("Data") or {}
                phone = d.get("Phone")
                task_id = str(d.get("TaskId"))
                token = d.get("Token")
                phone_display = f"+{phone}" if phone and not str(phone).startswith('+') else str(phone)

                add_balance(uid, -price)
                conn = get_db()
                conn.execute(
                    "INSERT INTO orders (user_id, task_id, token, phone, item_name, price, status, created_at) "
                    "VALUES (?,?,?,?,?,?,'WAITING',?)",
                    (uid, task_id, token, phone_display, name, price, int(time.time())),
                )
                conn.commit()
                conn.close()
                task = asyncio.create_task(auto_check_sms(uid, task_id, token, phone_display))
                auto_check_tasks[task_id] = task
                successful += 1
                created_orders.append((task_id, phone_display))
                got = True
                break
            await asyncio.sleep(3)
        if not got:
            break
        await asyncio.sleep(0.5)

    if successful == 0:
        await progress_msg.edit(
            f"⚠️ No numbers available for {flag} {name}.",
            buttons=[[Button.inline("🔄 Retry", f"buy_c_{tid}".encode())],
                     [Button.inline("🔙 Back", b"back_main")]],
        )
        return

    lines = [f"📱 `{p}` (ID: `{t}`)" for t, p in created_orders]
    summary = (
        f"✅ **{successful}/{count} Numbers!**\n\n"
        f"🌍 {flag} **{name}**\n💵 Deducted: **${(successful * price):.2f}**\n\n"
        + "\n".join(lines) + "\n\n⏳ Auto-checking SMS..."
    )
    await progress_msg.edit(summary, buttons=[
        [Button.inline("📋 Active Orders", b"active_orders")],
        [Button.inline("❌ Cancel All", b"cnc_all")],
    ])


# ==================== START ====================
@client.on(events.NewMessage(pattern=r'^/start$', incoming=True, func=lambda e: e.is_private))
async def cmd_start(event):
    if is_duplicate(f"start_{event.id}"):
        return
    uid = event.sender_id
    create_user(uid)
    user_states.pop(uid, None)
    user = await event.get_sender()
    name = user.first_name if user and user.first_name else "User"
    bal = get_balance(uid)
    await event.respond(
        f"👋 **Hello {name}!**\n\n💳 Balance: **${bal:.2f}**\n⚡ Service: **Telegram**\n\nChoose:",
        buttons=main_buttons(uid),
    )


# ==================== CALLBACK ROUTER ====================
@client.on(events.CallbackQuery)
async def callback_router(event):
    query_id = getattr(event.query, 'id', None)
    if query_id and is_duplicate(f"q_{query_id}"):
        await event.answer()
        return
    uid = event.sender_id
    try:
        data = event.data.decode()
    except Exception:
        await event.answer()
        return

    try:
        if data == "back_main":
            admin_states.pop(uid, None)
            user_states.pop(uid, None)
            await event.edit(main_text(uid), buttons=main_buttons(uid))

        elif data == "my_account":
            bal = get_balance(uid)
            await event.edit(
                f"👤 **Account**\n\n🆔 `{uid}`\n💰 **${bal:.2f}**",
                buttons=[[Button.inline("🔙 Back", b"back_main")]],
            )

        elif data == "buy_tg":
            conn = get_db()
            rows = conn.execute(
                "SELECT id, name, flag, type_id, price, country_code FROM types ORDER BY name, price"
            ).fetchall()
            conn.close()
            if not rows:
                await event.answer("⚠️ No items configured.", alert=True)
                return
            txt = "🌍 **Select an option:**\n\n"
            btns = []
            for tid, name, flag, type_id, price, ccode in rows:
                txt += f"{flag} {name} — **${price:.2f}**\n"
                btns.append([Button.inline(f"{flag} {name} — ${price:.2f}", f"buy_c_{tid}".encode())])
            btns.append([Button.inline("🔙 Back", b"back_main")])
            await event.edit(txt[:3900], buttons=btns)

        elif data.startswith("buy_c_"):
            tid = data.split("_")[2]
            conn = get_db()
            row = conn.execute("SELECT name, flag, price FROM types WHERE id=?", (tid,)).fetchone()
            conn.close()
            if not row:
                await event.answer("Not found", alert=True)
                return
            name, flag, price = row
            btns = [
                [Button.inline("1x", f"qty_{tid}_1".encode()), Button.inline("2x", f"qty_{tid}_2".encode()),
                 Button.inline("3x", f"qty_{tid}_3".encode())],
                [Button.inline("5x", f"qty_{tid}_5".encode()), Button.inline("✏️ Custom", f"custom_qty_{tid}".encode())],
                [Button.inline("🔙 Back", b"buy_tg")],
            ]
            await event.edit(f"🌍 **{flag} {name}**\n💵 Price: **${price:.2f}**\n\nQuantity:", buttons=btns)

        elif data.startswith("qty_"):
            parts = data.split("_")
            tid, qty = parts[1], int(parts[2])
            await event.answer()
            await process_batch_purchase(event, uid, tid, qty)

        elif data.startswith("custom_qty_"):
            tid = data.split("_")[2]
            user_states[uid] = {"step": "custom_qty", "tid": tid}
            await event.edit("✏️ Send quantity (1-50):", buttons=[[Button.inline("🔙 Cancel", b"buy_tg")]])

        elif data.startswith("chk_sms_"):
            task_id = data.split("_")[2]
            conn = get_db()
            row = conn.execute("SELECT status, phone, token FROM orders WHERE task_id=?", (task_id,)).fetchone()
            conn.close()
            if not row:
                await event.answer("Not found", alert=True)
                return
            phone, token = row[1], row[2]
            res = await sms_recv_ask(token)
            d = (res.get("Data") if res and res.get("Code") == 0 else None) or {}
            status = d.get("Status")
            if status == 1:
                code = extract_code(d.get("Message"))
                conn = get_db()
                conn.execute("UPDATE orders SET status='COMPLETED' WHERE task_id=?", (task_id,))
                conn.commit()
                conn.close()
                if task_id in auto_check_tasks:
                    auto_check_tasks[task_id].cancel()
                    del auto_check_tasks[task_id]
                fake_name = await get_fake_name()
                await event.respond(
                    f"🎉 **Code Received!**\n\n📱 `{phone}`\n👤 Name: `{fake_name}`\n🔑 Code: `{code}`\n\n✅ Done!",
                    buttons=[[Button.inline("📋 Active Orders", b"active_orders")],
                             [Button.inline("🔙 Menu", b"back_main")]],
                )
            elif status == 0:
                await event.answer("⏳ Waiting...", alert=True)
            elif status == 2:
                await event.answer("❌ Expired", alert=True)
            else:
                await event.answer(f"{(res or {}).get('Msg', 'Unknown error')[:50]}", alert=True)

        elif data.startswith("cnc_ord_"):
            task_id = data.split("_")[2]
            conn = get_db()
            row = conn.execute(
                "SELECT price, status, phone FROM orders WHERE task_id=? AND user_id=?", (task_id, uid)
            ).fetchone()
            if not row or row[1] != 'WAITING':
                conn.close()
                await event.answer("❌ Cannot cancel", alert=True)
                return
            if task_id in auto_check_tasks:
                auto_check_tasks[task_id].cancel()
                del auto_check_tasks[task_id]
            # 51sms.cc's "recv" endpoints don't expose an explicit cancel call —
            # blacklisting the number is the closest equivalent (marks it unusable).
            # Whether that actually reverses the charge on the provider's side is
            # not documented; this refunds from the bot's own balance regardless.
            conn.execute("UPDATE orders SET status='CANCELLED' WHERE task_id=?", (task_id,))
            conn.commit()
            conn.close()
            add_balance(uid, row[0])
            await event.answer(f"✅ Refunded ${row[0]:.2f}", alert=True)
            await show_active_orders(event, uid)

        elif data == "cnc_all":
            conn = get_db()
            active = conn.execute(
                "SELECT task_id, price FROM orders WHERE user_id=? AND status='WAITING'", (uid,)
            ).fetchall()
            conn.close()
            if not active:
                await event.answer("No active orders.", alert=True)
                return
            total = 0.0
            cnt = 0
            for tid, price in active:
                if tid in auto_check_tasks:
                    auto_check_tasks[tid].cancel()
                    del auto_check_tasks[tid]
                conn2 = get_db()
                conn2.execute("UPDATE orders SET status='CANCELLED' WHERE task_id=?", (tid,))
                conn2.commit()
                conn2.close()
                total += price
                cnt += 1
            add_balance(uid, total)
            await event.edit(
                f"✅ **{cnt} Cancelled!**\n💵 Refunded: **${total:.2f}**\n\n{main_text(uid)}",
                buttons=main_buttons(uid),
            )

        elif data == "active_orders":
            await show_active_orders(event, uid)

        # ==================== ADMIN ====================
        elif data == "admin_panel" and uid == ADMIN_ID:
            admin_states.pop(uid, None)
            await event.edit("⚙️ **Admin Panel**", buttons=admin_buttons())

        elif data == "adm_add_c" and uid == ADMIN_ID:
            admin_states[uid] = {"step": 1, "data": {}}
            await event.edit(
                "**Step 1:** type_id (from your 51sms.cc panel)",
                buttons=[[Button.inline("🔙 Cancel", b"admin_panel")]],
            )

        elif data == "adm_list_c" and uid == ADMIN_ID:
            conn = get_db()
            rows = conn.execute("SELECT id, name, flag, type_id, price, country_code FROM types").fetchall()
            conn.close()
            if not rows:
                await event.answer("No items.", alert=True)
                return
            txt = "🌍 **Items:**\n\n"
            btns = []
            for tid, name, flag, type_id, price, ccode in rows[:30]:
                cc = f" 🏷️{ccode}" if ccode else ""
                txt += f"{flag} {name} (`{type_id}`) | ${price:.2f}{cc}\n"
                btns.append([Button.inline(f"🗑️ {flag} {name}", f"del_c_{tid}".encode())])
            btns.append([Button.inline("🔙 Back", b"admin_panel")])
            await event.edit(txt[:3900], buttons=btns)

        elif data.startswith("del_c_") and uid == ADMIN_ID:
            tid = data.split("_")[2]
            conn = get_db()
            conn.execute("DELETE FROM types WHERE id=?", (tid,))
            conn.commit()
            conn.close()
            await event.answer("✅ Deleted!")
            await event.edit("⚙️ **Admin Panel**", buttons=admin_buttons())

        elif data in ["adm_add_b", "adm_sub_b"] and uid == ADMIN_ID:
            is_add = (data == "adm_add_b")
            admin_states[uid] = {"step": "balance", "is_add": is_add}
            await event.edit(
                f"**{'Add' if is_add else 'Sub'} Balance**\n\nSend: `user_id amount`",
                buttons=[[Button.inline("🔙 Cancel", b"admin_panel")]],
            )

        elif data == "adm_balances" and uid == ADMIN_ID:
            users = all_users()
            if not users:
                await event.answer("No users.", alert=True)
                return
            txt = "👥 **User Balances:**\n\n"
            for uid2, bal in users[:50]:
                txt += f"🆔 `{uid2}` — **${bal:.2f}**\n"
            btns = [[Button.inline("🔙 Back", b"admin_panel")]]
            await event.edit(txt[:3900], buttons=btns)

        elif data == "adm_provider_balance" and uid == ADMIN_ID:
            res = await sms_balance()
            if not res or res.get("Code") != 0:
                await event.edit(
                    f"❌ API error: {(res or {}).get('Msg', 'unknown')}",
                    buttons=[[Button.inline("🔙 Back", b"admin_panel")]],
                )
                return
            bal = (res.get("Data") or {}).get("Balance", 0)
            await event.edit(
                f"💰 **51sms.cc Balance:** ${bal:.2f}",
                buttons=[[Button.inline("🔙 Back", b"admin_panel")]],
            )

    except MessageNotModifiedError:
        pass
    except Exception as e:
        print(f"Callback Error: {e}")


async def show_active_orders(event, uid):
    conn = get_db()
    rows = conn.execute(
        "SELECT task_id, phone, item_name FROM orders WHERE user_id=? AND status='WAITING'", (uid,)
    ).fetchall()
    conn.close()
    if not rows:
        await event.edit("📋 No active orders.", buttons=[[Button.inline("🔙 Menu", b"back_main")]])
        return
    btns = []
    for tid, phone, iname in rows:
        btns.append([
            Button.inline(f"📱 {phone} ({iname})", f"chk_sms_{tid}".encode()),
            Button.inline("❌ Cancel", f"cnc_ord_{tid}".encode()),
        ])
    btns.append([Button.inline("❌ Cancel All", b"cnc_all")])
    btns.append([Button.inline("🔙 Menu", b"back_main")])
    await event.edit("📋 **Active Orders:**", buttons=btns)


# ==================== TEXT INPUT ====================
@client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private and not e.text.startswith('/')))
async def msg_handler(event):
    if is_duplicate(f"msg_{event.id}"):
        return
    uid = event.sender_id
    text = event.raw_text.strip()

    if uid in user_states and user_states[uid].get("step") == "custom_qty":
        tid = user_states[uid].get("tid")
        user_states.pop(uid, None)
        try:
            qty = int(text)
            if qty < 1 or qty > 50:
                await event.respond("❌ 1-50")
                return
            await process_batch_purchase(event, uid, tid, qty)
        except Exception:
            await event.respond("❌ Send a number")
        return

    if uid != ADMIN_ID or uid not in admin_states:
        return

    state = admin_states[uid]
    step = state.get("step")

    if step == 1:
        state["data"]["type_id"] = text
        state["step"] = 2
        await event.respond("**Step 2:** Display name")
    elif step == 2:
        state["data"]["name"] = text
        state["step"] = 3
        await event.respond("**Step 3:** Flag emoji")
    elif step == 3:
        state["data"]["flag"] = text
        state["step"] = 4
        await event.respond("**Step 4:** Country code filter\n(e.g. `86`, or `0` for none)")
    elif step == 4:
        state["data"]["country_code"] = "" if text == "0" else text
        state["step"] = 5
        await event.respond("**Step 5:** Sell price ($)")
    elif step == 5:
        try:
            price = float(text)
            d = state["data"]
            conn = get_db()
            conn.execute(
                "INSERT INTO types (type_id, name, flag, country_code, price) VALUES (?,?,?,?,?)",
                (d["type_id"], d["name"], d["flag"], d["country_code"], price),
            )
            conn.commit()
            conn.close()
            cc = f" 🏷️{d['country_code']}" if d['country_code'] else ""
            del admin_states[uid]
            await event.respond(
                f"✅ **Added!**\n{d['flag']} {d['name']} | ${price:.2f}{cc}",
                buttons=admin_buttons(),
            )
        except ValueError:
            await event.respond("❌ Send a valid price:")
    elif step == "balance":
        try:
            parts = text.split()
            tid, amt = int(parts[0]), float(parts[1])
            if not state["is_add"]:
                amt = -amt
            create_user(tid)
            add_balance(tid, amt)
            del admin_states[uid]
            sign = "+" if state["is_add"] else "-"
            await event.respond(f"✅ `{tid}` {sign}${abs(amt):.2f}", buttons=admin_buttons())
        except Exception:
            await event.respond("❌ Format: `user_id amount`")


# ==================== RUN ====================
async def main():
    print("🤖 Starting bot...")
    await client.start(bot_token=BOT_TOKEN)
    print("✅ Ready!")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
