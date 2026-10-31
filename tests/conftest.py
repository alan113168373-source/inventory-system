from app import main


def authenticate(client,username='测试管理员'):
    with main.db() as c:
        c.execute("INSERT OR IGNORE INTO users(username,password_hash,role) VALUES(?,?,'admin')",(username,main.auth.password_hash('test-password-123')))
    response=client.post('/api/auth/login',json={'username':username,'password':'test-password-123'})
    assert response.status_code==200
    client.headers['X-CSRF-Token']=response.json()['csrf']
    return client
