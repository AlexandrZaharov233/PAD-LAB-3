@echo off
cd /d "%~dp0"
echo Open http://127.0.0.1:8082 after the forwarding message appears.
echo Keep this window open. Press Ctrl+C to stop forwarding.
kubectl --context parkslot-lab3 -n parkslot port-forward --address 127.0.0.1 service/gateway 8082:8000
if errorlevel 1 pause
