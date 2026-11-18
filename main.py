import os, re, sqlite3, logging, time
from contextlib import closing
from fastapi import FastAPI, Request, HTTPException
from telegram import Update, ChatPermissions
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('gayzone-bot')
TOKEN = os.environ['BOT_TOKEN']
WEBHOOK_SECRET = os.environ['WEBHOOK_SECRET']
BASE_URL = os.environ.get('RENDER_EXTERNAL_URL', '').rstrip('/')
DB_PATH = os.environ.get('DB_PATH', 'gayzone.sqlite3')
LOG_CHAT_ID = os.environ.get('LOG_CHAT_ID')
LOG_CHAT_ID = int(LOG_CHAT_ID) if LOG_CHAT_ID else None

api = FastAPI()
bot_app = Application.builder().token(TOKEN).build()
LINK_RE = re.compile(r'(?i)(https?://|www\.|t\.me/|telegram\.me/|(?:[a-z0-9-]+\.)+(?:com|net|org|ru|io|co|me|info|biz|app|gg)\b)')
# Explicit phrases only. Ambiguous abbreviations such as CP are intentionally not blocked alone.
SEVERE_RE = re.compile(r'(?i)çocuk\s+pornosu|çocuk\s+istismarı|çocuğa\s+yönelik\s+cinsel\s+istismar|çocuk\s+cinsel\s+istismarı|çocuk\s+çıplaklığı\s+(?:satışı|paylaşımı)')
DEFAULTS = {'links':'on','welcome':'off','service_cleanup':'on','warn_limit':'3','mute_minutes':'60','flood_limit':'6'}


def connect():
    con = sqlite3.connect(DB_PATH)
    con.execute('CREATE TABLE IF NOT EXISTS settings(chat_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(chat_id,key))')
    con.execute('CREATE TABLE IF NOT EXISTS words(chat_id INTEGER, word TEXT, PRIMARY KEY(chat_id,word))')
    con.execute('CREATE TABLE IF NOT EXISTS warnings(chat_id INTEGER, user_id INTEGER, count INTEGER NOT NULL, PRIMARY KEY(chat_id,user_id))')
    con.commit()
    return con


def setting(chat_id, key):
    with closing(connect()) as con:
        row = con.execute('SELECT value FROM settings WHERE chat_id=? AND key=?',(chat_id,key)).fetchone()
    return row[0] if row else DEFAULTS.get(key,'off')


def set_setting(chat_id,key,value):
    with closing(connect()) as con:
        con.execute('INSERT INTO settings VALUES(?,?,?) ON CONFLICT(chat_id,key) DO UPDATE SET value=excluded.value',(chat_id,key,value)); con.commit()


def words_for(chat_id):
    with closing(connect()) as con:
        return [r[0] for r in con.execute('SELECT word FROM words WHERE chat_id=?',(chat_id,))]


def is_group(update):
    return bool(update.effective_chat and update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP))


async def admin(update, context):
    if not is_group(update) or not update.effective_user: return False
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False


async def admin_gate(update, context):
    if not is_group(update):
        if update.effective_message: await update.effective_message.reply_text('Bu komut bir grupta kullanılmalı.')
        return False
    if not await admin(update,context):
        await update.effective_message.reply_text('Bu komut yalnızca grup yöneticileri içindir.')
        return False
    return True


async def audit(context, chat_id, message):
    log.info('chat=%s %s', chat_id, message)
    if LOG_CHAT_ID:
        try: await context.bot.send_message(LOG_CHAT_ID, f'Grup {chat_id}: {message}')
        except Exception: log.exception('Audit channel error')


async def target_reply(update):
    reply = update.effective_message.reply_to_message
    if not reply or not reply.from_user:
        await update.effective_message.reply_text('Hedef kişinin mesajına yanıt vererek komutu kullan.')
        return None
    return reply.from_user


async def start(update, context):
    await update.effective_message.reply_text('Çoklu grup yönetim botu. Beni grubuna ekle, yönetici yap ve /yardim yaz.')

async def help_cmd(update, context):
    if not is_group(update): return
    await update.effective_message.reply_text(
        'YÖNETİCİ KOMUTLARI\n'
        '/panel — bu grubun ayarları\n/banla (mesaja yanıt) • /unban USER_ID\n'
        '/mute [dakika] (yanıt) • /unmute (yanıt) • /kick (yanıt)\n'
        '/warn (yanıt) • /warnings (yanıt) • /warnreset (yanıt)\n'
        '/temizle (yanıt) • /link on|off\n'
        '/kelimeekle KELİME • /kelimesil KELİME • /kelimeler\n'
        '/hosgeldin on|off • /servismesaj on|off • /durum'
    )

async def panel(update, context):
    if not await admin_gate(update,context): return
    c=update.effective_chat.id
    await update.effective_message.reply_text(
        f'⚙️ {update.effective_chat.title or "Grup"} ayarları\n'
        f'Link filtresi: {setting(c,"links")}\nHoş geldin: {setting(c,"welcome")}\n'
        f'Sistem mesajı temizliği: {setting(c,"service_cleanup")}\nUyarı sınırı: {setting(c,"warn_limit")}\n'
        'Ayar komutları: /link on|off, /hosgeldin on|off, /servismesaj on|off'
    )

async def banla(update, context):
    if not await admin_gate(update,context): return
    user=await target_reply(update)
    if not user:return
    if user.id==update.effective_user.id:
        await update.effective_message.reply_text('Kendini yasaklayamazsın.');return
    try:
        await context.bot.ban_chat_member(update.effective_chat.id,user.id)
        await update.effective_message.reply_text(f'{user.full_name} gruptan yasaklandı.')
        await audit(context,update.effective_chat.id,f'BAN user={user.id} by={update.effective_user.id}')
    except Exception as e: await update.effective_message.reply_text(f'Ban başarısız: {e}')

async def unban(update, context):
    if not await admin_gate(update,context):return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text('Kullanım: /unban USER_ID');return
    uid=int(context.args[0])
    try:
        await context.bot.unban_chat_member(update.effective_chat.id,uid,only_if_banned=True)
        await update.effective_message.reply_text(f'{uid} kullanıcısının yasağı kaldırıldı.')
        await audit(context,update.effective_chat.id,f'UNBAN user={uid} by={update.effective_user.id}')
    except Exception as e: await update.effective_message.reply_text(f'Unban başarısız: {e}')

async def mute(update,context):
    if not await admin_gate(update,context):return
    user=await target_reply(update)
    if not user:return
    mins=max(1,min(int(context.args[0]),10080)) if context.args and context.args[0].isdigit() else 60
    try:
        await context.bot.restrict_chat_member(update.effective_chat.id,user.id,ChatPermissions(can_send_messages=False),until_date=int(time.time())+mins*60)
        await update.effective_message.reply_text(f'{user.full_name} {mins} dakika susturuldu.')
        await audit(context,update.effective_chat.id,f'MUTE user={user.id} minutes={mins} by={update.effective_user.id}')
    except Exception as e: await update.effective_message.reply_text(f'Susturma başarısız: {e}')

async def unmute(update,context):
    if not await admin_gate(update,context):return
    user=await target_reply(update)
    if not user:return
    perms=ChatPermissions(can_send_messages=True,can_send_audios=True,can_send_documents=True,can_send_photos=True,can_send_videos=True,can_send_video_notes=True,can_send_voice_notes=True,can_send_polls=True,can_send_other_messages=True,can_add_web_page_previews=True)
    try:
        await context.bot.restrict_chat_member(update.effective_chat.id,user.id,perms)
        await update.effective_message.reply_text(f'{user.full_name} susturması kaldırıldı.')
        await audit(context,update.effective_chat.id,f'UNMUTE user={user.id} by={update.effective_user.id}')
    except Exception as e: await update.effective_message.reply_text(f'İşlem başarısız: {e}')

async def kick(update,context):
    if not await admin_gate(update,context):return
    user=await target_reply(update)
    if not user:return
    try:
        await context.bot.ban_chat_member(update.effective_chat.id,user.id)
        await context.bot.unban_chat_member(update.effective_chat.id,user.id,only_if_banned=True)
        await update.effective_message.reply_text(f'{user.full_name} gruptan çıkarıldı; yeniden katılabilir.')
        await audit(context,update.effective_chat.id,f'KICK user={user.id} by={update.effective_user.id}')
    except Exception as e: await update.effective_message.reply_text(f'Çıkarma başarısız: {e}')

async def warn(update,context):
    if not await admin_gate(update,context):return
    user=await target_reply(update)
    if not user:return
    cid=update.effective_chat.id
    with closing(connect()) as con:
        con.execute('INSERT INTO warnings VALUES(?,?,1) ON CONFLICT(chat_id,user_id) DO UPDATE SET count=count+1',(cid,user.id));con.commit()
        count=con.execute('SELECT count FROM warnings WHERE chat_id=? AND user_id=?',(cid,user.id)).fetchone()[0]
    limit=int(setting(cid,'warn_limit'))
    if count>=limit:
        mins=int(setting(cid,'mute_minutes'))
        try:
            await context.bot.restrict_chat_member(cid,user.id,ChatPermissions(can_send_messages=False),until_date=int(time.time())+mins*60)
            await update.effective_message.reply_text(f'{user.full_name}: {count}/{limit} uyarı. {mins} dakika susturuldu.')
        except Exception as e: await update.effective_message.reply_text(f'Uyarı kaydedildi ancak susturma başarısız: {e}')
    else: await update.effective_message.reply_text(f'{user.full_name} uyarıldı ({count}/{limit}).')
    await audit(context,cid,f'WARN user={user.id} count={count} by={update.effective_user.id}')

async def warnings(update,context):
    if not await admin_gate(update,context):return
    user=await target_reply(update)
    if not user:return
    with closing(connect()) as con: row=con.execute('SELECT count FROM warnings WHERE chat_id=? AND user_id=?',(update.effective_chat.id,user.id)).fetchone()
    await update.effective_message.reply_text(f'{user.full_name}: {row[0] if row else 0} uyarı.')

async def warnreset(update,context):
    if not await admin_gate(update,context):return
    user=await target_reply(update)
    if not user:return
    with closing(connect()) as con: con.execute('DELETE FROM warnings WHERE chat_id=? AND user_id=?',(update.effective_chat.id,user.id));con.commit()
    await update.effective_message.reply_text('Üyenin uyarıları sıfırlandı.')

async def temizle(update,context):
    if not await admin_gate(update,context):return
    m=update.effective_message
    if not m.reply_to_message:
        await m.reply_text('Silinecek mesaja yanıt verip /temizle yaz.');return
    try: await m.reply_to_message.delete(); await m.delete()
    except Exception as e: await m.reply_text(f'Mesaj silinemedi: {e}')

async def toggle(update,context,key,label):
    if not await admin_gate(update,context):return
    if not context.args or context.args[0] not in ('on','off'):
        await update.effective_message.reply_text(f'Kullanım: /{label} on veya off');return
    set_setting(update.effective_chat.id,key,context.args[0])
    await update.effective_message.reply_text(f'{label}: {context.args[0]}')

async def link_cmd(u,c): await toggle(u,c,'links','link')
async def welcome_cmd(u,c): await toggle(u,c,'welcome','hosgeldin')
async def service_cmd(u,c): await toggle(u,c,'service_cleanup','servismesaj')

async def kelimeekle(update,context):
    if not await admin_gate(update,context):return
    word=' '.join(context.args).strip().casefold()
    if len(word)<2: await update.effective_message.reply_text('Kullanım: /kelimeekle kelime');return
    with closing(connect()) as con: con.execute('INSERT OR IGNORE INTO words VALUES(?,?)',(update.effective_chat.id,word));con.commit()
    await update.effective_message.reply_text(f'Bu gruba yasaklı kelime eklendi: {word}')

async def kelimesil(update,context):
    if not await admin_gate(update,context):return
    word=' '.join(context.args).strip().casefold()
    with closing(connect()) as con: con.execute('DELETE FROM words WHERE chat_id=? AND word=?',(update.effective_chat.id,word));con.commit()
    await update.effective_message.reply_text(f'Kelime silindi: {word}')

async def kelimeler(update,context):
    if not await admin_gate(update,context):return
    arr=words_for(update.effective_chat.id)
    await update.effective_message.reply_text('Bu grubun yasaklı kelimeleri: '+(', '.join(arr) if arr else 'Liste boş.'))

async def durum(update,context):
    if not await admin_gate(update,context):return
    c=update.effective_chat.id
    await update.effective_message.reply_text(f'Bot aktif: {update.effective_chat.title}\nLink: {setting(c,"links")}\nHoş geldin: {setting(c,"welcome")}\nKelime sayısı: {len(words_for(c))}\nUyarı eşiği: {setting(c,"warn_limit")}')

async def moderate(update,context):
    if not is_group(update):return
    m=update.effective_message
    if not m or not m.from_user or m.from_user.is_bot:return
    try:
        member=await context.bot.get_chat_member(update.effective_chat.id,m.from_user.id)
        if member.status in (ChatMemberStatus.ADMINISTRATOR,ChatMemberStatus.OWNER):return
    except Exception:return
    text=(m.text or m.caption or '').casefold()
    cid=update.effective_chat.id
    # Explicit severe phrases are removed and escalated to admins for review; no automatic ban on ambiguous text.
    if SEVERE_RE.search(text):
        try:
            await m.delete()
            await context.bot.send_message(cid,'⚠️ Ağır güvenlik ihlali şüphesi tespit edildi. Mesaj silindi; yöneticiler lütfen olayı inceleyin.')
            await audit(context,cid,f'SEVERE_CONTENT_REVIEW user={m.from_user.id}; automatic ban not applied')
        except Exception:log.exception('Severe-content handling failed')
        return
    match=next((w for w in words_for(cid) if w in text),None)
    has_link=setting(cid,'links')=='on' and bool(LINK_RE.search(text))
    if match or has_link:
        try:
            await m.delete()
            with closing(connect()) as con:
                con.execute('INSERT INTO warnings VALUES(?,?,1) ON CONFLICT(chat_id,user_id) DO UPDATE SET count=count+1',(cid,m.from_user.id));con.commit()
                count=con.execute('SELECT count FROM warnings WHERE chat_id=? AND user_id=?',(cid,m.from_user.id)).fetchone()[0]
            limit=int(setting(cid,'warn_limit'))
            if count>=limit:
                mins=int(setting(cid,'mute_minutes'))
                await context.bot.restrict_chat_member(cid,m.from_user.id,ChatPermissions(can_send_messages=False),until_date=int(time.time())+mins*60)
                await context.bot.send_message(cid,f'{m.from_user.mention_html()} tekrarlanan ihlal nedeniyle {mins} dakika susturuldu. ({count}/{limit})',parse_mode='HTML')
            else:
                reason='yasaklı kelime' if match else 'izinsiz bağlantı'
                await context.bot.send_message(cid,f'{m.from_user.mention_html()} mesajı silindi ({reason}). Uyarı: {count}/{limit}.',parse_mode='HTML')
            await audit(context,cid,f'AUTO_MOD user={m.from_user.id} warning={count} link={bool(has_link)} word={match or "-"}')
        except Exception:log.exception('Moderation action failed')

async def service_messages(update,context):
    if not is_group(update):return
    if setting(update.effective_chat.id,'service_cleanup')!='on':return
    m=update.effective_message
    if m and (m.new_chat_members or m.left_chat_member):
        try:
            if m.new_chat_members and setting(update.effective_chat.id,'welcome')=='on':
                for u in m.new_chat_members:
                    await context.bot.send_message(update.effective_chat.id,f'Hoş geldin, {u.full_name}! GAYZONE grup kurallarına göz atmayı unutma.')
            await m.delete()
        except Exception:log.exception('Service message handling failed')

handlers=[
 CommandHandler('start',start),CommandHandler(['yardim','help'],help_cmd),CommandHandler('panel',panel),
 CommandHandler('banla',banla),CommandHandler('unban',unban),CommandHandler('mute',mute),CommandHandler('unmute',unmute),CommandHandler('kick',kick),
 CommandHandler('warn',warn),CommandHandler('warnings',warnings),CommandHandler('warnreset',warnreset),CommandHandler('temizle',temizle),
 CommandHandler('link',link_cmd),CommandHandler('hosgeldin',welcome_cmd),CommandHandler('servismesaj',service_cmd),
 CommandHandler('kelimeekle',kelimeekle),CommandHandler('kelimesil',kelimesil),CommandHandler('kelimeler',kelimeler),CommandHandler('durum',durum),
 MessageHandler(filters.StatusUpdate.ALL,service_messages),MessageHandler((filters.TEXT|filters.CAPTION)&~filters.COMMAND,moderate)
]
for h in handlers:bot_app.add_handler(h)

@api.on_event('startup')
async def startup():
    with closing(connect()):pass
    await bot_app.initialize();await bot_app.start()
    if BASE_URL:
        await bot_app.bot.set_webhook(url=f'{BASE_URL}/telegram',secret_token=WEBHOOK_SECRET,allowed_updates=Update.ALL_TYPES)
    else: log.warning('RENDER_EXTERNAL_URL not set; webhook not configured')

@api.post('/telegram')
async def webhook(request:Request):
    if request.headers.get('X-Telegram-Bot-Api-Secret-Token')!=WEBHOOK_SECRET:raise HTTPException(status_code=403,detail='Forbidden')
    update=Update.de_json(await request.json(),bot_app.bot)
    await bot_app.update_queue.put(update)
    return {'ok':True}

@api.get('/health')
async def health():return {'ok':True,'service':'multi-group-telegram-moderation-bot'}

@api.on_event('shutdown')
async def shutdown():
    await bot_app.stop();await bot_app.shutdown()
