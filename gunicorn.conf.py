# Gunicorn loads this file automatically from the working directory (the
# Docker image's /app, or the folder start.sh runs from).
#
# Same as gunicorn's default access log line, except the request is logged as
# method + path + protocol without the query string. Webhook URLs carry the
# webhook secret as ?secret=… (Plex can't send it any other way), and the
# default format would write it to the container log.
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s "%(f)s" "%(a)s"'

# The control socket (gunicorn 26+) isn't used, and it can't be created in
# /app when running as a non-root user. Older gunicorn ignores this setting.
control_socket_disable = True


def on_starting(server):
    # Check the data folder in the master process, before any worker starts,
    # so a permissions problem stops the container with a clear message.
    from config_store import check_data_dir
    check_data_dir()
