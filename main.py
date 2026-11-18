import os, re, sqlite3, logging, time, asyncio, html
from contextlib import closing
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException
from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

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
DEFAULTS = {'links':'on','welcome':'off','service_cleanup':'on','warn_limit':'3','mute_minutes':'60','flood_limit':'6'}
scheduler_task = None


def connect():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('CREATE TABLE IF NOT EXISTS settings(chat_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(chat_id,key))')
    con.execute('CREATE TABLE IF NOT EXISTS words(chat_id INTEGER, word TEXT, PRIMARY KEY(chat_id,word))')
    con.execute('CREATE TABLE IF NOT EXISTS warnings(chat_id INTEGER, user_id INTEGER, count INTEGER NOT NULL, PRIMARY KEY(chat_id,user_id))')
    con.execute('CREATE TABLE IF NOT EXISTS bans(chat_id INTEGER, user_id INTEGER, username TEXT, full_name TEXT, created_at INTEGER, by_user INTEGER, PRIMARY KEY(chat_id,user_id))')
    con.execute('CREATE TABLE IF NOT EXISTS scheduled_messages(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, text TEXT NOT NULL, interval_seconds INTEGER NOT NULL, next_at INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_by INTEGER)')
    con.execute('CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, action TEXT, actor INTEGER, target INTEGER, created_at INTEGER)')
    con.execute('CREATE TABLE IF NOT EXISTS temp_bans(chat_id INTEGER, user_id INTEGER, expires_at INTEGER, PRIMARY KEY(chat_id,user_id))')
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


async def audit(context, chat_id, action, actor=None, target=None):
    log.info('chat=%s action=%s actor=%s target=%s', chat_id, action, actor, target)
    with closing(connect()) as con:
        con.execute('INSERT INTO audit(chat_id,action,actor,target,created_at) VALUES(?,?,?,?,?)',(chat_id,action,actor,target,int(time.time()))); con.commit()
    if LOG_CHAT_ID:
        try: await context.bot.send_message(LOG_CHAT_ID, f'Grup {chat_id}: {action} | yönetici={actor} | hedef={target}')
        except Exception: log.exception('Audit channel error')


async def target_reply(update):
    reply = update.effective_message.reply_to_message
    if not reply or not reply.from_user:
        await update.effective_message.reply_text('Hedef kişinin mesajına yanıt vererek komutu kullan.')
        return None
    return reply.from_user


async def start(update, context):
    await update.effective_message.reply_text('GAYZONE Yönetim Botu aktif. Beni grubuna ekle, yönetici yap ve /yardim yaz.')


async def help_cmd(update, context):
    await update.effective_message.reply_text(
        'GAYZONE BOT KOMUTLARI\n\n'
        'ÜYE: /ban (yanıt) • /banla (yanıt) • /unban ID • /banlist • /kick (yanıt)\n'
        '/mute dakika (yanıt) • /unmute (yanıt) • /tempban dakika (yanıt) • /tempmute dakika (yanıt)\n'
        '/warn (yanıt) • /unwarn (yanıt) • /warns (yanıt) • /resetwarns (yanıt) • /info (yanıt) • /id\n\n'
        'MESAJ: /del (yanıt) • /purge (yanıt) • /pin (yanıt) • /unpin • /unpinall • /pinned\n'
        'DUYURU: /duyuru 20m Mesaj • /duyurular • /duyurudurdur ID • /duyurubaslat ID • /duyurusil ID\n'
        'AYAR: /panel • /link on|off • /hosgeldin on|off • /servismesaj on|off\n'
        'KELİME: /kelimeekle kelime • /kelimesil kelime • /kelimeler • /durum\n\n'
        'Not: Küfür filtresi kapalıdır. Komutlar grup yöneticileri içindir.'
    )


async def panel(update, context):
    if not await admin_gate(update,context): return
    c=update.effective_chat.id
    buttons = [[InlineKeyboardButton('🔗 Link: '+setting(c,'links'),callback_data='toggle:links'), InlineKeyboardButton('👋 Hoş geldin: '+setting(c,'welcome'),callback_data='toggle:welcome')],
               [InlineKeyboardButton('🧹 Servis mesajı: '+setting(c,'service_cleanup'),callback_data='toggle:service_cleanup')],
               [InlineKeyboardButton('📢 Duyurular',callback_data='announcements'),InlineKeyboardButton('🚫 Ban listesi',callback_data='banlist:0')]]
    await update.effective_message.reply_text(f'⚙️ {update.effective_chat.title or "Grup"} yönetim paneli',reply_markup=InlineKeyboardMarkup(buttons))


async def panel_callback(update, context):
    q=update.callback_query
    await q.answer()
    chat_id=q.message.chat_id
    try:
        member=await context.bot.get_chat_member(chat_id,q.from_user.id)
        if member.status not in (ChatMemberStatus.ADMINISTRATOR,ChatMemberStatus.OWNER):
            await q.answer('Bu panel yalnızca yöneticilere açık.',show_alert=True); return
    except Exception:
        await q.answer('Yönetici yetkisi doğrulanamadı.',show_alert=True); return
    data=q.data
    if data.startswith('toggle:'):
        key=data.split(':',1)[1]
        if key not in ('links','welcome','service_cleanup'): return
        val='off' if setting(chat_id,key)=='on' else 'on'; set_setting(chat_id,key,val)
        await panel_from_callback(q,chat_id)
    elif data.startswith('banlist:'):
        page=max(0,int(data.split(':')[1])); await show_banlist(q,chat_id,page)
    elif data.startswith('unban:'):
        uid=int(data.split(':')[1])
        try:
            await context.bot.unban_chat_member(chat_id,uid,only_if_banned=True)
            with closing(connect()) as con: con.execute('DELETE FROM bans WHERE chat_id=? AND user_id=?',(chat_id,uid)); con.commit()
            await audit(context,chat_id,'UNBAN',q.from_user.id,uid)
            await q.edit_message_text(f'{uid} kullanıcısının yasağı kaldırıldı.')
        except Exception as e: await q.answer(f'İşlem başarısız: {e}',show_alert=True)
    elif data=='announcements':
        await q.edit_message_text('Duyuru oluşturmak için grupta şu komutu kullan:\n/duyuru 20m Mesajın\n\nListe: /duyurular')


async def panel_from_callback(q,chat_id):
    buttons=[[InlineKeyboardButton('🔗 Link: '+setting(chat_id,'links'),callback_data='toggle:links'),InlineKeyboardButton('👋 Hoş geldin: '+setting(chat_id,'welcome'),callback_data='toggle:welcome')],
             [InlineKeyboardButton('🧹 Servis mesajı: '+setting(chat_id,'service_cleanup'),callback_data='toggle:service_cleanup')],
             [InlineKeyboardButton('📢 Duyurular',callback_data='announcements'),InlineKeyboardButton('🚫 Ban listesi',callback_data='banlist:0')]]
    await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))


async def show_banlist(q,chat_id,page=0):
    with closing(connect()) as con:
        rows=con.execute('SELECT user_id,full_name,username FROM bans WHERE chat_id=? ORDER BY created_at DESC LIMIT 8 OFFSET ?',(chat_id,page*8)).fetchall()
        total=con.execute('SELECT COUNT(*) FROM bans WHERE chat_id=?',(chat_id,)).fetchone()[0]
    if not rows:
        await q.edit_message_text('Bu botun kayıtlarında yasaklı kullanıcı bulunmuyor.'); return
    buttons=[]
    for uid,name,username in rows:
        label=(name or str(uid))[:28]
        buttons.append([InlineKeyboardButton('🔓 '+label+' · yasağı kaldır',callback_data=f'unban:{uid}')])
    nav=[]
    if page>0: nav.append(InlineKeyboardButton('⬅️',callback_data=f'banlist:{page-1}'))
    if (page+1)*8<total: nav.append(InlineKeyboardButton('➡️',callback_data=f'banlist:{page+1}'))
    if nav: buttons.append(nav)
    await q.edit_message_text(f'🚫 Yasaklı kullanıcılar ({total} kayıt)\nYasağını kaldırmak için ismine dokun:',reply_markup=InlineKeyboardMarkup(buttons))


async def ban_cmd(update, context):
    if not await admin_gate(update,context): return
    user=await target_reply(update)
    if not user:return
    if user.id==update.effective_user.id:
        await update.effective_message.reply_text('Kendini yasaklayamazsın.');return
    try:
        await context.bot.ban_chat_member(update.effective_chat.id,user.id)
        with closing(connect()) as con:
            con.execute('INSERT OR REPLACE INTO bans VALUES(?,?,?,?,?,?)',(update.effective_chat.id,user.id,user.username,user.full_name,int(time.time()),update.effective_user.id));con.commit()
        await update.effective_message.reply_text(f'{user.full_name} gruptan yasaklandı.')
        await audit(context,update.effective_chat.id,'BAN',update.effective_user.id,user.id)
    except Exception as e: await update.effective_message.reply_text(f'Ban başarısız: {e}')


async def banlist(update,context):
    if not await admin_gate(update,context):return
    with closing(connect()) as con:
        rows=con.execute('SELECT user_id,full_name FROM bans WHERE chat_id=? ORDER BY created_at DESC LIMIT 8',(update.effective_chat.id,)).fetchall()
        total=con.execute('SELECT COUNT(*) FROM bans WHERE chat_id=?',(update.effective_chat.id,)).fetchone()[0]
    if not rows:
        await update.effective_message.reply_text('Bu botun kayıtlarında yasaklı kullanıcı yok. Telegram önceden yasaklanan herkesi otomatik listelemeye izin vermez.');return
    buttons=[[InlineKeyboardButton('🔓 '+(name or str(uid))[:28]+' · yasağı kaldır',callback_data=f'unban:{uid}')] for uid,name in rows]
    await update.effective_message.reply_text(f'🚫 Yasaklılar: {total}\nYasağı kaldırmak için dokun:',reply_markup=InlineKeyboardMarkup(buttons))


async def tempban(update,context):
    if not await admin_gate(update,context): return
    user=await target_reply(update)
    if not user: return
    mins=int(context.args[0]) if context.args and context.args[0].isdigit() else 60
    mins=max(1,min(mins,10080))
    try:
        await context.bot.ban_chat_member(update.effective_chat.id,user.id)
        with closing(connect()) as con:
            con.execute('INSERT OR REPLACE INTO bans VALUES(?,?,?,?,?,?)',(update.effective_chat.id,user.id,user.username,user.full_name,int(time.time()),update.effective_user.id))
            con.execute('INSERT OR REPLACE INTO temp_bans VALUES(?,?,?)',(update.effective_chat.id,user.id,int(time.time())+mins*60)); con.commit()
        await update.effective_message.reply_text(f'{user.full_name} {mins} dakika yasaklandı; süre bitince yasağı otomatik kaldırılacak.')
        await audit(context,update.effective_chat.id,'TEMPBAN',update.effective_user.id,user.id)
    except Exception as e: await update.effective_message.reply_text(f'Süreli ban başarısız: {e}')


async def unban(update, context):
    if not await admin_gate(update,context):return
    if not context.args or not context.args[0].lstrip('-').isdigit():
        await update.effective_message.reply_text('Kullanım: /unban USER_ID');return
    uid=int(context.args[0])
    try:
        await context.bot.unban_chat_member(update.effective_chat.id,uid,only_if_banned=True)
        with closing(connect()) as con: con.execute('DELETE FROM bans WHERE chat_id=? AND user_id=?',(update.effective_chat.id,uid));con.commit()
        await update.effective_message.reply_text(f'{uid} kullanıcısının yasağı kaldırıldı.')
        await audit(context,update.effective_chat.id,'UNBAN',update.effective_user.id,uid)
    except Exception as e: await update.effective_message.reply_text(f'Unban başarısız: {e}')


async def mute_user(update,context,temporary=False):
    if not await admin_gate(update,context):return
    user=await target_reply(update)
    if not user:return
    mins=int(context.args[0]) if context.args and context.args[0].isdigit() else 60
    mins=max(1,min(mins,10080))
    try:
        await context.bot.restrict_chat_member(update.effective_chat.id,user.id,ChatPermissions(can_send_messages=False),until_date=int(time.time())+mins*60)
        await update.effective_message.reply_text(f'{user.full_name} {mins} dakika susturuldu.')
        await audit(context,update.effective_chat.id,'TEMPMUTE' if temporary else 'MUTE',update.effective_user.id,user.id)
    except Exception as e: await update.effective_message.reply_text(f'Susturma başarısız: {e}')


async def unmute(update,context):
    if not await admin_gate(update,context):return
    user=await target_reply(update)
    if not user:return
    perms=ChatPermissions(can_send_messages=True,can_send_audios=True,can_send_documents=True,can_send_photos=True,can_send_videos=True,can_send_video_notes=True,can_send_voice_notes=True,can_send_polls=True,can_send_other_messages=True,can_add_web_page_previews=True)
    try:
        await context.bot.restrict_chat_member(update.effective_chat.id,user.id,perms)
        await update.effective_message.reply_text(f'{user.full_name} susturması kaldırıldı.')
        await audit(context,update.effective_chat.id,'UNMUTE',update.effective_user.id,user.id)
    except Exception as e: await update.effective_message.reply_text(f'İşlem başarısız: {e}')


async def kick(update,context):
    if not await admin_gate(update,context):return
    user=await target_reply(update)
    if not user:return
    try:
        await context.bot.ban_chat_member(update.effective_chat.id,user.id)
        await context.bot.unban_chat_member(update.effective_chat.id,user.id,only_if_banned=True)
        await update.effective_message.reply_text(f'{user.full_name} gruptan çıkarıldı; yeniden katılabilir.')
        await audit(context,update.effective_chat.id,'KICK',update.effective_user.id,user.id)
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
    await audit(context,cid,'WARN',update.effective_user.id,user.id)


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


async def unwarn(update,context):
    if not await admin_gate(update,context):return
    user=await target_reply(update)
    if not user:return
    with closing(connect()) as con:
        con.execute('UPDATE warnings SET count=MAX(0,count-1) WHERE chat_id=? AND user_id=?',(update.effective_chat.id,user.id));con.commit()
        row=con.execute('SELECT count FROM warnings WHERE chat_id=? AND user_id=?',(update.effective_chat.id,user.id)).fetchone()
    await update.effective_message.reply_text(f'Uyarı azaltıldı. Kalan: {row[0] if row else 0}.')


async def info(update,context):
    user=await target_reply(update)
    if not user: return
    await update.effective_message.reply_text(f'Ad: {user.full_name}\nKullanıcı adı: @{user.username or "yok"}\nID: `{user.id}`',parse_mode='Markdown')


async def id_cmd(update,context):
    m=update.effective_message
    target=m.reply_to_message.from_user.id if m.reply_to_message and m.reply_to_message.from_user else update.effective_user.id
    await m.reply_text(f'Kullanıcı ID: `{target}`\nGrup ID: `{update.effective_chat.id}`\nMesaj ID: `{m.reply_to_message.message_id if m.reply_to_message else m.message_id}`',parse_mode='Markdown')


async def delete_cmd(update,context):
    if not await admin_gate(update,context):return
    m=update.effective_message
    if not m.reply_to_message: await m.reply_text('Silinecek mesaja yanıt verip /del yaz.');return
    try: await m.reply_to_message.delete(); await m.delete(); await audit(context,update.effective_chat.id,'DELETE',update.effective_user.id)
    except Exception as e: await m.reply_text(f'Mesaj silinemedi: {e}')


async def purge(update,context):
    if not await admin_gate(update,context):return
    m=update.effective_message
    if not m.reply_to_message: await m.reply_text('Toplu silinecek aralığın ilk mesajına yanıt verip /purge yaz.');return
    start=m.reply_to_message.message_id; end=m.message_id
    if end-start>100: await m.reply_text('Tek seferde en fazla 100 mesaj silebilirim.');return
    deleted=0
    for mid in range(start,end+1):
        try: await context.bot.delete_message(update.effective_chat.id,mid); deleted+=1
        except Exception: pass
    await context.bot.send_message(update.effective_chat.id,f'🧹 {deleted} mesaj silindi.')
    await audit(context,update.effective_chat.id,'PURGE',update.effective_user.id)


async def pin(update,context):
    if not await admin_gate(update,context):return
    m=update.effective_message
    if not m.reply_to_message: await m.reply_text('Sabitlemek istediğin mesaja yanıt verip /pin yaz.');return
    try: await m.reply_to_message.pin(); await m.reply_text('Mesaj sabitlendi.'); await audit(context,update.effective_chat.id,'PIN',update.effective_user.id)
    except Exception as e: await m.reply_text(f'Sabitleme başarısız: {e}')


async def unpin(update,context):
    if not await admin_gate(update,context):return
    try:
        if update.effective_message.reply_to_message: await update.effective_message.reply_to_message.unpin()
        else: await context.bot.unpin_chat_message(update.effective_chat.id)
        await update.effective_message.reply_text('Sabitleme kaldırıldı.')
    except Exception as e: await update.effective_message.reply_text(f'İşlem başarısız: {e}')


async def unpinall(update,context):
    if not await admin_gate(update,context):return
    try: await context.bot.unpin_all_chat_messages(update.effective_chat.id); await update.effective_message.reply_text('Tüm sabitli mesajlar kaldırıldı.')
    except Exception as e: await update.effective_message.reply_text(f'İşlem başarısız: {e}')


async def pinned(update,context):
    chat=update.effective_chat
    try:
        m=await context.bot.get_chat(chat.id)
        if m.pinned_message: await update.effective_message.reply_text('Grubun sabitli mesajı:',reply_to_message_id=m.pinned_message.message_id)
        else: await update.effective_message.reply_text('Sabitli mesaj bulunamadı.')
    except Exception as e: await update.effective_message.reply_text(f'Bilgi alınamadı: {e}')


def parse_interval(raw):
    match=re.fullmatch(r'(\d+)(s|sn|m|dk|h|sa|d|g)',raw.lower())
    if not match: return None
    n=int(match.group(1)); unit=match.group(2)
    mult={'s':1,'sn':1,'m':60,'dk':60,'h':3600,'sa':3600,'d':86400,'g':86400}[unit]
    seconds=n*mult
    if seconds<30: return -1
    return min(seconds, 30*86400)


async def announce(update,context):
    if not await admin_gate(update,context):return
    if len(context.args)<2:
        await update.effective_message.reply_text('Kullanım: /duyuru 20m Duyuru metni\nÖrnek: /duyuru 1h GAYZONE\'a hoş geldin!');return
    interval=parse_interval(context.args[0])
    if interval is None:
        await update.effective_message.reply_text('Aralık biçimi: 30s, 5m, 2h veya 1d.');return
    if interval==-1:
        await update.effective_message.reply_text('Telegram spam sınırlarına takılmamak için en kısa aralık 30 saniye.');return
    text=' '.join(context.args[1:]).strip()
    now=int(time.time())
    with closing(connect()) as con:
        cur=con.execute('INSERT INTO scheduled_messages(chat_id,text,interval_seconds,next_at,active,created_by) VALUES(?,?,?,?,1,?)',(update.effective_chat.id,text,interval,now+interval,update.effective_user.id)); sid=cur.lastrowid; con.commit()
    await update.effective_message.reply_text(f'📢 Duyuru #{sid} oluşturuldu. Her {context.args[0]} gönderilecek. Durdurmak: /duyurudurdur {sid}')
    await audit(context,update.effective_chat.id,'ANNOUNCEMENT_CREATE',update.effective_user.id,sid)


async def announcements(update,context):
    if not await admin_gate(update,context):return
    with closing(connect()) as con: rows=con.execute('SELECT id,text,interval_seconds,active FROM scheduled_messages WHERE chat_id=? ORDER BY id DESC',(update.effective_chat.id,)).fetchall()
    if not rows: await update.effective_message.reply_text('Henüz duyuru yok. Örnek: /duyuru 20m Mesaj');return
    lines=[]
    for sid,text,secs,active in rows:
        unit=f'{secs}s' if secs<60 else (f'{secs//60}dk' if secs<3600 else (f'{secs//3600}sa' if secs<86400 else f'{secs//86400}g'))
        lines.append(f'#{sid} | {"Aktif" if active else "Durdu"} | {unit} | {text[:70]}')
    await update.effective_message.reply_text('📢 DUYURULAR\n'+'\n'.join(lines))


async def announcement_action(update,context,action):
    if not await admin_gate(update,context):return
    if not context.args or not context.args[0].isdigit(): await update.effective_message.reply_text(f'Kullanım: /duyuru{action} ID');return
    sid=int(context.args[0]); active=1 if action=='baslat' else 0
    with closing(connect()) as con:
        cur=con.execute('UPDATE scheduled_messages SET active=?,next_at=? WHERE id=? AND chat_id=?',(active,int(time.time())+1 if active else 0,sid,update.effective_chat.id));con.commit()
    await update.effective_message.reply_text('Duyuru güncellendi.' if cur.rowcount else 'Bu grupta böyle bir duyuru bulunamadı.')


async def announcement_delete(update,context):
    if not await admin_gate(update,context):return
    if not context.args or not context.args[0].isdigit(): await update.effective_message.reply_text('Kullanım: /duyurusil ID');return
    with closing(connect()) as con:
        cur=con.execute('DELETE FROM scheduled_messages WHERE id=? AND chat_id=?',(int(context.args[0]),update.effective_chat.id));con.commit()
    await update.effective_message.reply_text('Duyuru silindi.' if cur.rowcount else 'Duyuru bulunamadı.')


async def scheduler_loop():
    while True:
        now=int(time.time())
        try:
            with closing(connect()) as con:
                rows=con.execute('SELECT id,chat_id,text,interval_seconds FROM scheduled_messages WHERE active=1 AND next_at<=?',(now,)).fetchall()
                expired=con.execute('SELECT chat_id,user_id FROM temp_bans WHERE expires_at<=?',(now,)).fetchall()
            for cid,uid in expired:
                try:
                    await bot_app.bot.unban_chat_member(cid,uid,only_if_banned=True)
                    with closing(connect()) as con:
                        con.execute('DELETE FROM temp_bans WHERE chat_id=? AND user_id=?',(cid,uid))
                        con.execute('DELETE FROM bans WHERE chat_id=? AND user_id=?',(cid,uid)); con.commit()
                except Exception: log.exception('Temporary ban expiry failed chat=%s user=%s',cid,uid)
            for sid,cid,msg_text,interval in rows:
                try:
                    await bot_app.bot.send_message(cid,msg_text)
                    with closing(connect()) as con: con.execute('UPDATE scheduled_messages SET next_at=? WHERE id=? AND active=1',(int(time.time())+interval,sid));con.commit()
                except Exception:
                    log.exception('Scheduled message failed id=%s',sid)
                    with closing(connect()) as con: con.execute('UPDATE scheduled_messages SET next_at=? WHERE id=?',(int(time.time())+interval,sid));con.commit()
        except Exception: log.exception('Scheduler loop error')
        await asyncio.sleep(5)


async def toggle(update,context,key,label):
    if not await admin_gate(update,context): return
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
    await update.effective_message.reply_text(f'Bot aktif: {update.effective_chat.title}\nLink: {setting(c,"links")}\nHoş geldin: {setting(c,"welcome")}\nKüfür filtresi: kapalı\nKelime sayısı: {len(words_for(c))}\nUyarı eşiği: {setting(c,"warn_limit")}')

async def moderate(update,context):
    if not is_group(update):return
    m=update.effective_message
    if not m or not m.from_user or m.from_user.is_bot:return
    try:
        member=await context.bot.get_chat_member(update.effective_chat.id,m.from_user.id)
        if member.status in (ChatMemberStatus.ADMINISTRATOR,ChatMemberStatus.OWNER):return
    except Exception:return
    text=(m.text or m.caption or '').casefold(); cid=update.effective_chat.id
    # Küfür filtresi bilerek yok; yalnızca yöneticinin eklediği yasaklı kelimeler ve link ayarı uygulanır.
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
            await audit(context,cid,'AUTO_MOD',None,m.from_user.id)
        except Exception:log.exception('Moderation action failed')

async def service_messages(update,context):
    if not is_group(update):return
    if setting(update.effective_chat.id,'service_cleanup')!='on':return
    m=update.effective_message
    if m and (m.new_chat_members or m.left_chat_member):
        try:
            if m.new_chat_members and setting(update.effective_chat.id,'welcome')=='on':
                for u in m.new_chat_members: await context.bot.send_message(update.effective_chat.id,f'Hoş geldin, {u.full_name}! GAYZONE grubuna hoş geldin.')
            await m.delete()
        except Exception:log.exception('Service message handling failed')

handlers=[
 CommandHandler('start',start),CommandHandler(['yardim','help'],help_cmd),CommandHandler('panel',panel),
 CommandHandler(['ban','banla'],ban_cmd),CommandHandler('banlist',banlist),CommandHandler('unban',unban),
 CommandHandler('mute',mute_user),CommandHandler('tempmute',lambda u,c: mute_user(u,c,True)),CommandHandler('unmute',unmute),
 CommandHandler('tempban',tempban),CommandHandler('kick',kick),CommandHandler('warn',warn),CommandHandler('warns',warnings),CommandHandler('warnings',warnings),CommandHandler('unwarn',unwarn),CommandHandler(['resetwarns','warnreset'],warnreset),
 CommandHandler('info',info),CommandHandler('id',id_cmd),CommandHandler(['del','temizle'],delete_cmd),CommandHandler('purge',purge),
 CommandHandler('pin',pin),CommandHandler('unpin',unpin),CommandHandler('unpinall',unpinall),CommandHandler('pinned',pinned),
 CommandHandler('duyuru',announce),CommandHandler('duyurular',announcements),CommandHandler('duyurudurdur',lambda u,c: announcement_action(u,c,'durdur')),
 CommandHandler('duyurubaslat',lambda u,c: announcement_action(u,c,'baslat')),CommandHandler('duyurusil',announcement_delete),
 CommandHandler('link',link_cmd),CommandHandler('hosgeldin',welcome_cmd),CommandHandler('servismesaj',service_cmd),
 CommandHandler('kelimeekle',kelimeekle),CommandHandler('kelimesil',kelimesil),CommandHandler('kelimeler',kelimeler),CommandHandler('durum',durum),
 CallbackQueryHandler(panel_callback),MessageHandler(filters.StatusUpdate.ALL,service_messages),
 MessageHandler((filters.TEXT|filters.CAPTION)&~filters.COMMAND,moderate)
]
for h in handlers: bot_app.add_handler(h)

@api.on_event('startup')
async def startup():
    global scheduler_task
    with closing(connect()): pass
    await bot_app.initialize(); await bot_app.start()
    if BASE_URL:
        await bot_app.bot.set_webhook(url=f'{BASE_URL}/telegram',secret_token=WEBHOOK_SECRET,allowed_updates=Update.ALL_TYPES)
    else: log.warning('RENDER_EXTERNAL_URL not set; webhook not configured')
    scheduler_task=asyncio.create_task(scheduler_loop())

@api.post('/telegram')
async def webhook(request:Request):
    if request.headers.get('X-Telegram-Bot-Api-Secret-Token')!=WEBHOOK_SECRET: raise HTTPException(status_code=403,detail='Forbidden')
    update=Update.de_json(await request.json(),bot_app.bot)
    await bot_app.update_queue.put(update)
    return {'ok':True}

@api.get('/health')
async def health(): return {'ok':True,'service':'gayzone-moderation-bot'}

@api.on_event('shutdown')
async def shutdown():
    global scheduler_task
    if scheduler_task:
        scheduler_task.cancel()
        try: await scheduler_task
        except asyncio.CancelledError: pass
    await bot_app.stop(); await bot_app.shutdown()
