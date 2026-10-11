# Gunicorn loads this file automatically from the working directory (the
# Docker image's /app, or the folder start.sh runs from).
#
# Same as gunicorn's default access log line, except the request is logged as
# method + path + protocol without the query string. Webhook URLs carry the
# webhook secret as ?secret=… (Plex can't send it any other way), and the
# default format would write it to the container log.
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s "%(f)s" "%(a)s"'
