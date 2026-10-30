from fastapi import FastAPI

app = FastAPI(title="库存进出管理系统")

@app.get('/api/health')
def health():
    return {'status': 'ok'}
