FROM python:3.12-slim
WORKDIR /srv/app
COPY app.py requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --system --uid 10001 app
USER app
ENV HOST=0.0.0.0
EXPOSE 8083
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8083/health')"
CMD ["python", "app.py"]
