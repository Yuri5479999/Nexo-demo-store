import os, sqlite3, logging, html, asyncio, uuid, re, io, base64, requests, qrcode, datetime, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton as B, InlineKeyboardMarkup as M, InputMediaPhoto
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

load_dotenv()
TOKEN=os.getenv("TENANT_BOT_TOKEN","")
ADMINS={int(x.strip()) for x in os.getenv("ADMIN_IDS",os.getenv("DEMO_OWNER_ID", "")).split(",") if x.strip().isdigit()}
DB=Path(os.getenv("DB_PATH", "/data/nexo_demo_official_fork.db" if Path("/data").is_dir() else str(Path(__file__).with_name("nexo_demo_official_fork.db"))))
ROOT_ADMINS=frozenset(ADMINS)
OWNER_ID=int(os.getenv("OWNER_ID", os.getenv("DEMO_OWNER_ID", "0")) or "0")
MP_TOKEN=""  # DEMO: Mercado Pago indisponível independentemente de variáveis
# E-mail de pagador válido, configurado somente no Railway (não é solicitado no Telegram).
MP_PAYER_EMAIL=os.getenv("MP_PAYER_EMAIL", "").strip()
MP_API="https://api.invalid/blocked-demo"
# Independent Telegram notification service; never put credentials in source.
NEXO_ALERTS_URL=""  # DEMO: anúncios externos desabilitados
NEXO_ALERTS_SECRET=""  # DEMO: anúncios externos desabilitados
logging.basicConfig(level=logging.INFO)
logging.warning("NEXO DEMO: operação demonstrativa; Mercado Pago e serviço de anúncios externos desativados")
logging.getLogger("httpx").setLevel(logging.WARNING)

def conn():
    c=sqlite3.connect(DB)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=10000")
    return c
def init():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,name TEXT,username TEXT,balance INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,description TEXT DEFAULT '',price INTEGER NOT NULL,active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS stock(id INTEGER PRIMARY KEY AUTOINCREMENT,product_id INTEGER NOT NULL,content TEXT NOT NULL,sold INTEGER DEFAULT 0,FOREIGN KEY(product_id) REFERENCES products(id));
        CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,product_id INTEGER,stock_id INTEGER,price INTEGER,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS topups(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,amount INTEGER,status TEXT DEFAULT 'pending',created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS pix_payments(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,kind TEXT NOT NULL,product_id INTEGER,amount INTEGER NOT NULL,email TEXT NOT NULL,mp_id TEXT UNIQUE,status TEXT NOT NULL DEFAULT 'creating',created_at TEXT DEFAULT CURRENT_TIMESTAMP,processed_at TEXT,order_id INTEGER);
        """)
        cols={row[1] for row in c.execute("PRAGMA table_info(products)")}
        if "image_file_id" not in cols:
            c.execute("ALTER TABLE products ADD COLUMN image_file_id TEXT")
        # Arquivamento seguro: mantém estoque, pedidos e pagamentos existentes.
        if "archived" not in cols:
            c.execute("ALTER TABLE products ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        for table, field, definition in (
            ('orders','original_price','INTEGER'),('orders','coupon_code','TEXT'),
            ('pix_payments','coupon_code','TEXT'),('pix_payments','original_price','INTEGER'),
        ):
            if field not in {r[1] for r in c.execute(f'PRAGMA table_info({table})')}:
                c.execute(f'ALTER TABLE {table} ADD COLUMN {field} {definition}')
        c.executescript("""
        CREATE TABLE IF NOT EXISTS coupons(
            code TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('percent','fixed')),
            amount INTEGER NOT NULL, max_uses INTEGER NOT NULL DEFAULT 0,
            used INTEGER NOT NULL DEFAULT 0, expires_at TEXT, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS coupon_uses(
            id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, user_id INTEGER NOT NULL,
            order_id INTEGER NOT NULL UNIQUE, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS restock_alerts(
            user_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id, product_id));
        CREATE TABLE IF NOT EXISTS balance_adjustments(
            id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL, amount INTEGER NOT NULL,
            reason TEXT NOT NULL, order_id INTEGER UNIQUE, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS nexo_restock_outbox(
            event_id TEXT PRIMARY KEY, product_id INTEGER NOT NULL,
            product_name TEXT NOT NULL, quantity_delta INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        """)
        c.executescript("""
        CREATE TABLE IF NOT EXISTS banned_users(user_id INTEGER PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS extra_admins(user_id INTEGER PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS activity(user_id INTEGER PRIMARY KEY, last_active INTEGER NOT NULL,
           last_purchase INTEGER NOT NULL DEFAULT 0, last_reminder INTEGER NOT NULL DEFAULT 0,
           reminders_off INTEGER NOT NULL DEFAULT 0, visit_token INTEGER NOT NULL DEFAULT 0,
           reminded_token INTEGER NOT NULL DEFAULT -1);
        CREATE TABLE IF NOT EXISTS unavailable_interest(user_id INTEGER NOT NULL,product_id INTEGER NOT NULL, created_at INTEGER NOT NULL, notified INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,product_id));
        CREATE TABLE IF NOT EXISTS registration_bonuses(user_id INTEGER PRIMARY KEY, amount INTEGER NOT NULL,
           created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS topup_bonuses(source TEXT NOT NULL, payment_id INTEGER NOT NULL,
           user_id INTEGER NOT NULL, bonus INTEGER NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
           PRIMARY KEY(source,payment_id));
        """)
        # Safe additive migration: preserve all existing queued notifications.
        alert_cols={r[1] for r in c.execute("PRAGMA table_info(nexo_restock_outbox)")}
        if 'stock_current' not in alert_cols:
            c.execute("ALTER TABLE nexo_restock_outbox ADD COLUMN stock_current INTEGER")
        if 'photo_file_id' not in alert_cols:
            c.execute("ALTER TABLE nexo_restock_outbox ADD COLUMN photo_file_id TEXT")
def money(cents): return f"R$ {cents/100:,.2f}".replace(",","X").replace(".",",").replace("X",".")
def parse_amount(raw):
    try:
        d=Decimal(raw.strip().replace("R$","").replace(" ","").replace(",","."))
        if not d.is_finite() or d<=0 or d>100000: return None
        cents=int((d*100).quantize(Decimal("1"),rounding=ROUND_HALF_UP))
        return cents if cents>0 else None
    except (InvalidOperation,ValueError): return None
def coupon_code(raw):
    code=raw.strip().upper()
    return code if re.fullmatch(r'[A-Z0-9_-]{3,24}',code) else None

def coupon_price(c, code, price):
    """Validate and compute discount inside caller's transaction."""
    if not code: return price, None
    row=c.execute('SELECT * FROM coupons WHERE code=?',(code,)).fetchone()
    if not row or not row['active'] or (row['max_uses'] and row['used']>=row['max_uses']):
        raise ValueError('Cupom inválido, desativado ou esgotado.')
    reserved=c.execute("SELECT COUNT(*) FROM pix_payments WHERE coupon_code=? AND status IN ('pending','creating')",(code,)).fetchone()[0]
    if row['max_uses'] and row['used']+reserved>=row['max_uses']:
        raise ValueError('Cupom sem usos disponíveis no momento.')
    if row['expires_at'] and row['expires_at'] < datetime.datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'):
        raise ValueError('Esse cupom expirou.')
    discount=(price*row['amount']//100) if row['kind']=='percent' else row['amount']
    return max(1,price-discount), row

def consume_coupon(c,code,user_id,order_id):
    if not code: return
    changed=c.execute('UPDATE coupons SET used=used+1 WHERE code=? AND active=1 AND (max_uses=0 OR used<max_uses)',(code,)).rowcount
    if not changed: raise ValueError('Cupom esgotado.')
    c.execute('INSERT INTO coupon_uses(code,user_id,order_id) VALUES(?,?,?)',(code,user_id,order_id))

def purchase_buttons(pid):
    return keyboard([[('💳 Comprar com saldo',f'buy:{pid}')],
                     [('⚡ Comprar com Pix',f'pixbuy:{pid}')],
                     [('🎟️ Aplicar cupom',f'coupon_apply:{pid}')],
                     [('↩️ Voltar','products')]])

def parse_stock_units(raw):
    """Cada bloco de texto livre é uma unidade; --- separa unidades."""
    blocks=re.split(r"(?m)^\s*---\s*$", raw.strip())
    if not 1 <= len(blocks) <= 50:
        raise ValueError("Envie entre 1 e 50 unidades por mensagem.")
    units=[]
    for i, block in enumerate(blocks, 1):
        content=block.strip()
        if not content:
            raise ValueError(f"Unidade {i} está vazia. Remova separadores extras.")
        if len(content) > 3900:
            raise ValueError(f"Unidade {i} tem texto demais (máximo 3900 caracteres).")
        units.append(content)
    return units

def after_purchase_buttons(pid):
    """Ações rápidas depois da entrega; também funcionam para Pix confirmado."""
    return keyboard([[('🛍️ Comprar novamente',f'product:{pid}')],
                     [('📦 Produtos','products'),('🏠 Menu inicial','home')]])

def delivery_text(content):
    """Entrega o conteúdo salvo sem acrescentar cabeçalho ou garantia duplicada."""
    return html.escape(content)

def keyboard(rows): return M([[B(label,callback_data=action) for label,action in row] for row in rows])
SUPPORT_URL=""
GROUP_URL=""
WEB_URL=""

MENU_BUTTONS = [
    ('products','📦 PRODUTOS'), ('public_stock','📊 ESTOQUE'),
    ('topup','💳 ADICIONAR SALDO'), ('balance','💰 MEU SALDO'),
    ('history','📜 HISTÓRICO'), ('account','👤 MINHA CONTA'),
    ('support','📞 SUPORTE'), ('group','👥 GRUPO'), ('web','🌐 LOJA WEB'),
]

def setting(key, default=None):
    with conn() as c:
        row=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
    return row['value'] if row else default

def save_setting(key,value):
    with conn() as c:
        c.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',(key,value))

def menu_label(key):
    return setting('menu_button_'+key,dict(MENU_BUTTONS)[key])

def main_menu():
    rows=[
        [B(menu_label('products'),callback_data='products')],
        [B(menu_label('public_stock'),callback_data='public_stock'),B(menu_label('topup'),callback_data='topup')],
        [B(menu_label('balance'),callback_data='balance'),B(menu_label('history'),callback_data='history')],
        [B(menu_label('account'),callback_data='account')],
    ]
    optional=[]
    for key,default in [('support',SUPPORT_URL),('group',GROUP_URL),('web',WEB_URL)]:
        url=setting('menu_url_'+key,default)
        if url and re.fullmatch(r'https://[^\s]+',url):
            optional.append(B(menu_label(key),url=url))
    if optional: rows.append(optional[:2])
    if len(optional)>2: rows.append(optional[2:])
    return M(rows)

def welcome_text(user):
    with conn() as c:
        row=c.execute('SELECT balance FROM users WHERE id=?',(user.id,)).fetchone()
    balance=row['balance'] if row else 0
    username='@'+html.escape(user.username) if user.username else 'Não informado'
    intro=html.escape(setting('store_name','Minha Loja Demo'))+'\n\n'+html.escape(setting('menu_welcome_text','💜 Bem-vindo à sua loja demonstrativa!\n🛍️ Personalize tudo pelo painel /admin.\n⚠️ Pagamentos reais desativados.'))
    return (intro+'\n\n👤 <b>Minha conta</b>\n'
            f'📛 <b>Nome:</b> {html.escape(user.full_name)}\n'
            f'👤 <b>Usuário:</b> {username}\n'
            f'🆔 <b>ID:</b> <code>{user.id}</code>\n'
            f'💰 <b>Saldo:</b> {money(balance)}')

def catalog_buttons(rows):
    buttons=[[(f"{r['name'][:34]} • {money(r['price'])} • {r['qty']} un.",f"product:{r['id']}")] for r in rows]
    buttons.append([('🏠 Voltar','home')])
    return keyboard(buttons)

async def show_catalog(update):
    with conn() as c:
        rows=c.execute('SELECT p.*, (SELECT COUNT(*) FROM stock s WHERE s.product_id=p.id AND s.sold=0) qty FROM products p WHERE active=1 ORDER BY id DESC').fetchall()
    if not rows:
        return await send(update,'Ainda não há produtos cadastrados.',main_menu())
    caption=html.escape(setting('catalog_title','📦 PRODUTOS DISPONÍVEIS'))+'\n\n'+html.escape(setting('catalog_subtitle','Selecione um produto:'))
    image_id=setting('catalog_image_file_id')
    if image_id:
        return await product_photo(update,image_id,caption[:1024],catalog_buttons(rows))
    return await send(update,caption,catalog_buttons(rows))

async def dispatch_nexo_restock_alerts(ctx):
    """Non-blocking side effect: retries carry the same idempotency event_id.

    Queued events contain product names/counts only, never stock access content.
    This separate notification path must never affect checkout or inventory.
    """
    if not NEXO_ALERTS_URL or not NEXO_ALERTS_SECRET:
        return
    with conn() as c:
        rows=c.execute("SELECT event_id,product_id,product_name,quantity_delta,stock_current,photo_file_id FROM nexo_restock_outbox WHERE status='pending' ORDER BY created_at LIMIT 12").fetchall()
    for row in rows:
        payload={'event_id':row['event_id'], 'source':'telegram',
                 'product_id':str(row['product_id']),
                 'product_name':row['product_name'],
                 'quantity_delta':row['quantity_delta'], 'stock_current':row['stock_current']}
        try:
            # Telegram file_id belongs to the original bot: the announcer cannot reuse it.
            # Download using THIS bot's credentials; transmit only image bytes, never tokens.
            if row['photo_file_id']:
                try:
                    tgfile=await asyncio.wait_for(ctx.bot.get_file(row['photo_file_id']),timeout=10)
                    if tgfile.file_size is None or tgfile.file_size <= 3_000_000:
                        photo_bytes=bytes(await asyncio.wait_for(tgfile.download_as_bytearray(),timeout=15))
                        if 0 < len(photo_bytes) <= 3_000_000 and photo_bytes.startswith(b'\xff\xd8\xff'):
                            payload['photo_data_b64']=base64.b64encode(photo_bytes).decode('ascii')
                except (TelegramError, asyncio.TimeoutError, OSError, ValueError):
                    logging.warning('Photo relay unavailable; sending notification as text for this event')
            def post_event():
                return requests.post(NEXO_ALERTS_URL+'/events',json=payload,
                    headers={'X-Nexo-Webhook-Secret':NEXO_ALERTS_SECRET},timeout=25,
                    allow_redirects=False)
            response=await asyncio.to_thread(post_event)
            if response.status_code==202:
                with conn() as c:
                    c.execute("UPDATE nexo_restock_outbox SET status='accepted' WHERE event_id=? AND status='pending'",(row['event_id'],))
            elif response.status_code in (400,401,403,404):
                logging.warning('Alert service rejected event, HTTP %s; event remains pending',response.status_code)
        except requests.RequestException:
            logging.warning('Nexo alert service unreachable; queued event will be retried')

async def announce_group_restock(ctx,pid,added):
    """Publish new inventory only after a successful admin stock insertion."""
    target=setting('restock_group_chat_id')
    if not target: return
    with conn() as c:
        p=c.execute('SELECT name,active,image_file_id FROM products WHERE id=?',(pid,)).fetchone()
        qty=c.execute('SELECT COUNT(*) FROM stock WHERE product_id=? AND sold=0',(pid,)).fetchone()[0]
    if not p or not p['active'] or not qty: return
    bot_username=(await ctx.bot.get_me()).username
    link=f'https://t.me/{bot_username}?start=produto_{pid}'
    web_link=setting('menu_url_web',WEB_URL)
    rows=[[B('🛒 Comprar no Telegram',url=link)]]
    if web_link and re.fullmatch(r'https://[^\s]+',web_link): rows.append([B('🌐 Comprar no Site',url=web_link)])
    buttons=M(rows)
    text=(f'🚨 <b>ESTOQUE REPOSTO!</b>\n\n'
          f'💎 <b>Produto:</b> {html.escape(p["name"])}\n'
          f'📥 <b>Quantidade adicionada:</b> +{added} unidades\n'
          f'📦 <b>Estoque atual:</b> {qty} unidades\n\n'
          '⚡ Garanta o seu antes que acabe!')
    try:
        if p['image_file_id']:
            await ctx.bot.send_photo(chat_id=int(target),photo=p['image_file_id'],caption=text,
                                     parse_mode='HTML',reply_markup=buttons)
        else:
            await ctx.bot.send_message(chat_id=int(target),text=text,parse_mode='HTML',reply_markup=buttons)
    except TelegramError:
        logging.exception('Falha ao publicar reposição no grupo %s',target)

async def vinculargrupo(update,ctx):
    """Run /vinculargrupo inside the chosen Telegram group as an authorized admin."""
    if not update.effective_user or update.effective_user.id not in ADMINS: return
    chat=update.effective_chat
    if chat.type not in ('group','supergroup'):
        return await update.effective_message.reply_text('Envie /vinculargrupo dentro do grupo de reposições.')
    try:
        me=await ctx.bot.get_chat_member(chat.id,(await ctx.bot.get_me()).id)
        if me.status not in ('administrator','creator'):
            return await update.effective_message.reply_text('Adicione o bot como administrador deste grupo, com permissão para enviar mensagens.')
    except TelegramError:
        return await update.effective_message.reply_text('Não consegui verificar as permissões do bot neste grupo.')
    save_setting('restock_group_chat_id',str(chat.id))
    await update.effective_message.reply_text('✅ Grupo vinculado! As próximas reposições adicionadas pelo /admin serão publicadas aqui automaticamente.')

async def notify_restock(ctx,pid):
    with conn() as c:
        c.execute('BEGIN IMMEDIATE')
        p=c.execute('SELECT name,active FROM products WHERE id=?',(pid,)).fetchone()
        qty=c.execute('SELECT COUNT(*) FROM stock WHERE product_id=? AND sold=0',(pid,)).fetchone()[0]
        if not p or not p['active'] or not qty: return
        subscribers=c.execute('SELECT user_id FROM restock_alerts WHERE product_id=?',(pid,)).fetchall()
        c.execute('DELETE FROM restock_alerts WHERE product_id=?',(pid,))
    for row in subscribers:
        try:
            await ctx.bot.send_message(chat_id=row['user_id'],
                text=f"🔔 <b>Produto disponível novamente!</b>\n📦 {html.escape(p['name'])}\n📊 Estoque: {qty} un.",
                parse_mode='HTML',reply_markup=keyboard([[('🛍 Ver produto',f'product:{pid}')]]))
        except TelegramError:
            logging.info('Não foi possível enviar aviso de reposição ao usuário %s',row['user_id'])


def is_admin(uid):
    if uid in ROOT_ADMINS: return True
    with conn() as c:
        return c.execute('SELECT 1 FROM extra_admins WHERE user_id=?',(uid,)).fetchone() is not None

def owner_only(uid):
    return uid == OWNER_ID if OWNER_ID else uid in ROOT_ADMINS

def is_banned(uid):
    with conn() as c:return c.execute('SELECT 1 FROM banned_users WHERE user_id=?',(uid,)).fetchone() is not None

def setting_int(key,default):
    try:return int(setting(key,str(default)))
    except (TypeError,ValueError):return default

def activity_touch(uid):
    now=int(time.time())
    with conn() as c:
        c.execute('INSERT INTO activity(user_id,last_active,visit_token) VALUES(?,?,1) ON CONFLICT(user_id) DO UPDATE SET last_active=excluded.last_active,visit_token=CASE WHEN activity.reminded_token=activity.visit_token THEN activity.visit_token+1 ELSE activity.visit_token END',(uid,now))

def record_purchase(uid):
    with conn() as c:
        c.execute('INSERT INTO activity(user_id,last_active,last_purchase,visit_token) VALUES(?,?,?,1) ON CONFLICT(user_id) DO UPDATE SET last_purchase=excluded.last_purchase,last_active=excluded.last_active,reminded_token=activity.visit_token',(uid,int(time.time()),int(time.time())))

def admin_menu():
    return keyboard([[('📦 Produtos e estoque','adm_products_menu'),('👥 Usuários','adm_users_menu')],
                     [('💰 Saldo e bônus','adm_finance_menu'),('🏆 Rankings','adm_ranking')],
                     [('🛡 Administradores','adm_admin_users'),('⚙️ Configurações','adm_settings_menu')],
                     [('🛡️ Backup','adm_backup'),('🏠 Início','home')]])

def admin_products_menu():
    return keyboard([[('➕ Produto','adm_add'),('📥 Adicionar estoque','adm_stock')],
      [('🗂 Gerenciar unidades','adm_inventory'),('🧹 Limpar estoque','adm_clear_stock')],
      [('✏️ Editar','adm_edit_products'),('🗑 Excluir','adm_delete_products')],
      [('💲 Preços','adm_price'),('🖼 Fotos','adm_images')],
      [('📋 Produtos','adm_products'),('↩️ Painel','adm_panel')]])

def admin_finance_menu():
    return keyboard([[('💰 Ajustar saldo','adm_balance'),('↩️ Reembolsar','adm_refund')],
      [('🎟️ Cupons','adm_coupons'),('💸 Recargas','adm_topups')],
      [('🎁 Bônus de depósito','adm_bonus_topup'),('🎉 Bônus de registro','adm_bonus_register')],
      [('📐 Limites de recarga','adm_topup_limits'),('📊 Estatísticas','adm_stats')], [('↩️ Painel','adm_panel')]])

def admin_settings_menu():
    return keyboard([[('🚧 Manutenção','adm_maintenance'),('🔔 Lembretes','adm_reminders')],
      [('👥 Entrada obrigatória','adm_force_join'),('🎨 Editar menu','adm_customize')],
      [('🖼 Imagem do catálogo','adm_catalog_image'),('🖼 Imagem inicial','adm_menu_image')],
      [('↩️ Painel','adm_panel')]])

async def access_guard(update,ctx):
    u=update.effective_user
    if not u:return False
    uid=u.id
    if is_admin(uid):return True
    if is_banned(uid):
        await send(update,'🚫 Seu acesso está bloqueado. Entre em contato com o suporte.');return False
    if setting('maintenance','0')=='1':
        await send(update,html.escape(setting('maintenance_message','🚧 Loja em manutenção. Tente novamente mais tarde.')));return False
    channel=setting('forced_chat','').strip()
    if channel:
        try:
            member=await ctx.bot.get_chat_member(chat_id=channel,user_id=uid)
            if member.status not in ('creator','administrator','member') and not (member.status=='restricted' and member.is_member):
                await show_forced_join_prompt(update,ctx,channel)
                return False
        except TelegramError:
            logging.warning('Falha ao verificar participação obrigatória; verifique permissões do bot')
            await send(update,'⚠️ Não foi possível verificar a participação. Fale com o suporte.');return False
    return True

def forced_join_url(channel):
    """Link de entrada. Para grupo privado, configure o convite pelo /admin."""
    url=setting('forced_chat_url','').strip()
    if url.startswith('https://t.me/') or url.startswith('https://telegram.me/'):
        return url
    if re.fullmatch(r'@[A-Za-z0-9_]{5,32}',channel):
        return 'https://t.me/'+channel[1:]
    return None

async def show_forced_join_prompt(update,ctx,channel):
    url=forced_join_url(channel)
    buttons=[]
    if url:buttons.append([B('👥 Entrar no canal/grupo',url=url)])
    buttons.append([B('✅ Já entrei — verificar',callback_data='verify_forced_join')])
    label=html.escape(channel)
    message=('👥 <b>Entrada obrigatória</b>\n\n'
             f'Para usar {html.escape(setting("store_name","esta loja"))}, participe de <b>{label}</b>.\n'
             'Depois toque em <b>✅ Já entrei — verificar</b>.')
    if not url:message+='\n\n⚠️ Peça o convite de entrada ao suporte.'
    return await send(update,message,M(buttons))

async def abandoned_visit_reminders(ctx):
    if setting('reminders_enabled','1')!='1':return
    now=int(time.time())
    with conn() as c:
        rows=c.execute('SELECT a.user_id,a.visit_token FROM activity a LEFT JOIN banned_users b ON b.user_id=a.user_id WHERE b.user_id IS NULL AND a.reminders_off=0 AND a.last_active<=? AND a.last_active>? AND a.last_purchase<a.last_active AND a.reminded_token!=a.visit_token AND (a.last_reminder=0 OR a.last_reminder<=?) LIMIT 20', (now-1800,now-86400*3,now-86400)).fetchall()
    for row in rows:
        with conn() as c:
            pending=c.execute('SELECT product_id FROM unavailable_interest WHERE user_id=? AND notified=0 AND created_at<=? LIMIT 1',(row['user_id'],now-1800)).fetchone()
            claim=c.execute('UPDATE activity SET reminded_token=visit_token,last_reminder=? WHERE user_id=? AND visit_token=? AND reminded_token!=visit_token',(now,row['user_id'],row['visit_token'])).rowcount
            if claim and pending:c.execute('UPDATE unavailable_interest SET notified=1 WHERE user_id=? AND product_id=?',(row['user_id'],pending['product_id']))
        if not claim:continue
        try:
            text=('🔔 O produto que você procurou estava esgotado! Quer ser avisado quando voltar? Use /alertas para ativar os avisos de reposição. 💜' if pending else setting('abandon_message','👋 Oi! Tudo bem? 💜\n\nVocê passou pela nossa loja mais cedo. Encontrou o que procurava ou ficou alguma dúvida?\n\n🛍️ Veja os produtos com /start\n🔔 Reposições: /alertas\n\nPara cancelar lembretes, envie /semlembretes.'))
            await ctx.bot.send_message(row['user_id'],text)
        except TelegramError:
            logging.info('Não foi possível enviar lembrete de visita')

async def alertas_command(update,ctx):
    user_save(update.effective_user)
    if not await access_guard(update,ctx):return
    with conn() as c:
        rows=c.execute('SELECT p.id,p.name FROM products p WHERE p.active=1 AND p.archived=0 AND NOT EXISTS (SELECT 1 FROM stock s WHERE s.product_id=p.id AND s.sold=0) ORDER BY p.id DESC LIMIT 25').fetchall()
    if not rows:return await send(update,'✅ Todos os produtos disponíveis no momento! Use /start para ver o catálogo.')
    return await send(update,'🔔 Escolha um produto para receber aviso quando ele voltar ao estoque:',keyboard([[(r['name'][:45],f"restock_sub:{r['id']}")] for r in rows]+[[('🏠 Menu','home')]]))

async def no_reminders(update,ctx):
    user_save(update.effective_user)
    with conn() as c:c.execute('INSERT INTO activity(user_id,last_active,reminders_off) VALUES(?,?,1) ON CONFLICT(user_id) DO UPDATE SET reminders_off=1',(update.effective_user.id,int(time.time())))
    await send(update,'🔕 Lembretes de visitas desativados. Você continuará recebendo apenas as mensagens relacionadas às suas compras.')

async def enable_reminders(update,ctx):
    user_save(update.effective_user)
    with conn() as c:c.execute('UPDATE activity SET reminders_off=0 WHERE user_id=?',(update.effective_user.id,))
    await send(update,'✅ Lembretes de visitas reativados.')

def user_save(u):
    activity_touch(u.id)
    with conn() as c:
        c.execute('BEGIN IMMEDIATE')
        existing=c.execute('SELECT id FROM users WHERE id=?',(u.id,)).fetchone()
        c.execute("INSERT INTO users(id,name,username) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,username=excluded.username",(u.id,u.full_name,u.username or ""))
        if not existing and u.id not in ROOT_ADMINS and not is_banned_registration(c,u.id):
            bonus=setting_int_registration(c,'bonus_register_cents',0)
            if bonus>0:
                changed=c.execute('INSERT OR IGNORE INTO registration_bonuses(user_id,amount) VALUES(?,?)',(u.id,bonus)).rowcount
                if changed:c.execute('UPDATE users SET balance=balance+? WHERE id=?',(bonus,u.id))

def is_banned_registration(c,uid):
    return bool(c.execute('SELECT 1 FROM banned_users WHERE user_id=?',(uid,)).fetchone())

def setting_int_registration(c,key,default):
    r=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
    try:return int(r[0]) if r else default
    except (ValueError,TypeError):return default

def apply_topup_bonus(c,source,payment_id,uid,amount):
    percent=max(0,min(100,setting_int_registration(c,'bonus_topup_percent',0)))
    bonus=amount*percent//100
    if bonus:
        changed=c.execute('INSERT OR IGNORE INTO topup_bonuses(source,payment_id,user_id,bonus) VALUES(?,?,?,?)',(source,payment_id,uid,bonus)).rowcount
        if changed:c.execute('UPDATE users SET balance=balance+? WHERE id=?',(bonus,uid))
    return bonus

async def send(update,text,reply_markup=None):
    """Navega editando o menu anterior, sem acumular mensagens a cada clique."""
    q=update.callback_query
    if not q:
        return await update.message.reply_text(text,reply_markup=reply_markup,parse_mode="HTML")
    try:
        await q.answer()
    except BadRequest:
        pass
    msg=q.message
    if msg.photo or msg.video or msg.document or msg.animation:
        # Telegram não converte mídia em mensagem de texto por edit_message_text.
        # Exclui a mídia antiga e coloca o novo menu no lugar.
        try:
            await msg.delete()
        except TelegramError:
            logging.warning("Não foi possível excluir a mídia anterior")
        return await q.get_bot().send_message(chat_id=update.effective_chat.id,text=text,
                                               reply_markup=reply_markup,parse_mode="HTML")
    try:
        return await msg.edit_text(text,reply_markup=reply_markup,parse_mode="HTML")
    except BadRequest as exc:
        if "Message is not modified" in str(exc):
            return
        logging.warning("Não foi possível editar mensagem anterior: %s",exc)
        return await q.get_bot().send_message(chat_id=update.effective_chat.id,text=text,
                                               reply_markup=reply_markup,parse_mode="HTML")
async def product_photo(update, file_id, caption, buttons):
    """Mostra foto do produto e reaproveita a mensagem do menu quando possível."""
    q=update.callback_query
    if q:
        try:
            await q.answer()
        except BadRequest:
            pass
        try:
            return await q.edit_message_media(
                media=InputMediaPhoto(media=file_id,caption=caption,parse_mode="HTML"),
                reply_markup=buttons)
        except TelegramError as exc:
            logging.info("Não foi possível editar mídia do produto: %s",type(exc).__name__)
            try:
                await q.message.delete()
            except TelegramError:
                pass
    return await update.get_bot().send_photo(chat_id=update.effective_chat.id,
                                            photo=file_id,caption=caption,
                                            parse_mode="HTML",reply_markup=buttons)

async def clear_welcome_text(update, ctx):
    """Remove o texto auxiliar do menu ao navegar, sem tocar no banco."""
    mid = ctx.user_data.pop("welcome_text_message_id", None)
    if mid:
        try:
            await ctx.bot.delete_message(chat_id=update.effective_chat.id, message_id=mid)
        except TelegramError:
            logging.info("Mensagem antiga de boas-vindas já não pode ser removida")

async def start(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    user_save(update.effective_user)
    if not await access_guard(update,ctx):return
    ctx.user_data.pop("step",None)
    await clear_welcome_text(update,ctx)
    if ctx.args and re.fullmatch(r'produto_\d+',ctx.args[0]):
        pid=int(ctx.args[0].split('_',1)[1])
        with conn() as c:
            p=c.execute('SELECT p.*, (SELECT COUNT(*) FROM stock WHERE product_id=p.id AND sold=0) qty FROM products p WHERE p.id=? AND p.active=1',(pid,)).fetchone()
        if p:
            if not p['qty']:
                with conn() as c:c.execute('INSERT INTO unavailable_interest(user_id,product_id,created_at,notified) VALUES(?,?,?,0) ON CONFLICT(user_id,product_id) DO UPDATE SET created_at=excluded.created_at,notified=0',(update.effective_user.id,pid,int(time.time())))
                return await send(update,f'❌ {html.escape(p["name"])} está esgotado no momento.',keyboard([[('🔔 AVISAR QUANDO REPOR',f'restock_sub:{pid}')],[('📦 PRODUTOS','products')]]))
            description=html.escape(p['description'] or '')
            caption=f'📦 <b>{html.escape(p["name"])}</b>\n\n{description}\n\n💰 <b>Preço:</b> {money(p["price"])}\n📊 <b>Estoque:</b> {p["qty"]} un.'
            if p['image_file_id']:
                return await product_photo(update,p['image_file_id'],caption[:1024],purchase_buttons(pid))
            return await send(update,caption,purchase_buttons(pid))
    with conn() as c:
        r=c.execute("SELECT value FROM settings WHERE key='menu_image_file_id'").fetchone()
    presentation=welcome_text(update.effective_user)
    if r and r['value']:
        # Legenda, imagem e teclado no mesmo bloco visual do Telegram.
        q=update.callback_query
        if q:
            try:
                await q.answer()
            except BadRequest:
                pass
            try:
                await q.message.delete()
            except TelegramError:
                pass
        # O limite da legenda do Telegram é de 1024 caracteres depois do parse.
        # Se exceder, preserve o texto sem truncar nomes ou HTML.
        caption_ok = len(html.unescape(re.sub(r'<[^>]*>', '', presentation))) <= 1024
        if caption_ok:
            try:
                return await ctx.bot.send_photo(chat_id=update.effective_chat.id,
                                                photo=r['value'], caption=presentation,
                                                parse_mode='HTML', reply_markup=main_menu())
            except TelegramError:
                logging.exception('Falha ao enviar foto com legenda; utilizando mensagem de texto')
                return await ctx.bot.send_message(chat_id=update.effective_chat.id,
                                                  text=presentation, parse_mode='HTML',
                                                  reply_markup=main_menu())
        intro=await ctx.bot.send_message(chat_id=update.effective_chat.id,
                                         text=presentation,parse_mode='HTML')
        ctx.user_data['welcome_text_message_id']=intro.message_id
        try:
            return await ctx.bot.send_photo(chat_id=update.effective_chat.id,
                                            photo=r['value'],caption='🛍️ Escolha uma opção:',
                                            reply_markup=main_menu())
        except TelegramError:
            logging.exception('Não foi possível enviar imagem do menu; mantendo botões acessíveis')
            return await ctx.bot.send_message(chat_id=update.effective_chat.id,
                                              text='🛍️ Escolha uma opção abaixo:',
                                              reply_markup=main_menu())
    return await send(update,presentation,main_menu())

async def pix_command(update,ctx):
    """Atalho /pix: abre o mesmo fluxo seguro de recarga já usado no menu."""
    user_save(update.effective_user)
    if not await access_guard(update,ctx):return
    ctx.user_data['step']='pix_topup_amount'
    await send(update,'💳 Digite o valor da recarga em reais (exemplo: 25,00). O Pix será confirmado automaticamente.')

async def admin(update,ctx):
    user_save(update.effective_user)
    if not is_admin(update.effective_user.id): return
    ctx.user_data.pop("step",None)
    await clear_welcome_text(update,ctx)
    await send(update,"⚙️ <b>Painel administrativo</b>",admin_menu())
async def buy_with_balance(update,ctx,pid,code=None):
    uid=update.effective_user.id
    try:
        with conn() as c:
            c.execute('BEGIN IMMEDIATE')
            p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(pid,)).fetchone()
            stock=c.execute('SELECT * FROM stock WHERE product_id=? AND sold=0 ORDER BY id LIMIT 1',(pid,)).fetchone()
            user=c.execute('SELECT balance FROM users WHERE id=?',(uid,)).fetchone()
            if not p or not stock: raise ValueError('Produto sem estoque.')
            price,_=coupon_price(c,code,p['price'])
            if not user or user['balance']<price: raise ValueError('Saldo insuficiente. Use Adicionar saldo.')
            c.execute('UPDATE users SET balance=balance-? WHERE id=?',(price,uid))
            c.execute('UPDATE stock SET sold=1 WHERE id=?',(stock['id'],))
            oid=c.execute('INSERT INTO orders(user_id,product_id,stock_id,price,original_price,coupon_code) VALUES(?,?,?,?,?,?)',
                          (uid,pid,stock['id'],price,p['price'],code)).lastrowid
            consume_coupon(c,code,uid,oid)
    except ValueError as exc:
        return await send(update,html.escape(str(exc)),purchase_buttons(pid))
    record_purchase(uid)
    await send(update,delivery_text(stock['content']),after_purchase_buttons(pid))
    await check_low_stock(ctx.bot,pid)
    for aid in ADMINS:
        try:
            await ctx.bot.send_message(aid,f"🟣 VENDA CONFIRMADA!\n📦 Produto: {p['name']}\n💰 Valor: {money(price)}\n👤 Cliente: {update.effective_user.full_name}\n🆔 ID: {uid}")
        except Exception: logging.exception('Falha na notificação de venda')

async def check_low_stock(bot, pid):
    """Notify admins once when stock reaches zero; rearm after replenishment."""
    with conn() as c:
        c.execute('BEGIN IMMEDIATE')
        product=c.execute('SELECT name,active FROM products WHERE id=?',(pid,)).fetchone()
        if not product or not product['active']: return
        qty=c.execute('SELECT COUNT(*) FROM stock WHERE product_id=? AND sold=0',(pid,)).fetchone()[0]
        key=f'low_stock_alert_{pid}'
        previous=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        if qty>0:
            c.execute('DELETE FROM settings WHERE key=?',(key,))
            return
        if previous: return
        c.execute('INSERT INTO settings(key,value) VALUES(?,?)',(key,str(qty)))
        name=product['name']
    for aid in ADMINS:
        try:
            await bot.send_message(aid,
                f"🚨 ESTOQUE ESGOTADO!\n📦 Produto: {name}\n📊 Unidades disponíveis: 0\n🔔 Reponha pelo /admin.")
        except Exception:
            logging.exception('Erro ao notificar estoque baixo')

async def automatic_backup(ctx):
    """Daily consistent SQLite backup sent privately, once per São Paulo calendar day."""
    if not ADMINS: return
    now=datetime.datetime.now(timezone(timedelta(hours=-3)))
    if now.hour < 3: return
    today=now.strftime('%Y-%m-%d')
    with conn() as c:
        last=c.execute("SELECT value FROM settings WHERE key='automatic_backup_date'").fetchone()
        if last and last['value']==today: return
    try:
        snapshot=await asyncio.to_thread(create_database_backup)
        data=snapshot.getvalue()
        if len(data)>45*1024*1024:
            logging.error('Backup automático excede limite de tamanho de segurança')
            return
        sent=0
        for aid in ADMINS:
            try:
                doc=io.BytesIO(data)
                doc.name=snapshot.name
                await ctx.bot.send_document(
                    chat_id=aid,document=doc,filename=doc.name,
                    caption='🛡️ Backup automático diário da Loja Demonstração\nGuarde este arquivo em local seguro. Contém dados de clientes e estoque.',
                    protect_content=True)
                sent+=1
            except Exception:
                logging.exception('Erro ao enviar backup automático ao administrador %s',aid)
        if sent:
            with conn() as c:
                c.execute("INSERT INTO settings(key,value) VALUES('automatic_backup_date',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(today,))
    except Exception:
        logging.exception('Erro no backup automático')

def create_database_backup():
    """Snapshot SQLite consistente, mesmo com o bot em operação."""
    output=io.BytesIO()
    import tempfile
    with tempfile.TemporaryDirectory(prefix="nexo_backup_") as directory:
        destination=Path(directory)/"nexo.db"
        with conn() as source, sqlite3.connect(destination) as target:
            source.backup(target)
            result=target.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError("Falha na verificação de integridade do backup")
        output.write(destination.read_bytes())
    output.seek(0)
    output.name="nexo_backup_"+datetime.datetime.now().strftime("%Y%m%d_%H%M%S")+".sqlite3"
    return output

async def callback(update,ctx):
    q=update.callback_query
    action=q.data
    uid=q.from_user.id
    user_save(q.from_user)
    if action=='verify_forced_join':
        if await access_guard(update,ctx):return await start(update,ctx)
        return
    if not await access_guard(update,ctx):return
    if action=="home": return await start(update,ctx)
    await clear_welcome_text(update,ctx)
    if action=="adm_backup":
        if not is_admin(uid):
            return await send(update,"Acesso restrito.")
        try:
            await q.answer("Preparando cópia segura do banco...")
        except BadRequest:
            pass
        try:
            snapshot=await asyncio.to_thread(create_database_backup)
            await ctx.bot.send_document(
                chat_id=uid,
                document=snapshot,
                filename=snapshot.name,
                caption="🛡️ Backup da Loja Demonstração\nGuarde este arquivo em local privado. Ele contém dados de clientes e estoque.\n⚠️ Não encaminhe para outras pessoas.",
                protect_content=True,
            )
        except Exception:
            logging.exception("Erro ao gerar ou enviar backup")
            return await send(update,"⚠️ Não foi possível criar o backup. Confira os logs do Railway.",admin_menu())
        return await send(update,"✅ Backup enviado no seu privado! Salve o arquivo fora do Telegram, em um local seguro.",admin_menu())
    if action=="public_stock":
        with conn() as c:
            rows=c.execute("SELECT p.name, COUNT(s.id) qty FROM products p LEFT JOIN stock s ON s.product_id=p.id AND s.sold=0 WHERE p.active=1 GROUP BY p.id ORDER BY p.id DESC LIMIT 40").fetchall()
        text="📊 <b>Estoque disponível</b>\n\n"+ ("\n".join(f"📦 {html.escape(r['name'])}: <b>{r['qty']} un.</b>" for r in rows) if rows else "Nenhum produto cadastrado.")
        return await send(update,text,keyboard([[('🛍 Ver produtos','products')],[('🏠 Início','home')]]))
    if action=="products":
        return await show_catalog(update)
    if action.startswith("product:"):
        pid=int(action.split(":")[1])
        with conn() as c:
            r=c.execute("SELECT p.*, (SELECT COUNT(*) FROM stock s WHERE s.product_id=p.id AND s.sold=0) qty FROM products p WHERE id=? AND active=1",(pid,)).fetchone()
        if not r:return await send(update,"Produto indisponível.")
        if r['qty']==0:
            with conn() as c:c.execute('INSERT INTO unavailable_interest(user_id,product_id,created_at,notified) VALUES(?,?,?,0) ON CONFLICT(user_id,product_id) DO UPDATE SET created_at=excluded.created_at,notified=0',(uid,pid,int(time.time())))
            return await send(update,
                f"❌ <b>{html.escape(r['name'])}</b> está esgotado no momento.\n\nDeseja ser avisado quando voltar ao estoque?",
                keyboard([[('🔔 AVISAR QUANDO REPOR',f'restock_sub:{pid}')],[('↩️ PRODUTOS','products')]]))
        caption=f"📦 <b>{html.escape(r['name'])}</b>\n{html.escape(r['description'])}\n💰 {money(r['price'])}\n📊 Estoque: {r['qty']}"
        buttons=purchase_buttons(pid)
        if r["image_file_id"]:
            return await product_photo(update,r["image_file_id"],caption[:1024],buttons)
        return await send(update,caption,buttons)
    if action.startswith('restock_sub:'):
        pid=int(action.split(':')[1])
        with conn() as c:
            p=c.execute('SELECT id FROM products WHERE id=? AND active=1',(pid,)).fetchone()
            if not p:return await send(update,'Produto indisponível.',keyboard([[('↩️ PRODUTOS','products')]]))
            qty=c.execute('SELECT COUNT(*) FROM stock WHERE product_id=? AND sold=0',(pid,)).fetchone()[0]
            if qty:return await send(update,'✅ O produto já está disponível!',keyboard([[('🛍 Ver produto',f'product:{pid}')]]))
            c.execute('INSERT OR IGNORE INTO restock_alerts(user_id,product_id) VALUES(?,?)',(uid,pid))
        return await send(update,'🔔 Pronto! Você será avisado aqui quando esse produto voltar ao estoque.',keyboard([[('↩️ PRODUTOS','products')]]))
    if action.startswith('coupon_apply:'):
        pid=int(action.split(':')[1])
        ctx.user_data['step']='coupon_code';ctx.user_data['coupon_pid']=pid
        return await send(update,'🎟️ Digite o código do cupom:',keyboard([[('↩️ Produto',f'product:{pid}')]]))
    if action.startswith('coupon_checkout:'):
        _,method,pid,code=action.split(':')
        pid=int(pid)
        with conn() as c:
            p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(pid,)).fetchone()
            qty=c.execute('SELECT COUNT(*) FROM stock WHERE product_id=? AND sold=0',(pid,)).fetchone()[0]
            if not p or not qty:return await send(update,'Produto sem estoque.')
            try: price,_=coupon_price(c,code,p['price'])
            except ValueError as exc:return await send(update,html.escape(str(exc)),purchase_buttons(pid))
        if method=='pix':return await generate_pix(update,ctx,uid,'product',pid,price,coupon=code,original_price=p['price'])
        if method=='saldo':return await buy_with_balance(update,ctx,pid,code)
        return await send(update,'Opção inválida.')
    if action.startswith("pixbuy:"):
        pid=int(action.split(":")[1])
        with conn() as c:
            p=c.execute("SELECT p.*, (SELECT COUNT(*) FROM stock s WHERE s.product_id=p.id AND s.sold=0) qty FROM products p WHERE p.id=? AND p.active=1",(pid,)).fetchone()
        if not p or not p["qty"]:return await send(update,"Produto indisponível no momento.")
        return await generate_pix(update,ctx,uid,"product",pid,p["price"])
    if action.startswith("checkpix:"):
        rid=int(action.split(":")[1])
        with conn() as c:r=c.execute("SELECT * FROM pix_payments WHERE id=? AND user_id=?",(rid,uid)).fetchone()
        if not r:return await send(update,"Cobrança não encontrada.")
        await verify_payment(rid,ctx.application)
        with conn() as c:r=c.execute("SELECT status,kind,amount,product_id FROM pix_payments WHERE id=?",(rid,)).fetchone()
        if r["status"]=="approved":
            text=(f"✅ <b>Pagamento confirmado!</b>\n💰 {money(r['amount'])} adicionados ao seu saldo." if r["kind"]=="topup" else "✅ <b>Pagamento confirmado!</b>\n📦 Confira a entrega na conversa.")
        elif r["status"] in ("pending","creating"):
            text="⏳ Pagamento ainda não confirmado. Aguarde alguns instantes."
        else:
            text="⚠️ Não foi possível confirmar este pagamento. Entre em contato com o suporte."
        return await send(update,text,after_purchase_buttons(r["product_id"]) if r["status"]=="approved" and r["kind"]=="product" and r["product_id"] else main_menu())
    if action.startswith("buy:"):
        return await buy_with_balance(update,ctx,int(action.split(':')[1]))
    if action=="balance":
        with conn() as c:r=c.execute("SELECT balance FROM users WHERE id=?",(uid,)).fetchone()
        return await send(update,f"💰 Seu saldo: <b>{money(r['balance'])}</b>",main_menu())
    if action=="account":return await send(update,f"👤 <b>Minha conta</b>\nNome: {html.escape(q.from_user.full_name)}\nUsuário: @{html.escape(q.from_user.username or 'sem_usuario')}\nID: <code>{uid}</code>",main_menu())
    if action=="history":
        with conn() as c:rows=c.execute("SELECT o.id,p.name,o.price,o.created_at FROM orders o JOIN products p ON p.id=o.product_id WHERE o.user_id=? ORDER BY o.id DESC LIMIT 15",(uid,)).fetchall()
        return await send(update,"📦 <b>Últimas compras</b>\n"+("\n".join(f"#{r['id']} • {html.escape(r['name'])} • {money(r['price'])} • {r['created_at']}" for r in rows) if rows else "Nenhuma compra ainda."),main_menu())
    if action in ("support","group","web"):
        key={"support":"SUPPORT_URL","group":"GROUP_URL","web":"WEB_URL"}[action]
        url=setting('menu_url_'+action,{'support':SUPPORT_URL,'group':GROUP_URL,'web':WEB_URL}[action])
        return await send(update,f"🔗 {html.escape(url) if url else 'Link ainda não configurado.'}",main_menu())
    if action=="topup":
        ctx.user_data.pop("step",None)
        ctx.user_data["step"]="pix_topup_amount"
        return await send(update,"⚠️ Ambiente demonstrativo: pagamentos reais desativados. Não envie dinheiro.")
    if not is_admin(uid):return await send(update,"Acesso restrito.")
    if action=='adm_products_menu':return await send(update,'📦 <b>Produtos e estoque</b>',admin_products_menu())
    if action=='adm_finance_menu':return await send(update,'💰 <b>Saldo e bônus</b>',admin_finance_menu())
    if action=='adm_settings_menu':return await send(update,'⚙️ <b>Configurações</b>',admin_settings_menu())
    if action=='adm_users_menu':
        return await send(update,'👥 <b>Usuários</b>',keyboard([[('🚫 Banir usuário','adm_ban'),('✅ Desbanir','adm_unban')],[('↩️ Painel','adm_panel')]]))
    if action in ('adm_ban','adm_unban','adm_add_admin','adm_remove_admin'):
        if action in ('adm_add_admin','adm_remove_admin') and not owner_only(uid):return await send(update,'Somente o proprietário pode gerenciar administradores.')
        ctx.user_data['step']=action
        return await send(update,'Envie o ID numérico do Telegram do usuário:',keyboard([[('↩️ Painel','adm_panel')]]))
    if action=='adm_admin_users':
        with conn() as c:rows=c.execute('SELECT user_id FROM extra_admins ORDER BY user_id').fetchall()
        people=', '.join(str(row['user_id']) for row in rows) or 'Nenhum administrador adicional'
        return await send(update,'🛡️ <b>Administradores adicionais</b>\n'+people+'\n\nAdministradores principais definidos no Railway são protegidos.',keyboard([[('➕ Adicionar','adm_add_admin'),('➖ Remover','adm_remove_admin')],[('↩️ Painel','adm_panel')]]))
    if action=='adm_ranking':
        with conn() as c:
            products=c.execute('SELECT p.name,COUNT(*) n,COALESCE(SUM(o.price),0) revenue FROM orders o JOIN products p ON p.id=o.product_id GROUP BY p.id ORDER BY n DESC LIMIT 10').fetchall()
            customers=c.execute('SELECT user_id,COUNT(*) n FROM orders GROUP BY user_id ORDER BY n DESC LIMIT 10').fetchall()
            topups=c.execute("SELECT user_id,SUM(amount) total FROM (SELECT user_id,amount FROM pix_payments WHERE kind='topup' AND status='approved' UNION ALL SELECT user_id,amount FROM topups WHERE status='approved') GROUP BY user_id ORDER BY total DESC LIMIT 10").fetchall()
            revenue=c.execute('SELECT p.name,SUM(o.price) total FROM orders o JOIN products p ON p.id=o.product_id GROUP BY p.id ORDER BY total DESC LIMIT 10').fetchall()
        lines=['🏆 <b>Rankings Top 10</b>','\n📦 <b>Mais vendidos</b>']
        lines += [f"{i}. {html.escape(r['name'])} • {r['n']} venda(s)" for i,r in enumerate(products,1)] or ['Sem vendas']
        lines.append('\n🛒 <b>Clientes com mais compras</b>')
        lines += [f"{i}. ID {r['user_id']} • {r['n']}" for i,r in enumerate(customers,1)] or ['Sem compras']
        lines.append('\n💳 <b>Maiores recargas confirmadas</b>')
        lines += [f"{i}. ID {r['user_id']} • {money(r['total'])}" for i,r in enumerate(topups,1)] or ['Sem recargas']
        lines.append('\n💰 <b>Maior faturamento por produto</b>')
        lines += [f"{i}. {html.escape(r['name'])} • {money(r['total'])}" for i,r in enumerate(revenue,1)] or ['Sem vendas']
        return await send(update,'\n'.join(lines)[:3900],admin_menu())
    if action=='adm_clear_stock':
        with conn() as c:rows=c.execute('SELECT p.id,p.name,COUNT(s.id) qty FROM products p LEFT JOIN stock s ON s.product_id=p.id AND s.sold=0 WHERE p.archived=0 GROUP BY p.id HAVING qty>0 ORDER BY p.id DESC LIMIT 50').fetchall()
        if not rows:return await send(update,'Nenhum estoque disponível para limpar.',admin_products_menu())
        return await send(update,'🧹 Selecione o produto cujo estoque disponível será removido (histórico preservado):',keyboard([[(f"{r['name'][:32]} • {r['qty']} un.",f"clearstock:{r['id']}")] for r in rows]+[[('↩️ Voltar','adm_products_menu')]]))
    if action.startswith('clearstock:'):
        pid=int(action.split(':')[1])
        with conn() as c:
            p=c.execute('SELECT name FROM products WHERE id=?',(pid,)).fetchone()
            qty=c.execute('SELECT COUNT(*) FROM stock WHERE product_id=? AND sold=0',(pid,)).fetchone()[0]
        if not p:return await send(update,'Produto não encontrado.')
        return await send(update,f"⚠️ Limpar <b>{html.escape(p['name'])}</b>?\nSerão removidas {qty} unidades NÃO vendidas.\nEsta operação não tem desfazer e NÃO remove pedidos antigos.",keyboard([[('🧹 CONFIRMAR LIMPEZA',f'clearstock_confirm:{pid}')],[('❌ Cancelar','adm_products_menu')]]))
    if action.startswith('clearstock_confirm:'):
        pid=int(action.split(':')[1])
        with conn() as c:
            c.execute('BEGIN IMMEDIATE')
            n=c.execute('DELETE FROM stock WHERE product_id=? AND sold=0',(pid,)).rowcount
        return await send(update,f'✅ {n} unidade(s) não vendidas removidas. Histórico preservado.',admin_products_menu())
    if action=='adm_maintenance':
        state=setting('maintenance','0')=='1'
        return await send(update,f"🚧 Manutenção: <b>{'LIGADA' if state else 'DESLIGADA'}</b>. Administradores continuam com acesso.",keyboard([[('🔄 Alternar', 'adm_toggle_maintenance')],[('↩️ Voltar','adm_settings_menu')]]))
    if action=='adm_toggle_maintenance':
        save_setting('maintenance','0' if setting('maintenance','0')=='1' else '1')
        return await send(update,'✅ Manutenção atualizada.',admin_settings_menu())
    if action=='adm_reminders':
        on=setting('reminders_enabled','1')=='1'
        return await send(update,f"⏱️ Lembrete de visita após 30 minutos: <b>{'LIGADO' if on else 'DESLIGADO'}</b>.\nUma mensagem por visita, intervalo mínimo de 24h; cliente pode enviar /semlembretes.",keyboard([[('🔄 Alternar','adm_toggle_reminders')],[('↩️ Voltar','adm_settings_menu')]]))
    if action=='adm_toggle_reminders':
        save_setting('reminders_enabled','0' if setting('reminders_enabled','1')=='1' else '1')
        return await send(update,'✅ Lembretes atualizados.',admin_settings_menu())
    if action=='adm_force_join':
        chat=setting('forced_chat','')
        url=setting('forced_chat_url','')
        return await send(update,'👥 Entrada obrigatória: '+html.escape(chat or 'desligada')+'\n🔗 Convite: '+html.escape(url or '(automático para @ público)')+'\nO bot precisa ser administrador do canal/grupo e conseguir verificar os membros.',keyboard([[('➕ Configurar ID/@','adm_force_join_edit')],[('🔗 Configurar convite','adm_force_join_link'),('🔕 Desligar','adm_force_join_off')],[('↩️ Voltar','adm_settings_menu')]]))
    if action=='adm_force_join_edit':
        ctx.user_data['step']='forced_chat'
        return await send(update,'Envie o @canal público ou o ID numérico do chat. Confirme que o bot é administrador do grupo/canal.')
    if action=='adm_force_join_link':
        ctx.user_data['step']='forced_chat_link'
        return await send(update,'Envie o link https://t.me/... do canal/grupo. Em grupo privado, use o convite do grupo. Envie 0 para apagar o convite salvo.')
    if action=='adm_force_join_off':save_setting('forced_chat','');return await send(update,'✅ Entrada obrigatória desativada.',admin_settings_menu())
    if action=='adm_topup_limits':
        a=setting_int('min_topup',100);b=setting_int('max_topup',10000000)
        return await send(update,f'📐 Recargas: mínimo {money(a)}, máximo {money(b)}.',keyboard([[('✏️ Mínimo','adm_min_topup'),('✏️ Máximo','adm_max_topup')],[('↩️ Voltar','adm_finance_menu')]]))
    if action in ('adm_min_topup','adm_max_topup'):
        ctx.user_data['step']=action
        return await send(update,'Digite o valor em reais (exemplo: 5,00):')
    if action in ('adm_bonus_topup','adm_bonus_register'):
        key='bonus_topup_percent' if action=='adm_bonus_topup' else 'bonus_register_cents'
        value=setting_int(key,0)
        label=(f'{value}%' if action=='adm_bonus_topup' else money(value))
        return await send(update,f'🎁 Bônus atual: <b>{label}</b> (zero = desligado).',keyboard([[('✏️ Configurar',action+'_edit')],[('🔕 Desligar',action+'_off')],[('↩️ Voltar','adm_finance_menu')]]))
    if action in ('adm_bonus_topup_off','adm_bonus_register_off'):
        save_setting('bonus_topup_percent' if 'topup' in action else 'bonus_register_cents','0')
        return await send(update,'✅ Bônus desligado.',admin_finance_menu())
    if action in ('adm_bonus_topup_edit','adm_bonus_register_edit'):
        ctx.user_data['step']=action
        return await send(update,'Digite o percentual de bônus de recarga de 0 até 100 (exemplo: 10).' if 'topup' in action else 'Digite o bônus de cadastro em reais (exemplo: 2,00).')
    if action=='adm_coupons':
        with conn() as c: rows=c.execute('SELECT * FROM coupons ORDER BY rowid DESC LIMIT 15').fetchall()
        lines=['🎟️ <b>Cupons cadastrados</b>']
        for r in rows:
            v=f"{r['amount']}%" if r['kind']=='percent' else money(r['amount'])
            lines.append(f"<code>{r['code']}</code> • {v} • {r['used']}/{r['max_uses'] or '∞'} • {'🟢' if r['active'] else '🔴'}")
        buttons=[[('➕ Criar cupom','adm_coupon_new')]]
        buttons += [[(f"{'🔴 Desativar' if r['active'] else '🟢 Ativar'} {r['code']}",f"adm_coupon_toggle:{r['code']}")] for r in rows]
        buttons.append([('↩️ Painel','adm_panel')])
        return await send(update,'\n'.join(lines),keyboard(buttons))
    if action=='adm_coupon_new':
        ctx.user_data['step']='coupon_new_code'
        return await send(update,'🎟️ Envie o código (3–24 letras, números, _ ou -).')
    if action.startswith('adm_coupon_toggle:'):
        code=action.split(':',1)[1]
        with conn() as c:c.execute('UPDATE coupons SET active=1-active WHERE code=?',(code,))
        return await send(update,'✅ Status atualizado.',keyboard([[('↩️ Cupons','adm_coupons')]]))
    if action=='adm_balance':
        ctx.user_data['step']='balance_user'
        return await send(update,'💰 Envie o ID do Telegram do cliente ou o seu próprio ID para adicionar saldo.')
    if action=='adm_refund':
        ctx.user_data['step']='refund_order'
        return await send(update,'↩️ Envie o número do pedido para devolver o valor pago ao saldo do comprador.\n\n⚠️ Não estorna o Pix no Mercado Pago.')
    if action.startswith('refund_confirm:'):
        oid=int(action.split(':')[1])
        with conn() as c:
            c.execute('BEGIN IMMEDIATE')
            order=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
            if not order:return await send(update,'Pedido não encontrado.',admin_menu())
            if c.execute('SELECT 1 FROM balance_adjustments WHERE order_id=?',(oid,)).fetchone():
                return await send(update,'Esse pedido já foi reembolsado em saldo.',admin_menu())
            c.execute('UPDATE users SET balance=balance+? WHERE id=?',(order['price'],order['user_id']))
            c.execute('INSERT INTO balance_adjustments(admin_id,user_id,amount,reason,order_id) VALUES(?,?,?,?,?)',
                      (uid,order['user_id'],order['price'],'Reembolso em saldo',oid))
        try:await ctx.bot.send_message(order['user_id'],f"↩️ Reembolso do pedido #{oid}: {money(order['price'])} creditados ao seu saldo.")
        except TelegramError: logging.info('Cliente não recebeu aviso de reembolso')
        return await send(update,f"✅ Reembolso de {money(order['price'])} realizado uma única vez.",admin_menu())
    if action.startswith('balance_confirm:'):
        token=action.split(':',1)[1]
        pending=ctx.user_data.get('balance_pending')
        if not pending or pending['token']!=token:return await send(update,'Operação expirada. Reabra /admin.',admin_menu())
        with conn() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE users SET balance=balance+? WHERE id=?',(pending['amount'],pending['uid']))
            c.execute('INSERT INTO balance_adjustments(admin_id,user_id,amount,reason) VALUES(?,?,?,?)',
                      (uid,pending['uid'],pending['amount'],'Crédito administrativo'))
        ctx.user_data.pop('balance_pending',None)
        return await send(update,f"✅ {money(pending['amount'])} adicionados ao saldo de {pending['uid']}.",admin_menu())
    if action=='adm_customize':
        return await send(update,'🎨 <b>Personalizar sua loja</b>\nEscolha o que deseja editar:',
            keyboard([[('🏪 Nome da loja','adm_store_name'),('✍️ Texto inicial','adm_welcome_text')],
                      [('🖼 Imagem inicial','adm_menu_image'),('✏️ Texto do catálogo','adm_catalog_subtitle')],
                      [('📨 Lembrete automático','adm_abandon_message'),('🚧 Texto manutenção','adm_maintenance_message')],
                      [('🔘 Nomes dos botões','adm_button_labels'),('🔗 Links dos botões','adm_menu_links')],
                      [('📦 Título do catálogo','adm_catalog_title'),('🖼 Imagem do catálogo','adm_catalog_image')],
                      [('↩️ Painel','adm_panel')]]))
    if action in ('adm_store_name','adm_catalog_subtitle','adm_abandon_message','adm_maintenance_message'):
        step_map={'adm_store_name':'store_name','adm_catalog_subtitle':'catalog_subtitle',
                  'adm_abandon_message':'abandon_message','adm_maintenance_message':'maintenance_message'}
        field=step_map[action]
        ctx.user_data['step']='edit_config_'+field
        return await send(update,'✍️ Envie o novo texto (máximo 900 caracteres).\nUse /admin para cancelar.')
    if action=='adm_welcome_text':
        ctx.user_data['step']='custom_welcome_text'
        return await send(update,'✍️ Envie o texto de apresentação do menu.\n\nO resumo da conta (nome, usuário, ID e saldo) continuará automático.\nMáximo de 500 caracteres.',keyboard([[('↩️ Voltar','adm_customize')]]))
    if action=='adm_catalog_title':
        ctx.user_data['step']='custom_catalog_title'
        return await send(update,'📦 Envie o novo título do catálogo (máximo 100 caracteres).\nExemplo: 📦 PRODUTOS DISPONÍVEIS',keyboard([[('↩️ Voltar','adm_customize')]]))
    if action=='adm_catalog_image':
        ctx.user_data['step']='catalog_image'
        return await send(update,'🖼 Envie a foto para aparecer acima da lista de produtos.',keyboard([[('🗑 Remover imagem','adm_catalog_image_remove')],[('↩️ Voltar','adm_customize')]]))
    if action=='adm_catalog_image_remove':
        with conn() as c:c.execute("DELETE FROM settings WHERE key='catalog_image_file_id'")
        ctx.user_data.pop('step',None)
        return await send(update,'✅ Imagem do catálogo removida.',admin_menu())
    if action=='adm_button_labels':
        return await send(update,'🔘 Selecione qual botão deseja renomear:',keyboard(
            [[(menu_label(k)[:40],f'adm_label:{k}')] for k,_ in MENU_BUTTONS]+[[('↩️ Voltar','adm_customize')]]))
    if action.startswith('adm_label:'):
        key=action.split(':',1)[1]
        if key not in dict(MENU_BUTTONS):return await send(update,'Botão inválido.')
        ctx.user_data['step']='custom_button_label';ctx.user_data['custom_key']=key
        return await send(update,f'🔘 Envie o novo nome do botão {html.escape(menu_label(key))} (máximo 45 caracteres).')
    if action=='adm_menu_links':
        return await send(update,'🔗 Selecione o link que deseja alterar:',keyboard([
            [('📞 Suporte','adm_link:support')],[('👥 Grupo','adm_link:group')],
            [('🌐 Loja Web','adm_link:web')],[('↩️ Voltar','adm_customize')]]))
    if action.startswith('adm_link:'):
        key=action.split(':',1)[1]
        if key not in ('support','group','web'):return await send(update,'Link inválido.')
        ctx.user_data['step']='custom_menu_link';ctx.user_data['custom_key']=key
        return await send(update,'🔗 Envie o endereço completo começando com https:// (sem links de lojas de terceiros).')
    if action=="adm_menu_image":
        ctx.user_data['step']='menu_image'
        return await send(update,"🖼 Envie a foto que deseja mostrar no menu inicial da Loja Demonstração.\n\nVocê também pode remover a foto abaixo.",keyboard([[('🗑 Remover imagem','adm_menu_image_remove')],[('↩️ Painel','adm_panel')]]))
    if action=="adm_menu_image_remove":
        with conn() as c:c.execute("DELETE FROM settings WHERE key='menu_image_file_id'")
        ctx.user_data.pop('step',None)
        return await send(update,"✅ Imagem do menu removida.",admin_menu())
    if action=="adm_delete_products":
        with conn() as c:
            rows=c.execute("SELECT id,name FROM products WHERE archived=0 ORDER BY id DESC LIMIT 60").fetchall()
        if not rows:return await send(update,"Nenhum produto disponível para exclusão.",admin_menu())
        return await send(update,"🗑 <b>Escolha um produto para retirar do catálogo:</b>",keyboard([[(f"📦 #{r['id']} • {r['name'][:35]}",f"adm_delete_product:{r['id']}")] for r in rows]+[[('↩️ Painel','adm_panel')]]))
    if action.startswith('adm_delete_product:'):
        pid=int(action.split(':')[1])
        with conn() as c:
            r=c.execute("SELECT id,name FROM products WHERE id=? AND archived=0",(pid,)).fetchone()
            if not r:return await send(update,"Produto já excluído ou não encontrado.",admin_menu())
            available=c.execute("SELECT COUNT(*) FROM stock WHERE product_id=? AND sold=0",(pid,)).fetchone()[0]
            pending=c.execute("SELECT COUNT(*) FROM pix_payments WHERE product_id=? AND status IN ('pending','creating')",(pid,)).fetchone()[0]
        return await send(update,f"⚠️ <b>Excluir {html.escape(r['name'])} do catálogo?</b>\n\n📦 Estoque ainda disponível: {available} unidade(s)\n💳 Pix em aberto: {pending}\n\nO produto deixará de aparecer para clientes, mas pedidos, vendas, estoque e pagamentos anteriores serão preservados. Pix já emitidos ainda poderão ser concluídos.\n\n<b>Confirme somente se não pretende mais vender esse produto.</b>",keyboard([[('🗑 SIM, EXCLUIR',f'adm_delete_confirm:{pid}')],[('↩️ Cancelar','adm_delete_products')]]))
    if action.startswith('adm_delete_confirm:'):
        pid=int(action.split(':')[1])
        with conn() as c:
            c.execute('BEGIN IMMEDIATE')
            changed=c.execute('UPDATE products SET active=0,archived=1 WHERE id=? AND archived=0',(pid,)).rowcount
            if changed:c.execute('DELETE FROM restock_alerts WHERE product_id=?',(pid,))
        return await send(update,'✅ Produto excluído do catálogo. Estoque, vendas e pagamentos antigos foram preservados.' if changed else '⚠️ Produto já excluído ou não encontrado.',keyboard([[('🗑 Excluir outro','adm_delete_products')],[('↩️ Painel','adm_panel')]]))
    if action=="adm_edit_products":
        with conn() as c:rows=c.execute("SELECT id,name FROM products WHERE archived=0 ORDER BY id DESC LIMIT 60").fetchall()
        if not rows:return await send(update,"Nenhum produto cadastrado.",admin_menu())
        return await send(update,"✏️ <b>Escolha o produto que deseja editar:</b>",keyboard([[(f"📦 #{r['id']} • {r['name'][:35]}",f"adm_edit_product:{r['id']}")] for r in rows]+[[('↩️ Painel','adm_panel')]]))
    if action.startswith('adm_edit_product:'):
        pid=int(action.split(':')[1])
        with conn() as c:r=c.execute("SELECT * FROM products WHERE id=? AND archived=0",(pid,)).fetchone()
        if not r:return await send(update,"Produto não encontrado ou já excluído.",admin_menu())
        return await send(update,f"✏️ <b>{html.escape(r['name'])}</b>\nDescrição: {html.escape(r['description'])}\nPreço: {money(r['price'])}\nStatus: {'Ativo' if r['active'] else 'Oculto'}\n\nO estoque e o histórico de vendas serão preservados.",keyboard([[('📝 Nome',f'edit_field:{pid}:name'),('📄 Descrição',f'edit_field:{pid}:description')],[('💰 Preço',f'edit_field:{pid}:price'),('🖼 Foto',f'adm_image:{pid}')],[('👁 Ativar/Ocultar',f'edit_toggle:{pid}')],[('↩️ Produtos','adm_edit_products')]]))
    if action.startswith('edit_toggle:'):
        pid=int(action.split(':')[1])
        with conn() as c:c.execute("UPDATE products SET active=CASE WHEN active=1 THEN 0 ELSE 1 END WHERE id=? AND archived=0",(pid,))
        return await send(update,"✅ Visibilidade atualizada.",keyboard([[('↩️ Produto',f'adm_edit_product:{pid}')]]))
    if action.startswith('edit_field:'):
        _,pid,field=action.split(':')
        if field not in ('name','description','price'):return await send(update,"Campo inválido.")
        ctx.user_data['step']='edit_product_field';ctx.user_data['edit_pid']=int(pid);ctx.user_data['edit_field']=field
        label={'name':'nome','description':'descrição','price':'preço'}[field]
        return await send(update,f"✏️ Envie o novo {label} do produto:")
    if action=="adm_add":
        ctx.user_data["step"]="add_name"
        return await send(update,"Nome do novo produto:")
    if action=="adm_images":
        with conn() as c:
            rows=c.execute("SELECT id,name,image_file_id FROM products WHERE archived=0 ORDER BY id DESC LIMIT 60").fetchall()
        if not rows:
            return await send(update,"Nenhum produto cadastrado.",admin_menu())
        return await send(update,"🖼 <b>Escolha o produto para adicionar, trocar ou remover a foto:</b>",
                          keyboard([[(f"{r['name'][:45]} {'🖼' if r['image_file_id'] else '▫️'}",f"adm_image:{r['id']}")] for r in rows]+[[('↩️ Painel','home')]]))
    if action.startswith("adm_image:"):
        pid=int(action.split(":",1)[1])
        with conn() as c:
            r=c.execute("SELECT id,name,image_file_id FROM products WHERE id=? AND archived=0",(pid,)).fetchone()
        if not r:return await send(update,"Produto não encontrado.",admin_menu())
        ctx.user_data['image_pid']=pid
        ctx.user_data['step']='product_image'
        return await send(update,f"🖼 <b>{html.escape(r['name'])}</b>\nEnvie uma foto do produto pelo Telegram.\n\nOu escolha abaixo:",
                          keyboard([[('🗑 Remover foto',f'img_remove:{pid}')], [('↩️ Voltar','adm_images')]]))
    if action.startswith("img_remove:"):
        pid=int(action.split(":",1)[1])
        with conn() as c:c.execute("UPDATE products SET image_file_id=NULL WHERE id=? AND archived=0",(pid,))
        ctx.user_data.pop('step',None)
        ctx.user_data.pop('image_pid',None)
        return await send(update,"✅ Foto removida. O produto continua disponível.",admin_menu())
    if action=="adm_stock":
        ctx.user_data["step"]="stock_pid"
        return await send(update,"Informe o ID numérico do produto para adicionar estoque:")
    if action=="adm_inventory" or action.startswith("inventory_page:"):
        page=int(action.split(":")[1]) if action.startswith("inventory_page:") else 0
        page=max(0,page)
        with conn() as c:
            count=c.execute("SELECT COUNT(*) FROM stock WHERE sold=0").fetchone()[0]
            pages=max(1,(count+7)//8)
            page=min(page,pages-1)
            rows=c.execute("SELECT s.id,s.product_id,p.name FROM stock s JOIN products p ON p.id=s.product_id WHERE s.sold=0 ORDER BY s.id DESC LIMIT 8 OFFSET ?",(page*8,)).fetchall()
        buttons=[[(f"📦 #{r['id']} • {r['name'][:28]}",f"inventory_item:{r['id']}:{page}")] for r in rows]
        nav=[]
        if page>0:nav.append(("⬅️ Anterior",f"inventory_page:{page-1}"))
        if page+1<pages:nav.append(("Próxima ➡️",f"inventory_page:{page+1}"))
        if nav:buttons.append(nav)
        buttons.append([("⚙️ Painel", "adm_panel")])
        return await send(update,f"🗂 <b>Estoque disponível</b> • {count} unidades\nPágina {page+1}/{pages}\nSelecione uma unidade para visualizar, editar ou excluir.",keyboard(buttons))
    if action=="adm_panel":return await send(update,"⚙️ <b>Painel administrativo</b>",admin_menu())
    if action.startswith("inventory_item:"):
        _,sid,page=action.split(":");sid=int(sid);page=int(page)
        with conn() as c:
            r=c.execute("SELECT s.id,s.content,p.name FROM stock s JOIN products p ON p.id=s.product_id WHERE s.id=? AND s.sold=0",(sid,)).fetchone()
        if not r:return await send(update,"Essa unidade já foi vendida ou removida.",keyboard([[('↩️ Estoque',f'inventory_page:{page}')]]))
        return await send(update,f"📦 <b>{html.escape(r['name'])}</b> • Unidade #{sid}\n\n<code>{html.escape(r['content'])}</code>",keyboard([[('✏️ Editar',f'inventory_edit:{sid}:{page}')],[('🗑 Excluir',f'inventory_delete:{sid}:{page}')],[('↩️ Voltar',f'inventory_page:{page}')]]))
    if action.startswith("inventory_edit:"):
        _,sid,page=action.split(":");sid=int(sid);page=int(page)
        with conn() as c:r=c.execute("SELECT id FROM stock WHERE id=? AND sold=0",(sid,)).fetchone()
        if not r:return await send(update,"Unidade indisponível para edição.")
        ctx.user_data['step']='inventory_edit_content';ctx.user_data['inventory_id']=sid;ctx.user_data['inventory_page']=page
        return await send(update,"✏️ Envie a mensagem completa que deseja entregar nesta unidade. Pode usar qualquer texto e emojis.\n\nApenas esta unidade será atualizada.")
    if action.startswith("inventory_delete:"):
        _,sid,page=action.split(":");sid=int(sid);page=int(page)
        with conn() as c:r=c.execute("SELECT id FROM stock WHERE id=? AND sold=0",(sid,)).fetchone()
        if not r:return await send(update,"Unidade indisponível para exclusão.")
        return await send(update,f"⚠️ Excluir definitivamente a unidade #{sid}? Essa ação não pode ser desfeita.",keyboard([[('🗑 Sim, excluir',f'inventory_confirm_delete:{sid}:{page}')],[('Cancelar',f'inventory_item:{sid}:{page}')]]))
    if action.startswith("inventory_confirm_delete:"):
        _,sid,page=action.split(":");sid=int(sid);page=int(page)
        with conn() as c:
            c.execute('BEGIN IMMEDIATE')
            cur=c.execute('DELETE FROM stock WHERE id=? AND sold=0',(sid,))
            removed=cur.rowcount
        return await send(update,'✅ Unidade excluída.' if removed else '⚠️ Unidade já vendida ou removida.',keyboard([[('↩️ Estoque',f'inventory_page:{page}')]]))
    if action=="adm_price":
        ctx.user_data["step"]="price_pid"
        return await send(update,"Informe o ID numérico do produto para alterar o preço:")
    if action=="adm_products":
        with conn() as c:rows=c.execute("SELECT * FROM products WHERE archived=0 ORDER BY id DESC").fetchall()
        return await send(update,"📋 <b>Produtos</b>\n"+("\n".join(f"#{r['id']} • {html.escape(r['name'])} • {money(r['price'])}" for r in rows) if rows else "Nenhum produto."),admin_menu())
    if action=='adm_stats':
        with conn() as c:
            users=c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
            total=c.execute('SELECT COUNT(*),COALESCE(SUM(price),0),COALESCE(SUM(COALESCE(original_price,price)-price),0) FROM orders').fetchone()
            lines=['📊 <b>Estatísticas avançadas</b>',f'👥 Clientes: {users}',f'🛒 Vendas totais: {total[0]} • {money(total[1])}']
            for label,days in [('Hoje',0),('7 dias',7),('30 dias',30)]:
                # SQLite registra created_at em UTC.
                since=(datetime.datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0) if days==0 else datetime.datetime.now(timezone.utc)-datetime.timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
                r=c.execute('SELECT COUNT(*),COALESCE(SUM(price),0) FROM orders WHERE created_at>=?',(since,)).fetchone()
                lines.append(f'📅 {label}: {r[0]} vendas • {money(r[1])}')
            lines.append(f'🎟️ Descontos concedidos: {money(total[2])}')
            topups=c.execute("SELECT COALESCE(SUM(amount),0) FROM pix_payments WHERE kind='topup' AND status='approved'").fetchone()[0]
            topups+=c.execute("SELECT COALESCE(SUM(amount),0) FROM topups WHERE status='approved'").fetchone()[0]
            lines.append(f'💳 Recargas confirmadas: {money(topups)}')
            refunds=c.execute("SELECT COUNT(*),COALESCE(SUM(amount),0) FROM balance_adjustments WHERE order_id IS NOT NULL").fetchone()
            lines.append(f'↩️ Reembolsos em saldo: {refunds[0]} • {money(refunds[1])}')
            top=c.execute('SELECT p.name,COUNT(*) qty FROM orders o JOIN products p ON p.id=o.product_id GROUP BY o.product_id ORDER BY qty DESC LIMIT 5').fetchall()
            if top:lines.append('\n🏆 <b>Mais vendidos</b>')
            lines.extend(f"{i}. {html.escape(r['name'])}: {r['qty']}" for i,r in enumerate(top,1))
        return await send(update,'\n'.join(lines),admin_menu())
    if action=="adm_topups":
        with conn() as c:rows=c.execute("SELECT * FROM topups WHERE status='pending' ORDER BY id LIMIT 15").fetchall()
        if not rows:return await send(update,"Nenhuma recarga pendente.",admin_menu())
        return await send(update,"💸 <b>Recargas pendentes</b>\nConfirme no banco antes de aprovar.",keyboard([[(f"#{r['id']} • {r['user_id']} • {money(r['amount'])}",f"topup_view:{r['id']}")] for r in rows]))
    if action.startswith("topup_view:"):
        rid=int(action.split(":")[1])
        with conn() as c:r=c.execute("SELECT * FROM topups WHERE id=? AND status='pending'",(rid,)).fetchone()
        if not r:return await send(update,"Recarga já processada.")
        return await send(update,f"Recarga #{rid}\nCliente: {r['user_id']}\nValor: {money(r['amount'])}\n⚠️ Confira o Pix no banco.",keyboard([[("✅ Aprovar",f"approve:{rid}"),("❌ Rejeitar",f"reject:{rid}")]]))
    if action.startswith(("approve:","reject:")):
        op,rid=action.split(":");rid=int(rid)
        with conn() as c:
            c.execute("BEGIN IMMEDIATE")
            r=c.execute("SELECT * FROM topups WHERE id=? AND status='pending'",(rid,)).fetchone()
            if not r:return await send(update,"Recarga já processada.")
            status="approved" if op=="approve" else "rejected"
            c.execute("UPDATE topups SET status=? WHERE id=?",(status,rid))
            if op=="approve":c.execute("UPDATE users SET balance=balance+? WHERE id=?",(r["amount"],r["user_id"]))
        try:await ctx.bot.send_message(r["user_id"],f"{'✅ Recarga aprovada' if op=='approve' else '❌ Recarga rejeitada'}: {money(r['amount'])}")
        except Exception:logging.exception("Falha ao notificar cliente")
        return await send(update,f"Recarga #{rid}: {status}",admin_menu())
    await send(update,"Opção inválida.",main_menu())
async def message(update,ctx):
    uid=update.effective_user.id
    user_save(update.effective_user)
    value=(update.message.text or "").strip()
    step=ctx.user_data.get("step")
    if not await access_guard(update,ctx):return
    if not step:return await send(update,"Use /start para abrir o menu.")
    if step=='coupon_code':
        pid=ctx.user_data.pop('coupon_pid',None);ctx.user_data.pop('step',None)
        if not pid:return await send(update,'Escolha o produto novamente.',main_menu())
        code=coupon_code(value)
        if not code:return await send(update,'Código inválido.',purchase_buttons(pid))
        with conn() as c:
            p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(pid,)).fetchone()
            if not p:return await send(update,'Produto indisponível.',main_menu())
            try:price,_=coupon_price(c,code,p['price'])
            except ValueError as exc:return await send(update,html.escape(str(exc)),purchase_buttons(pid))
        return await send(update,f"🎟️ <b>Cupom {code}</b>\nAntes: <s>{money(p['price'])}</s>\nAgora: <b>{money(price)}</b>",
                          keyboard([[('💳 Pagar com saldo',f'coupon_checkout:saldo:{pid}:{code}')],
                                    [('⚡ Pagar com Pix',f'coupon_checkout:pix:{pid}:{code}')],
                                    [('↩️ Produto',f'product:{pid}')]]))
    if step=="pix_topup_amount":
        amount=parse_amount(value)
        if not amount:return await send(update,"Valor inválido. Exemplo: 25,00")
        if not setting_int("min_topup",100)<=amount<=setting_int("max_topup",10000000):return await send(update,"Valor fora dos limites de recarga configurados.")
        ctx.user_data.pop("step",None)
        return await generate_pix(update,ctx,uid,"topup",None,amount)
    if step=="topup_amount":
        amount=parse_amount(value)
        if not amount:return await send(update,"Valor inválido. Exemplo: 25,00")
        if not setting_int("min_topup",100)<=amount<=setting_int("max_topup",10000000):return await send(update,"Valor fora dos limites de recarga configurados.")
        with conn() as c:rid=c.execute("INSERT INTO topups(user_id,amount) VALUES(?,?)",(uid,amount)).lastrowid
        ctx.user_data.pop("step",None)
        pix=os.getenv("PIX_KEY","não configurada")
        await send(update,f"💳 <b>Solicitação #{rid}</b>\nValor: {money(amount)}\nChave Pix: <code>{html.escape(pix)}</code>\nFavorecido: {html.escape(os.getenv('PIX_RECEIVER','Loja Demonstração'))}\n\nApós pagar, aguarde a conferência. <b>O crédito não é automático.</b>",main_menu())
        for aid in ADMINS:
            try:await ctx.bot.send_message(aid,f"💸 Nova recarga #{rid}\nCliente: {uid}\nValor: {money(amount)}\nConfira o pagamento no banco e abra /admin.")
            except Exception:logging.exception("Falha na notificação de recarga")
        return
    if not is_admin(uid):
        ctx.user_data.pop("step",None);return await send(update,"Acesso restrito.")
    if step in ('adm_ban','adm_unban','adm_add_admin','adm_remove_admin'):
        if not value.isdigit() or int(value)<=0:return await send(update,'ID inválido. Envie somente números.')
        target=int(value)
        if step in ('adm_add_admin','adm_remove_admin') and not owner_only(uid):return await send(update,'Somente o proprietário pode gerenciar administradores.')
        if step=='adm_ban' and (is_admin(target) or target in ROOT_ADMINS):return await send(update,'Não é possível banir administradores.')
        if step=='adm_remove_admin' and target in ROOT_ADMINS:return await send(update,'Administradores principais são protegidos.')
        with conn() as c:
            if step=='adm_ban':c.execute('INSERT OR IGNORE INTO banned_users(user_id) VALUES(?)',(target,))
            elif step=='adm_unban':c.execute('DELETE FROM banned_users WHERE user_id=?',(target,))
            elif step=='adm_add_admin':c.execute('INSERT OR IGNORE INTO extra_admins(user_id) VALUES(?)',(target,))
            elif step=='adm_remove_admin':c.execute('DELETE FROM extra_admins WHERE user_id=?',(target,))
        ctx.user_data.pop('step',None)
        return await send(update,'✅ Alteração aplicada ao usuário '+str(target),admin_menu())
    if step in ('adm_min_topup','adm_max_topup'):
        v=parse_amount(value)
        if v is None:return await send(update,'Valor inválido.')
        low=setting_int('min_topup',100);high=setting_int('max_topup',10000000)
        if step=='adm_min_topup' and v>high:return await send(update,'Mínimo não pode ultrapassar o máximo.')
        if step=='adm_max_topup' and v<low:return await send(update,'Máximo não pode ser menor que o mínimo.')
        save_setting('min_topup' if step=='adm_min_topup' else 'max_topup',str(v))
        ctx.user_data.pop('step',None)
        return await send(update,'✅ Limite alterado.',admin_finance_menu())
    if step=='adm_bonus_topup_edit':
        if not value.isdigit() or not 0<=int(value)<=100:return await send(update,'Informe percentual inteiro de 0 até 100.')
        save_setting('bonus_topup_percent',value)
        ctx.user_data.pop('step',None)
        return await send(update,'✅ Bônus de depósito salvo.',admin_finance_menu())
    if step=='adm_bonus_register_edit':
        v=parse_amount(value) if value!='0' else 0
        if v is None or v>10000:return await send(update,'Bônus inválido (máximo R$ 100).')
        save_setting('bonus_register_cents',str(v))
        ctx.user_data.pop('step',None)
        return await send(update,'✅ Bônus de registro salvo.',admin_finance_menu())
    if step=='forced_chat_link':
        if value!='0' and not re.fullmatch(r'https://(?:t\.me|telegram\.me)/(?:\+[A-Za-z0-9_-]{8,}|[A-Za-z0-9_]{5,32})(?:/)?',value):
            return await send(update,'Link inválido. Envie https://t.me/seu_canal ou um convite https://t.me/+...')
        save_setting('forced_chat_url','' if value=='0' else value)
        ctx.user_data.pop('step',None)
        return await send(update,'✅ Link para entrar salvo.',admin_settings_menu())
    if step=='forced_chat':
        if not (re.fullmatch(r'@[A-Za-z0-9_]{5,32}',value) or re.fullmatch(r'-100[0-9]{6,18}',value)):
            return await send(update,'Envie um @canal válido ou ID de supergrupo/canal (-100...).')
        try:
            member=await ctx.bot.get_chat_member(chat_id=value,user_id=(await ctx.bot.get_me()).id)
            if member.status not in ('administrator','creator'):return await send(update,'O bot precisa ser administrador desse grupo/canal antes de ativar a exigência.')
        except TelegramError:return await send(update,'Não consegui acessar esse grupo/canal. Adicione o bot como administrador e tente de novo.')
        save_setting('forced_chat',value)
        ctx.user_data.pop('step',None)
        return await send(update,'✅ Entrada obrigatória ativada.',admin_settings_menu())
    if step=='coupon_new_code':
        code=coupon_code(value)
        if not code:return await send(update,'Código inválido. Use 3–24 letras, números, _ ou -.')
        with conn() as c:
            if c.execute('SELECT 1 FROM coupons WHERE code=?',(code,)).fetchone():return await send(update,'Esse código já existe.')
        ctx.user_data['new_coupon']={'code':code};ctx.user_data['step']='coupon_new_type'
        return await send(update,'Tipo do desconto: envie <b>percent</b> (porcentagem) ou <b>fixed</b> (reais).')
    if step=='coupon_new_type':
        if value.lower() not in ('percent','fixed'):return await send(update,'Digite percent ou fixed.')
        ctx.user_data['new_coupon']['kind']=value.lower();ctx.user_data['step']='coupon_new_amount'
        return await send(update,'Digite a porcentagem (ex.: 15) ou valor em reais (ex.: 5,00).')
    if step=='coupon_new_amount':
        coupon=ctx.user_data['new_coupon']
        if coupon['kind']=='percent':
            if not value.isdigit() or not 1<=int(value)<=99:return await send(update,'Porcentagem deve ser entre 1 e 99.')
            amount=int(value)
        else:
            amount=parse_amount(value)
            if not amount:return await send(update,'Valor inválido.')
        coupon['amount']=amount;ctx.user_data['step']='coupon_new_limit'
        return await send(update,'Limite de usos? Envie 0 para ilimitado ou um número inteiro.')
    if step=='coupon_new_limit':
        if not value.isdigit() or int(value)>1000000:return await send(update,'Digite um número entre 0 e 1000000.')
        ctx.user_data['new_coupon']['max_uses']=int(value);ctx.user_data['step']='coupon_new_expiry'
        return await send(update,'Validade: envie uma data AAAA-MM-DD (UTC), ou 0 para não expirar.')
    if step=='coupon_new_expiry':
        if value=='0':expiry=None
        else:
            try:
                date=datetime.date.fromisoformat(value)
                if date<datetime.datetime.now(timezone.utc).date():raise ValueError()
                expiry=date.strftime('%Y-%m-%d 23:59:59')
            except ValueError:return await send(update,'Data inválida. Use AAAA-MM-DD ou 0.')
        coupon=ctx.user_data['new_coupon']
        with conn() as c:
            c.execute('INSERT INTO coupons(code,kind,amount,max_uses,expires_at) VALUES(?,?,?,?,?)',
                      (coupon['code'],coupon['kind'],coupon['amount'],coupon['max_uses'],expiry))
        ctx.user_data.pop('new_coupon',None);ctx.user_data.pop('step',None)
        return await send(update,f"✅ Cupom <code>{coupon['code']}</code> criado!",admin_menu())
    if step=='balance_user':
        if not value.isdigit():return await send(update,'Envie um ID numérico.')
        target=int(value)
        with conn() as c:
            user=c.execute('SELECT id,name FROM users WHERE id=?',(target,)).fetchone()
        if not user:return await send(update,'Usuário não encontrado. Ele precisa iniciar o bot com /start primeiro.')
        ctx.user_data['balance_target']=target;ctx.user_data['step']='balance_amount'
        return await send(update,f"👤 {html.escape(user['name'] or str(target))}\nQuanto deseja adicionar? Ex.: 100,00")
    if step=='balance_amount':
        amount=parse_amount(value)
        if not amount:return await send(update,'Valor inválido. Ex.: 100,00')
        target=ctx.user_data.get('balance_target')
        token=uuid.uuid4().hex[:12]
        ctx.user_data['balance_pending']={'uid':target,'amount':amount,'token':token}
        ctx.user_data.pop('step',None)
        return await send(update,f'⚠️ Confirmar crédito de <b>{money(amount)}</b> para ID <code>{target}</code>?',
                          keyboard([[('✅ Confirmar',f'balance_confirm:{token}')],[('❌ Cancelar','adm_panel')]]))
    if step=='refund_order':
        if not value.isdigit():return await send(update,'Digite o número do pedido.')
        oid=int(value)
        with conn() as c:
            order=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
            refunded=c.execute('SELECT 1 FROM balance_adjustments WHERE order_id=?',(oid,)).fetchone()
        if not order:return await send(update,'Pedido não encontrado.')
        if refunded:return await send(update,'Pedido já reembolsado em saldo.',admin_menu())
        ctx.user_data.pop('step',None)
        return await send(update,f"⚠️ Reembolsar pedido #{oid}\nCliente: <code>{order['user_id']}</code>\nCrédito: <b>{money(order['price'])}</b>\n\nIsso NÃO estorna o Pix.",
                          keyboard([[('↩️ Confirmar reembolso',f'refund_confirm:{oid}')],[('❌ Cancelar','adm_panel')]]))
    if step and step.startswith('edit_config_'):
        field=step[len('edit_config_'):]
        if field not in {'store_name','catalog_subtitle','abandon_message','maintenance_message'}:
            return await send(update,'Campo de configuração inválido.')
        if not 1<=len(value)<=900:
            return await send(update,'Digite entre 1 e 900 caracteres.')
        save_setting(field,value)
        ctx.user_data.pop('step',None)
        return await send(update,'✅ Configuração salva. A alteração já está ativa.',
                          keyboard([[('🎨 Continuar editando','adm_customize')],[('⚙️ Painel','adm_panel')]]))
    if step in ('custom_welcome_text','custom_catalog_title','custom_button_label','custom_menu_link','catalog_image'):
        if step=='catalog_image':
            if not update.message.photo:return await send(update,'📷 Envie a imagem como foto pelo Telegram.')
            save_setting('catalog_image_file_id',update.message.photo[-1].file_id)
        elif step=='custom_welcome_text':
            if not 1<=len(value)<=500:return await send(update,'Envie entre 1 e 500 caracteres.')
            save_setting('menu_welcome_text',value)
        elif step=='custom_catalog_title':
            if not 1<=len(value)<=100:return await send(update,'Envie entre 1 e 100 caracteres.')
            save_setting('catalog_title',value)
        elif step=='custom_button_label':
            key=ctx.user_data.get('custom_key')
            if key not in dict(MENU_BUTTONS) or not 1<=len(value)<=45:return await send(update,'Nome inválido (máximo 45 caracteres).')
            save_setting('menu_button_'+key,value)
        elif step=='custom_menu_link':
            key=ctx.user_data.get('custom_key')
            if key not in ('support','group','web') or not re.fullmatch(r'https://[^\s]+',value) or len(value)>500:
                return await send(update,'Link inválido. Use um endereço https:// completo.')
            save_setting('menu_url_'+key,value)
        ctx.user_data.pop('step',None);ctx.user_data.pop('custom_key',None)
        return await send(update,'✅ Personalização salva!',keyboard([[('🎨 Editar mais','adm_customize')],[('⚙️ Painel','adm_panel')]]))
    if step=='menu_image':
        if not update.message.photo:return await send(update,"📷 Envie a imagem como foto pelo Telegram.")
        fid=update.message.photo[-1].file_id
        with conn() as c:c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('menu_image_file_id',?)",(fid,))
        ctx.user_data.pop('step',None)
        return await send(update,"✅ Imagem do menu salva! Abra /start para conferir.",admin_menu())
    if step=='edit_product_field':
        pid=ctx.user_data.get('edit_pid');field=ctx.user_data.get('edit_field')
        if field not in ('name','description','price') or not pid:return await send(update,"Abra /admin e escolha o produto novamente.")
        if field=='price':
            val=parse_amount(value)
            if not val:return await send(update,"Preço inválido. Exemplo: 19,90")
        elif field=='name':
            if not value or len(value)>100:return await send(update,"Nome obrigatório, máximo 100 caracteres.")
            val=value
        else:
            if len(value)>1000:return await send(update,"Descrição muito longa (máximo 1000 caracteres).")
            val=value
        with conn() as c:changed=c.execute(f"UPDATE products SET {field}=? WHERE id=? AND archived=0",(val,pid)).rowcount
        ctx.user_data.pop('step',None);ctx.user_data.pop('edit_pid',None);ctx.user_data.pop('edit_field',None)
        return await send(update,"✅ Produto atualizado!" if changed else "Produto não encontrado.",keyboard([[('↩️ Voltar ao produto',f'adm_edit_product:{pid}')]]))
    if step=="product_image":
        if not update.message.photo:
            return await send(update,"📷 Envie uma imagem como <b>Foto</b> no Telegram, não como arquivo.")
        pid=ctx.user_data.get('image_pid')
        if not pid:return await send(update,"Abra /admin e selecione 🖼 Imagens novamente.")
        file_id=update.message.photo[-1].file_id
        with conn() as c:
            changed=c.execute("UPDATE products SET image_file_id=? WHERE id=? AND archived=0",(file_id,pid)).rowcount
        ctx.user_data.pop('step',None)
        ctx.user_data.pop('image_pid',None)
        return await send(update,"✅ Foto salva! Abra 🛍 Produtos para visualizar." if changed else "Produto não encontrado.",admin_menu())
    if step=="add_name":
        if len(value)>100:return await send(update,"Nome muito longo (máx. 100).")
        ctx.user_data["name"]=value;ctx.user_data["step"]="add_description"
        return await send(update,"Descrição do produto:")
    if step=="add_description":
        ctx.user_data["description"]=value[:1000];ctx.user_data["step"]="add_price"
        return await send(update,"Preço em reais (exemplo: 19,90):")
    if step=="add_price":
        price=parse_amount(value)
        if not price:return await send(update,"Preço inválido.")
        with conn() as c:
            pid=c.execute("INSERT INTO products(name,description,price) VALUES(?,?,?)",(ctx.user_data["name"],ctx.user_data["description"],price)).lastrowid
        ctx.user_data.clear()
        return await send(update,f"✅ Produto #{pid} cadastrado! Adicione unidades pelo painel.",admin_menu())
    if step in ("stock_pid","price_pid"):
        if not value.isdigit():return await send(update,"Informe um ID numérico.")
        with conn() as c:r=c.execute("SELECT id FROM products WHERE id=? AND archived=0",(int(value),)).fetchone()
        if not r:return await send(update,"Produto não encontrado.")
        ctx.user_data["pid"]=int(value)
        ctx.user_data["step"]="stock_items" if step=="stock_pid" else "price_value"
        return await send(update,"📥 Envie a mensagem completa de cada unidade, com o texto e emojis que quiser.\n\nPara adicionar várias unidades do mesmo produto, separe cada mensagem com --- sozinho em uma linha.\n\nMáximo: 50 unidades por envio." if step=="stock_pid" else "Digite o novo preço:")
    if step=="inventory_edit_content":
        sid=ctx.user_data.get('inventory_id')
        page=ctx.user_data.get('inventory_page',0)
        try:
            units=parse_stock_units(value)
            if len(units)!=1:raise ValueError('Envie somente uma unidade por edição (sem separador ---).')
        except ValueError as exc:return await send(update,html.escape(str(exc)))
        with conn() as c:
            c.execute('BEGIN IMMEDIATE')
            cur=c.execute('UPDATE stock SET content=? WHERE id=? AND sold=0',(units[0],sid))
            changed=cur.rowcount
        ctx.user_data.clear()
        return await send(update,'✅ Unidade atualizada!' if changed else '⚠️ Unidade já vendida ou removida. Nenhuma alteração foi feita.',keyboard([[('🗂 Voltar ao estoque',f'inventory_page:{page}')]]))
    if step=="stock_items":
        try:items=parse_stock_units(value)
        except ValueError as exc:return await send(update,html.escape(str(exc)))
        pid=ctx.user_data['pid']
        with conn() as c:
            c.execute('BEGIN IMMEDIATE')
            c.executemany("INSERT INTO stock(product_id,content) VALUES(?,?)",[(pid,x) for x in items])
            row=c.execute("SELECT name,image_file_id FROM products WHERE id=? AND archived=0",(pid,)).fetchone()
            if row:
                current=c.execute("SELECT COUNT(*) FROM stock WHERE product_id=? AND sold=0",(pid,)).fetchone()[0]
                c.execute("INSERT INTO nexo_restock_outbox(event_id,product_id,product_name,quantity_delta,stock_current,photo_file_id) VALUES(?,?,?,?,?,?)",
                          ('tg-restock-'+str(uuid.uuid4()),pid,row['name'],len(items),current,row['image_file_id']))
        ctx.user_data.clear()
        await notify_restock(ctx,pid)
        await check_low_stock(ctx.bot,pid)
        await announce_group_restock(ctx,pid,len(items))
        return await send(update,f"✅ {len(items)} unidades adicionadas. Avisos de reposição enviados quando solicitados.",admin_menu())
    if step=="price_value":
        price=parse_amount(value)
        if not price:return await send(update,"Preço inválido.")
        with conn() as c:c.execute("UPDATE products SET price=? WHERE id=? AND archived=0",(price,ctx.user_data["pid"]))
        ctx.user_data.clear()
        return await send(update,"✅ Preço atualizado.",admin_menu())
async def generate_pix(update,ctx,uid,kind,pid,amount,coupon=None,original_price=None):
    if kind=='topup' and not setting_int('min_topup',100)<=amount<=setting_int('max_topup',10000000):
        return await send(update,'Valor fora dos limites permitidos de recarga.')
    if not MP_TOKEN:
        return await send(update,"Pix indisponível: configure MP_ACCESS_TOKEN no Railway.")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",MP_PAYER_EMAIL):
        return await send(update,"Pix indisponível: configure MP_PAYER_EMAIL no Railway. Fale com o suporte.")
    try:
        rid,code,qr=await asyncio.to_thread(create_pix,uid,kind,pid,amount,MP_PAYER_EMAIL,coupon,original_price)
        if qr:
            img=io.BytesIO(base64.b64decode(qr));img.name="pix.png"
        else:
            img=io.BytesIO();qrcode.make(code).save(img,format="PNG");img.seek(0);img.name="pix.png"
        buttons=keyboard([[('🔄 Conferir pagamento',f'checkpix:{rid}')],[('🏠 Início','home')]])
        caption=(f"🟣 <b>LOJA DEMONSTRAÇÃO</b>\n"
                 f"💰 Valor: <b>{money(amount)}</b>\n\n"
                 f"📋 <b>Pix Copia e Cola:</b>\n<code>{html.escape(code)}</code>\n\n"
                 "📷 Escaneie o QR Code acima para pagar.\n"
                 "✅ A confirmação será verificada automaticamente.")
        if len(caption) > 1024:
            # Em Pix excepcionalmente longos, não ultrapassar o limite do Telegram.
            caption=f"🟣 <b>LOJA DEMONSTRAÇÃO</b>\n💰 Valor: <b>{money(amount)}</b>\n📋 Pix Copia e Cola enviado abaixo."
            extra_code=True
        else:
            extra_code=False
        if update.callback_query:
            try:
                await update.callback_query.answer()
            except BadRequest:
                pass
            try:
                await update.callback_query.edit_message_media(
                    media=InputMediaPhoto(media=img,caption=caption,parse_mode="HTML"),
                    reply_markup=buttons)
            except TelegramError:
                logging.exception("Não foi possível transformar menu em QR Code; enviando nova mídia")
                await ctx.bot.send_photo(chat_id=update.effective_chat.id,photo=img,
                                         caption=caption,parse_mode="HTML",reply_markup=buttons)
        else:
            await ctx.bot.send_photo(chat_id=update.effective_chat.id,photo=img,
                                     caption=caption,parse_mode="HTML",reply_markup=buttons)
        if extra_code:
            # Código longo precisa de mensagem separada pelo limite de legenda.
            await ctx.bot.send_message(chat_id=update.effective_chat.id,
                                       text=f"📋 <b>Pix Copia e Cola:</b>\n<code>{html.escape(code)}</code>",
                                       parse_mode="HTML")
    except Exception:
        logging.exception("Falha ao criar Pix")
        await send(update,"Não foi possível gerar o Pix. Confira os logs do Railway ou contate o suporte.")

def create_pix(uid,kind,pid,amount,email,coupon=None,original_price=None):
    if kind not in ("topup","product"):raise ValueError("Tipo inválido")
    with conn() as c:
        c.execute('BEGIN IMMEDIATE')
        if kind=='product':
            p=c.execute('SELECT price FROM products WHERE id=? AND active=1',(pid,)).fetchone()
            if not p:raise ValueError('Produto indisponível')
            current, _=coupon_price(c,coupon,p['price'])
            if current!=amount:raise ValueError('Preço alterado; tente novamente')
            original_price=p['price']
        rid=c.execute("INSERT INTO pix_payments(user_id,kind,product_id,amount,email,coupon_code,original_price) VALUES(?,?,?,?,?,?,?)",(uid,kind,pid,amount,email,coupon,original_price)).lastrowid
    payload={"transaction_amount":amount/100,"description":f"Loja Demonstração {kind} #{rid}","payment_method_id":"pix","payer":{"email":email},"external_reference":f"nexo-{rid}"}
    try:
        resp=requests.post(MP_API,json=payload,headers={"Authorization":f"Bearer {MP_TOKEN}","X-Idempotency-Key":str(uuid.uuid5(uuid.NAMESPACE_URL,f"nexo-pix-{rid}"))},timeout=20)
    except requests.RequestException:
        with conn() as c:c.execute("UPDATE pix_payments SET status='failed' WHERE id=?",(rid,))
        raise
    if not resp.ok:
        try:
            body=resp.json()
            detail={"status":resp.status_code,"message":body.get("message"),"error":body.get("error"),"causes":[{"code":x.get("code"),"description":x.get("description")} for x in body.get("cause",[]) if isinstance(x,dict)]}
        except (ValueError,TypeError,AttributeError):
            detail={"status":resp.status_code,"message":"Resposta não estruturada da API"}
        logging.error("Mercado Pago recusou criação do Pix: %s",detail)
        with conn() as c:c.execute("UPDATE pix_payments SET status='failed' WHERE id=?",(rid,))
        raise ValueError(f"Mercado Pago HTTP {resp.status_code}")
    data=resp.json()
    mp_id=str(data["id"])
    with conn() as c:c.execute("UPDATE pix_payments SET mp_id=?,status='pending' WHERE id=?",(mp_id,rid))
    info=data.get("point_of_interaction",{}).get("transaction_data",{})
    code=info.get("qr_code")
    if not code:raise ValueError("Resposta sem QR Code")
    return rid,code,info.get("qr_code_base64")

def mp_get(payment_id):
    r=requests.get(f"{MP_API}/{payment_id}",headers={"Authorization":f"Bearer {MP_TOKEN}"},timeout=15)
    r.raise_for_status()
    return r.json()

async def verify_payment(rid,app):
    with conn() as c:r=c.execute("SELECT * FROM pix_payments WHERE id=?",(rid,)).fetchone()
    if not r or r["status"] not in ("pending","creating") or not r["mp_id"]:return
    try:data=await asyncio.to_thread(mp_get,r["mp_id"])
    except Exception:logging.exception("Falha ao verificar Pix %s",rid);return
    status=data.get("status")
    if status not in ("approved","rejected","cancelled","refunded","charged_back"):return
    notice=None
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        r=c.execute("SELECT * FROM pix_payments WHERE id=?",(rid,)).fetchone()
        if not r or r["status"]!="pending":return
        # Validação cruzada: origem, valor, moeda e referência do pagamento.
        actual=int((Decimal(str(data.get("transaction_amount",0)))*100).quantize(Decimal('1')))
        if str(data.get("id"))!=r["mp_id"] or data.get("external_reference")!=f"nexo-{rid}" or data.get("currency_id")!="BRL" or actual!=r["amount"]:
            logging.error("Pagamento %s não corresponde à cobrança local",rid);return
        if status!="approved":
            c.execute("UPDATE pix_payments SET status=?,processed_at=CURRENT_TIMESTAMP WHERE id=?",(status,rid))
            notice=(r["user_id"],f"Status do pagamento: {status}.",None)
        elif r["kind"]=="topup":
            c.execute("UPDATE users SET balance=balance+? WHERE id=?",(r["amount"],r["user_id"]))
            bonus=apply_topup_bonus(c,'pix',rid,r['user_id'],r['amount'])
            c.execute("UPDATE pix_payments SET status='approved',processed_at=CURRENT_TIMESTAMP WHERE id=?",(rid,))
            notice=(r["user_id"],f"✅ Pix confirmado! {money(r['amount'])} adicionados ao seu saldo."+(f" 🎁 Bônus: {money(bonus)}." if bonus else ''),None)
        else:
            stock=c.execute("SELECT id,content FROM stock WHERE product_id=? AND sold=0 ORDER BY id LIMIT 1",(r["product_id"],)).fetchone()
            if stock:
                c.execute("UPDATE stock SET sold=1 WHERE id=?",(stock["id"],))
                order_id=c.execute("INSERT INTO orders(user_id,product_id,stock_id,price,original_price,coupon_code) VALUES(?,?,?,?,?,?)",(r["user_id"],r["product_id"],stock["id"],r["amount"],r['original_price'],r['coupon_code'])).lastrowid
                # Se o cupom esgotou durante o pagamento, honra a cobrança já emitida.
                if r['coupon_code']:
                    c.execute('UPDATE coupons SET used=used+1 WHERE code=?',(r['coupon_code'],))
                    c.execute('INSERT INTO coupon_uses(code,user_id,order_id) VALUES(?,?,?)',(r['coupon_code'],r['user_id'],order_id))
                c.execute("UPDATE pix_payments SET status='approved',order_id=?,processed_at=CURRENT_TIMESTAMP WHERE id=?",(order_id,rid))
                notice=(r["user_id"],delivery_text(stock["content"]),r["amount"])
            else:
                c.execute("UPDATE users SET balance=balance+? WHERE id=?",(r["amount"],r["user_id"]))
                c.execute("UPDATE pix_payments SET status='approved',processed_at=CURRENT_TIMESTAMP WHERE id=?",(rid,))
                notice=(r["user_id"],f"⚠️ Pix confirmado, mas o produto esgotou. {money(r['amount'])} foram creditados na sua carteira. Fale com o suporte se preferir reembolso.",None)
    if notice:
        try:
            is_product_delivery = r['kind']=='product' and status=='approved' and notice[2] is not None
            await app.bot.send_message(
                notice[0],notice[1],
                parse_mode='HTML' if is_product_delivery else None,
                reply_markup=after_purchase_buttons(r['product_id']) if is_product_delivery else None)
        except Exception:logging.exception("Falha ao notificar cliente")
        if status=="approved" and r["kind"]=="product" and notice[2] is not None:
            record_purchase(r['user_id'])
            await check_low_stock(app.bot,r["product_id"])
        if status=="cancelled":
            with conn() as c:
                user=c.execute("SELECT name FROM users WHERE id=?",(r["user_id"],)).fetchone()
                product=c.execute("SELECT name FROM products WHERE id=?",(r["product_id"],)).fetchone() if r["kind"]=="product" else None
            client_name=user["name"] if user and user["name"] else "Nome não informado"
            item=f"📦 Produto: {product['name']}" if product else ("📦 Produto: Não encontrado" if r["kind"]=="product" else "💳 Tipo: Recarga de saldo")
            cancellation=(
                "🔴 PAGAMENTO CANCELADO!\n\n"
                f"{item}\n"
                f"💰 Valor: {money(r['amount'])}\n"
                f"👤 Cliente: {client_name}\n"
                f"🆔 ID Telegram: {r['user_id']}\n"
                f"🔖 ID Pix: {r['mp_id']}\n"
                "📌 Status: Cancelado ou expirado"
            )
            for aid in ADMINS:
                try:await app.bot.send_message(aid,cancellation)
                except Exception:logging.exception("Falha ao notificar cancelamento ao administrador")
        if status=="approved":
            with conn() as c:
                user=c.execute("SELECT name FROM users WHERE id=?",(r["user_id"],)).fetchone()
                product=c.execute("SELECT name FROM products WHERE id=?",(r["product_id"],)).fetchone() if r["kind"]=="product" else None
            client_name=(user["name"] if user and user["name"] else str(r["user_id"]))
            title="🟣 VENDA CONFIRMADA!" if r["kind"]=="product" else "🟣 PAGAMENTO CONFIRMADO!"
            product_line=f"📦 Produto: {product['name']}\n" if product else ""
            notification=f"{title}\n{product_line}💰 Valor: {money(r['amount'])}\n👤 Cliente: {client_name}"
            for aid in ADMINS:
                try:await app.bot.send_message(aid,notification)
                except Exception:logging.exception("Falha ao notificar administrador")

async def poll_payments(ctx):
    with conn() as c:ids=[r[0] for r in c.execute("SELECT id FROM pix_payments WHERE status='pending' AND mp_id IS NOT NULL ORDER BY id DESC LIMIT 100")]
    for rid in ids:await verify_payment(rid,ctx.application)

async def error(update,ctx):
    logging.exception("Erro no bot",exc_info=ctx.error)
    if update and update.effective_message:
        try:await update.effective_message.reply_text("Ocorreu um erro. Tente novamente com /start.")
        except Exception:pass
def main():
    if not TOKEN or TOKEN=="COLE_TOKEN_DO_BOTFATHER":raise RuntimeError("Configure TENANT_BOT_TOKEN no .env")
    DB.parent.mkdir(parents=True,exist_ok=True)
    init()
    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("admin",admin))
    app.add_handler(CommandHandler("pix",pix_command))
    app.add_handler(CommandHandler("vinculargrupo",vinculargrupo))
    app.add_handler(CommandHandler("alertas",alertas_command))
    app.add_handler(CommandHandler("semlembretes",no_reminders))
    app.add_handler(CommandHandler("comlembretes",enable_reminders))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.PHOTO,message))
    app.add_error_handler(error)
    app.job_queue.run_repeating(poll_payments,interval=30,first=10)
    app.job_queue.run_repeating(automatic_backup,interval=3600,first=120)
    app.job_queue.run_repeating(dispatch_nexo_restock_alerts,interval=35,first=15)
    app.job_queue.run_repeating(abandoned_visit_reminders,interval=180,first=240)
    app.run_polling(drop_pending_updates=True)
if __name__=="__main__":main()
