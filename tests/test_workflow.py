from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from app import main
from conftest import authenticate

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', tmp_path / 'test.db')
    return authenticate(TestClient(main.app))

def data(status='仓库', **kwargs):
    return dict(status=status, operator='张三', merchant='', occurred_at='2026-01-01T10:00:00+08:00', **kwargs)

def test_complete_workflow(client):
    p=client.post('/api/products',json=data(sku='SP-1',name='商品A'))
    assert p.status_code==201
    pid=p.json()['id']
    assert client.post('/api/products',json=data(sku='SP-1',name='重复')).status_code==409
    for day,status in enumerate(['第三方鉴定','商家','已售','退回到仓库','已售'],2):
        body=data(status);body.update(occurred_at=f'2026-01-0{day}T10:00:00+08:00',merchant='商家甲' if status=='商家' else '')
        assert client.post(f'/api/products/{pid}/move',json=body).status_code==200
    assert client.get('/api/merchants').json()[0]['name']=='商家甲'
    assert len(client.get(f'/api/products/{pid}/events').json())==6
    assert client.get('/api/products',params={'status':'已售'}).json()[0]['sku']=='SP-1'
    params={'start':'2026-01-01T00:00:00+08:00','end':'2026-01-31T23:59:59+08:00'}
    report=client.get('/api/report',params=params).json()
    assert (report['outbound_count'],report['sales_count'],report['sales_products'])==(2,2,1)
    excel=client.get('/api/export',params=params)
    wb=load_workbook(BytesIO(excel.content))
    assert wb.sheetnames==['当前库存','状态汇总','期间汇总','期间流水']
    assert wb['当前库存']['C2'].value=='已售'
    assert wb['期间流水'].max_row==7
    narrow=client.get('/api/report',params={'start':'2026-01-03T10:00:00+08:00','end':'2026-01-03T10:00:00+08:00'}).json()
    assert len(narrow['events'])==1

def test_validation_and_persistence(client):
    p=client.post('/api/products',json=data(sku='=1+1',name='<script>'))
    pid=p.json()['id']
    body=data('商家')
    assert client.post(f'/api/products/{pid}/move',json=body).status_code==422
    body=data('未知')
    assert client.post(f'/api/products/{pid}/move',json=body).status_code==422
    body=data();body['occurred_at']='2025-01-01T00:00:00Z'
    assert client.post(f'/api/products/{pid}/move',json=body).status_code==422
    assert client.post('/api/merchants',json={'name':'  '}).status_code==422
    wb=load_workbook(BytesIO(client.get('/api/export').content))
    assert wb['当前库存']['A2'].data_type=='s'
    assert wb['当前库存']['A2'].value=="'=1+1"
    with TestClient(main.app) as other:
        other.cookies.update(client.cookies)
        assert len(other.get('/api/products').json())==1
    assert client.get('/').status_code==200
    assert client.get('/api/health').json()=={'status':'ok'}

def test_all_statuses_export_and_invalid_ranges(client):
    for i,status in enumerate(main.STATUSES):
        body=data(status,sku=f'ALL-{i}',name=status)
        if status=='商家': body['merchant']='持久商家'
        assert client.post('/api/products',json=body).status_code==201
    wb=load_workbook(BytesIO(client.get('/api/export').content))
    assert set(row[2] for row in wb['当前库存'].iter_rows(min_row=2,values_only=True))==set(main.STATUSES)
    assert [row[1] for row in wb['状态汇总'].iter_rows(min_row=2,values_only=True)]==[1]*5
    assert client.get('/api/export',params={'start':'2026-01-01T00:00:00Z'}).status_code==422
    assert client.get('/api/report',params={'start':'2026-02-01T00:00:00Z','end':'2026-01-01T00:00:00Z'}).status_code==422
    assert client.get('/api/report',params={'start':'2026-01-01T00:00:00','end':'2026-02-01T00:00:00'}).status_code==422
