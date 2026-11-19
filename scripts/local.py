"""Optional native development launch: ten services and Gateway, with a real NATS."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from parking_common.runtime import SERVICE_PORTS

SERVICES = {**SERVICE_PORTS, "gateway": 8080}


def main():
    config = dotenv_values(ROOT / ".env")
    if not config.get("JWT_PUBLIC_KEY"):
        raise SystemExit("Run python scripts/configure.py first")
    processes=[]
    try:
        for name,port in SERVICES.items():
            env={**os.environ,**config,"PYTHONPATH":str(ROOT),"DATABASE_PATH":str(ROOT/"data"/(name+".db"))}
            if name!="auth":
                env.pop("JWT_PRIVATE_KEY",None);env.pop("ADMIN_PASSWORD",None)
            directory=ROOT/"gateway" if name=="gateway" else ROOT/"services"/(name+"-service")
            processes.append(subprocess.Popen([sys.executable,"-m","uvicorn","app:app","--host","127.0.0.1",
                "--port",str(port),"--app-dir",str(directory),"--no-access-log"],cwd=ROOT,env=env))
        print("ParkSlot: http://127.0.0.1:8080. Ctrl+C stops only these child processes.")
        while all(p.poll() is None for p in processes):
            time.sleep(.5)
        raise SystemExit("One of the services stopped. Review its log.")
    except KeyboardInterrupt:
        pass
    finally:
        for p in processes:
            if p.poll() is None:p.terminate()
        for p in processes:
            try:p.wait(timeout=5)
            except subprocess.TimeoutExpired:p.kill();p.wait()


if __name__=="__main__":main()
