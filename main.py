import os, re, sqlite3, logging, time, asyncio, html
from contextlib import closing
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException
from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.constants import ChatMemberStatus, ChatType
from telegram import BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats, BotCommandScopeChatAdministrators
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
DEFAULTS = {'links':'on','welcome':'off','service_cleanup':'on','age_gate':'on','warn_limit':'3','mute_minutes':'60','flood_limit':'6'}
scheduler_task = None


def connect():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('CREATE TABLE IF NOT EXISTS settings(chat_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(chat_id,key))')
    con.execute('CREATE TABLE IF NOT EXISTS words(chat_id INTEGER, word TEXT, PRIMARY KEY(chat_id,word))')
    con.execute('CREATE TABLE IF NOT EXISTS allowed_links(chat_id INTEGER, value TEXT, PRIMARY KEY(chat_id,value))')
    con.execute('CREATE TABLE IF NOT EXISTS warnings(chat_id INTEGER, user_id INTEGER, count INTEGER NOT NULL, PRIMARY KEY(chat_id,user_id))')
    con.execute('CREATE TABLE IF NOT EXISTS bans(chat_id INTEGER, user_id INTEGER, username TEXT, full_name TEXT, created_at INTEGER, by_user INTEGER, PRIMARY KEY(chat_id,user_id))')
    con.execute('CREATE TABLE IF NOT EXISTS scheduled_messages(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, text TEXT NOT NULL, interval_seconds INTEGER NOT NULL, next_at INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_by INTEGER)')
    cols={r[1] for r in con.execute('PRAGMA table_info(scheduled_messages)')}
    if 'photo_file_id' not in cols: con.execute('ALTER TABLE scheduled_messages ADD COLUMN photo_file_id TEXT')
    if 'media_kind' not in cols: con.execute('ALTER TABLE scheduled_messages ADD COLUMN media_kind TEXT')
    con.execute('CREATE TABLE IF NOT EXISTS age_checks(chat_id INTEGER, user_id INTEGER, confirmed_adult INTEGER NOT NULL DEFAULT 0, checked_at INTEGER, PRIMARY KEY(chat_id,user_id))')
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


def allowed_link_values(chat_id):
    with closing(connect()) as con:
        return [r[0] for r in con.execute('SELECT value FROM allowed_links WHERE chat_id=?',(chat_id,))]

def link_is_allowed(text, chat_id):
    low=text.casefold()
    return any(v.casefold() in low for v in allowed_link_values(chat_id))

async def linkizinle(update,context):
    if not await admin_gate(update,context): return
    value=' '.join(context.args).strip().casefold()
    value=re.sub(r'^https?://','',value).strip().rstrip('/')
    if not value:
        await update.effective_message.reply_text('Kullanım: /linkizinle t.me/gayzoneee')
        return
    with closing(connect()) as con:
        con.execute('INSERT OR IGNORE INTO allowed_links VALUES(?,?)',(update.effective_chat.id,value)); con.commit()
    await update.effective_message.reply_text(f'İzinli bağlantı eklendi: {value}')

async def linkizinsil(update,context):
    if not await admin_gate(update,context): return
    value=' '.join(context.args).strip().casefold()
    value=re.sub(r'^https?://','',value).strip().rstrip('/')
    if not value:
        await update.effective_message.reply_text('Kullanım: /linkizinsil t.me/gayzoneee')
        return
    with closing(connect()) as con:
        cur=con.execute('DELETE FROM allowed_links WHERE chat_id=? AND value=?',(update.effective_chat.id,value)); con.commit()
    await update.effective_message.reply_text('İzin kaldırıldı.' if cur.rowcount else 'Bu izinli bağlantı bulunamadı.')


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


async def delete_admin_command(update):
    """Başarılı yönetim işlemlerinde komut mesajını gruptan kaldır."""
    message = update.effective_message
    if message:
        try:
            await message.delete()
        except Exception:
            log.exception('Admin command message could not be deleted')


async def target_reply(update):
    reply = update.effective_message.reply_to_message
    if not reply or not reply.from_user:
        await update.effective_message.reply_text('Hedef kişinin mesajına yanıt vererek komutu kullan.')
        return None
    return reply.from_user


async def start(update, context):
    await update.effective_message.reply_text('💎 GAYZONE Yönetim Botu aktif. Bilgi ve kullanım: /bilgi')


async def bilgi(update, context):
    await update.effective_message.reply_text(
        '💎 GAYZONE BOT · V5\n\nBu bot grup güvenliği, moderasyon ve duyuru yönetimi içindir.\n'
        'Üye komutları: /bilgi · /id · /yas18 (18 yaşından büyük olduğunu doğrulamak için)\n\n'
        'Yönetim komutları grup yöneticilerine özeldir: /panel, /siktir, /unsiktir, /mute, /unmute, /warn, '
        '/warns, /resetwarns, /kick, /duyuru, /fotoduyuru, /duyurular, /duyurudurdur, /duyurubaslat, '
        '/duyuruduzenle, /duyurusil, /kelimeekle, /kelimesil, /kelimeler.\n\n'
        'Güvenlik filtresi metin ve altyazıları inceler; fotoğraf/video içeriğinin kendisini otomatik analiz etmez.'
    )


async def yas18(update, context):
    if not is_group(update):
        await update.effective_message.reply_text('Yaş doğrulaması yalnızca grupta yapılabilir.')
        return
    if not update.effective_user: return
    with closing(connect()) as con:
        con.execute('INSERT INTO age_checks(chat_id,user_id,confirmed_adult,checked_at) VALUES(?,?,1,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET confirmed_adult=1,checked_at=excluded.checked_at', (update.effective_chat.id, update.effective_user.id, int(time.time())))
        con.commit()
    await update.effective_message.reply_text('18 yaşından büyük olduğunu beyan ettin. Yanlış beyan grup kurallarına aykırıdır.')


async def set_command_menus(context, chat_id=None):
    private=[BotCommand('bilgi','Bot hakkında bilgi'),BotCommand('id','Kullanıcı ve grup ID bilgisi'),BotCommand('yas18','18+ yaş beyanı')]
    admin_commands=[BotCommand('panel','Premium yönetim paneli'),BotCommand('siktir','Üyeyi yasakla'),BotCommand('unsiktir','Yasağı kaldır'),BotCommand('warn','Uyar ve kalıcı sustur'),BotCommand('unmute','Susturmayı kaldır'),BotCommand('duyuru','Metin duyurusu oluştur'),BotCommand('fotoduyuru','Fotoğraflı duyuru oluştur'),BotCommand('duyurular','Duyuruları listele'),BotCommand('bilgi','Komut bilgileri')]
    try:
        await context.bot.set_my_commands(private, scope=BotCommandScopeAllPrivateChats())
        await context.bot.set_my_commands([], scope=BotCommandScopeAllGroupChats())
        if chat_id: await context.bot.set_my_commands(admin_commands, scope=BotCommandScopeChatAdministrators(chat_id=chat_id))
    except Exception: log.exception('Telegram command menu setup failed')


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
        'Not: Yönetim komutları yalnızca grup yöneticilerine açıktır. /bilgi herkese açıktır.'
    )


async def panel(update, context):
    if not await admin_gate(update,context): return
    c=update.effective_chat.id
    await set_command_menus(context,c)
    buttons = [
      [InlineKeyboardButton('👥 ÜYE YÖNETİMİ',callback_data='panel:members'),InlineKeyboardButton('🛡 GÜVENLİK',callback_data='panel:safety')],
      [InlineKeyboardButton('📢 DUYURULAR',callback_data='announcements'),InlineKeyboardButton('🚫 BAN LİSTESİ',callback_data='banlist:0')],
      [InlineKeyboardButton('🔗 Link: '+setting(c,'links'),callback_data='toggle:links'),InlineKeyboardButton('👋 Hoş geldin: '+setting(c,'welcome'),callback_data='toggle:welcome')],
      [InlineKeyboardButton('🧹 Servis: '+setting(c,'service_cleanup'),callback_data='toggle:service_cleanup')]
    ]
    await update.effective_message.reply_text('💎 G A Y Z O N E\n━━━━━━━━━━━━━━\n⚙️ PREMIUM YÖNETİM MERKEZİ\n\nYönetim araçlarını aşağıdan seç.',reply_markup=InlineKeyboardMarkup(buttons))


async def panel_callback(update, context):
    q=update.callback_query
    await q.answer()
    chat_id=q.message.chat_id
    if q.data.startswith('age:'):
        # Yaş beyanı yalnızca katılan kullanıcı tarafından verilebilir.
        parts=q.data.split(':')
        if len(parts) != 3 or not parts[2].isdigit():
            await q.answer('Yaş doğrulama bağlantısı geçersiz.', show_alert=True)
            return
        action, expected_user_id = parts[1], int(parts[2])
        if q.from_user.id != expected_user_id:
            await q.answer('Bu yaş doğrulaması başka bir üyeye ait.', show_alert=True)
            return
        if action=='yes':
            perms=ChatPermissions(can_send_messages=True,can_send_audios=True,can_send_documents=True,can_send_photos=True,can_send_videos=True,can_send_video_notes=True,can_send_voice_notes=True,can_send_polls=True,can_send_other_messages=True,can_add_web_page_previews=True)
            try:
                await context.bot.restrict_chat_member(chat_id,q.from_user.id,perms)
                with closing(connect()) as con:
                    con.execute('INSERT INTO age_checks(chat_id,user_id,confirmed_adult,checked_at) VALUES(?,?,1,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET confirmed_adult=1,checked_at=excluded.checked_at',(chat_id,q.from_user.id,int(time.time()))); con.commit()
                await q.edit_message_text('18+ beyanın kaydedildi. GAYZONE kurallarına hoş geldin.')
            except Exception: await q.answer('Yaş doğrulaması tamamlanamadı. Yöneticiyle iletişime geç.',show_alert=True)
        else:
            try:
                await context.bot.ban_chat_member(chat_id,q.from_user.id)
                await q.edit_message_text('18 yaş altı kullanıcılar GAYZONE grubuna katılamaz.')
            except Exception: await q.answer('Çıkarma işlemi başarısız. Yöneticiyle iletişime geç.',show_alert=True)
        return
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
        await q.edit_message_text('📢 DUYURU MERKEZİ\nMetin: /duyuru 30m Mesaj\nFotoğraf: Fotoğrafa yanıt verip /fotoduyuru 1h\nListe: /duyurular\nDüzenle: /duyuruduzenle ID Yeni metin\nDurdur: /duyurudurdur ID')
    elif data=='panel:members':
        await q.edit_message_text('👥 ÜYE YÖNETİMİ\n/siktir (mesaja yanıt) · /unsiktir USER_ID\n/kick (yanıt) · /mute dakika (yanıt) · /unmute (yanıt)\n/warn (yanıt) · /warns (yanıt) · /resetwarns (yanıt)')
    elif data=='panel:safety':
        await q.edit_message_text('🛡 GÜVENLİK\n18+ kuralı: yeni üyeler butonla yaş beyan eder; 18 altı beyanı gruptan çıkarılır.\nYasaklı ifade: /kelimeekle ifade\nListe: /kelimeler\nLink filtresi: /link on veya /link off')


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
        await delete_admin_command(update)
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
        await delete_admin_command(update)
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
        await delete_admin_command(update)
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
        await delete_admin_command(update)
        await audit(context,update.effective_chat.id,'TEMPMUTE' if temporary else 'MUTE',update.effective_user.id,user.id)
    except Exception as e: await update.effective_message.reply_text(f'Susturma başarısız: {e}')


async def unmute(update,context):
    if not await admin_gate(update,context):return
    user=await target_reply(update)
    if not user:return
    perms=ChatPermissions(can_send_messages=True,can_send_audios=True,can_send_documents=True,can_send_photos=True,can_send_videos=True,can_send_video_notes=True,can_send_voice_notes=True,can_send_polls=True,can_send_other_messages=True,can_add_web_page_previews=True)
    try:
        await context.bot.restrict_chat_member(update.effective_chat.id,user.id,perms)
        await delete_admin_command(update)
        await audit(context,update.effective_chat.id,'UNMUTE',update.effective_user.id,user.id)
    except Exception as e: await update.effective_message.reply_text(f'İşlem başarısız: {e}')


async def kick(update,context):
    if not await admin_gate(update,context):return
    user=await target_reply(update)
    if not user:return
    try:
        await context.bot.ban_chat_member(update.effective_chat.id,user.id)
        await context.bot.unban_chat_member(update.effective_chat.id,user.id,only_if_banned=True)
        await delete_admin_command(update)
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
    try:
        await context.bot.restrict_chat_member(cid,user.id,ChatPermissions(can_send_messages=False))
        await delete_admin_command(update)
    except Exception as e: await update.effective_message.reply_text(f'Uyarı kaydedildi ancak susturma başarısız: {e}')
    await audit(context,cid,'WARN_PERMANENT_MUTE',update.effective_user.id,user.id)


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
    if not await admin_gate(update,context): return
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
    match=re.fullmatch(r'(\d+)(s|sn|m|dk|h|sa|d|g|dakika|saat|gun|gün)',raw.lower())
    if not match: return None
    n=int(match.group(1)); unit=match.group(2)
    mult={'s':1,'sn':1,'m':60,'dk':60,'dakika':60,'h':3600,'sa':3600,'saat':3600,'d':86400,'g':86400,'gun':86400,'gün':86400}[unit]
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


async def photo_announce(update,context):
    if not await admin_gate(update,context): return
    m=update.effective_message
    source=m.reply_to_message
    if not source or not source.photo:
        await m.reply_text('Önce fotoğrafı gruba gönder, sonra fotoğraf mesajına yanıt verip /fotoduyuru 30m yaz.'); return
    if not context.args:
        await m.reply_text('Kullanım: fotoğrafa yanıt verip /fotoduyuru 30m'); return
    interval=parse_interval(context.args[0])
    if not interval or interval < 30:
        await m.reply_text('Süre örnekleri: 1m, 30m, 1h, 2h, 1d. En kısa süre 1 dakikadır.'); return
    caption=source.caption or ''
    photo_id=source.photo[-1].file_id
    with closing(connect()) as con:
        cur=con.execute('INSERT INTO scheduled_messages(chat_id,text,interval_seconds,next_at,active,created_by,photo_file_id,media_kind) VALUES(?,?,?,?,1,?,?,?)',(update.effective_chat.id,caption,interval,int(time.time())+interval,update.effective_user.id,photo_id,'photo')); sid=cur.lastrowid; con.commit()
    await m.reply_text(f'📸 Fotoğraflı duyuru #{sid} oluşturuldu. Her {context.args[0]} gönderilecek.')


async def announcement_edit(update,context):
    if not await admin_gate(update,context): return
    if len(context.args)<2 or not context.args[0].isdigit():
        await update.effective_message.reply_text('Kullanım: /duyuruduzenle ID Yeni duyuru metni'); return
    sid=int(context.args[0]); new=' '.join(context.args[1:])
    with closing(connect()) as con:
        cur=con.execute('UPDATE scheduled_messages SET text=? WHERE id=? AND chat_id=?',(new,sid,update.effective_chat.id)); con.commit()
    await update.effective_message.reply_text('Duyuru metni güncellendi.' if cur.rowcount else 'Duyuru bulunamadı.')


async def announcements(update,context):
    if not await admin_gate(update,context):return
    with closing(connect()) as con: rows=con.execute('SELECT id,text,interval_seconds,active,photo_file_id FROM scheduled_messages WHERE chat_id=? ORDER BY id DESC',(update.effective_chat.id,)).fetchall()
    if not rows: await update.effective_message.reply_text('Henüz duyuru yok. Örnek: /duyuru 20m Mesaj');return
    lines=[]
    for sid,text,secs,active,photo_id in rows:
        unit=f'{secs}s' if secs<60 else (f'{secs//60}dk' if secs<3600 else (f'{secs//3600}sa' if secs<86400 else f'{secs//86400}g'))
        lines.append(f'#{sid} | {"Aktif" if active else "Durdu"} | {unit} | {"📸 Fotoğraf" if photo_id else text[:70]}')
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
                rows=con.execute('SELECT id,chat_id,text,interval_seconds,photo_file_id FROM scheduled_messages WHERE active=1 AND next_at<=?',(now,)).fetchall()
                expired=con.execute('SELECT chat_id,user_id FROM temp_bans WHERE expires_at<=?',(now,)).fetchall()
            for cid,uid in expired:
                try:
                    await bot_app.bot.unban_chat_member(cid,uid,only_if_banned=True)
                    with closing(connect()) as con:
                        con.execute('DELETE FROM temp_bans WHERE chat_id=? AND user_id=?',(cid,uid))
                        con.execute('DELETE FROM bans WHERE chat_id=? AND user_id=?',(cid,uid)); con.commit()
                except Exception: log.exception('Temporary ban expiry failed chat=%s user=%s',cid,uid)
            for sid,cid,msg_text,interval,photo_id in rows:
                try:
                    if photo_id: await bot_app.bot.send_photo(cid,photo_id,caption=msg_text or None)
                    else: await bot_app.bot.send_message(cid,msg_text)
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
    match=next((w for w in words_for(cid) if w in text),None)
    # Çocuk güvenliği ve cinsel içerik takası: bağlamlı eşleşme; CP tek başına engellenmez.
    safety_patterns=[
      r'\bcp\b.{0,40}\b(takas|porno|porn|video|çocuk|cocuk|istismar)\b',
      r'\b(takas|porno|porn|video|çocuk|cocuk|istismar)\b.{0,40}\bcp\b',
      r'\b(çocuk|cocuk)\s*(pornosu|pornografisi|istismarı|istismari)\b',
      r'\b(porno|porn|pornografi)\s*(takas|paylaşım|paylasim|video)\b',
      r'\b(video|videolar)\s*(takas|değişimi|degisimi)\b',
      r'\b(takas|değiş tokuş|degis tokus)\s*(video|porno|porn|cinsel içerik|cinsel icerik)\b',
      r'\b(çocuk|cocuk).{0,35}(cinsel içerik|cinsel icerik|çıplak|ciplak|porno|pornografi)\b',
      r'\b(cinsel içerik|cinsel icerik|çıplak|ciplak|porno|pornografi).{0,35}(çocuk|cocuk)\b'
    ]
    safety_match=any(re.search(p,text,re.I) for p in safety_patterns)
    # Dış bağlantı, Telegram grup/kanal daveti ve profil bağlantıları.
    # @kullanıcı adı tek başına engellenmez; grup reklamı/üye toplama bağlamı aranır.
    recruit_context = bool(re.search(r'(?i)(grubumuza|kanalımıza|kanalımıza|gruba katıl|kanala katıl|üy(e|eleri) (çek|topla|davet)|uye (cek|topla|davet)|katılın|katilin|gelin|reklam|tanıtım|tanitim|takip edin|profilim|dm|özelden yaz)', text))
    has_username = bool(re.search(r'(?<![\w])@[A-Za-z0-9_]{5,32}\b', text)) and recruit_context
    has_telegram_profile = bool(re.search(r'(?i)(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/[A-Za-z0-9_]{5,32}', text))
    has_invite = bool(re.search(r'(?i)(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/|addlist/)', text))
    has_external_link = bool(LINK_RE.search(text))
    has_link = setting(cid,'links')=='on' and (has_external_link or has_username or has_telegram_profile or has_invite) and not link_is_allowed(text,cid)
    recruitment_ad = recruit_context and (has_username or has_telegram_profile or has_invite or has_external_link)
    if match or safety_match or has_link or recruitment_ad:
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
                reason='çocuk güvenliği/cinsel içerik takası filtresi' if safety_match else ('yasaklı kelime' if match else ('grup/profil reklamı veya üye toplama' if recruitment_ad else 'izinsiz bağlantı'))
                await context.bot.send_message(cid,f'{m.from_user.mention_html()} mesajı silindi ({reason}). Uyarı: {count}/{limit}.',parse_mode='HTML')
            await audit(context,cid,'AUTO_MOD',None,m.from_user.id)
        except Exception:log.exception('Moderation action failed')

async def service_messages(update,context):
    if not is_group(update):return
    m=update.effective_message
    if m and (m.new_chat_members or m.left_chat_member):
        try:
            if m.new_chat_members:
                for u in m.new_chat_members:
                    if setting(update.effective_chat.id,'age_gate')=='on':
                        try:
                            await context.bot.restrict_chat_member(update.effective_chat.id,u.id,ChatPermissions(can_send_messages=False))
                            kb=InlineKeyboardMarkup([[InlineKeyboardButton('18 yaşından büyüğüm',callback_data=f'age:yes:{u.id}')],[InlineKeyboardButton('18 yaşından küçüğüm',callback_data=f'age:no:{u.id}')]])
                            await context.bot.send_message(update.effective_chat.id,f'🔞 {u.mention_html()} — GAYZONE yalnızca 18 yaş ve üzeri içindir. Devam etmek için yaşını beyan et:',parse_mode='HTML',reply_markup=kb)
                        except Exception: log.exception('Age gate failed for user=%s',u.id)
                    elif setting(update.effective_chat.id,'welcome')=='on':
                        await context.bot.send_message(update.effective_chat.id,f'Hoş geldin, {u.full_name}! GAYZONE grubuna hoş geldin.')
            # Telegram'ın katıldı/ayrıldı/çıkarıldı servis mesajları grup sohbetinde görünmesin.
            # Botun yönetici olması ve mesaj silme izni bulunması gerekir.
            if setting(update.effective_chat.id,'service_cleanup')=='on' or m.left_chat_member:
                await m.delete()
        except Exception:log.exception('Service message handling failed')

handlers=[
 CommandHandler('start',start),CommandHandler(['bilgi','yardim','help'],bilgi),CommandHandler('yas18',yas18),CommandHandler('panel',panel),
 CommandHandler(['siktir','ban','banla'],ban_cmd),CommandHandler(['unsiktir','unban'],unban),CommandHandler('banlist',banlist),
 CommandHandler('mute',mute_user),CommandHandler('tempmute',lambda u,c: mute_user(u,c,True)),CommandHandler('unmute',unmute),
 CommandHandler('tempban',tempban),CommandHandler('kick',kick),CommandHandler('warn',warn),CommandHandler(['warns','warnings'],warnings),CommandHandler('unwarn',unwarn),CommandHandler(['resetwarns','warnreset'],warnreset),
 CommandHandler('info',info),CommandHandler('id',id_cmd),CommandHandler(['del','temizle'],delete_cmd),CommandHandler('purge',purge),
 CommandHandler('pin',pin),CommandHandler('unpin',unpin),CommandHandler('unpinall',unpinall),CommandHandler('pinned',pinned),
 CommandHandler('duyuru',announce),CommandHandler('fotoduyuru',photo_announce),CommandHandler('duyuruduzenle',announcement_edit),CommandHandler('duyurular',announcements),CommandHandler('duyurudurdur',lambda u,c: announcement_action(u,c,'durdur')),
 CommandHandler('duyurubaslat',lambda u,c: announcement_action(u,c,'baslat')),CommandHandler('duyurusil',announcement_delete),
 CommandHandler('link',link_cmd),CommandHandler('linkizinle',linkizinle),CommandHandler('linkizinsil',linkizinsil),CommandHandler('hosgeldin',welcome_cmd),CommandHandler('servismesaj',service_cmd),
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
    await set_command_menus(type('Ctx',(),{'bot':bot_app.bot})())
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
