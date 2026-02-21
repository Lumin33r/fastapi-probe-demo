# FastAPI EKS Probe Demo

A lightweight, generic FastAPI application designed for deploying to EKS and teaching Kubernetes probes, pod health monitoring, and cluster operations.

## Project Structure

```
fastapi-demo/
├── Dockerfile
├── .dockerignore
├── app/
│   ├── main.py              # FastAPI application (swap-friendly)
│   └── requirements.txt
└── k8s/
    ├── namespace.yaml        # Isolated namespace
    ├── deployment.yaml       # 3 replicas, all 3 probe types
    ├── service.yaml          # ClusterIP (internal)
    └── service-nodeport.yaml # NodePort (external access)
```

## Task 1: Prepare Your Container Registry (GHCR)

Your container image needs to be in a registry that the EKS cluster can access. We'll use **GitHub Container Registry (GHCR)**.

### Step 1 — Create a GitHub Personal Access Token (PAT)

1. Go to **GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)**
2. Click **Generate new token (classic)**
3. Give it a descriptive name (e.g., `ghcr-push`)
4. Select these scopes:
   - `write:packages` — push images to GHCR
   - `read:packages` — pull images from GHCR
5. Click **Generate token** and **copy it immediately** (you won't see it again)
6. Save it as an environment variable:

```bash
export GHCR_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
export GITHUB_USER=<your-github-username>
```

### Step 2 — Authenticate Docker with GHCR

```bash
echo $GHCR_TOKEN | docker login ghcr.io -u $GITHUB_USER --password-stdin
```

You should see `Login Succeeded`.

### Step 3 — Build and Tag the Image

```bash
# Build the image
docker build -t fastapi-probe-demo:latest .

# Tag for GHCR
docker tag fastapi-probe-demo:latest ghcr.io/$GITHUB_USER/fastapi-probe-demo:latest
```

### Step 4 — Push to GHCR

```bash
docker push ghcr.io/$GITHUB_USER/fastapi-probe-demo:latest
```

### Step 5 — Verify the Package

1. Go to **https://github.com/<your-username>?tab=packages**
2. You should see `fastapi-probe-demo` listed
3. Click into it to verify the `latest` tag exists

### Step 6 — Make the Package Public (required for EKS to pull without a secret)

By default, GHCR packages are **private**. For EKS to pull without an `imagePullSecret`:

1. Go to the package page on GitHub
2. Click **Package settings** (right sidebar)
3. Under **Danger Zone**, change visibility to **Public**

> **Alternative:** If you want to keep the image private, create a Kubernetes secret with your GHCR credentials and reference it in the deployment. See [Private Image Pull Secret](#private-image-pull-secret-optional) below.

### Step 7 — Update deployment.yaml

Replace the image reference in `k8s/deployment.yaml`:

```yaml
image: ghcr.io/<your-github-username>/fastapi-probe-demo:latest
```

---

### Private Image Pull Secret (Optional)

If you keep your GHCR image private, create a pull secret in your namespace:

```bash
kubectl create secret docker-registry ghcr-secret \
  --namespace fastapi-demo-troy \
  --docker-server=ghcr.io \
  --docker-username=$GITHUB_USER \
  --docker-password=$GHCR_TOKEN
```

Then add `imagePullSecrets` to your deployment's pod spec:

```yaml
spec:
  template:
    spec:
      imagePullSecrets:
        - name: ghcr-secret
      containers:
        - name: fastapi
          image: ghcr.io/<your-github-username>/fastapi-probe-demo:latest
```

---

## Quick Start (ECR Alternative)

<details>
<summary>Click to expand ECR instructions (if not using GHCR)</summary>

```bash
# Build locally
docker build -t fastapi-probe-demo:latest .

# Tag and push to ECR (replace with your registry)
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <account>.dkr.ecr.us-east-1.amazonaws.com
docker tag fastapi-probe-demo:latest <account>.dkr.ecr.us-east-1.amazonaws.com/fastapi-probe-demo:latest
docker push <account>.dkr.ecr.us-east-1.amazonaws.com/fastapi-probe-demo:latest
```

Update deployment.yaml:

```yaml
image: <account>.dkr.ecr.us-east-1.amazonaws.com/fastapi-probe-demo:latest
```

</details>

---

## Deploy to EKS

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml

# Watch pods come up
kubectl get pods -n fastapi-demo-troy -w
```

### Access the App

```bash
# Port-forward to your local machine
kubectl port-forward svc/demo-service -n fastapi-demo-troy 8080:80

# Open http://localhost:8080
```

## Endpoints

| Endpoint         | Probe Type | Purpose                                              |
| ---------------- | ---------- | ---------------------------------------------------- |
| `/`              | —          | Landing page with navigation and kubectl cheat sheet |
| `/healthz`       | Liveness   | Returns 200 when healthy, 503 when toggled off       |
| `/ready`         | Readiness  | Returns 503 during startup delay, then 200           |
| `/startup`       | Startup    | Returns 503 for first 2s, then 200                   |
| `/info`          | —          | Pod metadata, IP, node, environment variables        |
| `/toggle-health` | —          | Flip liveness on/off (triggers restarts)             |
| `/toggle-ready`  | —          | Flip readiness on/off (removes from endpoints)       |
| `/stress`        | —          | 2s CPU burn for resource monitoring demos            |
| `/docs`          | —          | Swagger UI (auto-generated by FastAPI)               |

## Lab Exercises

### Exercise 1: Watch Startup → Readiness Transition

```bash
# Apply and immediately watch
kubectl apply -f k8s/deployment.yaml && kubectl get pods -n fastapi-demo-troy -w

# In another terminal, watch endpoints
kubectl get endpoints demo-service -n fastapi-demo-troy -w
```

### Exercise 2: Trigger a Liveness Failure

```bash
# Get a pod name
POD=$(kubectl get pods -n fastapi-demo-troy -o jsonpath='{.items[0].metadata.name}')

# Port-forward that pod
kubectl port-forward pod/$POD -n fastapi-demo-troy 8080:8000 &

# Toggle health off via curl
curl -s localhost:8080/toggle-health

# Watch it restart (in another terminal)
kubectl get pods -n fastapi-demo-troy -w

# Check events
kubectl describe pod $POD -n fastapi-demo-troy | tail -15
```

### Exercise 3: Remove a Pod from the Load Balancer

```bash
# Port-forward a pod and toggle readiness off
kubectl port-forward pod/$POD -n fastapi-demo-troy 8080:8000 &
curl -s localhost:8080/toggle-ready

# Watch it disappear from endpoints (while other pods stay)
kubectl get endpoints demo-service -n fastapi-demo-troy -w

# The pod is still running — just not receiving traffic
kubectl get pods -n fastapi-demo-troy
```

### Exercise 4: Observe Load Balancing Across Pods

```bash
# Port-forward the Service to see different pods respond
kubectl port-forward svc/demo-service -n fastapi-demo-troy 8080:80 &

# Hit it 10 times — notice different pod names
for i in $(seq 1 10); do
  curl -s localhost:8080/info | grep -o 'Pod Name.*</code>' | head -1
done
```

## Swapping in Your Own App

The FastAPI source is in `app/main.py`. To use your own application:

1. Keep the probe endpoints (`/healthz`, `/ready`, `/startup`) — or adjust the deployment.yaml probe paths
2. Replace or extend the other routes with your app logic
3. Add dependencies to `requirements.txt`
4. Rebuild the Docker image
