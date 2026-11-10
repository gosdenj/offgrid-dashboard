# The off-grid dashboard, in a container.
#
# The image holds the dashboard and the server that serves it. The
# settings - which carry the Home Assistant long-lived access token in
# plain text - are mounted in from the host at run time (see
# docker-compose.portainer.yml), so a rebuild can never lose them and a
# built image can never leak them.
#
# The server is written against the Python standard library only, so
# there is nothing to install and nothing to compile: the image is the
# official Python one, plus the timezone database.

FROM python:3.13-slim

# The settings page validates the timezone name the user types, and it
# needs the timezone database to do it. Without tzdata, ZoneInfo raises
# ZoneInfoNotFoundError and every timezone would be reported as wrong.
#
# DEBIAN_FRONTEND=noninteractive and TZ stop tzdata from asking
# questions while the image is being built.
ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Etc/UTC \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

# The dashboard does not need root. It needs to read the code, and to
# write one thing: the settings file it is handed.
#
# The uid is 1000 because that is the first ordinary user on a Raspberry Pi
# OS, so the mounted settings file usually belongs to the right person
# already. docker-compose.portainer.yml can override it with
# OFFGRID_UID/OFFGRID_GID.
RUN groupadd --gid 1000 offgrid \
 && useradd --uid 1000 --gid 1000 --home-dir /home/offgrid \
            --shell /usr/sbin/nologin offgrid \
 && mkdir -p /home/offgrid /config \
 && chown -R offgrid:offgrid /home/offgrid /config

WORKDIR /app

# Only what the server needs: the dashboard and the entry point. The
# tests and the notes stay out of it.
COPY --chown=root:root offgrid-dashboard.html /app/offgrid-dashboard.html
COPY --chown=root:root server.py /app/server.py

# Where the settings live, and how to listen. server.py reads all three
# from the environment, so the container works whether it is started by
# compose or by hand.
ENV OFFGRID_CONFIG=/config/config.json \
    OFFGRID_HOST=0.0.0.0 \
    OFFGRID_PORT=8767

EXPOSE 8767

USER offgrid

# The liveness check. /healthz answers {"ok": true} as soon as the server
# is listening, and it needs nothing but the standard library, so there
# is no curl to install in the image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import os,sys,urllib.request; p=os.environ.get('OFFGRID_PORT','8767'); r=urllib.request.urlopen('http://127.0.0.1:'+p+'/healthz', timeout=4); sys.exit(0 if r.status==200 else 1)"

CMD ["python3", "server.py"]
