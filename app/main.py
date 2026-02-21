"""
FastAPI EKS Probe Demo Application
===================================
A lightweight, generic FastAPI app designed for deploying to EKS
and teaching Kubernetes probes, pod health, and cluster operations.

SWAP GUIDE: To replace this with your own app, keep the /healthz and /ready
endpoints intact and add your own routes in a separate router file.
"""

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import os
import socket
import time
import datetime
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("uvicorn.error")

# Thread pool for K8s API calls — prevents blocking the uvicorn event loop
_k8s_executor = ThreadPoolExecutor(max_workers=1)

# ---------------------------------------------------------------------------
# Kubernetes API client — for peer pod discovery
# ---------------------------------------------------------------------------
try:
    from kubernetes import client, config
    config.load_incluster_config()
    k8s_v1 = client.CoreV1Api()
    K8S_AVAILABLE = True
    logger.info("Kubernetes in-cluster config loaded — peer discovery enabled")
except Exception:
    k8s_v1 = None
    K8S_AVAILABLE = False
    logger.warning("Kubernetes client not available — peer discovery disabled")

# ---------------------------------------------------------------------------
# App state — simulates real-world readiness conditions
# ---------------------------------------------------------------------------
APP_START_TIME = time.time()
READY = False
HEALTHY = True
STARTUP_DELAY = int(os.getenv("STARTUP_DELAY", "5"))  # seconds before "ready"
POD_NAME = os.getenv("HOSTNAME", "unknown-pod")
NAMESPACE = os.getenv("POD_NAMESPACE", "default")
NODE_NAME = os.getenv("NODE_NAME", "unknown-node")
APP_VERSION = os.getenv("APP_VERSION", "1.0.0")

app = FastAPI(
    title="EKS Probe Demo",
    description="Generic FastAPI image for learning Kubernetes probes & operations",
    version=APP_VERSION,
)


# ---------------------------------------------------------------------------
# Peer discovery — DNS-based (headless Service) + K8s API fallback
# ---------------------------------------------------------------------------
HEADLESS_SVC = os.getenv("HEADLESS_SVC", "demo-headless")
SERVICE_NAME = os.getenv("SERVICE_NAME", "demo-service")


def _discover_via_dns():
    """
    Resolve the headless Service DNS name to get all pod IPs.
    No RBAC or API server access needed — just standard cluster DNS.
    """
    dns_name = f"{HEADLESS_SVC}.{NAMESPACE}.svc.cluster.local"
    my_ip = socket.gethostbyname(socket.gethostname())
    try:
        results = socket.getaddrinfo(dns_name, 8000, socket.AF_INET, socket.SOCK_STREAM)
        seen = set()
        peers = []
        for _, _, _, _, (ip, _) in results:
            if ip in seen:
                continue
            seen.add(ip)
            peers.append({
                "name": ip,  # DNS only gives IPs, not pod names
                "ip": ip,
                "node": "—",
                "phase": "Running",
                "ready": True,  # only ready pods appear in DNS
                "restarts": 0,
                "is_self": ip == my_ip,
            })
        return sorted(peers, key=lambda p: p["ip"])
    except socket.gaierror:
        return []


def _discover_via_k8s_api():
    """
    Query the Kubernetes API for all pods in this Deployment (blocking).
    Returns richer data (pod names, node, restarts) but requires API server access.
    """
    if not K8S_AVAILABLE:
        return []
    try:
        pods = k8s_v1.list_namespaced_pod(
            namespace=NAMESPACE,
            label_selector="app=demo",
            _request_timeout=2,
        )
        my_ip = socket.gethostbyname(socket.gethostname())
        peers = []
        for pod in pods.items:
            pod_ip = pod.status.pod_ip or "pending"
            ready = False
            if pod.status.conditions:
                for cond in pod.status.conditions:
                    if cond.type == "Ready" and cond.status == "True":
                        ready = True
            peers.append({
                "name": pod.metadata.name,
                "ip": pod_ip,
                "node": pod.spec.node_name or "unknown",
                "phase": pod.status.phase,
                "ready": ready,
                "restarts": sum(
                    cs.restart_count for cs in (pod.status.container_statuses or [])
                ),
                "is_self": pod_ip == my_ip,
            })
        return sorted(peers, key=lambda p: p["name"])
    except Exception as e:
        logger.warning(f"K8s API peer discovery failed: {e}")
        return []


def _fetch_peer_pods():
    """Try K8s API first (richer data), fall back to DNS discovery."""
    peers = _discover_via_k8s_api()
    if peers:
        return peers
    return _discover_via_dns()


async def get_peer_pods():
    """Async wrapper — runs discovery in a thread with a timeout."""
    try:
        loop = asyncio.get_event_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(_k8s_executor, _fetch_peer_pods),
            timeout=3.0,
        )
    except (asyncio.TimeoutError, Exception) as e:
        logger.warning(f"Peer discovery timed out or failed: {e}")
        return []


def render_peer_table(peers):
    """Render the peer pods table HTML with links to each pod's endpoints."""
    if not peers:
        return """
<div class="card">
  <h2>🔗 Peer Pods</h2>
  <p><em>Peer discovery unavailable — RBAC or Kubernetes client not configured.</em></p>
  <pre>
# Apply RBAC to enable peer discovery:
kubectl apply -f k8s/rbac.yaml
  </pre>
</div>"""

    rows = ""
    for p in peers:
        badge = " <span class='tag'>← YOU</span>" if p["is_self"] else ""
        ready_icon = "<span class='ok'>✓</span>" if p["ready"] else "<span class='fail'>✗</span>"
        # Links to this pod's endpoints via direct pod IP (cluster-internal)
        links = (
            f'<a href="http://{p["ip"]}:8000/">home</a> · '
            f'<a href="http://{p["ip"]}:8000/info">info</a> · '
            f'<a href="http://{p["ip"]}:8000/healthz">healthz</a> · '
            f'<a href="http://{p["ip"]}:8000/ready">ready</a> · '
            f'<a href="http://{p["ip"]}:8000/toggle-health">toggle-health</a> · '
            f'<a href="http://{p["ip"]}:8000/toggle-ready">toggle-ready</a>'
        )
        rows += f"""<tr>
  <td><code>{p["name"]}</code>{badge}</td>
  <td><code>{p["ip"]}</code></td>
  <td><code>{p["node"]}</code></td>
  <td>{ready_icon} {p["phase"]}</td>
  <td>{p["restarts"]}</td>
  <td style="font-size:0.85em">{links}</td>
</tr>"""

    return f"""
<div class="card">
  <h2>🔗 Peer Pods ({len(peers)} replicas)</h2>
  <p>Live data from the Kubernetes API — each pod discovers its siblings via a ServiceAccount
     with RBAC read access to Endpoints and Pods.</p>
  <table>
    <tr><th>Pod Name</th><th>IP</th><th>Node</th><th>Status</th><th>Restarts</th><th>Endpoints</th></tr>
    {rows}
  </table>
  <p style="font-size:0.85em; color:#8b949e">
    ⚠️ Pod-IP links only work from <strong>inside the cluster</strong> (or via port-forward to
    a specific pod). From your local terminal, use:
    <code>kubectl port-forward pod/&lt;name&gt; -n {NAMESPACE} 8080:8000</code>
  </p>
</div>"""


# ---------------------------------------------------------------------------
# CSS shared across all HTML pages
# ---------------------------------------------------------------------------
STYLE = """
<style>
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0d1117;
         color: #c9d1d9; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }
  h1, h2 { color: #58a6ff; }
  code, pre { background: #161b22; padding: 2px 6px; border-radius: 6px;
              font-size: 0.95em; color: #7ee787; }
  pre { padding: 1rem; overflow-x: auto; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
          padding: 1.25rem; margin: 1rem 0; }
  .ok { color: #3fb950; font-weight: bold; }
  .fail { color: #f85149; font-weight: bold; }
  a { color: #58a6ff; }
  table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
  th, td { border: 1px solid #30363d; padding: 0.5rem 0.75rem; text-align: left; }
  th { background: #161b22; }
  .tag { display: inline-block; background: #1f6feb; color: #fff;
         padding: 2px 8px; border-radius: 12px; font-size: 0.8em; margin: 0 2px; }
</style>
"""


# ---------------------------------------------------------------------------
# PROBE ENDPOINTS — These are what Kubernetes calls
# ---------------------------------------------------------------------------

@app.get("/healthz", response_class=HTMLResponse, tags=["probes"])
async def liveness():
    """
    LIVENESS PROBE — /healthz
    ─────────────────────────
    Kubernetes calls this to ask: "Is the container still alive?"
    If this returns non-200, K8s kills and restarts the container.

    From your local terminal (after port-forwarding):
      kubectl port-forward pod/<pod> -n <namespace> 8080:8000
      curl -s localhost:8080/healthz
    """
    if not HEALTHY:
        return HTMLResponse(
            content=f"{STYLE}<h1 class='fail'>UNHEALTHY</h1>"
                    f"<p>Pod <code>{POD_NAME}</code> is reporting unhealthy. "
                    f"Kubernetes will restart this container.</p>",
            status_code=503,
        )
    uptime = round(time.time() - APP_START_TIME, 1)
    return HTMLResponse(content=f"""
{STYLE}
<h1>🟢 Liveness Probe — <code>/healthz</code></h1>
<div class="card">
  <h2>Status: <span class="ok">HEALTHY</span></h2>
  <p><strong>Pod:</strong> <code>{POD_NAME}</code></p>
  <p><strong>Uptime:</strong> {uptime}s</p>
</div>
<div class="card">
  <h2>📖 How This Works</h2>
  <p>Kubernetes sends a <code>GET /healthz</code> request at a regular interval
     (<code>periodSeconds</code>). If this endpoint returns <strong>HTTP 200-399</strong>,
     the container is considered alive.</p>
  <p>If it fails <code>failureThreshold</code> consecutive times, Kubernetes
     <strong>kills and restarts</strong> the container.</p>
  <h2>🔧 Commands to Try</h2>
  <pre>
# Port-forward this pod, then check liveness with curl
kubectl port-forward pod/{POD_NAME} -n {NAMESPACE} 8080:8000 &
curl -s localhost:8080/healthz

# Watch for liveness failures in events
kubectl describe pod {POD_NAME} -n {NAMESPACE} | grep -A5 Events

# Trigger a failure (hit /toggle-health) then watch restarts
kubectl get pods -n {NAMESPACE} -w
  </pre>
</div>
""")


@app.get("/ready", response_class=HTMLResponse, tags=["probes"])
async def readiness():
    """
    READINESS PROBE — /ready
    ────────────────────────
    Kubernetes calls this to ask: "Should I send traffic to this pod?"
    If this returns non-200, the pod is removed from Service endpoints.
    The pod is NOT restarted.

    From your local terminal (after port-forwarding):
      kubectl port-forward pod/<pod> -n <namespace> 8080:8000
      curl -s localhost:8080/ready
    """
    global READY
    elapsed = time.time() - APP_START_TIME
    if elapsed < STARTUP_DELAY:
        remaining = round(STARTUP_DELAY - elapsed, 1)
        return HTMLResponse(
            content=f"{STYLE}<h1>⏳ Not Ready Yet</h1>"
                    f"<p>Simulating startup delay... {remaining}s remaining</p>"
                    f"<p>Pod <code>{POD_NAME}</code> will NOT receive traffic until ready.</p>",
            status_code=503,
        )
    READY = True
    return HTMLResponse(content=f"""
{STYLE}
<h1>🟢 Readiness Probe — <code>/ready</code></h1>
<div class="card">
  <h2>Status: <span class="ok">READY</span></h2>
  <p><strong>Pod:</strong> <code>{POD_NAME}</code></p>
  <p><strong>Serving traffic:</strong> Yes</p>
</div>
<div class="card">
  <h2>📖 How This Works</h2>
  <p>Kubernetes sends <code>GET /ready</code> periodically. A <strong>200</strong> means
     the pod is added to the Service's <code>Endpoints</code> and receives traffic.</p>
  <p>A non-200 removes the pod from endpoints — <strong>no traffic is routed</strong>,
     but the pod is NOT restarted (unlike liveness).</p>
  <p>This pod simulates a <strong>{STARTUP_DELAY}s startup delay</strong> using the
     <code>STARTUP_DELAY</code> env var.</p>
  <h2>🔧 Commands to Try</h2>
  <pre>
# Watch endpoints appear/disappear
kubectl get endpoints demo-service -n {NAMESPACE} -w

# Port-forward this pod, then check readiness with curl
kubectl port-forward pod/{POD_NAME} -n {NAMESPACE} 8080:8000 &
curl -s localhost:8080/ready

# See which pods are Ready vs Not Ready
kubectl get pods -n {NAMESPACE} -o wide
  </pre>
</div>
""")


@app.get("/startup", response_class=HTMLResponse, tags=["probes"])
async def startup():
    """
    STARTUP PROBE — /startup
    ────────────────────────
    Called only during container startup. While failing, liveness and
    readiness probes are disabled. Once it passes, it never runs again.
    """
    elapsed = time.time() - APP_START_TIME
    if elapsed < 2:  # simulate 2s init
        return HTMLResponse(
            content=f"{STYLE}<h1>⏳ Starting up...</h1><p>{round(2 - elapsed, 1)}s remaining</p>",
            status_code=503,
        )
    return HTMLResponse(content=f"""
{STYLE}
<h1>🟢 Startup Probe — <code>/startup</code></h1>
<div class="card">
  <h2>Status: <span class="ok">STARTED</span></h2>
  <p>The application has finished initializing.</p>
  <p>Liveness and readiness probes are now active.</p>
</div>
<div class="card">
  <h2>📖 How This Works</h2>
  <p>The startup probe runs first and <strong>blocks</strong> liveness/readiness probes
     until it passes. This protects slow-starting apps from being killed prematurely.</p>
  <p><strong>Max startup window</strong> =
     <code>initialDelaySeconds + (failureThreshold × periodSeconds)</code></p>
  <p>Once it passes once, it <strong>never runs again</strong> for the lifetime of the container.</p>
</div>
""")


# ---------------------------------------------------------------------------
# OPERATIONAL / DEMO ENDPOINTS
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, tags=["info"])
async def index():
    """Landing page with navigation and educational overview."""
    uptime = round(time.time() - APP_START_TIME, 1)
    peers = await get_peer_pods()
    peer_html = render_peer_table(peers)
    return HTMLResponse(content=f"""
{STYLE}
<h1>🚀 EKS Probe Demo — FastAPI</h1>
<div class="card">
  <p><strong>Pod:</strong> <code>{POD_NAME}</code></p>
  <p><strong>Node:</strong> <code>{NODE_NAME}</code></p>
  <p><strong>Namespace:</strong> <code>{NAMESPACE}</code></p>
  <p><strong>Version:</strong> <code>{APP_VERSION}</code></p>
  <p><strong>Uptime:</strong> {uptime}s</p>
  <p><strong>Hostname:</strong> <code>{socket.gethostname()}</code></p>
  <p><strong>IP:</strong> <code>{socket.gethostbyname(socket.gethostname())}</code></p>
</div>

{peer_html}

<h2>📡 Probe Endpoints</h2>
<table>
  <tr><th>Endpoint</th><th>Probe Type</th><th>Purpose</th></tr>
  <tr><td><a href="/healthz">/healthz</a></td><td><span class="tag">Liveness</span></td>
      <td>Is the container alive? Failure → restart</td></tr>
  <tr><td><a href="/ready">/ready</a></td><td><span class="tag">Readiness</span></td>
      <td>Can this pod serve traffic? Failure → remove from Service</td></tr>
  <tr><td><a href="/startup">/startup</a></td><td><span class="tag">Startup</span></td>
      <td>Has the app finished initializing?</td></tr>
</table>

<h2>🔧 Operational Endpoints</h2>
<table>
  <tr><th>Endpoint</th><th>Method</th><th>Purpose</th></tr>
  <tr><td><a href="/info">/info</a></td><td>GET</td>
      <td>Pod metadata, env, and cluster context</td></tr>
  <tr><td><a href="/toggle-health">/toggle-health</a></td><td>GET</td>
      <td>Flip liveness on/off — triggers restart</td></tr>
  <tr><td><a href="/toggle-ready">/toggle-ready</a></td><td>GET</td>
      <td>Flip readiness on/off — removes from Service</td></tr>
  <tr><td><a href="/stress">/stress</a></td><td>GET</td>
      <td>Simulate CPU load for resource monitoring</td></tr>
  <tr><td><a href="/docs">/docs</a></td><td>GET</td>
      <td>Swagger UI — interactive API docs</td></tr>
</table>

<h2>📖 Quick kubectl Cheat Sheet</h2>
<pre>
# See all pods and which node they're on
kubectl get pods -n {NAMESPACE} -o wide

# Watch pod status changes in real time
kubectl get pods -n {NAMESPACE} -w

# Describe a pod (events, probe config, restarts)
kubectl describe pod {POD_NAME} -n {NAMESPACE}

# Check Service endpoints
kubectl get endpoints demo-service -n {NAMESPACE}

# View logs
kubectl logs {POD_NAME} -n {NAMESPACE}

# Exec into the pod
kubectl exec -it {POD_NAME} -n {NAMESPACE} -- /bin/sh

# Port-forward to access locally
kubectl port-forward pod/{POD_NAME} -n {NAMESPACE} 8080:8000
</pre>
""")


@app.get("/info", response_class=HTMLResponse, tags=["info"])
async def info():
    """Detailed pod metadata and environment information."""
    uptime = round(time.time() - APP_START_TIME, 1)
    peers = await get_peer_pods()
    peer_html = render_peer_table(peers)
    env_rows = ""
    for key in sorted(os.environ):
        if any(s in key.upper() for s in ["SECRET", "PASSWORD", "TOKEN", "KEY"]):
            env_rows += f"<tr><td><code>{key}</code></td><td>••••••••</td></tr>"
        else:
            env_rows += f"<tr><td><code>{key}</code></td><td><code>{os.environ[key][:100]}</code></td></tr>"

    return HTMLResponse(content=f"""
{STYLE}
<h1>📋 Pod Info — <code>{POD_NAME}</code></h1>
<div class="card">
  <h2>Container Details</h2>
  <table>
    <tr><td><strong>Pod Name</strong></td><td><code>{POD_NAME}</code></td></tr>
    <tr><td><strong>Namespace</strong></td><td><code>{NAMESPACE}</code></td></tr>
    <tr><td><strong>Node</strong></td><td><code>{NODE_NAME}</code></td></tr>
    <tr><td><strong>Hostname</strong></td><td><code>{socket.gethostname()}</code></td></tr>
    <tr><td><strong>IP Address</strong></td><td><code>{socket.gethostbyname(socket.gethostname())}</code></td></tr>
    <tr><td><strong>Uptime</strong></td><td>{uptime}s</td></tr>
    <tr><td><strong>Healthy</strong></td><td>{"<span class='ok'>Yes</span>" if HEALTHY else "<span class='fail'>No</span>"}</td></tr>
    <tr><td><strong>Ready</strong></td><td>{"<span class='ok'>Yes</span>" if READY else "<span class='fail'>No</span>"}</td></tr>
    <tr><td><strong>Version</strong></td><td><code>{APP_VERSION}</code></td></tr>
    <tr><td><strong>Time</strong></td><td>{datetime.datetime.now(datetime.timezone.utc).isoformat()}</td></tr>
  </table>
</div>
<div class="card">
  <h2>📖 What This Shows</h2>
  <p>Each pod gets its own hostname, IP, and environment. When you have multiple replicas,
     hitting <code>/info</code> through the Service will show <strong>different pods responding</strong>
     — this is Kubernetes load balancing in action.</p>
  <pre>
# Port-forward the Service, then hit it multiple times to see different pods
kubectl port-forward svc/demo-service -n {NAMESPACE} 8080:80 &
for i in $(seq 1 5); do
  curl -s localhost:8080/info | grep "Pod Name"
done
  </pre>
</div>
{peer_html}
<div class="card">
  <h2>Environment Variables</h2>
  <table>
    <tr><th>Variable</th><th>Value</th></tr>
    {env_rows}
  </table>
</div>
""")


@app.get("/toggle-health", response_class=HTMLResponse, tags=["chaos"])
async def toggle_health():
    """
    TOGGLE LIVENESS — /toggle-health
    ─────────────────────────────────
    Flips the health status. When unhealthy, /healthz returns 503
    and Kubernetes will restart the container after failureThreshold.
    """
    global HEALTHY
    HEALTHY = not HEALTHY
    status = "HEALTHY" if HEALTHY else "UNHEALTHY"
    css_class = "ok" if HEALTHY else "fail"
    return HTMLResponse(content=f"""
{STYLE}
<h1>⚡ Health Toggled</h1>
<div class="card">
  <h2>Liveness is now: <span class="{css_class}">{status}</span></h2>
  <p><strong>Pod:</strong> <code>{POD_NAME}</code></p>
  {"<p>⚠️ The liveness probe at <code>/healthz</code> will now return <strong>503</strong>. "
   "Kubernetes will restart this container after <code>failureThreshold</code> consecutive failures.</p>"
   if not HEALTHY else
   "<p>✅ The liveness probe is passing again.</p>"}
</div>
<div class="card">
  <h2>� What Just Happened</h2>
  <p>This endpoint flips a Python variable (<code>HEALTHY</code>) <strong>in memory on this one pod</strong>.
     The other replicas are unaffected — they're separate processes with their own state.</p>
  <p><code>/healthz</code> starts returning <strong>503 immediately</strong> — there's no delay.
     But Kubernetes won't act until the probe fails enough times:</p>
  <table>
    <tr><th>Time</th><th>What Happens</th></tr>
    <tr><td>t=0</td><td>You hit <code>/toggle-health</code> → <code>HEALTHY = False</code></td></tr>
    <tr><td>Next probe</td><td>K8s sends <code>GET /healthz</code> → 503 → failure #1</td></tr>
    <tr><td>+10s</td><td>Next probe (<code>periodSeconds: 10</code>) → 503 → failure #2</td></tr>
    <tr><td>+20s</td><td>Next probe → 503 → failure #3 = <code>failureThreshold</code> reached</td></tr>
    <tr><td>+20-30s</td><td>K8s <strong>kills and restarts the container</strong> inside this pod</td></tr>
  </table>
</div>
<div class="card">
  <h2>🔄 Restart, Not Replace</h2>
  <p>Kubernetes <strong>restarts the container inside the existing pod</strong> — it does NOT
     delete the pod and create a new one. The pod name stays the same, but:</p>
  <ul>
    <li>The <code>RESTARTS</code> counter increments</li>
    <li>The container process starts fresh → <code>HEALTHY</code> resets to <code>True</code></li>
    <li>The pod recovers and passes probes again automatically</li>
  </ul>
  <p>New pods (replicas) are only created when the Deployment's <code>replicas</code> count changes,
     or a pod is evicted/deleted entirely (node failure, <code>kubectl delete pod</code>).</p>
</div>
<div class="card">
  <h2>⏱️ Worst-Case Timing</h2>
  <p>With <code>periodSeconds: 10</code> and <code>failureThreshold: 3</code>:</p>
  <ul>
    <li><strong>Best case:</strong> ~20s (toggle right before a probe fires)</li>
    <li><strong>Worst case:</strong> ~40s (toggle right after a probe just passed)</li>
  </ul>
</div>
<div class="card">
  <h2>�🔧 Watch It Happen</h2>
  <pre>
# In another terminal, watch for the restart
kubectl get pods -n {NAMESPACE} -w

# Check events for the liveness failure
kubectl describe pod {POD_NAME} -n {NAMESPACE} | tail -20
  </pre>
</div>
<p><a href="/toggle-health">Toggle again</a> | <a href="/">Home</a></p>
""")


@app.get("/toggle-ready", response_class=HTMLResponse, tags=["chaos"])
async def toggle_ready():
    """
    TOGGLE READINESS — /toggle-ready
    ─────────────────────────────────
    Flips readiness. When not ready, /ready returns 503 and the pod
    is removed from Service endpoints — no traffic is routed to it.
    """
    global READY
    READY = not READY
    status = "READY" if READY else "NOT READY"
    css_class = "ok" if READY else "fail"
    return HTMLResponse(content=f"""
{STYLE}
<h1>⚡ Readiness Toggled</h1>
<div class="card">
  <h2>Readiness is now: <span class="{css_class}">{status}</span></h2>
  <p><strong>Pod:</strong> <code>{POD_NAME}</code></p>
  {"<p>⚠️ The readiness probe at <code>/ready</code> will now return <strong>503</strong>. "
   "This pod will be <strong>removed from the Service endpoints</strong> — no traffic routed here. "
   "The pod is NOT restarted.</p>"
   if not READY else
   "<p>✅ The pod is ready and will receive traffic again.</p>"}
</div>
<div class="card">
  <h2>� What Just Happened</h2>
  <p>This endpoint flips a Python variable (<code>READY</code>) <strong>in memory on this one pod</strong>.
     The other replicas are unaffected — they keep serving traffic normally.</p>
  <p><code>/ready</code> starts returning <strong>503 immediately</strong>, but Kubernetes
     needs to see enough consecutive failures before acting:</p>
  <table>
    <tr><th>Time</th><th>What Happens</th></tr>
    <tr><td>t=0</td><td>You hit <code>/toggle-ready</code> → <code>READY = False</code></td></tr>
    <tr><td>Next probe</td><td>K8s sends <code>GET /ready</code> → 503 → failure #1</td></tr>
    <tr><td>+5s</td><td>Next probe (<code>periodSeconds: 5</code>) → 503 → failure #2 = <code>failureThreshold</code> reached</td></tr>
    <tr><td>+5-10s</td><td>Pod is <strong>removed from Service endpoints</strong> — no traffic routed here</td></tr>
  </table>
</div>
<div class="card">
  <h2>🚫 Removed, Not Restarted</h2>
  <p>Unlike liveness, readiness failure does <strong>NOT restart the container</strong>. The pod
     keeps running — it's just taken out of the load balancer. This is the key difference:</p>
  <table>
    <tr><th>Probe</th><th>Failure Action</th><th>Pod Status</th></tr>
    <tr><td><code>/healthz</code> (liveness)</td><td>Kill &amp; restart container</td><td>RESTARTS increments</td></tr>
    <tr><td><code>/ready</code> (readiness)</td><td>Remove from Service endpoints</td><td>Shows <code>0/1 Ready</code>, keeps running</td></tr>
  </table>
  <p>This is intentional — readiness handles <strong>temporary</strong> issues (DB connection lost,
     cache warming) where restarting wouldn't help. The pod stays alive and can recover on its own.</p>
</div>
<div class="card">
  <h2>🔄 Recovery</h2>
  <p>When you toggle readiness back on, the pod must pass <code>successThreshold: 2</code>
     consecutive checks before Kubernetes adds it back to the Service endpoints.</p>
  <table>
    <tr><th>Time</th><th>What Happens</th></tr>
    <tr><td>t=0</td><td>You hit <code>/toggle-ready</code> again → <code>READY = True</code></td></tr>
    <tr><td>Next probe</td><td><code>GET /ready</code> → 200 → success #1</td></tr>
    <tr><td>+5s</td><td>Next probe → 200 → success #2 = <code>successThreshold</code> reached</td></tr>
    <tr><td>+5-10s</td><td>Pod is <strong>added back</strong> to Service endpoints — traffic resumes</td></tr>
  </table>
</div>
<div class="card">
  <h2>⏱️ Timing</h2>
  <p>With <code>periodSeconds: 5</code> and <code>failureThreshold: 2</code>:</p>
  <ul>
    <li><strong>Removal:</strong> ~5-15s after toggling off</li>
    <li><strong>Recovery:</strong> ~5-15s after toggling back on (needs 2 successes)</li>
  </ul>
</div>
<div class="card">
  <h2>🔧 Watch It Happen</h2>
  <pre>
# Watch endpoints — the pod IP appears/disappears
kubectl get endpoints demo-service -n {NAMESPACE} -w

# See the pod go to 0/1 Ready (still Running, not restarted)
kubectl get pods -n {NAMESPACE} -w

# Describe the pod to see readiness events
kubectl describe pod {POD_NAME} -n {NAMESPACE} | tail -20
  </pre>
</div>
<p><a href="/toggle-ready">Toggle again</a> | <a href="/">Home</a></p>
""")


@app.get("/stress", response_class=HTMLResponse, tags=["chaos"])
async def stress():
    """
    STRESS ENDPOINT — /stress
    ─────────────────────────
    Burns CPU for ~2 seconds. Use this to demo resource monitoring,
    HPA scaling, and kubectl top.
    """
    start = time.time()
    # Simple CPU burn — ~2 seconds of computation
    x = 0
    while time.time() - start < 2:
        x += sum(i * i for i in range(1000))
    elapsed = round(time.time() - start, 2)

    return HTMLResponse(content=f"""
{STYLE}
<h1>🔥 Stress Test Complete</h1>
<div class="card">
  <p><strong>Pod:</strong> <code>{POD_NAME}</code></p>
  <p><strong>Duration:</strong> {elapsed}s of CPU burn</p>
</div>
<div class="card">
  <h2>📖 Why This Exists</h2>
  <p>Use this to observe CPU usage with <code>kubectl top</code> or trigger
     Horizontal Pod Autoscaler (HPA) scaling if resource requests/limits are set.</p>
  <pre>
# Watch resource usage (requires metrics-server)
kubectl top pods -n {NAMESPACE}

# Describe HPA (if configured)
kubectl get hpa -n {NAMESPACE}
  </pre>
</div>
<p><a href="/stress">Run again</a> | <a href="/">Home</a></p>
""")
