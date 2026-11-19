# Build the test runner; application images use each service's Dockerfile.
FROM python:3.12.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
COPY requirements.txt .
COPY constraints.txt .
RUN pip install --no-cache-dir -c constraints.txt -r requirements.txt && \
    groupadd --gid 10001 parking && useradd --uid 10001 --gid 10001 parking && \
    mkdir /data && chown parking:parking /data
COPY parking_common /app/parking_common
ARG APP_PATH=services/auth-service
COPY ${APP_PATH}/ /app/
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

FROM runtime AS tests
USER root
RUN pip install --no-cache-dir -c constraints.txt pytest==9.2.0
COPY services /app/services
COPY gateway /app/gateway
COPY tests /app/tests
COPY scripts/configure.py /app/scripts/configure.py
COPY scripts/ci_test_runner.py /app/scripts/ci_test_runner.py
COPY pytest.ini /app/pytest.ini
USER 10001:10001
ENTRYPOINT ["python", "-m", "pytest"]
CMD ["-q"]
