from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import time

from fastapi.testclient import TestClient
from openpyxl import load_workbook
import pytest
from app import main
from conftest import authenticate


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'DB_PATH',tmp_path/'inventory.db')
    return authenticate(TestClient(main.app),'张三')


def add(client):
    r=client.post('/api/products',json={'sku':'BATCH-100','name':'Polo衬衫','quantity':100,'status':'仓库','operator':'冒充老板','occurred_at':'2026-01-01T00:00:00Z'})
    assert r.status_code==201
    return r.json()['id']


def move(client,pid,qty,status='第三方鉴定',**changes):
    return client.post(f'/api/products/{pid}/move',json={'quantity':qty,'status':status,'operator':'冒充他人','occurred_at':'2026-01-02T00:00:00Z',**changes})


def test_partial_moves_conserve_stock_history_and_reports(client):
    pid=add(client)
    first=move(client,pid,30).json()
    assert first['split'] and first['quantity']==30
    a=first['product_id']
    second=move(client,pid,20,'商家',merchant='Aloha').json()
    b=second['product_id']
    third=move(client,a,10,'已售',occurred_at='2026-01-03T00:00:00Z').json()
    assert third['split']
    rows=client.get('/api/products').json()
    assert sum(r['quantity'] for r in rows)==100
    assert {r['id']:r['quantity'] for r in rows}=={pid:50,a:20,b:20,third['product_id']:10}
    assert all(r['batch_id']==pid and r['batch_sku']=='BATCH-100' for r in rows)
    assert move(client,pid,51).status_code==409
    for qty in [0,-1,1.5,'3']:
        assert move(client,pid,qty).status_code==422
    assert move(client,pid,2,occurred_at='2025-01-01T00:00:00Z').status_code==422
    params={'start':'2026-01-01T00:00:00Z','end':'2026-02-01T00:00:00Z'}
    report=client.get('/api/report',params=params).json()
    assert (report['outbound_quantity'],report['sales_quantity'])==(50,10)
    assert report['outbound_products']==report['sales_products']==1
    assert len(report['events'])==4
    assert all(e['operator']=='张三' for e in report['events'])
    history=client.get(f'/api/products/{pid}/events').json()
    assert history==client.get(f"/api/products/{third['product_id']}/events").json()
    assert len(history)==4
    wb=load_workbook(BytesIO(client.get('/api/export',params=params).content))
    assert sum(row[8] for row in wb['当前库存'].iter_rows(min_row=2,values_only=True))==100
    assert all(row[9]=='BATCH-100' for row in wb['当前库存'].iter_rows(min_row=2,values_only=True))


def test_same_location_records_selected_quantity_without_splitting(client):
    pid=add(client)
    assert not move(client,pid,7,'仓库').json()['split']
    assert client.get('/api/products').json()[0]['quantity']==100
    child=move(client,pid,30,'商家',merchant='Aloha').json()['product_id']
    assert not move(client,child,5,'商家',merchant='Aloha').json()['split']
    changed=move(client,child,5,'商家',merchant='Beta').json()
    assert changed['split']
    rows=client.get('/api/products').json()
    assert sum(r['quantity'] for r in rows)==100
    assert next(r for r in rows if r['id']==child)['quantity']==25
    assert next(r for r in rows if r['merchant']=='Beta')['quantity']==5


def test_concurrent_outbound_cannot_overdraw(client):
    pid=add(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes=list(pool.map(lambda _:move(client,pid,70).status_code,range(2)))
    assert sorted(codes)==[200,409]
    rows=client.get('/api/products').json()
    assert sum(r['quantity'] for r in rows)==100
    assert sorted(r['quantity'] for r in rows)==[30,70]


def test_permissions_csrf_revocation_and_employee_identity(client):
    anonymous=TestClient(main.app)
    assert anonymous.get('/api/products').status_code==401
    assert anonymous.get('/api/export').status_code==401
    for username,role in [('李四','employee'),('reader','viewer')]:
        assert client.post('/api/users',json={'username':username,'password':'staff-password-123','role':role}).status_code==201
    staff=TestClient(main.app)
    login=staff.post('/api/auth/login',json={'username':'李四','password':'staff-password-123'})
    assert 'HttpOnly' in login.headers['set-cookie'] and 'SameSite=strict' in login.headers['set-cookie']
    staff.headers['X-CSRF-Token']=login.json()['csrf']
    assert staff.post('/api/auth/logout',headers={'X-CSRF-Token':''}).status_code==403
    assert staff.post('/api/auth/logout',headers={'Origin':'https://evil.example'}).status_code==403
    assert staff.get('/api/users').status_code==403
    pid=add(staff)
    assert move(staff,pid,10).status_code==200
    assert all(e['operator']=='李四' for e in client.get(f'/api/products/{pid}/events').json())
    users=client.get('/api/users').json()
    assert all('password_hash' not in u for u in users)
    uid=next(u['id'] for u in users if u['username']=='李四')
    assert client.put(f'/api/users/{uid}',json={'role':'employee','active':False}).status_code==200
    assert staff.get('/api/products').status_code==401
    aid=next(u['id'] for u in users if u['username']=='张三')
    assert client.put(f'/api/users/{aid}',json={'role':'employee','active':True}).status_code==409
    viewer=TestClient(main.app)
    login=viewer.post('/api/auth/login',json={'username':'reader','password':'staff-password-123'})
    viewer.headers['X-CSRF-Token']=login.json()['csrf']
    assert viewer.get('/api/products').status_code==200
    assert viewer.get('/api/export').status_code==403
    assert move(viewer,pid,1).status_code==403


def test_password_logout_expiry_and_throttle(client):
    assert client.post('/api/auth/password',json={'current_password':'wrong','password':'new-password-123'}).status_code==400
    assert client.post('/api/auth/password',json={'current_password':'test-password-123','password':'new-password-123'}).status_code==200
    assert client.get('/api/products').status_code==401
    login=client.post('/api/auth/login',json={'username':'张三','password':'new-password-123'})
    client.headers['X-CSRF-Token']=login.json()['csrf']
    assert client.post('/api/auth/logout').status_code==200
    assert client.get('/api/products').status_code==401
    assert client.post('/api/auth/login',json={'username':'张三','password':'new-password-123'}).status_code==200
    with main.db() as c:
        c.execute('UPDATE sessions SET expires=?',(time.time()-1,))
    assert client.get('/api/products').status_code==401
    for _ in range(5):
        assert client.post('/api/auth/login',json={'username':'unknown','password':'bad'}).status_code==401
    assert client.post('/api/auth/login',json={'username':'unknown','password':'bad'}).status_code==429


def test_first_administrator_is_local_and_only_created_once(tmp_path,monkeypatch):
    from app import manage_admin
    monkeypatch.setattr(main,'DB_PATH',tmp_path/'first.db')
    monkeypatch.setattr('sys.argv',['manage_admin','--if-needed'])
    monkeypatch.setattr('builtins.input',lambda _: '管理员')
    monkeypatch.setattr(manage_admin,'getpass',lambda _: 'local-password-123')
    manage_admin.main()
    def no_prompt(_):
        raise AssertionError('Existing installation must not ask to create another administrator')
    monkeypatch.setattr('builtins.input',no_prompt)
    monkeypatch.setattr(manage_admin,'getpass',no_prompt)
    manage_admin.main()
    with main.db() as c:
        users=c.execute('SELECT * FROM users').fetchall()
        assert len(users)==1 and users[0]['role']=='admin'
        assert users[0]['password_hash']!='local-password-123'
        assert main.auth.verify('local-password-123',users[0]['password_hash'])
