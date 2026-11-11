"""Local Unix-socket control bridge for the unprivileged preview coordinator."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import subprocess
from conveyor.automation.overnight_store import OvernightStore

ROOT=Path('/srv/conveyor-preview/coordinator')
SOCKET=Path('/srv/conveyor-preview/control/nightly.sock')


class Controller:
    def __init__(self,root=ROOT,*,start=None,stop=None):
        self.root=Path(root);self.store=OvernightStore(self.root/'runs.sqlite3')
        self.start=start or (lambda ident:subprocess.run(['sudo','-n','systemctl','start',f'conveyor-nightly@{ident}.service'],check=True,timeout=30,capture_output=True))
        self.stop=stop or (lambda ident:subprocess.run(['sudo','-n','systemctl','stop',f'conveyor-nightly@{ident}.service'],check=True,timeout=45,capture_output=True))

    def resume_runs(self):
        with self.store.db() as db:
            identities=[row[0] for row in db.execute("SELECT id FROM nightly_runs WHERE state='running'")]
        resumed=[]
        for ident in identities:
            if self.store.reconcile(ident)['state']=='running':
                self.start(ident)
                resumed.append(ident)
        return resumed

    def request(self,data):
        action=data.get('action')
        if action=='start':
            objective=data.get('objective','')
            if not isinstance(objective,str) or not 1 <= len(objective.strip()) <= 2000:
                raise ValueError('an objective of 1–2000 characters is required')
            import time
            with self.store.db() as db:
                active=db.execute("SELECT id FROM nightly_runs WHERE state='running' AND deadline>? LIMIT 1",(time.time(),)).fetchone()
            if active:
                raise ValueError('another nightly run is active; inspect or cancel it before starting a new run')
            config=json.loads((self.root/'config.json').read_text())
            revision=(self.root.parent/'current').resolve().name
            if not re.fullmatch(r'[0-9a-f]{40}',revision):
                raise ValueError('preview revision is unavailable')
            hours=float(data.get('hours',1));max_tasks=int(data.get('max_tasks',12))
            ident=self.store.create(objective,hours=hours,max_tasks=max_tasks)
            self.store.bind_configuration(ident,config)
            self.store.enqueue(ident,'discover','initial',{'revision':revision,'clean_passes':0})
            try:
                self.start(ident)
            except Exception:
                self.store.cancel(ident)
                raise RuntimeError('nightly service did not start; run cancelled')
        elif action in {'status','cancel'}:
            ident=data.get('run_id','')
            if not isinstance(ident,str) or not re.fullmatch(r'[0-9a-f]{32}',ident):
                raise ValueError('invalid run ID')
            self.store.status(ident)
            if action=='cancel':
                self.store.cancel(ident)
                self.stop(ident)
        else:
            raise ValueError('unsupported nightly action')
        return self.store.reconcile(ident)


def client(data,path=SOCKET):
    encoded=json.dumps(data).encode()+b'\n'
    if len(encoded)>16000:
        raise ValueError('request too large')
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
        connection.settimeout(60);connection.connect(str(path));connection.sendall(encoded)
        with connection.makefile('rb') as stream:
            response=json.loads(stream.readline(1_000_000))
    if not response.get('ok'):
        raise RuntimeError(response.get('error','nightly control failed'))
    return response['run']


def main():
    import grp
    controller=Controller()
    controller.resume_runs()
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(60)
            try:
                raw=self.rfile.readline(16001)
                if len(raw)>16000 or not raw.endswith(b'\n'):
                    raise ValueError('request too large')
                data=json.loads(raw)
                if not isinstance(data,dict):
                    raise ValueError('expected object')
                result={'ok':True,'run':controller.request(data)}
            except Exception as exc:
                # Do not expose provider credentials or subprocess diagnostics.
                result={'ok':False,'error':type(exc).__name__}
            self.wfile.write(json.dumps(result).encode()+b'\n')
    SOCKET.parent.mkdir(mode=0o750,parents=True,exist_ok=True)
    if SOCKET.exists():
        SOCKET.unlink()
    with socketserver.UnixStreamServer(str(SOCKET),Handler) as server:
        os.chown(SOCKET,-1,grp.getgrnam('conveyor-preview').gr_gid);SOCKET.chmod(0o660)
        server.serve_forever()


if __name__=='__main__':
    main()
