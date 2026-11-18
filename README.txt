GAYZONE MULTI-GROUP MODERATION BOT v5

Bu sürüm çoklu grup desteklidir. Tek bir sabit grup ID'sine bağlı değildir. Botun yönetici olarak eklendiği her grup kendi ayarlarını, kelime listesini ve uyarı sayaçlarını ayrı tutar.

KURULUM ORTAM DEĞİŞKENLERİ:
BOT_TOKEN = BotFather tokenı (GitHub'a koymayın; Render Environment bölümünde saklayın)
WEBHOOK_SECRET = Render tarafından üretilebilir gizli değer
RENDER_EXTERNAL_URL = Render tarafından otomatik sağlanır
DB_PATH = kalıcı disk yolu (Render yaml içinde /var/data/gayzone.sqlite3)
LOG_CHAT_ID = isteğe bağlı özel yönetim kayıt grubunun chat ID'si

TELEGRAM İZİNLERİ:
Botu her gruba ekleyip yönetici yapın. Mesajları silme, kullanıcıları yasaklama ve üyeleri kısıtlama yetkilerini açın. BotFather grup gizliliği (privacy mode) kapatılmalı ki normal mesajları filtreleyebilsin.

KOMUTLAR (yönetici):
/panel, /yardim, /durum
/banla (hedefin mesajına yanıt), /unban USER_ID
/mute [dakika] (yanıt), /unmute (yanıt), /kick (yanıt)
/warn (yanıt), /warnings (yanıt), /warnreset (yanıt)
/temizle (yanıt)
/link on|off
/kelimeekle KELİME, /kelimesil KELİME, /kelimeler
/hosgeldin on|off
/servismesaj on|off

DENGELİ MODERASYON:
Link veya grupça belirlenen yasaklı kelime içeren metin/altyazı silinir. Varsayılan 3 ihlalde 60 dakika susturma uygulanır. Ayarlar ve kelime listeleri her grup için ayrıdır.

AĞIR GÜVENLİK:
Çocuk cinsel istismarıyla ilgili açık ifadeler silinir ve yönetici incelemesine bildirilir. Belirsiz kısaltmalar (ör. CP) tek başına yasak nedeni değildir. Otomatik tespit tek başına kesin kanıt sayılmaz ve otomatik ban uygulanmaz.

SINIRLAMALAR:
Bot resim/video içeriğinin kendisini analiz etmez; metin ve altyazı filtreler. Telegram bot yetkileri, grup gizlilik ayarı ve sunucunun çalışır durumda olması gerekir. Ücretsiz sunucu uykuya geçebilir; kalıcı veritabanı diski barındırma sağlayıcısının ücret ve plan koşullarına bağlıdır. Render hesabında disk/free-plan uyumluluğunu dağıtımdan önce kontrol edin.
