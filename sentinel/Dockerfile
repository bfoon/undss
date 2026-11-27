# NES Sentinel for any cloud: a ~60 MB image that runs as an unprivileged
# user on a read-only filesystem. Its only state is the /data volume.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NES_SENTINEL_HOME=/data
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
 && useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin sentinel \
 && install -d -m 0700 -o sentinel -g sentinel /data
COPY nes_sentinel.py /app/nes_sentinel.py
USER 10001
VOLUME ["/data"]
ENTRYPOINT ["python", "-I", "/app/nes_sentinel.py"]
CMD ["run"]
