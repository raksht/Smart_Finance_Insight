"""WSGI entry point for production servers (gunicorn, uWSGI, etc).

Local dev: run `python app.py` as before.
Production: `gunicorn wsgi:app` (see Procfile). Set the SECRET_KEY
environment variable before deploying - see README.md.
"""
from app import app

if __name__ == "__main__":
    app.run()
