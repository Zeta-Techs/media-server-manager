import os

from waitress import serve

from .app import create_app

if __name__ == "__main__":
    host = os.environ.get("CLP_WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("CLP_WEB_PORT", "8088"))
    threads = max(2, int(os.environ.get("CLP_WEB_THREADS", "8")))
    serve(create_app(), host=host, port=port, threads=threads)
