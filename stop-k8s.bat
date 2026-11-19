@echo off
cd /d "%~dp0"
minikube -p parkslot-lab3 stop
if errorlevel 1 pause
