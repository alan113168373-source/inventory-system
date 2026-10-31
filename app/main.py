import io
import os
import sqlite3
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from app import auth
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import Workbook
from pydantic import BaseModel, Field

STATUSES = ['仓库', '第三方鉴定', '商家', '已售', '退回到仓库']
DB_PATH = Path(os.getenv('INVENTORY_DB', 'data/inventory.db'))
app = FastAPI(title='库存进出管理系统')
app.mount('/static', StaticFiles(directory=Path(__file__).parent / 'static'), name='static')

@contextmanager
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS merchants(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
    CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, sku TEXT NOT NULL UNIQUE, name TEXT NOT NULL, status TEXT NOT NULL, merchant TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id), from_status TEXT NOT NULL, to_status TEXT NOT NULL, merchant TEXT NOT NULL, operator TEXT NOT NULL, occurred_at TEXT NOT NULL, note TEXT NOT NULL);
    ''')
    conn.execute('BEGIN IMMEDIATE')
    for table, additions in {
        'products': [('style', "TEXT NOT NULL DEFAULT ''"), ('condition', "TEXT NOT NULL DEFAULT ''"), ('size', "TEXT NOT NULL DEFAULT ''"), ('quantity', 'INTEGER NOT NULL DEFAULT 1'), ('root_id', 'INTEGER'), ('parent_id', 'INTEGER')],
        'events': [('quantity', 'INTEGER NOT NULL DEFAULT 1'), ('source_product_id', 'INTEGER')],
    }.items():
        columns = {r['name'] for r in conn.execute(f'PRAGMA table_info({table})')}
        for name, definition in additions:
            if name not in columns:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
    conn.execute('''CREATE TABLE IF NOT EXISTS options(
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT '', builtin INTEGER NOT NULL DEFAULT 0,
        UNIQUE(kind,name))''')
    for name in STATUSES:
        category = 'warehouse' if name in ['仓库', '退回到仓库'] else ('sold' if name == '已售' else 'external')
        conn.execute("INSERT OR IGNORE INTO options(kind,name,category,builtin) VALUES('status',?,?,1)", (name, category))
    # Seed defaults only once so removing an unused option stays effective.
    conn.execute('CREATE TABLE IF NOT EXISTS migrations(name TEXT PRIMARY KEY)')
    if not conn.execute("SELECT 1 FROM migrations WHERE name='catalog-v2'").fetchone():
        for name in ['全新', '近新', '轻微使用', '明显使用']:
            conn.execute("INSERT OR IGNORE INTO options(kind,name) VALUES('condition',?)", (name,))
        conn.execute("INSERT INTO migrations VALUES('catalog-v2')")
    conn.commit()
    auth.schema(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

class Merchant(BaseModel):
    name: str = Field(min_length=1, max_length=100)

class Move(BaseModel):
    status: str
    operator: str = Field(default='', max_length=100)
    quantity: int | None = Field(default=None, ge=1, le=1000000, strict=True)
    merchant: str = Field(default='', max_length=100)
    occurred_at: datetime
    note: str = Field(default='', max_length=1000)

class Product(Move):
    sku: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    style: str = Field(default='', max_length=100)
    condition: str = Field(default='', max_length=100)
    size: str = Field(default='', max_length=100)
    quantity: int = Field(default=1, ge=1, le=1000000, strict=True)

class Option(BaseModel):
    kind: str
    name: str = Field(min_length=1, max_length=100)
    category: str = ''

def option_valid(m):
    if m.kind not in ['status','condition','style','size'] or not m.name.strip():
        raise HTTPException(422, '请选择有效选项类型并填写名称')
    if m.kind == 'status' and m.category not in ['warehouse','external','sold']:
        raise HTTPException(422, '状态需选择在库、外出或已售统计分类')

@app.get('/api/options')
def options():
    with db() as c:
        return [dict(r) for r in c.execute('SELECT * FROM options ORDER BY id')]

@app.post('/api/options', status_code=201)
def add_option(m: Option):
    option_valid(m)
    with db() as c:
        try:
            c.execute('INSERT INTO options(kind,name,category) VALUES(?,?,?)', (m.kind,m.name.strip(),m.category if m.kind=='status' else ''))
        except sqlite3.IntegrityError:
            raise HTTPException(409, '该选项已存在')
    return {'status':'ok'}

@app.put('/api/options/{oid}')
def edit_option(oid: int, m: Option):
    option_valid(m)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        old = c.execute('SELECT * FROM options WHERE id=?',(oid,)).fetchone()
        if not old:
            raise HTTPException(404, '选项不存在')
        if old['builtin'] or old['kind'] != m.kind:
            raise HTTPException(409, '系统状态不可编辑；可新建自定义状态')
        try:
            c.execute('UPDATE options SET name=?,category=? WHERE id=?', (m.name.strip(),m.category if m.kind=='status' else '',oid))
        except sqlite3.IntegrityError:
            raise HTTPException(409, '该名称已存在')
        column = m.kind
        c.execute(f'UPDATE products SET "{column}"=? WHERE "{column}"=?', (m.name.strip(),old['name']))
        if m.kind=='status':
            for col in ['from_status','to_status']:
                c.execute(f'UPDATE events SET {col}=? WHERE {col}=?', (m.name.strip(),old['name']))
    return {'status':'ok'}

@app.delete('/api/options/{oid}')
def delete_option(oid: int):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT * FROM options WHERE id=?',(oid,)).fetchone()
        if not old:
            raise HTTPException(404,'选项不存在')
        used=c.execute(f'SELECT 1 FROM products WHERE "{old["kind"]}"=?',(old['name'],)).fetchone()
        if old['kind']=='status':
            used=used or c.execute('SELECT 1 FROM events WHERE from_status=? OR to_status=?',(old['name'],old['name'])).fetchone()
        if old['builtin'] or used:
            raise HTTPException(409,'系统状态或已被商品、历史流水使用的选项不能删除')
        c.execute('DELETE FROM options WHERE id=?',(oid,))
    return {'status':'ok'}

def checked(m):
    with db() as c:
        if not c.execute("SELECT 1 FROM options WHERE kind='status' AND name=?",(m.status,)).fetchone():
            raise HTTPException(422, '请先在自定义选项中新增该库存状态')
    if not m.operator.strip():
        raise HTTPException(422, '操作人不能为空')
    if m.status == '商家' and not m.merchant.strip():
        raise HTTPException(422, '转到商家时必须选择或输入商家')
    if m.occurred_at.tzinfo is None:
        raise HTTPException(422, '操作时间必须包含时区')
    return m.occurred_at.astimezone(timezone.utc).isoformat()

def record(c, pid, previous, m, time, quantity=1, source_product_id=None):
    merchant = m.merchant.strip() if m.status == '商家' else ''
    if merchant:
        c.execute('INSERT OR IGNORE INTO merchants(name) VALUES(?)', (merchant,))
    c.execute('INSERT INTO events(product_id,from_status,to_status,merchant,operator,occurred_at,note,quantity,source_product_id) VALUES(?,?,?,?,?,?,?,?,?)', (pid, previous, m.status, merchant, m.operator.strip(), time, m.note.strip(),quantity,source_product_id))
    return merchant

@app.get('/')
@app.get('/inventory')
@app.get('/inventory/new')
@app.get('/movements')
@app.get('/reports')
@app.get('/settings')
@app.get('/accounts')
@app.get('/login')
def index():
    return FileResponse(Path(__file__).parent / 'static/index.html')

@app.get('/api/health')
def health():
    with db() as c:
        c.execute('SELECT 1')
    return {'status': 'ok'}

@app.get('/api/merchants')
def merchants():
    with db() as c:
        return [dict(r) for r in c.execute('SELECT * FROM merchants ORDER BY name')]

@app.post('/api/merchants', status_code=201)
def add_merchant(m: Merchant):
    if not m.name.strip():
        raise HTTPException(422, '商家名称不能为空')
    with db() as c:
        c.execute('INSERT OR IGNORE INTO merchants(name) VALUES(?)', (m.name.strip(),))
    return {'name': m.name.strip()}

@app.post('/api/products', status_code=201)
def create(m: Product, request: Request):
    m.operator = request.state.user['username']
    time = checked(m)
    if not m.sku.strip() or not m.name.strip():
        raise HTTPException(422, '商品编号和名称不能为空')
    with db() as c:
        try:
            pid = c.execute('INSERT INTO products(sku,name,status,merchant,created_at,style,condition,size,quantity) VALUES(?,?,?,?,?,?,?,?,?)', (m.sku.strip(), m.name.strip(), m.status, m.merchant.strip() if m.status == '商家' else '', time,m.style.strip(),m.condition.strip(),m.size.strip(),m.quantity)).lastrowid
        except sqlite3.IntegrityError:
            raise HTTPException(409, '商品编号已存在')
        for kind in ['condition','style','size']:
            value=getattr(m,kind).strip()
            if value:
                c.execute('INSERT OR IGNORE INTO options(kind,name) VALUES(?,?)',(kind,value))
        c.execute('UPDATE products SET root_id=? WHERE id=?',(pid,pid))
        record(c, pid, '', m, time,m.quantity)
    return {'id': pid}

@app.post('/api/products/{pid}/move')
def move(pid: int, m: Move, request: Request):
    m.operator = request.state.user['username']
    time = checked(m)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        p = c.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
        if not p:
            raise HTTPException(404, '商品不存在')
        latest = c.execute('SELECT MAX(occurred_at) FROM events WHERE product_id=? OR source_product_id=?', (pid,pid)).fetchone()[0]
        if latest and time < latest:
            raise HTTPException(422, '操作时间不能早于该商品上一次记录')
        quantity = p['quantity'] if m.quantity is None else m.quantity
        if quantity > p['quantity']:
            raise HTTPException(409, f'当前可流转数量为{p["quantity"]}件，请刷新库存后重试')
        merchant = m.merchant.strip() if m.status=='商家' else ''
        same_location = m.status==p['status'] and merchant==p['merchant']
        target_id = pid
        if quantity < p['quantity'] and not same_location:
            root_id=p['root_id'] or p['id']
            root=c.execute('SELECT sku FROM products WHERE id=?',(root_id,)).fetchone()
            while True:
                sku=root['sku'][:80]+'-S'+secrets.token_hex(4).upper()
                if not c.execute('SELECT 1 FROM products WHERE sku=?',(sku,)).fetchone():
                    break
            target_id=c.execute('INSERT INTO products(sku,name,status,merchant,created_at,style,condition,size,quantity,root_id,parent_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)', (sku,p['name'],m.status,merchant,p['created_at'],p['style'],p['condition'],p['size'],quantity,root_id,pid)).lastrowid
            c.execute('UPDATE products SET quantity=quantity-? WHERE id=?',(quantity,pid))
        merchant = record(c, target_id, p['status'], m, time,quantity,pid)
        if target_id==pid and not same_location:
            c.execute('UPDATE products SET status=?,merchant=? WHERE id=?', (m.status, merchant, pid))
        target=c.execute('SELECT sku FROM products WHERE id=?',(target_id,)).fetchone()
    return {'status':'ok','quantity':quantity,'product_id':target_id,'sku':target['sku'],'split':target_id!=pid}

@app.get('/api/products')
def products(q: str = '', status: str = '', condition: str = '', style: str = '', size: str = ''):
    with db() as c:
        return [dict(r) for r in c.execute('SELECT p.*,COALESCE(p.root_id,p.id) AS batch_id,(SELECT sku FROM products r WHERE r.id=COALESCE(p.root_id,p.id)) AS batch_sku FROM products p WHERE (instr(sku,?)>0 OR instr(name,?)>0 OR instr(style,?)>0) AND (?=\'\' OR status=?) AND (?=\'\' OR condition=?) AND (?=\'\' OR style=?) AND (?=\'\' OR size=?) ORDER BY id DESC', (q,q,q,status,status,condition,condition,style,style,size,size))]

@app.get('/api/events')
def events():
    with db() as c:
        return [dict(r) for r in c.execute('SELECT e.*,p.sku,p.name FROM events e JOIN products p ON p.id=e.product_id ORDER BY e.occurred_at DESC,e.id DESC')]

@app.get('/api/products/{pid}/events')
def history(pid: int):
    with db() as c:
        p=c.execute('SELECT COALESCE(root_id,id) AS batch_id FROM products WHERE id=?',(pid,)).fetchone()
        if not p:
            raise HTTPException(404,'商品不存在')
        return [dict(r) for r in c.execute('SELECT e.*,p.sku FROM events e JOIN products p ON p.id=e.product_id WHERE COALESCE(p.root_id,p.id)=? ORDER BY e.occurred_at DESC,e.id DESC', (p['batch_id'],))]

def report_data(start: datetime, end: datetime):
    if start.tzinfo is None or end.tzinfo is None or start > end:
        raise HTTPException(422, '请选择带时区的有效起止时间')
    with db() as c:
        rows = [dict(r) for r in c.execute('SELECT e.*,p.sku,p.name,COALESCE(p.root_id,p.id) AS batch_id FROM events e JOIN products p ON p.id=e.product_id WHERE occurred_at>=? AND occurred_at<=? ORDER BY occurred_at,id', (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()))]
        categories={r['name']:r['category'] for r in c.execute("SELECT * FROM options WHERE kind='status'")}
    outbound = [r for r in rows if categories.get(r['from_status'])=='warehouse' and categories.get(r['to_status']) in ['external','sold']]
    sales = [r for r in rows if categories.get(r['to_status'])=='sold' and categories.get(r['from_status'])!='sold']
    return {'outbound_count':len(outbound), 'outbound_products':len({r['batch_id'] for r in outbound}), 'sales_count':len(sales), 'sales_products':len({r['batch_id'] for r in sales}), 'outbound_quantity':sum(r['quantity'] for r in outbound), 'sales_quantity':sum(r['quantity'] for r in sales), 'events':rows}

@app.get('/api/report')
def report(start: datetime, end: datetime):
    return report_data(start,end)

def sheet(wb, title, headers, rows):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for row in rows:
        # Treat all user input as text, preventing spreadsheet formula injection.
        ws.append([("'" + v) if isinstance(v,str) and v.startswith(('=','+','-','@')) else v for v in row])
    ws.freeze_panes='A2'
    ws.auto_filter.ref=ws.dimensions
    for column in ws.columns:
        ws.column_dimensions[column[0].column_letter].width=24

@app.get('/api/export')
def export(start: datetime | None = None, end: datetime | None = None):
    if (start is None) != (end is None):
        raise HTTPException(422, '起止时间必须同时提供')
    wb=Workbook(); wb.remove(wb.active)
    ps=products()
    sheet(wb,'当前库存',['编号','商品','状态','商家','登记时间（UTC）','款式','成色','尺寸','数量','原始批次编号'],[[p[k] for k in ['sku','name','status','merchant','created_at','style','condition','size','quantity','batch_sku']] for p in ps])
    names=[o['name'] for o in options() if o['kind']=='status']
    sheet(wb,'状态汇总',['状态','件数'],[[s,sum(p['quantity'] for p in ps if p['status']==s)] for s in names])
    if start is not None:
        r=report_data(start,end)
        sheet(wb,'期间汇总',['指标','数量'],[[k,r[k]] for k in ['outbound_count','outbound_products','sales_count','sales_products','outbound_quantity','sales_quantity']])
        sheet(wb,'期间流水',['编号','商品','原状态','新状态','商家','操作人','时间（UTC）','备注','数量'],[[e[k] for k in ['sku','name','from_status','to_status','merchant','operator','occurred_at','note','quantity']] for e in r['events']])
    stream=io.BytesIO(); wb.save(stream); stream.seek(0)
    return StreamingResponse(stream,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename=inventory.xlsx'})

auth.install(app,db)
