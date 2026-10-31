"""Create the first administrator on the local console; no default password."""
import argparse
from getpass import getpass
import re
import sqlite3

from app.main import db
from app.auth import password_hash


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--if-needed',action='store_true')
    args=parser.parse_args()
    with db() as c:
        if c.execute("SELECT 1 FROM users WHERE role='admin' AND active=1").fetchone():
            if not args.if_needed:
                print('已有管理员，请登录网页的“员工账号”管理账号。')
            return
    print('首次使用，请设置管理员账号（支持中文姓名、字母、数字、点、下划线或短横线）。')
    username=input('管理员账号：').strip()
    if not re.fullmatch(r'[\w.-]{1,100}',username):
        raise SystemExit('账号格式不正确')
    password=getpass('管理员密码（至少10位，输入不显示）：')
    if not 10<=len(password)<=128 or password!=getpass('再次输入密码：'):
        raise SystemExit('密码长度不符或两次输入不一致')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT 1 FROM users WHERE role='admin' AND active=1").fetchone():
            raise SystemExit('管理员已由其他进程创建，请重新启动后登录。')
        try:
            c.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,'admin')",(username,password_hash(password)))
        except sqlite3.IntegrityError:
            raise SystemExit('账号名称已存在，请使用其他名称。')
    print('管理员创建成功。登录后可创建员工账号。')


if __name__=='__main__':
    main()
