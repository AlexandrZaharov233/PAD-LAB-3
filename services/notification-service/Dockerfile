FROM python:3.12.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app

# Install dependencies before copying code so edits reuse this layer.
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -c constraints.txt -r requirements.txt && \
    groupadd --gid 10001 parking && \
    useradd --uid 10001 --gid 10001 --no-create-home --no-log-init parking && \
    install -d -m 0750 -o 10001 -g 10001 /data

COPY parking_common /app/parking_common
COPY services/notification-service/ /app/

USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["python", "-m", "uvicorn"]
CMD ["app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
