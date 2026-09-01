FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY relay ./relay
COPY control_bot ./control_bot

RUN mkdir -p /app/data

EXPOSE 8765

CMD ["python", "main.py"]