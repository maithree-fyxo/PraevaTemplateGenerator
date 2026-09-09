"""Entry point.

Local dev:   python run.py            -> http://127.0.0.1:8000
Hosted:      the platform sets PORT; we bind 0.0.0.0:$PORT.
             (Render/Railway/Fly usually run: uvicorn app.main:app --host 0.0.0.0 --port $PORT)
"""
import os
import uvicorn

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0" if os.getenv("PORT") else "127.0.0.1")
    reload = os.getenv("RELOAD", "").lower() in ("1", "true", "yes")
    uvicorn.run("app.main:app", host=host, port=port, reload=reload)
