"""Gunicorn configuration — auto-detected by gunicorn."""

import os

bind = f"0.0.0.0:{os.getenv('PORT', '10000')}"
workers = 1
threads = 2
timeout = 300
max_requests = 50
max_requests_jitter = 10
accesslog = "-"
errorlog = "-"
loglevel = "info"
