"""Password hashes, revocable sessions and server-side access checks."""
import hashlib
import hmac
import os
import secrets
import sqlite3
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

ROLES = {'admin':'管理员', 'employee':'员工', 'viewer':'只读人员'}
COOKIE = 'inventory_session'


def schema(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,role TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),csrf TEXT NOT NULL,expires REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS login_attempts(key TEXT PRIMARY KEY,count INTEGER NOT NULL,until REAL NOT NULL);
    ''')


def password_hash(password):
    salt=secrets.token_hex(16)
    return salt+':'+hashlib.scrypt(password.encode(),salt=salt.encode(),n=16384,r=8,p=1).hex()


def verify(password,encoded):
    salt,digest=encoded.split(':')
    actual=hashlib.scrypt(password.encode(),salt=salt.encode(),n=16384,r=8,p=1).hex()
    return hmac.compare_digest(actual,digest)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def public(user):
    return {key:user[key] for key in ['id','username','role','active']}


class Credentials(BaseModel):
    username: str = Field(min_length=1,max_length=100,pattern=r'^[\w.-]+$')
    password: str = Field(min_length=1,max_length=128)


class NewUser(Credentials):
    role: str = 'employee'


class UpdateUser(BaseModel):
    role: str
    active: bool
    password: str | None = Field(default=None,min_length=10,max_length=128)


class ChangePassword(BaseModel):
    current_password: str = Field(min_length=1,max_length=128)
    password: str = Field(min_length=10,max_length=128)


def install(app,db):
    @app.middleware('http')
    async def authentication(request: Request,call_next):
        path=request.url.path
        if not path.startswith('/api/') or path=='/api/health':
            return await call_next(request)
        write=request.method not in ('GET','HEAD','OPTIONS')
        if write and request.headers.get('origin') not in (None,str(request.base_url).rstrip('/')):
            return JSONResponse({'detail':'请求来源不匹配'},status_code=403)
        if path=='/api/auth/login':
            return await call_next(request)
        with db() as c:
            user=c.execute('SELECT u.*,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE token_hash=? AND expires>? AND u.active=1',(token_hash(request.cookies.get(COOKIE,'')),time.time())).fetchone()
        if not user:
            return JSONResponse({'detail':'请先登录，或登录已过期'},status_code=401,headers={'Cache-Control':'no-store'})
        if write and not hmac.compare_digest(request.headers.get('x-csrf-token','').encode(),user['csrf'].encode()):
            return JSONResponse({'detail':'安全令牌失效，请刷新页面重新登录'},status_code=403)
        if path.startswith('/api/users') and user['role']!='admin':
            return JSONResponse({'detail':'仅管理员可管理员工账号'},status_code=403)
        if user['role']=='viewer' and (path=='/api/export' or (write and not path.startswith('/api/auth/'))):
            return JSONResponse({'detail':'只读账号不能修改或导出数据'},status_code=403)
        request.state.user=dict(user)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        return response

    @app.post('/api/auth/login')
    def login(body: Credentials,request: Request):
        now=time.time()
        key=(request.client.host if request.client else 'local')+':'+body.username
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            attempt=c.execute('SELECT * FROM login_attempts WHERE key=?',(key,)).fetchone()
            if attempt and attempt['until']>now and attempt['count']>=5:
                raise HTTPException(429,'登录失败次数过多，请15分钟后重试')
            user=c.execute('SELECT * FROM users WHERE username=?',(body.username,)).fetchone()
            valid=verify(body.password,user['password_hash'] if user else '0'*32+':'+'0'*128)
            if not user or not valid or not user['active']:
                count=attempt['count']+1 if attempt and attempt['until']>now else 1
                until=attempt['until'] if attempt and attempt['until']>now else now+900
                c.execute('INSERT OR REPLACE INTO login_attempts VALUES(?,?,?)',(key,count,until))
                return JSONResponse({'detail':'账号或密码错误，或账号已停用'},status_code=401,headers={'Cache-Control':'no-store'})
            c.execute('DELETE FROM login_attempts WHERE key=?',(key,))
            c.execute('DELETE FROM sessions WHERE expires<=? OR token_hash=?',(now,token_hash(request.cookies.get(COOKIE,''))))
            token,csrf=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
            c.execute('INSERT INTO sessions VALUES(?,?,?,?)',(token_hash(token),user['id'],csrf,now+43200))
        response=JSONResponse({**public(user),'csrf':csrf},headers={'Cache-Control':'no-store'})
        response.set_cookie(COOKIE,token,httponly=True,samesite='strict',secure=os.getenv('COOKIE_SECURE')=='1',max_age=43200)
        return response

    @app.get('/api/auth/me')
    def me(request: Request):
        return {**public(request.state.user),'csrf':request.state.user['csrf']}

    @app.post('/api/auth/logout')
    def logout(request: Request):
        with db() as c:
            c.execute('DELETE FROM sessions WHERE token_hash=?',(token_hash(request.cookies.get(COOKIE,'')),))
        response=JSONResponse({'status':'ok'})
        response.delete_cookie(COOKIE)
        return response

    @app.post('/api/auth/password')
    def password(body: ChangePassword,request: Request):
        with db() as c:
            user=c.execute('SELECT * FROM users WHERE id=?',(request.state.user['id'],)).fetchone()
            if not verify(body.current_password,user['password_hash']):
                raise HTTPException(400,'原密码不正确')
            c.execute('UPDATE users SET password_hash=? WHERE id=?',(password_hash(body.password),user['id']))
            c.execute('DELETE FROM sessions WHERE user_id=?',(user['id'],))
        return {'status':'ok'}

    @app.get('/api/users')
    def users():
        with db() as c:
            return [public(r) for r in c.execute('SELECT * FROM users ORDER BY id')]

    @app.post('/api/users',status_code=201)
    def create_user(body: NewUser):
        if body.role not in ROLES or len(body.password)<10:
            raise HTTPException(422,'请选择有效角色，密码至少10位')
        with db() as c:
            try:
                c.execute('INSERT INTO users(username,password_hash,role) VALUES(?,?,?)',(body.username,password_hash(body.password),body.role))
            except sqlite3.IntegrityError:
                raise HTTPException(409,'该账号已存在')
        return {'status':'ok'}

    @app.put('/api/users/{uid}')
    def update_user(uid: int,body: UpdateUser):
        if body.role not in ROLES:
            raise HTTPException(422,'角色无效')
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            user=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
            if not user:
                raise HTTPException(404,'账号不存在')
            if user['role']=='admin' and user['active'] and (body.role!='admin' or not body.active):
                if c.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]<=1:
                    raise HTTPException(409,'必须保留至少一名启用的管理员')
            c.execute('UPDATE users SET role=?,active=? WHERE id=?',(body.role,body.active,uid))
            if body.password:
                c.execute('UPDATE users SET password_hash=? WHERE id=?',(password_hash(body.password),uid))
            c.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
        return {'status':'ok'}
