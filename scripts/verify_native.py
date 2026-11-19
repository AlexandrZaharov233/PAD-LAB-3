"""Run real isolated processes, API tests and fault recovery without Docker.

Requires a locally installed nats-server. Production code is not replaced with mocks.
All files and logs live in the temporary directory printed at startup.
"""
import argparse
import importlib.metadata
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import dotenv_values

sys.path.insert(0,str(Path(__file__).resolve().parent))
from configure import configure

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from parking_common.runtime import SERVICE_PORTS

PORTS = {**SERVICE_PORTS, "gateway":8080}


def wait_for(getter,predicate,timeout=30):
    deadline=time.monotonic()+timeout
    last=None
    while time.monotonic()<deadline:
        try:
            last=getter()
            if predicate(last):return last
        except (httpx.HTTPError,sqlite3.Error):pass
        time.sleep(.2)
    raise RuntimeError(f"Readiness timeout: {last}")


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--nats",required=True,help="Absolute path to nats-server executable")
    parser.add_argument("--browser",help="Optional browser smoke script, executed while servers are running")
    parser.add_argument("--output",default=str(ROOT/"docs"/"verification.json"))
    args=parser.parse_args()
    work=Path(tempfile.mkdtemp(prefix="parkslot-verify-"))
    print("Verification directory:",work,flush=True)
    configure(work/".env")
    config=dotenv_values(work/".env")
    nats_config=work/"nats.conf"
    nats_config.write_text('host: "127.0.0.1"\nport: 4222\nhttp_port: 8222\nauthorization { user: '+json.dumps(config["NATS_USER"])+', password: '+json.dumps(config["NATS_PASSWORD"])+' }\njetstream { store_dir: '+json.dumps(str(work/"jetstream"))+' }\n')
    processes={};logs=[];all_processes=[]
    def start(name):
        log=(work/(name+".log")).open("a",encoding="utf-8");logs.append(log)
        if name=="nats":
            command=[args.nats,"-c",str(nats_config)];env=os.environ.copy()
        else:
            env={**os.environ,**config,"PYTHONPATH":str(ROOT),"DATABASE_PATH":str(work/(name+".db")),"PYTHONUNBUFFERED":"1"}
            if name!="auth":env.pop("JWT_PRIVATE_KEY",None);env.pop("ADMIN_PASSWORD",None)
            directory=ROOT/"gateway" if name=="gateway" else ROOT/"services"/(name+"-service")
            command=[sys.executable,"-m","uvicorn","app:app","--app-dir",str(directory),"--host","127.0.0.1","--port",str(PORTS[name]),"--no-access-log"]
        process=subprocess.Popen(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
        processes[name]=process;all_processes.append(process)
        return process
    def stop(name):
        process=processes.pop(name)
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait()
    result={"date":datetime.now(timezone.utc).isoformat(),"python":sys.version.split()[0],"platform":sys.platform,
            "mode":"11 independent native HTTP processes + real NATS JetStream","docker_executed":False,
            "versions":{p:importlib.metadata.version(p) for p in ["fastapi","nats-py","httpx","PyJWT"]},"checks":{}}
    try:
        with httpx.Client(timeout=3,trust_env=False) as client:
            start("nats")
            wait_for(lambda:client.get("http://127.0.0.1:8222/healthz").status_code,lambda x:x==200)
            for name in PORTS:start(name)
            wait_for(lambda:client.get("http://127.0.0.1:8080/ready").status_code,lambda x:x==200,45)
        print(f"All {len(SERVICE_PORTS)} services and Gateway are ready.",flush=True)
        env={**os.environ,"RUN_E2E":"1","E2E_URL":"http://127.0.0.1:8080",
             "E2E_ADMIN_EMAIL":config["ADMIN_EMAIL"],"E2E_ADMIN_PASSWORD":config["ADMIN_PASSWORD"]}
        completed=subprocess.run([sys.executable,"-m","pytest","-q","--junitxml="+str(work/"tests.xml")],cwd=ROOT,env=env)
        if completed.returncode:raise RuntimeError("Automated tests failed")
        import xml.etree.ElementTree as ET
        suite=ET.parse(work/"tests.xml").find("testsuite")
        result["pytest"]={key:int(suite.attrib.get(key,0)) for key in ("tests","failures","errors","skipped")}
        result["checks"]["unit_and_live_api"]=True
        with httpx.Client(base_url="http://127.0.0.1:8080",timeout=8,trust_env=False) as client:
            def notifications():
                response=client.get("/api/notifications")
                response.raise_for_status()
                return response.json()
            response=client.post("/api/auth/register",json={"email":"recovery-"+uuid.uuid4().hex+"@example.test","name":"Проверка восстановления","password":"Recovery-password-123"})
            response.raise_for_status();client.headers["Authorization"]="Bearer "+response.json()["access_token"]
            vehicle=client.post("/api/profiles/me/vehicles",json={"plate":"REC 123","label":"Recovery"}).json()
            lot=client.get("/api/parking/lots").json()[0]
            spaces=client.get("/api/parking/lots/"+lot["id"]+"/spaces").json()
            start_at=datetime.now(timezone.utc)+timedelta(days=3)
            body={"space_id":spaces[0]["id"],"vehicle_id":vehicle["id"],"start_at":start_at.isoformat(),"end_at":(start_at+timedelta(hours=1)).isoformat(),"idempotency_key":str(uuid.uuid4())}
            stop("notification")
            created=client.post("/api/bookings",json=body);created.raise_for_status();booking=created.json()
            def published():
                with sqlite3.connect(work/"booking.db") as db:
                    return db.execute("SELECT published_at FROM outbox WHERE type='BookingCreated' AND payload LIKE ?",('%'+booking["id"]+'%',)).fetchone()[0]
            wait_for(published,bool)
            start("notification")
            notices=wait_for(notifications,lambda rows: any(x.get("booking_id")==booking["id"] for x in rows))
            assert sum(x["booking_id"]==booking["id"] for x in notices)==1
            result["checks"]["consumer_restart_durable_delivery"]=True
            print("Consumer restart: queued event delivered once.",flush=True)
            stop("nats")
            body2={**body,"space_id":spaces[1]["id"],"idempotency_key":str(uuid.uuid4())}
            created=client.post("/api/bookings",json=body2);created.raise_for_status();second=created.json()
            with sqlite3.connect(work/"booking.db") as db:
                pending=db.execute("SELECT published_at FROM outbox WHERE type='BookingCreated' AND payload LIKE ?",('%'+second["id"]+'%',)).fetchone()[0]
            assert pending is None
            start("nats")
            wait_for(lambda:client.get("/ready").status_code,lambda x:x==200,45)
            wait_for(notifications,lambda rows:any(x.get("booking_id")==second["id"] for x in rows))
            result["checks"]["broker_restart_outbox_recovery"]=True
            print("Broker restart: committed outbox event recovered.",flush=True)
            stop("booking");start("booking")
            wait_for(lambda:client.get("/ready").status_code,lambda x:x==200)
            saved=client.get("/api/bookings/"+second["id"]);saved.raise_for_status()
            assert saved.json()["id"]==second["id"]
            result["checks"]["service_restart_persistence"]=True
            print("Service restart: booking persisted.",flush=True)
        if args.browser:
            browser_env={**env,"QA_OUTPUT":str(work),"PARKING_BROWSER":os.environ.get("PARKING_BROWSER","")}
            subprocess.run([sys.executable,args.browser],cwd=ROOT,env=browser_env,check=True)
            result["checks"]["browser_flow"]=True
        for name in ("booking","parking","profile"):
            text=(work/(name+".log")).read_text()
            if '"request_id": "e2e-request-through-services"' not in text:
                raise AssertionError("Request ID missing in "+name)
        result["checks"]["correlated_logs"]=True
        Path(args.output).parent.mkdir(parents=True,exist_ok=True)
        Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        print("Verification complete:",args.output,flush=True)
    except BaseException:
        for name in processes:
            print("--- Last lines:",name,flush=True)
            path=work/(name+".log")
            if path.exists():print("\n".join(path.read_text(errors="replace").splitlines()[-12:]),flush=True)
        raise
    finally:
        for name in list(processes):stop(name)
        for log in logs:log.close()


if __name__=="__main__":main()
