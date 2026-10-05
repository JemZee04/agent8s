FROM python:3.12-alpine
RUN pip install --no-cache-dir "aiohttp>=3.9,<4"
WORKDIR /app
COPY server.py /app/server.py
COPY web /srv/web
USER nobody
EXPOSE 8765
ENTRYPOINT ["python", "/app/server.py", "--host", "0.0.0.0", "--port", "8765", "--web-dir", "/srv/web"]
CMD ["--prefix", "/agent8s"]
