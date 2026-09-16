"""
Gunicorn settings for The Hub.

Threads rather than processes: the work here is waiting on yt-dlp, ffmpeg, and
network calls, and background upload threads need to live in the same process
as the request that started them.
"""
import multiprocessing
import os

bind = f"{os.getenv('HOST', '0.0.0.0')}:{os.getenv('PORT', '5002')}"

workers = int(os.getenv("WEB_WORKERS", 1))
threads = int(os.getenv("WEB_THREADS", max(8, multiprocessing.cpu_count() * 4)))
worker_class = "gthread"

# Video responses stream for a long time; don't cut them off.
timeout = int(os.getenv("WEB_TIMEOUT", 600))
graceful_timeout = 30
keepalive = 5

max_requests = 2000
max_requests_jitter = 200

accesslog = os.getenv("ACCESS_LOG", "-")
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info")

# Uploads are big; give them room before hitting the line-length limit.
limit_request_line = 8190
limit_request_field_size = 16380

proc_name = "thehub"
