"""Generate a repeatable, clearly marked 100-piece test inventory."""
import random
from datetime import datetime, timedelta, timezone

from app.main import db

PREFIX = 'DEMO-RL-POLO-'


def seed():
    rng = random.Random(20261031)
    attributes = [(condition, size) for condition in ['全新', '有使用痕迹'] for size in ['L', 'M'] for _ in range(25)]
    destinations = ['鉴定仓'] * 50 + ['商家'] * 50
    rng.shuffle(attributes)
    rng.shuffle(destinations)
    now = datetime.now(timezone.utc)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        existing = c.execute('SELECT COUNT(*) FROM products WHERE substr(sku,1,?)=?', (len(PREFIX), PREFIX)).fetchone()[0]
        if existing:
            if existing != 100:
                raise ValueError('检测到部分同编号测试商品；为避免覆盖或混入数据，导入已取消。')
            print('这批100件测试商品已存在，未重复添加，也未修改现有商品。')
            return False
        c.execute("INSERT OR IGNORE INTO options(kind,name,category) VALUES('status','鉴定仓','external')")
        c.execute("INSERT OR IGNORE INTO merchants(name) VALUES('Aloha')")
        for kind, values in [('condition', ['全新', '有使用痕迹']), ('style', ['经典短袖Polo衬衫']), ('size', ['L', 'M'])]:
            for value in values:
                c.execute('INSERT OR IGNORE INTO options(kind,name) VALUES(?,?)', (kind, value))
        for i, ((condition, size), destination) in enumerate(zip(attributes, destinations), 1):
            entered = (now - timedelta(days=rng.randint(2, 7), minutes=i)).isoformat()
            moved = (datetime.fromisoformat(entered) + timedelta(hours=12)).isoformat()
            merchant = 'Aloha' if destination == '商家' else ''
            pid = c.execute('INSERT INTO products(sku,name,status,merchant,created_at,style,condition,size,quantity) VALUES(?,?,?,?,?,?,?,?,1)', (f'{PREFIX}{i:03}', '拉夫劳伦 Ralph Lauren Polo衬衫', destination, merchant, entered, '经典短袖Polo衬衫', condition, size)).lastrowid
            for previous, target, store, occurred in [('', '仓库', '', entered), ('仓库', destination, merchant, moved)]:
                c.execute('INSERT INTO events(product_id,from_status,to_status,merchant,operator,occurred_at,note,quantity) VALUES(?,?,?,?,?,?,?,1)', (pid, previous, target, store, '虚拟数据生成器', occurred, '虚拟测试数据；非真实库存'))
    print('已生成100件虚拟商品：全新50件、有使用痕迹50件；L/M各50件；鉴定仓50件、Aloha商家50件。')
    return True


if __name__ == '__main__':
    seed()
