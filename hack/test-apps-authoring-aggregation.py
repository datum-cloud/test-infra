#!/usr/bin/env python3
"""Local real Milo-to-Apps API aggregation test. RBAC only; not IAM/FGA proof."""
import argparse, base64, datetime, http.client, json, os, pathlib, signal, socket, ssl
import subprocess, sys, tempfile, threading, time, urllib.error, urllib.request, uuid

MILO_PIN = "ba4b06e8dde99dd70e9a2f34e011d635e4408a76"
APPS_PIN = "ccb6cedf6e5a8b29ec1ac74e1dfdaa5ff3db6f23"
DATUMCTL_PIN = "28f4d364302a9df451b7a453cce1ce1cb99ce0a0"
PROJECT = "resourcemanager.miloapis.com"
GROUP = "apps.miloapis.com"
SERVICE = "apps-authoring.apps-system.svc:8443"
API_PATH = f"/apis/{GROUP}/v1alpha1"

def freeport():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); v = s.getsockname()[1]; s.close(); return v

def sh(args, **kw):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20, **kw)

def verify_binary(path, revision, modified):
    info=sh(["go","version","-m",str(path)]).stdout
    if f"build\tvcs.revision={revision}" not in info:
        raise RuntimeError(f"binary {path.name} is not traceable to required source revision {revision}")
    if f"build\tvcs.modified={str(modified).lower()}" not in info:
        raise RuntimeError(f"binary {path.name} has an unexpected Go vcs.modified build setting")

def cert(d, ca_key, ca, name, usage, sans=""):
    key, csr, crt, ext = [d / (name + x) for x in (".key", ".csr", ".crt", ".ext")]
    sh(["openssl","req","-newkey","rsa:2048","-nodes","-subj","/CN="+name,"-keyout",str(key),"-out",str(csr)])
    ext.write_text("basicConstraints=CA:FALSE\nextendedKeyUsage="+usage+"\n"+("subjectAltName="+sans+"\n" if sans else ""))
    sh(["openssl","x509","-req","-in",str(csr),"-CA",str(ca),"-CAkey",str(ca_key),"-CAcreateserial","-days","2","-extfile",str(ext),"-out",str(crt)])
    csr.unlink(); ext.unlink()
    return key, crt

def ca(d, name):
    key, crt = d/(name+".key"), d/(name+".crt")
    sh(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","2","-subj","/CN="+name,"-addext","basicConstraints=critical,CA:TRUE","-addext","keyUsage=critical,keyCertSign,cRLSign","-keyout",str(key),"-out",str(crt)])
    return key, crt

def req(url, token="", method="GET", obj=None, headers=None, ctx=None, timeout=6):
    data = None if obj is None else json.dumps(obj,separators=(",",":")).encode()
    h={"Accept":"application/json"}
    if token: h["Authorization"]="Bearer "+token
    if data is not None: h["Content-Type"]="application/json"
    h.update(headers or {})
    q=urllib.request.Request(url,data=data,headers=h,method=method)
    try:
        with urllib.request.urlopen(q,context=ctx,timeout=timeout) as r:
            raw=r.read(4*1024*1024+1)
            if len(raw)>4*1024*1024: raise RuntimeError("response exceeded bound")
            try: body=json.loads(raw) if raw else None
            except Exception: body={"raw":raw.decode("utf8","replace")[:500]}
            return r.status,body
    except urllib.error.HTTPError as e:
        raw=e.read(1024*1024)
        try: body=json.loads(raw)
        except Exception: body={"message":raw.decode("utf8","replace")[:300]}
        return e.code,body

def wait(url, token="", ctx=None, timeout=60, code=200):
    end=time.monotonic()+timeout; last="no response"
    while time.monotonic()<end:
        try:
            status,_=req(url,token,ctx=ctx,timeout=2);last=f"HTTP {status}"
            if status==code:return
        except Exception as e: last=f"{type(e).__name__}: {e}"
        time.sleep(.2)
    raise RuntimeError("readiness timeout ("+last+")")

def wait_tcp(port, timeout=20):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        try:
            with socket.create_connection(("127.0.0.1",port),timeout=1):return
        except OSError:time.sleep(.1)
    raise RuntimeError("TCP listener did not start")

class Connect:
    def __init__(self, port, appport, certfile, keyfile, cafile):
        self.port,self.appport=port,appport; self.stop=threading.Event(); self.reject=[]; self.accept=0; self.lock=threading.Lock(); self.clients=set(); self.upstreams=set(); self.handlers=[]
        self.tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);self.tls.load_cert_chain(str(certfile),str(keyfile));self.tls.load_verify_locations(cafile=str(cafile));self.tls.verify_mode=ssl.CERT_REQUIRED
        self.sock=socket.socket(); self.sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); self.sock.bind(("127.0.0.1",port)); self.sock.listen(); self.sock.settimeout(.2)
        self.thread=threading.Thread(target=self.serve,daemon=True); self.thread.start()
    def serve(self):
        while not self.stop.is_set():
            try: c,_=self.sock.accept()
            except socket.timeout:continue
            except OSError:return
            c.settimeout(3)
            try:c=self.tls.wrap_socket(c,server_side=True)
            except OSError:
                c.close();continue
            t=threading.Thread(target=self.one,args=(c,),daemon=True)
            with self.lock:self.clients.add(c);self.handlers.append(t)
            t.start()
    def one(self,c):
        u=None
        try:
          with c:
            c.settimeout(3); data=b""
            try:
                while b"\r\n\r\n" not in data and len(data)<4096:
                    chunk=c.recv(1024)
                    if not chunk:return
                    data+=chunk
                if b"\r\n\r\n" not in data:
                    c.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n"); return
                first=data.split(b"\r\n",1)[0].decode("ascii","replace").split()
                target=first[1] if len(first)>1 else ""
                if len(first)!=3 or first[0]!="CONNECT" or first[2]!="HTTP/1.1" or target!=SERVICE:
                    self.reject.append(target); c.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n"); return
                u=socket.create_connection(("127.0.0.1",self.appport),timeout=3); self.accept+=1
                with self.lock:self.upstreams.add(u)
                c.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n"); c.settimeout(None); u.settimeout(None)
                def pump(a,b):
                    try:
                        while True:
                            x=a.recv(65536)
                            if not x:break
                            b.sendall(x)
                    except OSError:pass
                ts=[threading.Thread(target=pump,args=(c,u),daemon=True),threading.Thread(target=pump,args=(u,c),daemon=True)]
                [t.start() for t in ts]; [t.join(10) for t in ts]; u.close()
            except OSError:pass
        finally:
            with self.lock:self.clients.discard(c)
            if u is not None:
                with self.lock:self.upstreams.discard(u)
    def close(self):
        self.stop.set(); self.sock.close(); self.thread.join(2)
        if self.thread.is_alive(): raise RuntimeError("CONNECT accept thread did not stop")
        with self.lock:clients=list(self.clients);upstreams=list(self.upstreams);handlers=list(self.handlers)
        for c in clients:
            try:c.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            c.close()
        for u in upstreams:
            try:u.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            u.close()
        for t in handlers:t.join(2)
        if any(t.is_alive() for t in handlers):raise RuntimeError("CONNECT handler threads did not stop")

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--milo-binary",required=True,type=pathlib.Path); p.add_argument("--apps-binary",required=True,type=pathlib.Path); p.add_argument("--datumctl-binary",required=True,type=pathlib.Path); p.add_argument("--etcd-binary",required=True,type=pathlib.Path)
    p.add_argument("--milo-source-sha",required=True); p.add_argument("--apps-source-sha",required=True); p.add_argument("--datumctl-source-sha",required=True)
    a=p.parse_args()
    if a.milo_source_sha!=MILO_PIN or a.apps_source_sha!=APPS_PIN or a.datumctl_source_sha!=DATUMCTL_PIN:raise SystemExit("pinned source mismatch")
    # Source cleanliness is established by the build helper; Go build metadata
    # is an additional consistency check, not source provenance by itself.
    verify_binary(a.milo_binary,MILO_PIN,True)
    verify_binary(a.apps_binary,APPS_PIN,False)
    verify_binary(a.datumctl_binary,DATUMCTL_PIN,True)
    with tempfile.TemporaryDirectory(prefix="apps-aggregation-") as td:
        d=pathlib.Path(td); os.chmod(d,0o700)
        mp,ap,ep,cp,hp=[freeport() for _ in range(5)]
        tokens={x:uuid.uuid4().hex+uuid.uuid4().hex for x in ("admin","appsroot","author","denied")}
        mca_key,mca=ca(d,"milo-ca"); mkey,mcert=cert(d,mca_key,mca,"milo-server","serverAuth","DNS:localhost,IP:127.0.0.1")
        pca_key,pca=ca(d,"proxy-ca"); pkey,pcert=cert(d,pca_key,pca,"milo-aggregation-probe","clientAuth")
        eclientkey,eclientcert=cert(d,pca_key,pca,"apps-egress-client","clientAuth")
        eserverkey,eservercert=cert(d,pca_key,pca,"apps-egress-proxy","serverAuth","IP:127.0.0.1")
        aca_key,aca=ca(d,"apps-ca"); akey,acert=cert(d,aca_key,aca,"apps-serving","serverAuth","DNS:apps-authoring.apps-system.svc")
        tokenfile=d/"tokens.csv"
        tokenfile.write_text("\n".join([
          f"{tokens['admin']},root,root-uid,system:masters",
          f"{tokens['appsroot']},apps-aggregation-service,apps-root-uid,apps-root",
          f"{tokens['author']},author,author-uid,template-authors",
          f"{tokens['denied']},denied,denied-uid,no-template-permissions"])+"\n"); os.chmod(tokenfile,0o600)
        audit=d/"audit.jsonl"; policy=d/"audit-policy.yaml"
        policy.write_text("""apiVersion: audit.k8s.io/v1
kind: Policy
rules:
- level: Request
  users: [apps-aggregation-service]
  verbs: [create]
  resources:
  - group: authorization.k8s.io
    resources: [subjectaccessreviews]
- level: Metadata
""")
        egress=d/"egress.yaml"
        egress.write_text(f"""apiVersion: apiserver.k8s.io/v1beta1
kind: EgressSelectorConfiguration
egressSelections:
- name: cluster
  connection:
    proxyProtocol: HTTPConnect
    transport:
      tcp:
        url: https://127.0.0.1:{cp}
        tlsConfig:
          caBundle: {pca}
          clientKey: {eclientkey}
          clientCert: {eclientcert}
""")
        data=d/"etcd"; data.mkdir(mode=0o700)
        logs=[]; procs=[]; proxy=None
        def start(args,env=None,name="proc"):
            log=(d/(name+".log")).open("wb"); logs.append(log)
            pr=subprocess.Popen(args,env=env,stdout=log,stderr=subprocess.STDOUT);procs.append(pr);return pr
        try:
            etcd=start([str(a.etcd_binary),"--name=apps-proof",f"--data-dir={data}","--listen-client-urls",f"http://127.0.0.1:{ep}","--advertise-client-urls",f"http://127.0.0.1:{ep}","--listen-peer-urls","http://127.0.0.1:0","--initial-advertise-peer-urls","http://127.0.0.1:0","--initial-cluster","apps-proof=http://127.0.0.1:0","--initial-cluster-state=new"],name="etcd")
            wait(f"http://127.0.0.1:{ep}/health",timeout=20)
            proxy=Connect(cp,ap,eservercert,eserverkey,pca)
            kube=d/"apps-root.kubeconfig"
            kube.write_text(f"""apiVersion: v1
kind: Config
clusters:
- name: root
  cluster:
    server: https://127.0.0.1:{mp}
    certificate-authority: {mca}
users:
- name: root
  user:
    token: {tokens['appsroot']}
contexts:
- name: root
  context:
    cluster: root
    user: root
current-context: root
"""); os.chmod(kube,0o600)
            cli_kube=d/"datumctl-author.kubeconfig"
            cli_kube.write_text(f"""apiVersion: v1
kind: Config
clusters:
- name: root
  cluster:
    server: https://127.0.0.1:{mp}
    certificate-authority: {mca}
users:
- name: author
  user:
    token: {tokens['author']}
contexts:
- name: author
  context:
    cluster: root
    user: author
current-context: author
"""); os.chmod(cli_kube,0o600)
            appenv=os.environ.copy()
            app=start([str(a.apps_binary),f"--listen=127.0.0.1:{ap}",f"--health-listen=127.0.0.1:{hp}",f"--tls-cert-file={acert}",f"--tls-private-key-file={akey}",f"--requestheader-client-ca-file={pca}","--requestheader-allowed-names=milo-aggregation-probe",f"--milo-kubeconfig={kube}",f"--etcd-endpoints=http://127.0.0.1:{ep}","--etcd-prefix=/apps-authoring-proof","--allow-insecure-loopback-etcd","--authoring-max-project-templates=100","--authoring-max-project-revisions=1000","--authoring-max-project-bytes=4194304"],name="apps")
            wait(f"http://127.0.0.1:{hp}/readyz",timeout=30)
            env=os.environ.copy()
            cmd=[str(a.milo_binary),f"--secure-port={mp}","--bind-address=127.0.0.1",f"--tls-cert-file={mcert}",f"--tls-private-key-file={mkey}",f"--etcd-servers=http://127.0.0.1:{ep}","--etcd-prefix=/registry","--authorization-mode=RBAC",f"--token-auth-file={tokenfile}",f"--requestheader-client-ca-file={pca}","--requestheader-allowed-names=milo-aggregation-probe","--requestheader-username-headers=X-Remote-User","--requestheader-uid-headers=X-Remote-Uid","--requestheader-group-headers=X-Remote-Group","--requestheader-extra-headers-prefix=X-Remote-Extra-",f"--proxy-client-cert-file={pcert}",f"--proxy-client-key-file={pkey}",f"--audit-policy-file={policy}",f"--audit-log-path={audit}",f"--egress-selector-config-file={egress}","--service-account-issuer=https://milo.local",f"--service-account-key-file={mcert}",f"--service-account-signing-key-file={mkey}"]
            milo_proc=start(cmd,env,name="milo")
            time.sleep(1)
            if milo_proc.poll() is not None:
                text=(d/"milo.log").read_text(errors="replace")
                for secret in tokens.values(): text=text.replace(secret,"<redacted>")
                lines=text.splitlines()
                raise RuntimeError("Milo exited during startup: "+" | ".join(lines[:12]+lines[-8:]))
            tls=ssl.create_default_context(cafile=str(mca));base=f"https://127.0.0.1:{mp}"
            try: wait(base+"/readyz",tokens["admin"],tls,timeout=75)
            except Exception:
                text=(d/"milo.log").read_text(errors="replace") if (d/"milo.log").exists() else ""
                for secret in tokens.values(): text=text.replace(secret,"<redacted>")
                print("Milo startup diagnostic: "+" | ".join(text.splitlines()[-12:]),file=sys.stderr)
                raise
            def root(method,path,obj=None,expected=None):
                code,value=req(base+path,tokens["admin"],method,obj,ctx=tls)
                if expected is not None and code!=expected:raise RuntimeError(f"Milo {method} {path}: {code}, expected {expected}: {value.get('message','') if isinstance(value,dict) else ''}")
                return value
            def datumctl(*args, expected=0, error_contains=""):
                cli_env=os.environ.copy();cli_env["KUBECONFIG"]=str(cli_kube)
                result=subprocess.run([str(a.datumctl_binary),"apps",*args,"--project","apps-proof-a"],env=cli_env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=15)
                if (result.returncode==0)!=(expected==0):
                    message=result.stderr.strip().replace(tokens["author"],"<redacted>")
                    raise RuntimeError(f"datumctl {' '.join(args[:4])} exited {result.returncode}, expected {expected}: {message[:400]}")
                if error_contains and error_contains not in result.stderr:
                    message=result.stderr.strip().replace(tokens["author"],"<redacted>")
                    raise RuntimeError(f"datumctl {' '.join(args[:4])} did not report expected denial ({error_contains!r}): {message[:400]}")
                return result.stdout
            # API fixtures are actual resources stored by Milo; no Project/SAR handler is substituted.
            root("POST","/api/v1/namespaces",{"apiVersion":"v1","kind":"Namespace","metadata":{"name":"apps-system"}},201)
            root("POST",f"/apis/{PROJECT}/v1alpha1/organizations",{"apiVersion":f"{PROJECT}/v1alpha1","kind":"Organization","metadata":{"name":"apps-proof-org"},"spec":{"type":"Standard","contactInfo":{"email":"owner@example.test","name":"Owner"}}},201)
            puids={}
            for name in ("apps-proof-a","apps-proof-b"):
                obj=root("POST",f"/apis/{PROJECT}/v1alpha1/projects",{"apiVersion":f"{PROJECT}/v1alpha1","kind":"Project","metadata":{"name":name},"spec":{"ownerRef":{"kind":"Organization","name":"apps-proof-org"}}},201)
                puids[name]=obj["metadata"]["uid"]
            root("POST","/apis/rbac.authorization.k8s.io/v1/clusterroles",{"apiVersion":"rbac.authorization.k8s.io/v1","kind":"ClusterRole","metadata":{"name":"apps-proof-root"},"rules":[{"apiGroups":[PROJECT],"resources":["projects"],"verbs":["get"]},{"apiGroups":["authorization.k8s.io"],"resources":["subjectaccessreviews"],"verbs":["create"]}]},201)
            root("POST","/apis/rbac.authorization.k8s.io/v1/clusterrolebindings",{"apiVersion":"rbac.authorization.k8s.io/v1","kind":"ClusterRoleBinding","metadata":{"name":"apps-proof-root"},"roleRef":{"apiGroup":"rbac.authorization.k8s.io","kind":"ClusterRole","name":"apps-proof-root"},"subjects":[{"kind":"User","apiGroup":"rbac.authorization.k8s.io","name":"apps-aggregation-service"}]},201)
            root("POST","/apis/rbac.authorization.k8s.io/v1/clusterroles",{"apiVersion":"rbac.authorization.k8s.io/v1","kind":"ClusterRole","metadata":{"name":"apps-proof-author"},"rules":[{"apiGroups":[GROUP],"resources":["apptemplates","apptemplaterevisions"],"verbs":["get","list","create","update","patch","delete"]}]},201)
            root("POST","/apis/rbac.authorization.k8s.io/v1/clusterrolebindings",{"apiVersion":"rbac.authorization.k8s.io/v1","kind":"ClusterRoleBinding","metadata":{"name":"apps-proof-author"},"roleRef":{"apiGroup":"rbac.authorization.k8s.io","kind":"ClusterRole","name":"apps-proof-author"},"subjects":[{"kind":"Group","apiGroup":"rbac.authorization.k8s.io","name":"template-authors"}]},201)
            # Milo's pinned default ServiceResolver constructs the standard
            # <service>.<namespace>.svc DNS URL without querying Service CRUD.
            # Its remote APIService availability controller is disabled; the
            # strict CONNECT test transport maps only this exact host to Apps.
            asvc={"apiVersion":"apiregistration.k8s.io/v1","kind":"APIService","metadata":{"name":"v1alpha1.apps.miloapis.com"},"spec":{"group":GROUP,"version":"v1alpha1","groupPriorityMinimum":1000,"versionPriority":15,"caBundle":base64.b64encode(aca.read_bytes()).decode(),"service":{"name":"apps-authoring","namespace":"apps-system","port":8443}}}
            root("POST","/apis/apiregistration.k8s.io/v1/apiservices",asvc,201)
            # This Milo build deliberately disables the remote Available-condition
            # controller. Marking the disposable APIService available lets us
            # exercise its actual proxy handler; it is not a readiness assertion.
            asp="/apis/apiregistration.k8s.io/v1/apiservices/v1alpha1.apps.miloapis.com"
            current=root("GET",asp)
            current["status"]={"conditions":[{"type":"Available","status":"True","reason":"LocalTransportFixture","message":"test-only CONNECT transport; not a production availability check","lastTransitionTime":datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00","Z")} ]}
            root("PUT",asp+"/status",current,200)
            prox=base+f"/apis/{PROJECT}/v1alpha1/projects/apps-proof-a/control-plane/apis/{GROUP}/v1alpha1"
            end=time.monotonic()+60
            while time.monotonic()<end:
                code,disc=req(prox,tokens["author"],ctx=tls)
                if code==200 and disc and disc.get("kind")=="APIResourceList":break
                time.sleep(.25)
            else:
                status_obj=root("GET",asp)
                conditions=status_obj.get("status",{}).get("conditions",[])
                message=disc.get("message","") if isinstance(disc,dict) else ""
                raise RuntimeError(f"APIService discovery failed (HTTP {code}, message={message!r}, Available={conditions!r}, CONNECT accepted={proxy.accept}, rejected={proxy.reject!r})")
            if not any(x.get("name")=="apptemplates" for x in disc.get("resources",[])):raise RuntimeError("aggregate discovery lacks apptemplates")
            tpl=prox+"/apptemplates"
            body={"apiVersion":f"{GROUP}/v1alpha1","kind":"AppTemplate","metadata":{"name":"probe"},"spec":{"description":"initial","inputSchema":{"type":"object","additionalProperties":False,"properties":{}},"components":[],"outputs":[]}}
            code,first=req(tpl,tokens["author"],"POST",body,ctx=tls)
            if code!=201:raise RuntimeError(f"aggregated create failed ({code})")
            upd=dict(first);upd["spec"]=dict(first["spec"],description="updated")
            code,_=req(tpl+"/probe",tokens["author"],"PUT",upd,ctx=tls)
            if code!=200:raise RuntimeError(f"aggregated update failed ({code})")
            code,hist=req(prox+"/apptemplaterevisions",tokens["author"],ctx=tls)
            if code!=200 or len(hist.get("items",[]))!=2:raise RuntimeError("revision history count mismatch")
            first_revision=min(hist["items"],key=lambda r:r["spec"]["sequence"])
            cli_history=json.loads(datumctl("templates","history","probe"))
            if cli_history.get("templateUID")!=first["metadata"]["uid"] or len(cli_history.get("revisions",[]))!=2:
                raise RuntimeError("datumctl history did not select the current template UID")
            diff=datumctl("templates","diff","probe","--revision",first_revision["metadata"]["name"])
            if "initial" not in diff or "updated" not in diff:
                raise RuntimeError("datumctl diff did not show the expected current/revision definitions")
            before_restore_count=len(hist["items"])
            datumctl("templates","restore","probe","--revision",first_revision["metadata"]["name"],"--dry-run=server")
            code,after_dry=req(prox+"/apptemplaterevisions",tokens["author"],ctx=tls)
            code,current=req(tpl+"/probe",tokens["author"],ctx=tls)
            if code!=200 or len(after_dry.get("items",[]))!=before_restore_count or current["spec"].get("description")!="updated":
                raise RuntimeError("datumctl restore dry-run changed persisted authoring or history")
            bprox=base+f"/apis/{PROJECT}/v1alpha1/projects/apps-proof-b/control-plane/apis/{GROUP}/v1alpha1"
            foreign_body={"apiVersion":f"{GROUP}/v1alpha1","kind":"AppTemplate","metadata":{"name":"foreign"},"spec":{"description":"foreign","inputSchema":{"type":"object","additionalProperties":False,"properties":{}},"components":[],"outputs":[]}}
            code,foreign=req(bprox+"/apptemplates",tokens["author"],"POST",foreign_body,ctx=tls)
            if code!=201:raise RuntimeError(f"foreign project template setup failed ({code})")
            code,foreign_history=req(bprox+"/apptemplaterevisions",tokens["author"],ctx=tls)
            foreign_revision=next((r for r in foreign_history.get("items",[]) if r["spec"]["templateUID"]==foreign["metadata"]["uid"]),None)
            if foreign_revision is None:raise RuntimeError("foreign Project revision fixture missing")
            datumctl("templates","restore","probe","--revision",foreign_revision["metadata"]["name"],expected=1,error_contains="unavailable in this project")
            code,after_foreign=req(prox+"/apptemplaterevisions",tokens["author"],ctx=tls)
            if code!=200 or len(after_foreign.get("items",[]))!=before_restore_count:
                raise RuntimeError("foreign Project revision changed current Project history")
            restored=datumctl("templates","restore","probe","--revision",first_revision["metadata"]["name"])
            restore_summary=json.loads(restored)
            code,current=req(tpl+"/probe",tokens["author"],ctx=tls)
            code,after_restore=req(prox+"/apptemplaterevisions",tokens["author"],ctx=tls)
            latest=max(after_restore.get("items",[]),key=lambda r:r["spec"]["sequence"])
            if code!=200 or len(after_restore.get("items",[]))!=before_restore_count+1 or restore_summary.get("action")!="restored" or current["spec"]!=first["spec"] or latest["spec"]["definition"]!=first["spec"]:
                raise RuntimeError("datumctl restore did not accept the exact immutable definition")
            old_uid=current["metadata"]["uid"]; old_rv=current["metadata"]["resourceVersion"]
            history_before_delete=len(after_restore.get("items",[]))
            datumctl("templates","delete","probe","--uid",old_uid,"--resource-version",old_rv,"--dry-run=server")
            code,after_delete_dry=req(prox+"/apptemplaterevisions",tokens["author"],ctx=tls)
            code,still_current=req(tpl+"/probe",tokens["author"],ctx=tls)
            if code!=200 or still_current["metadata"]["uid"]!=old_uid or len(after_delete_dry.get("items",[]))!=history_before_delete:
                raise RuntimeError("datumctl delete dry-run changed the current object or history")
            deleted=json.loads(datumctl("templates","delete","probe","--uid",old_uid,"--resource-version",old_rv))
            if deleted.get("uid")!=old_uid or deleted.get("action")!="deleted":raise RuntimeError("datumctl delete summary lost the deleted UID")
            gone_code,_=req(tpl+"/probe",tokens["author"],ctx=tls)
            history_code,retained=req(prox+"/apptemplaterevisions",tokens["author"],ctx=tls)
            old_history=json.loads(datumctl("templates","history","--uid",old_uid))
            if gone_code!=404 or history_code!=200 or len(retained.get("items",[]))!=history_before_delete or old_history.get("templateUID")!=old_uid or len(old_history.get("revisions",[]))!=history_before_delete:
                raise RuntimeError(f"datumctl delete/history mismatch (GET={gone_code}, list={history_code}, list items={len(retained.get('items',[]))}, CLI history items={len(old_history.get('revisions',[]))})")
            code,replacement=req(tpl,tokens["author"],"POST",body,ctx=tls)
            if code!=201 or replacement["metadata"]["uid"]==old_uid:raise RuntimeError("same-name template recreation did not get a fresh UID")
            datumctl("templates","delete","probe","--uid",old_uid,"--resource-version",old_rv,expected=1,error_contains="changed or its name was reused")
            code,recreated=req(tpl+"/probe",tokens["author"],ctx=tls)
            if code!=200 or recreated["metadata"]["uid"]!=replacement["metadata"]["uid"]:
                raise RuntimeError("stale delete after name reuse removed or changed the replacement")
            old_history_again=json.loads(datumctl("templates","history","--uid",old_uid))
            if len(old_history_again.get("revisions",[]))!=history_before_delete:
                raise RuntimeError("same-name recreation obscured old UID history")
            for rev in hist["items"]:
                if rev["spec"]["projectUID"]!=puids["apps-proof-a"] or rev["spec"]["effectiveActorUID"]!="author-uid":raise RuntimeError("actor/project attribution mismatch")
            code,_=req(prox.replace("apps-proof-a","apps-proof-b")+"/apptemplates/probe",tokens["author"],ctx=tls)
            if code!=404:raise RuntimeError("Project B read crossed Project A namespace")
            spoof={"X-Remote-User":"root","X-Remote-Uid":"forged","X-Remote-Group":"system:masters","X-Remote-Extra-iam.miloapis.com%2fparent-name":"apps-proof-b"}
            code,_=req(tpl+"/probe",tokens["author"],headers=spoof,ctx=tls)
            if code!=200:raise RuntimeError("inbound X-Remote spoof changed caller context")
            code,rbac_denial=req(tpl,tokens["denied"],"POST",dict(body,metadata={"name":"denied"}),ctx=tls)
            denial_message=rbac_denial.get("message","") if isinstance(rbac_denial,dict) else ""
            if code!=403 or "cannot create resource" not in denial_message:raise RuntimeError("Milo's root authorization did not produce the expected denied aggregated create")
            # Valid front-proxy mTLS, but a caller denied by Milo's real SAR.
            deniedctx=ssl.create_default_context(cafile=str(aca));deniedctx.check_hostname=False;deniedctx.load_cert_chain(str(pcert),str(pkey))
            hdr={"X-Remote-User":"denied","X-Remote-Uid":"denied-uid","X-Remote-Group":"no-template-permissions","X-Remote-Extra-iam.miloapis.com%2fparent-api-group":PROJECT,"X-Remote-Extra-iam.miloapis.com%2fparent-type":"Project","X-Remote-Extra-iam.miloapis.com%2fparent-name":"apps-proof-a"}
            code,reason=req(f"https://127.0.0.1:{ap}{API_PATH}/apptemplates","", "POST",dict(body,metadata={"name":"sar-denied"}),hdr,deniedctx)
            if code!=403 or "delegated authorization denied" not in json.dumps(reason):raise RuntimeError("actual Milo SAR did not deny separate subject")
            # Read only audit summaries; never print raw events or tokens.
            events=set()
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if audit.exists():
                    for line in audit.read_text(errors="replace").splitlines():
                        try:
                            e=json.loads(line);s=e.get("requestObject",{}).get("spec",{})
                            if e.get("user",{}).get("username")=="apps-aggregation-service" and e.get("objectRef",{}).get("resource")=="subjectaccessreviews":
                                ra=s.get("resourceAttributes",{});events.add((s.get("user"),s.get("uid"),ra.get("resource"),ra.get("verb")))
                        except Exception:pass
                if ("author","author-uid","apptemplates","create") in events and ("denied","denied-uid","apptemplates","create") in events:break
                time.sleep(.1)
            if ("author","author-uid","apptemplates","create") not in events or ("denied","denied-uid","apptemplates","create") not in events:
                raise RuntimeError(f"Milo audit SAR summary incomplete (subjects={sorted({(e[0],e[1]) for e in events if e[0] is not None})!r}, resource-actions={sorted({(e[2],e[3]) for e in events if e[2] is not None})!r})")
            if not proxy.accept or proxy.reject:raise RuntimeError("unexpected CONNECT routing result")
            badctx=ssl.create_default_context(cafile=str(pca));badctx.load_cert_chain(str(eclientcert),str(eclientkey))
            s=badctx.wrap_socket(socket.create_connection(("127.0.0.1",cp),timeout=2),server_hostname="127.0.0.1")
            s.sendall(b"CONNECT evil.invalid:443 HTTP/1.1\r\nHost: evil.invalid:443\r\n\r\n")
            if b"403" not in s.recv(512):raise RuntimeError("CONNECT proxy accepted unapproved host")
            s.close()
            if "evil.invalid:443" not in proxy.reject:raise RuntimeError("CONNECT negative control missing")
            print("PASS: real Milo APIService aggregation and exported datumctl Apps commands; history, diff, restore dry-run/commit, foreign-revision denial, UID/RV delete dry-run/commit, retained history by deleted UID, same-name recreation and stale-delete rejection; project isolation, Milo SAR, proxy TLS and spoofed-header controls.")
            print(f"Pins: Milo {MILO_PIN}; Apps {a.apps_source_sha}; datumctl {a.datumctl_source_sha}; CONNECT accepted {proxy.accept}, rejected {len(proxy.reject)}.")
            print("Limit: datumctl launcher invokes the exact exported command implementation but supplies test kubeconfig/project routing instead of OAuth login and the production root factory; local Milo RBAC only, no IAM/FGA or live deployment DNS proof.")
        finally:
            try:
                if proxy:proxy.close()
            finally:
                try:
                    for pr in reversed(procs):
                        if pr.poll() is None:
                            pr.send_signal(signal.SIGTERM)
                            try:pr.wait(timeout=5)
                            except subprocess.TimeoutExpired:pr.kill();pr.wait(timeout=2)
                finally:
                    for f in logs:
                        try:f.close()
                        except Exception:pass

def _overall_timeout(signum, frame):
    raise TimeoutError("aggregation proof exceeded 120 second overall limit")

if __name__=="__main__":
    signal.signal(signal.SIGALRM,_overall_timeout)
    signal.setitimer(signal.ITIMER_REAL,120)
    try:main()
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}",file=sys.stderr)
        raise
    finally:
        signal.setitimer(signal.ITIMER_REAL,0)
