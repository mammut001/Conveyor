"""One attempt at an approved G3 GET/POST through pinned HTTPS, no redirects.

Must only be invoked AFTER durable state moved to dispatched. Any exception
is uncertain: a remote server may already have committed the request.
"""
from __future__ import annotations
import http.client
import socket
import threading
from urllib.parse import urlencode
from workflow_browser_external_fetch import _PinnedHTTPSConnection

def send_once(host, port, ip, path, method, pairs):
    if method not in ("GET","POST") or not path.startswith("/") or "#" in path or "?" in path:
        raise ValueError("g3_unsafe_request")
    # JSON round-trips form pairs as lists; urllib.urlencode requires tuples.
    body=urlencode([(name, value) for name, value in pairs]).encode("ascii")
    if len(body)>8192:
        raise ValueError("g3_payload_too_large")
    authority=host if port==443 else f"{host}:{port}"
    target=path+("?" + body.decode("ascii") if method=="GET" and body else "")
    conn=_PinnedHTTPSConnection(host,port,ip)
    stop=threading.Event()
    active=[None]
    def expire():
        stop.set()
        sock=active[0] or conn.sock
        if sock:
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            sock.close()
    timer=threading.Timer(12,expire)
    timer.daemon=True
    timer.start()
    try:
        headers={"Host":authority,"Accept":"text/html, text/plain",
                 "Accept-Encoding":"identity","Connection":"close"}
        if method=="POST":
            headers["Content-Type"]="application/x-www-form-urlencoded"
        conn.request(method,target,body=body if method=="POST" else None,headers=headers)
        active[0]=conn.sock
        if conn.sock: conn.sock.settimeout(8)
        response=conn.getresponse()
        if stop.is_set():
            raise TimeoutError("g3_submit_timeout")
        headers=[(k.lower(),v) for k,v in response.getheaders()]
        if (not 200<=response.status<300 or
            any(k in ("set-cookie","location","content-disposition","content-encoding")
                for k,_ in headers)):
            raise ValueError("g3_submit_response_unverifiable")
        return response.status
    finally:
        timer.cancel()
        conn.close()
