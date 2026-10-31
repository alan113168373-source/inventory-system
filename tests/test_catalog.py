import sqlite3
from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import load_workbook
import pytest
from app import main


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'DB_PATH',tmp_path/'inventory.db')
    return TestClient(main.app)


def product(**changes):
    return dict(sku='BATCH-001',name='手袋',style='托特款',condition='九成新',size='中号',quantity=8,status='仓库',operator='操作员',occurred_at='2026-01-01T10:00:00+08:00',**changes)


def test_fields_filters_custom_status_and_quantity_report(client):
    assert client.post('/api/options',json={'kind':'status','name':'送修中','category':'external'}).status_code==201
    r=client.post('/api/products',json=product())
    assert r.status_code==201
    pid=r.json()['id']
    rows=client.get('/api/products',params={'condition':'九成新','style':'托特款','size':'中号'}).json()
    assert len(rows)==1 and rows[0]['quantity']==8
    assert client.get('/api/products',params={'size':'小号'}).json()==[]
    saved=client.get('/api/options').json()
    assert any(o['kind']=='condition' and o['name']=='九成新' for o in saved)
    body={'status':'送修中','operator':'调拨员','occurred_at':'2026-01-02T10:00:00+08:00'}
    assert client.post(f'/api/products/{pid}/move',json=body).status_code==200
    body.update(status='已售',occurred_at='2026-01-03T10:00:00+08:00')
    assert client.post(f'/api/products/{pid}/move',json=body).status_code==200
    params={'start':'2026-01-01T00:00:00Z','end':'2026-02-01T00:00:00Z'}
    report=client.get('/api/report',params=params).json()
    assert report['outbound_quantity']==8 and report['sales_quantity']==8
    assert report['outbound_count']==1 and report['sales_products']==1
    assert all(e['quantity']==8 for e in client.get('/api/events').json())
    wb=load_workbook(BytesIO(client.get('/api/export',params=params).content))
    assert [c.value for c in wb['当前库存'][2]][5:]==['托特款','九成新','中号',8]
    assert dict(wb['状态汇总'].iter_rows(min_row=2,values_only=True))['已售']==8
    assert wb['期间流水']['I2'].value==8


def test_options_edit_delete_and_validation(client):
    client.post('/api/options',json={'kind':'status','name':'待上架','category':'warehouse'})
    item=next(o for o in client.get('/api/options').json() if o['name']=='待上架')
    body=product();body['status']='待上架'
    assert client.post('/api/products',json=body).status_code==201
    assert client.put(f"/api/options/{item['id']}",json={'kind':'status','name':'待整理','category':'warehouse'}).status_code==200
    assert client.get('/api/products',params={'status':'待整理'}).json()[0]['status']=='待整理'
    assert client.get('/api/events').json()[0]['to_status']=='待整理'
    assert client.delete(f"/api/options/{item['id']}").status_code==409
    assert client.post('/api/options',json={'kind':'size','name':'XL'}).status_code==201
    size=next(o for o in client.get('/api/options').json() if o['name']=='XL')
    assert client.delete(f"/api/options/{size['id']}").status_code==200
    assert not any(o['name']=='XL' for o in client.get('/api/options').json())
    builtin=next(o for o in client.get('/api/options').json() if o['builtin'])
    assert client.delete(f"/api/options/{builtin['id']}").status_code==409
    assert client.post('/api/options',json={'kind':'status','name':'任意','category':''}).status_code==422
    assert client.post('/api/options',json={'kind':'bad','name':'x'}).status_code==422
    for invalid in [0,-1,1.5,'3']:
        body=product();body['quantity']=invalid
        assert client.post('/api/products',json=body).status_code==422


def test_legacy_database_migration(client):
    # Deliberately construct the previous release's schema before any API call.
    with sqlite3.connect(main.DB_PATH) as c:
        c.executescript('''
        CREATE TABLE products(id INTEGER PRIMARY KEY,sku TEXT UNIQUE NOT NULL,name TEXT NOT NULL,status TEXT NOT NULL,merchant TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
        CREATE TABLE events(id INTEGER PRIMARY KEY,product_id INTEGER NOT NULL,from_status TEXT NOT NULL,to_status TEXT NOT NULL,merchant TEXT NOT NULL,operator TEXT NOT NULL,occurred_at TEXT NOT NULL,note TEXT NOT NULL);
        INSERT INTO products VALUES(1,'OLD-1','旧商品','仓库','','2025-01-01T00:00:00+00:00');
        INSERT INTO events VALUES(1,1,'','仓库','','旧操作员','2025-01-01T00:00:00+00:00','旧备注');
        ''')
    for _ in range(2):
        p=client.get('/api/products').json()[0]
        assert p['sku']=='OLD-1' and p['quantity']==1 and p['condition']==''
        e=client.get('/api/events').json()[0]
        assert e['quantity']==1 and e['note']=='旧备注' and e['operator']=='旧操作员'
    for route in ['/inventory','/inventory/new','/movements','/reports','/settings']:
        assert client.get(route).status_code==200
