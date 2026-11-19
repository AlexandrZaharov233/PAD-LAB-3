@echo off
cd /d "%~dp0"
docker compose --profile test run --build --rm tests
pause
