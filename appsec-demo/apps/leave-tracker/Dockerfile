FROM python:3.12-slim
WORKDIR /srv/app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt && useradd --system --uid 10001 app
USER app
ENV HOST=0.0.0.0
EXPOSE 8082
CMD ["python", "app.py"]
