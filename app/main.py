import io
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
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
    operator: str = Field(min_length=1, max_length=100)
    merchant: str = Field(default='', max_length=100)
    occurred_at: datetime
    note: str = Field(default='', max_length=1000)

class Product(Move):
    sku: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)

def checked(m):
    if m.status not in STATUSES:
        raise HTTPException(422, '请选择有效商品状态')
    if not m.operator.strip():
        raise HTTPException(422, '操作人不能为空')
    if m.status == '商家' and not m.merchant.strip():
        raise HTTPException(422, '转到商家时必须选择或输入商家')
    if m.occurred_at.tzinfo is None:
        raise HTTPException(422, '操作时间必须包含时区')
    return m.occurred_at.astimezone(timezone.utc).isoformat()

def record(c, pid, previous, m, time):
    merchant = m.merchant.strip() if m.status == '商家' else ''
    if merchant:
        c.execute('INSERT OR IGNORE INTO merchants(name) VALUES(?)', (merchant,))
    c.execute('INSERT INTO events(product_id,from_status,to_status,merchant,operator,occurred_at,note) VALUES(?,?,?,?,?,?,?)', (pid, previous, m.status, merchant, m.operator.strip(), time, m.note.strip()))
    return merchant

@app.get('/')
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
def create(m: Product):
    time = checked(m)
    if not m.sku.strip() or not m.name.strip():
        raise HTTPException(422, '商品编号和名称不能为空')
    with db() as c:
        try:
            pid = c.execute('INSERT INTO products(sku,name,status,merchant,created_at) VALUES(?,?,?,?,?)', (m.sku.strip(), m.name.strip(), m.status, m.merchant.strip() if m.status == '商家' else '', time)).lastrowid
        except sqlite3.IntegrityError:
            raise HTTPException(409, '商品编号已存在')
        record(c, pid, '', m, time)
    return {'id': pid}

@app.post('/api/products/{pid}/move')
def move(pid: int, m: Move):
    time = checked(m)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        p = c.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
        if not p:
            raise HTTPException(404, '商品不存在')
        latest = c.execute('SELECT MAX(occurred_at) FROM events WHERE product_id=?', (pid,)).fetchone()[0]
        if time < latest:
            raise HTTPException(422, '操作时间不能早于该商品上一次记录')
        merchant = record(c, pid, p['status'], m, time)
        c.execute('UPDATE products SET status=?,merchant=? WHERE id=?', (m.status, merchant, pid))
    return {'status': 'ok'}

@app.get('/api/products')
def products(q: str = '', status: str = ''):
    with db() as c:
        return [dict(r) for r in c.execute('SELECT * FROM products WHERE (instr(sku,?)>0 OR instr(name,?)>0) AND (?=\'\' OR status=?) ORDER BY id DESC', (q,q,status,status))]

@app.get('/api/products/{pid}/events')
def history(pid: int):
    with db() as c:
        return [dict(r) for r in c.execute('SELECT * FROM events WHERE product_id=? ORDER BY occurred_at DESC,id DESC', (pid,))]

def report_data(start: datetime, end: datetime):
    if start.tzinfo is None or end.tzinfo is None or start > end:
        raise HTTPException(422, '请选择带时区的有效起止时间')
    with db() as c:
        rows = [dict(r) for r in c.execute('SELECT e.*,p.sku,p.name FROM events e JOIN products p ON p.id=e.product_id WHERE occurred_at>=? AND occurred_at<=? ORDER BY occurred_at,id', (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()))]
    outbound = [r for r in rows if r['from_status'] in ['仓库','退回到仓库'] and r['to_status'] in ['第三方鉴定','商家','已售']]
    sales = [r for r in rows if r['to_status']=='已售' and r['from_status']!='已售']
    return {'outbound_count':len(outbound), 'outbound_products':len({r['product_id'] for r in outbound}), 'sales_count':len(sales), 'sales_products':len({r['product_id'] for r in sales}), 'events':rows}

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
    sheet(wb,'当前库存',['编号','商品','状态','商家','登记时间（UTC）'],[[p[k] for k in ['sku','name','status','merchant','created_at']] for p in ps])
    sheet(wb,'状态汇总',['状态','件数'],[[s,sum(p['status']==s for p in ps)] for s in STATUSES])
    if start is not None:
        r=report_data(start,end)
        sheet(wb,'期间汇总',['指标','数量'],[[k,r[k]] for k in ['outbound_count','outbound_products','sales_count','sales_products']])
        sheet(wb,'期间流水',['编号','商品','原状态','新状态','商家','操作人','时间（UTC）','备注'],[[e[k] for k in ['sku','name','from_status','to_status','merchant','operator','occurred_at','note']] for e in r['events']])
    stream=io.BytesIO(); wb.save(stream); stream.seek(0)
    return StreamingResponse(stream,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename=inventory.xlsx'})
