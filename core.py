"""Nexo Bot Store MVP: persistent, tenant-scoped store operations. No real payment processing."""
import os
import sqlite3
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

DB_PATH = os.getenv('NEXO_MVP_DB', 'nexo_bot_store.sqlite3')

@contextmanager
def db(path=None):
    c=sqlite3.connect(path or DB_PATH, timeout=10)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def initialize(path=None):
    with db(path) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS tenants(
          id INTEGER PRIMARY KEY, bot_username TEXT NOT NULL UNIQUE,
          owner_id INTEGER NOT NULL, title TEXT NOT NULL,
          welcome TEXT NOT NULL DEFAULT 'Bem-vindo à nossa loja!',
          support TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS products(
          id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL REFERENCES tenants(id),
          title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
          price_cents INTEGER NOT NULL CHECK(price_cents>=0),
          stock_count INTEGER NOT NULL DEFAULT 0 CHECK(stock_count>=0),
          active INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_products_tenant ON products(tenant_id,active);
        CREATE TABLE IF NOT EXISTS visits(
          tenant_id INTEGER NOT NULL REFERENCES tenants(id),
          user_id INTEGER NOT NULL, last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(tenant_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS audit(
          id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL REFERENCES tenants(id),
          actor_id INTEGER NOT NULL, action TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        ''')

def add_tenant(username, owner_id, title, path=None):
    username=username.strip().lstrip('@').lower()
    if not username or not username.replace('_','').isalnum():
        raise ValueError('Username inválido')
    if owner_id <= 0: raise ValueError('Owner inválido')
    with db(path) as c:
        cur=c.execute('INSERT INTO tenants(bot_username,owner_id,title) VALUES(?,?,?)',(username,owner_id,title.strip()))
        return cur.lastrowid

def tenant(tenant_id,path=None):
    with db(path) as c:
        return c.execute('SELECT * FROM tenants WHERE id=?',(tenant_id,)).fetchone()

def assert_owner(c,tenant_id,user_id):
    row=c.execute('SELECT owner_id FROM tenants WHERE id=?',(tenant_id,)).fetchone()
    if row is None or row['owner_id'] != user_id: raise PermissionError('Acesso não autorizado')

def update_shop(tenant_id, actor_id, field, value, path=None):
    if field not in {'title','welcome','support'}: raise ValueError('Campo não permitido')
    value=value.strip()
    if not value or len(value)>1000: raise ValueError('Valor vazio ou muito longo')
    with db(path) as c:
        assert_owner(c,tenant_id,actor_id)
        c.execute(f'UPDATE tenants SET {field}=? WHERE id=?',(value,tenant_id))
        c.execute('INSERT INTO audit(tenant_id,actor_id,action) VALUES(?,?,?)',(tenant_id,actor_id,f'update:{field}'))

def parse_price(value):
    try:
        d=Decimal(value.strip().replace(',','.'))
        if not d.is_finite() or d<0 or d>Decimal('1000000'): raise ValueError('Preço inválido')
        return int((d*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    except (InvalidOperation,AttributeError): raise ValueError('Preço inválido')

def add_product(tenant_id,actor_id,title,price,stock=0,path=None):
    if not title.strip() or len(title)>150 or stock<0 or stock>1000000: raise ValueError('Produto inválido')
    cents=parse_price(price)
    with db(path) as c:
        assert_owner(c,tenant_id,actor_id)
        cur=c.execute('INSERT INTO products(tenant_id,title,price_cents,stock_count) VALUES(?,?,?,?)',(tenant_id,title.strip(),cents,stock))
        c.execute('INSERT INTO audit(tenant_id,actor_id,action) VALUES(?,?,?)',(tenant_id,actor_id,f'product:create:{cur.lastrowid}'))
        return cur.lastrowid

def products(tenant_id,path=None):
    with db(path) as c:
        return c.execute('SELECT id,title,description,price_cents,stock_count FROM products WHERE tenant_id=? AND active=1 ORDER BY id DESC',(tenant_id,)).fetchall()

def edit_product(tenant_id,actor_id,product_id,field,value,path=None):
    if field not in {'title','price_cents','stock_count','active'}: raise ValueError('Campo não permitido')
    if field=='price_cents': value=parse_price(value)
    elif field in {'stock_count','active'}:
        value=int(value)
        if value<0 or (field=='active' and value>1): raise ValueError('Valor inválido')
    else:
        value=str(value).strip()
        if not value or len(value)>150: raise ValueError('Nome inválido')
    with db(path) as c:
        assert_owner(c,tenant_id,actor_id)
        cur=c.execute(f'UPDATE products SET {field}=? WHERE id=? AND tenant_id=?',(value,product_id,tenant_id))
        if not cur.rowcount: raise LookupError('Produto não encontrado nesta loja')
        c.execute('INSERT INTO audit(tenant_id,actor_id,action) VALUES(?,?,?)',(tenant_id,actor_id,f'product:update:{product_id}:{field}'))

def visit(tenant_id,user_id,path=None):
    with db(path) as c:
        c.execute('INSERT INTO visits(tenant_id,user_id) VALUES(?,?) ON CONFLICT(tenant_id,user_id) DO UPDATE SET last_seen=CURRENT_TIMESTAMP',(tenant_id,user_id))

def money(cents):
    return f'R$ {cents//100},{cents%100:02d}'
