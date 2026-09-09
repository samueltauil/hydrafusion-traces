FROM python:3.13-slim

WORKDIR /app
COPY fusion_tailer.py .

ENV PYTHONUNBUFFERED=1
USER nobody
ENTRYPOINT ["python", "fusion_tailer.py"]
