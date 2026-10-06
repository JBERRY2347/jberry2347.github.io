FROM python:latest
WORKDIR /srv/app
COPY . .
RUN pip install -r requirements.txt
ENV HOST=0.0.0.0
EXPOSE 8081
CMD ["python", "app.py"]
