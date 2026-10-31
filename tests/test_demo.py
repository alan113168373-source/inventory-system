from collections import Counter

from app import main
from scripts.seed_demo import seed, PREFIX


def test_demo_balanced_repeatable_and_preserves_other_stock(tmp_path, monkeypatch):
    monkeypatch.setattr(main,'DB_PATH',tmp_path/'demo.db')
    with main.db() as c:
        c.execute("INSERT INTO products(sku,name,status,created_at) VALUES('REAL-1','已有商品','仓库','2026-01-01T00:00:00Z')")
    assert seed() is True
    assert seed() is False
    with main.db() as c:
        rows=c.execute('SELECT * FROM products WHERE sku LIKE ?', (PREFIX+'%',)).fetchall()
        assert len(rows)==100 and sum(r['quantity'] for r in rows)==100
        assert Counter(r['condition'] for r in rows)=={'全新':50,'有使用痕迹':50}
        assert Counter(r['size'] for r in rows)=={'L':50,'M':50}
        assert Counter((r['condition'],r['size']) for r in rows)=={('全新','L'):25,('全新','M'):25,('有使用痕迹','L'):25,('有使用痕迹','M'):25}
        assert Counter(r['status'] for r in rows)=={'鉴定仓':50,'商家':50}
        assert all(r['merchant']=='Aloha' for r in rows if r['status']=='商家')
        assert c.execute('SELECT COUNT(*) FROM events').fetchone()[0]==200
        assert c.execute("SELECT name FROM products WHERE sku='REAL-1'").fetchone()[0]=='已有商品'
