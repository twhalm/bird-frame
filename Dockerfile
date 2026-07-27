FROM python:3.12-slim

# Small runtime image. Plate images are NOT baked in: they are fetched on first
# sighting of each species and cached in the /cache volume, so the image stays
# ~150MB instead of ~640MB and disk only grows with birds you actually get.

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/
COPY data/ /data/
COPY web/ /web/

# plates.py resolves ../data and ../web relative to itself, so mirror the
# repo layout inside the image.
RUN mkdir -p /srv && ln -s /app /srv/app && ln -s /data /srv/data && ln -s /web /srv/web
WORKDIR /srv/app

RUN useradd -u 10001 -m birdframe \
 && mkdir -p /cache/plates \
 && chown -R birdframe:birdframe /cache
USER birdframe

VOLUME ["/cache"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"

# gunicorn with a single worker: the detection history and the poller live in
# process memory, so multiple workers would each hold a different gallery.
# Threads handle the concurrent image requests fine.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", \
     "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]
