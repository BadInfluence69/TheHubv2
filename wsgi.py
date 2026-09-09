"""
WSGI entry point for production servers.

    gunicorn -c gunicorn.conf.py wsgi:app        (Linux / macOS)
    waitress-serve --port=5002 wsgi:app          (Windows)
"""
from hub import create_app

app = create_app()
