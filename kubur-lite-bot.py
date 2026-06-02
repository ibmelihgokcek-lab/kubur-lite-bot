#!/usr/bin/env python3
# KUBUR LITE - SADECE RAVEN & BALANCE, TEK SESSION

import os
import re
import asyncio
import random
import json
import logging
from datetime import datetime
from telethon import TelegramClient, events, errors

# Basit logging
logging.basicConfig(level=logging.INFO)

# ---------------------------- KONFIG ----------------------------
API_ID = int(os.environ.get("API_ID", 31924590))
API_HASH = os.environ.get("API_HASH", '5c22bfad88d4ef054ac7eab21ecaf1b5')
SESSION_NAME = 'kubur_oturum_v3'

RAVEN_BOT = '@RavenB2_BOT'
BALANCE_BOT = '@balancechkbot'
TARGET_GROUP_ID = int(os.environ.get("TARGET_GROUP_ID", -1003979220547))

# Basit mizah listesi (opsiyonel)
MIZAH = [
    "🐺 Kurtlukta düşeni yemek kanundur, dayıya geçmiş olsun.",
    "🔪 Azdan az, dayının hesaptan çok gider qral!",
    "🚀 Bize de mi lolo dayı? Approved geldi valla!",
    "💰 Savaş abi bizim buralarda bakiye bitmez!",
]

# ---------------------------- GLOBAL ----------------------------
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
user_tasks = {}          # kullanıcı -> beklenecek işlem
pending_cards = {}       # kullanıcı -> toplanan kart listesi
pending_duplicate = {}   # kullanıcı -> mükerrer kart kontrolü
balance_queue = asyncio.Queue()   # balance işlemleri kuyruğu
raven_queue = asyncio.Queue()     # raven işlemleri kuyruğu
balance_busy = False
raven_busy = False

# Debounce / birleştirme: büyük yapıştırmaları birkaç kısa mesaj halinde gelen
# parçaları birleştirip kullanıcıya tek seferde seçim sormak için.
user_timers = {}        # uid -> asyncio.Task
DEBOUNCE_SECONDS = 3

# Basit hafıza (daha önce sorgulanan kartlar)
MEMORY_FILE = "kubur_memory.json"

def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_memory(mem):
    with open(MEMORY_FILE, 'w') as f:
        json.dump(mem, f, indent=2)

memory = load_memory()

# ---------------------------- YARDIMCILAR ----------------------------
def card_parser(text):
    """Mesaj içindeki tüm kartları algıla ve liste döndür: num|MM|YYYY|CVV"""
    pattern = r'(\d{15,16})[\s:/|\\,.-]+(\d{2})[\s:/|\\,.-]+(\d{2,4})(?:[\s:/|\\,.-]+(\d{3,4}))?'
    matches = re.findall(pattern, text)
    results = []
    for match in matches:
        num, month, year, cvv = match
        year = "20" + year[-2:]
        parsed = f"{num}|{month}|{year}|{cvv}" if cvv else f"{num}|{month}|{year}"
        results.append(parsed)
    return results if results else None

async def bin_sorgula(kart_no):
    """BIN bilgisi al (basit)"""
    try:
        import urllib.request
        bin_kod = kart_no[:6]
        url = f"https://lookup.binlist.net/{bin_kod}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept-Version': '3'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            banka = data.get('bank', {}).get('name', 'Bilinmiyor')
            ulke = data.get('country', {}).get('alpha2', '??')
            return f"🏦 {banka} | 🌍 {ulke}"
    except:
        return "🏦 Bilinmiyor"

# ---------------------------- BEKLEME FONKSİYONU ----------------------------
async def wait_for_response(worker_client, command, card, target_chat, timeout=90):
    """Mesaj gönder, cevabı bekle"""
    try:
        msg_payload = f"{command} {card}".strip() if command else card
        sent = await worker_client.send_message(target_chat, msg_payload)
        kart_no = card.split('|')[0]  # Kart numarasını ayıkla
        start = datetime.now()
        while (datetime.now() - start).total_seconds() < timeout:
            await asyncio.sleep(2)
            # Daha fazla mesajı kontrol et (20 yerine 50) ve ters sırada ara
            async for msg in worker_client.iter_messages(target_chat, limit=50):
                # Mesaj gönderimizden sonra mı?
                if msg.id <= sent.id:
                    continue
                # Mesajda kart numarası var mı ve metni boş değil mi?
                if msg.text and kart_no in msg.text:
                    return msg.text
        return "TIMEOUT"
    except errors.FloodWaitError as e:
        await asyncio.sleep(e.seconds)
        return "FLOOD"
    except Exception as e:
        return f"ERROR: {str(e)[:50]}"

# ---------------------------- RAVEN İŞLEM ----------------------------
async def process_raven(cards, user):
    """Kartları Raven botuna gönder"""
    results = []
    for card in cards:
        # Mükerrer kontrol
        kart_no = card.split('|')[0]
        if kart_no in memory:
            await client.send_message(TARGET_GROUP_ID, f"♻️ @{user}, `{kart_no}` daha önce sorgulanmış: {memory[kart_no]}")
            continue
        # Sorgula
        res = await wait_for_response(client, ".chk", card, RAVEN_BOT, 90)
        res_text = res if res else "TIMEOUT"
        results.append(f"{card} | {res_text}")
        memory[kart_no] = res_text[:100]
        save_memory(memory)
        # Approved kontrolü
        if "approved" in res_text.lower():
            mizah = random.choice(MIZAH)
            bin_info = await bin_sorgula(kart_no)
            msg = f"{mizah}\n\n💳 `{card}`\nℹ️ {bin_info}\n✅ Approved\n👤 @{user}"
            await client.send_message(TARGET_GROUP_ID, msg)
        await asyncio.sleep(8)  # flood koruma
    # Rapor gönder
    if results:
        report = "\n".join(results)
        if len(report) > 4000:
            report = report[:4000] + "\n...(kesildi)"
        await client.send_message(TARGET_GROUP_ID, f"🏁 @{user} Raven raporu:\n```\n{report}\n```")

# Raven worker - sıra sistemi
async def raven_worker():
    global raven_busy
    while True:
        if raven_busy:
            await asyncio.sleep(1)
            continue
        try:
            task = await asyncio.wait_for(raven_queue.get(), timeout=2)
        except asyncio.TimeoutError:
            continue
        raven_busy = True
        try:
            await process_raven(task['cards'], task['user'])
        except Exception as e:
            await client.send_message(TARGET_GROUP_ID, f"❌ Raven hatası: {e}")
        finally:
            raven_busy = False
            raven_queue.task_done()

# ---------------------------- BALANCE İŞLEM ----------------------------
async def process_balance(cards, user, amount):
    results = []
    for card in cards:
        kart_no = card.split('|')[0]
        if kart_no in memory:
            await client.send_message(TARGET_GROUP_ID, f"♻️ @{user}, `{kart_no}` daha önce sorgulanmış: {memory[kart_no]}")
            continue
        # Balance bot expects a command prefix; send with /check
        res = await wait_for_response(client, "/check", f"{card} {amount}", BALANCE_BOT, 35)
        res_text = res if res else "TIMEOUT"
        results.append(f"{card} | {res_text}")
        memory[kart_no] = res_text[:100]
        save_memory(memory)
        if "balance query successful" in res_text.lower() or "bakiye" in res_text.lower():
            bin_info = await bin_sorgula(kart_no)
            msg = f"💰 Bakiye başarılı\n💳 `{card}`\nℹ️ {bin_info}\n💵 Miktar: {amount}\n👤 @{user}"
            await client.send_message(TARGET_GROUP_ID, msg)
        await asyncio.sleep(12)
    if results:
        report = "\n".join(results)
        if len(report) > 4000:
            report = report[:4000] + "\n...(kesildi)"
        await client.send_message(TARGET_GROUP_ID, f"🏁 @{user} Balance raporu:\n```\n{report}\n```")

# ---------------------------- BALANCE KUYRUĞU ----------------------------
async def balance_worker():
    global balance_busy
    while True:
        if balance_busy:
            await asyncio.sleep(1)
            continue
        try:
            task = await asyncio.wait_for(balance_queue.get(), timeout=2)
        except asyncio.TimeoutError:
            continue
        balance_busy = True
        try:
            await process_balance(task['cards'], task['user'], task['amount'])
        except Exception as e:
            await client.send_message(TARGET_GROUP_ID, f"❌ Balance hatası: {e}")
        finally:
            balance_busy = False
            balance_queue.task_done()


async def debounce_send_prompt(uid, username):
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
        # eğer kullanıcı zaten seçim aşamasındaysa prompt atma
        if uid in user_tasks:
            return
        # eğer kart yoksa bir şey yapma
        cards = pending_cards.get(uid, [])
        if not cards:
            return
        user_tasks[uid] = True
        # promptu hedef grupta atıyoruz, kullanıcı adıyla belirt
        await client.send_message(TARGET_GROUP_ID, f"📦 @{username} {len(cards)} kart toplandı.\n1️⃣ Raven (hızlı sorgu)\n2️⃣ Balance (bakiye)\nSeçiminizi yapın (1/2):")
    except Exception:
        return
    finally:
        # timer görevini temizle
        if uid in user_timers:
            user_timers.pop(uid, None)

# ---------------------------- KART TOPLAMA VE SEÇİM ----------------------------
@client.on(events.NewMessage(chats=TARGET_GROUP_ID))
async def target_handler(event):
    uid = event.sender_id
    text = event.raw_text
    sender = await event.get_sender()
    username = sender.username or str(uid)

    # Komutlar (basit)
    if text.lower() == ".iptal":
        if uid in user_tasks:
            del user_tasks[uid]
        if uid in pending_cards:
            del pending_cards[uid]
        # iptal edildiyse debounce timerı da iptal et
        if uid in user_timers:
            try:
                user_timers[uid].cancel()
            except Exception:
                pass
            user_timers.pop(uid, None)
        await event.reply("🚫 İşleminiz iptal edildi.")
        return

    # Kart parse et (tüm kartları yakala)
    parsed_list = card_parser(text)
    if parsed_list:
        if uid not in pending_cards:
            pending_cards[uid] = []
        for parsed in parsed_list:
            kart_no = parsed.split('|')[0]
            # Daha önce sorgulanmış mı?
            if kart_no in memory:
                await event.reply(f"⚠️ `{kart_no}` daha önce sorgulanmış: {memory[kart_no]}\nDevam etmek için 'devam' yazın, iptal için '.iptal'")
                pending_duplicate[uid] = {'card': parsed, 'username': username}
                continue
            pending_cards[uid].append(parsed)
        # Yeni kart mesajı geldi, varsa önceki debounce'i iptal et ve yenisini başlat
        if uid in user_timers:
            try:
                user_timers[uid].cancel()
            except Exception:
                pass
            user_timers.pop(uid, None)
        # Kısa bir süre (DEBOUNCE_SECONDS) bekleyip gelen tüm parçalar tamamlandığında
        # tek seferde seçim soracak görev başlat
        user_timers[uid] = asyncio.create_task(debounce_send_prompt(uid, username))
        # küçük bir geri bildirim ver
        await event.reply(f"🧩 Parçalar alındı, birleştiriliyor... Lütfen birkaç saniye bekleyin.")
    elif text.lower().startswith("devam") and uid in pending_duplicate:
        job = pending_duplicate.pop(uid)
        if uid not in pending_cards:
            pending_cards[uid] = []
        pending_cards[uid].append(job['card'])
        await event.reply(f"✅ Devam ediliyor. Toplam {len(pending_cards[uid])} kart.")
        # Seçim sor
        user_tasks[uid] = True
        await event.reply(f"📦 {len(pending_cards[uid])} kart toplandı.\n1️⃣ Raven\n2️⃣ Balance\nSeçiminiz:")
    elif text in ["1", "2"] and uid in user_tasks:
        # Seçim yapıldı
        # eğer bir debounce timer'ı varsa iptal et
        if uid in user_timers:
            try:
                user_timers[uid].cancel()
            except Exception:
                pass
            user_timers.pop(uid, None)
        del user_tasks[uid]
        cards = pending_cards.pop(uid, [])
        if not cards:
            await event.reply("❌ Kart bulunamadı.")
            return
        if text == "1":
            # Raven'a kuyruğa al
            task_data = {
                'cards': cards,
                'user': username
            }
            await raven_queue.put(task_data)
            await event.reply("✅ Raven kuyruğuna alındı. Sıranız geldiğinde işleminiz yapılacak.")
        else:  # text == "2"
            # Balance için miktar sor
            user_tasks[uid] = {'cards': cards, 'username': username, 'step': 'wait_amount'}
            await event.reply("💰 Lütfen sorgulamak istediğiniz miktarı yazın (örn: 5000):")
    elif uid in user_tasks and isinstance(user_tasks[uid], dict) and user_tasks[uid].get('step') == 'wait_amount':
        amount = text.strip()
        if amount.isdigit():
            task_data = {
                'cards': user_tasks[uid]['cards'],
                'user': user_tasks[uid]['username'],
                'amount': amount
            }
            del user_tasks[uid]
            await balance_queue.put(task_data)
            await event.reply("✅ Balance kuyruğuna alındı. Sıranız geldiğinde işleminiz yapılacak.")
        else:
            await event.reply("❌ Lütfen sadece sayı girin.")

# ---------------------------- MAIN ----------------------------
async def main():
    try:
        BOT_TOKEN = os.environ.get('BOT_TOKEN')
        if BOT_TOKEN:
            await client.start(bot_token=BOT_TOKEN)
        else:
            await client.start()
        me = await client.get_me()
        logging.info(f"✅ Bot başlatıldı: {me.first_name} (@{me.username})")
        logging.info(f"📌 Hedef grup ID: {TARGET_GROUP_ID}")
        asyncio.create_task(balance_worker())
        asyncio.create_task(raven_worker())
        await client.run_until_disconnected()
    except Exception:
        logging.exception("Başlatma hatası")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Kapatılıyor...")
    finally:
        try:
            if client.is_connected():
                asyncio.run(client.disconnect())
        except Exception:
            pass
