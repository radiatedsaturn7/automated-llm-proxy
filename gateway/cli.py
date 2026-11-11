import uvicorn


def main():
    uvicorn.run("gateway.main:app", host="0.0.0.0", port=8080, proxy_headers=False)
