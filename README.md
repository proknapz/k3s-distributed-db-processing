# Distributed Database Processing Platform (K3s)

Users submit database-processing jobs in a web UI. The API splits each job into independent
tasks stored in a PostgreSQL queue. Python worker pods on **both** worker nodes claim tasks
atomically (`FOR UPDATE SKIP LOCKED`), do the CPU-heavy work in Python, and write results back.

```
 browser -> api (NodePort 30080) -> PostgreSQL (task queue + data, Worker Node 1, PVC)
                                         ^   ^
                       worker pods  -----+   +-----  worker pods
                       (Worker Node 1)               (Worker Node 2)
```

## Layout

| Path | Purpose |
|---|---|
| `app/dbp/api.py`, `static/index.html` | FastAPI app + web UI (create dataset, submit jobs, see per-node distribution) |
| `app/dbp/worker.py` | Worker loop: claim, process, save, finalize/reduce, recover lost tasks |
| `app/dbp/processing.py` | Split / run / reduce for each task type |
| `k8s/` | Namespace+config, PostgreSQL StatefulSet+PVC, API Deployment+Service, Worker Deployment |
| `scripts/deploy.ps1` | Label nodes, create DB secret, apply manifests, set the image |
| `.github/workflows/build-image.yml` | Builds and pushes the image to GHCR on every push to `main` |

Task types: Calculate Statistics, Find Duplicates, Join Records on Key, Detect Anomalies,
Sort Data, Generate Report. The join operation compares distinct record pairs sharing a key
and reports the pair count and sum of absolute value differences without storing joined rows.
(Compare Datasets is not implemented yet; add a runner/reducer pair in `processing.py`.)
Anomaly detection uses z-scores computed per chunk, which is an approximation on random data.



The workflow publishes `ghcr.io/<you>/<repo>/dbp-platform:latest`. The DB password is **not** in the repo;
`deploy.ps1` generates it and stores it only in the cluster. If the GHCR package is private, either
make it public (Package settings) or pass `-RegistryUser`/`-RegistryToken` (a PAT with `read:packages`) to the deploy script.

> **Per-machine command list (master, worker 1, worker 2): see [docs/NODE-SETUP.md](docs/NODE-SETUP.md).**

## 1. K3s over Tailscale (optional, lets the nodes live on different networks)

Install Tailscale on the master and both workers (`tailscale up`), then note each node's address (`tailscale ip -4`).

### Master Node Setup (Windows PC with WSL)

**On your Windows PC with internet access:**

1. Download the K3s binary:
```powershell
mkdir C:\k3s-install
cd C:\k3s-install

# Get latest release (or specify version like v1.37.1)
$latestRelease = ((Invoke-WebRequest -Uri "https://api.github.com/repos/k3s-io/k3s/releases/latest" -UseBasicParsing).Content | ConvertFrom-Json)
$downloadUrl = $latestRelease.assets | Where-Object {$_.name -eq "k3s"} | Select-Object -ExpandProperty browser_download_url
Invoke-WebRequest -Uri $downloadUrl -OutFile "k3s"

# Verify file size is ~80-150MB
ls -la k3s
```

**In WSL on that same PC:**

2. Copy K3s binary and start the control-plane:
```bash
# Copy from Windows to WSL
cp /mnt/c/k3s-install/k3s /tmp/k3s
chmod +x /tmp/k3s

# Get your Tailscale IP
MASTER_IP=$(tailscale ip -4)
echo "Master IP: $MASTER_IP"

# Start K3s server with Tailscale networking
sudo /tmp/k3s server \
  --node-ip $MASTER_IP \
  --advertise-address $MASTER_IP \
  --tls-san $MASTER_IP \
  --tls-san 127.0.0.1 \
  --tls-san localhost \
  --flannel-iface tailscale0 \
  --node-taint node-role.kubernetes.io/control-plane=true:NoSchedule \
  --write-kubeconfig-mode 644
```

3. Verify the master is running (in a new WSL terminal):
```bash
sudo /tmp/k3s kubectl get nodes
```

4. Get the node token for workers:
```bash
sudo cat /var/lib/rancher/k3s/server/node-token
```

### Worker Node Setup (any machine on Tailscale)

On each worker machine (after installing Tailscale and getting its IP):

```bash
MASTER_IP=<master-tailscale-ip>
NODE_IP=$(tailscale ip -4)
TOKEN=<token-from-master>

curl -sfL https://get.k3s.io | K3S_URL=https://$MASTER_IP:6443 K3S_TOKEN=$TOKEN sh -s - agent \
  --node-ip $NODE_IP \
  --flannel-iface tailscale0
```

### Access from your PC

On your PC (also on the tailnet):

```bash
# Copy kubeconfig from master via Tailscale
scp user@<master-ts-ip>:/etc/rancher/k3s/k3s.yaml ~/.kube/config

# Update the server address (if needed)
sed -i 's/127.0.0.1/<master-ts-ip>/g' ~/.kube/config

# Verify connectivity
kubectl get nodes
```

**Troubleshooting:**
- If pods on different nodes cannot communicate, check Tailscale ACLs allow traffic between nodes
- If `kubectl` gets certificate errors, ensure `--tls-san` includes the IP you're connecting with
- If K3s fails to start due to port in use: `sudo pkill -9 -f k3s; sudo rm -rf /var/lib/rancher/k3s`

## 2. Deploy

Find the node names with `kubectl get nodes` (Worker Node 1 hosts PostgreSQL).

```powershell
# image pulled from GHCR by the nodes (recommended)
.\scripts\deploy.ps1 -Node1 <worker1-name> -Node2 <worker2-name> -Image ghcr.io/<you>/<repo>/dbp-platform:latest

# or build locally and copy to the workers over SSH (passwordless sudo needed; use Tailscale IPs)
.\scripts\deploy.ps1 -Node1 <worker1-name> -Node2 <worker2-name> -Workers user@<w1-ts-ip>,user@<w2-ts-ip>
```

The script labels the nodes (`dbp/worker=true` on both, `dbp/db=true` on Worker 1), creates the
`postgres-credentials` secret once (random password), applies `k8s/`, and points the API and worker
Deployments at the image. Re-running it is safe and keeps the existing password.

Open `http://<node-tailscale-ip>:30080` from any device on your tailnet, generate a dataset
(e.g. 2,000,000 rows), and submit jobs. The job detail shows how many tasks each node claimed
and its CPU busy time. The app has no login, so do not expose it with Tailscale Funnel.

## Experiments

```bash
kubectl -n dbp get pods -o wide                      # workers should be split across both nodes
kubectl top nodes; kubectl top pods -n dbp           # metrics-server ships with K3s
kubectl -n dbp scale deployment/worker --replicas=8  # more workers
kubectl -n dbp logs deploy/worker --tail=20
```

Adjust CPU requests/limits in `k8s/30-worker.yaml` and `k8s/10-postgres.yaml` to study contention
on Worker Node 1. Smaller `chunk size` => more tasks => finer load balancing.
Worker placement uses `topologySpreadConstraints` (maxSkew 1), so replicas stay balanced; with
`DoNotSchedule`, a replica stays Pending if it cannot satisfy the spread.

## Notes

- To pull a new image after a push: `kubectl -n dbp rollout restart deployment/api deployment/worker`
  (the `latest` tag with `IfNotPresent` is cached, so prefer deploying a commit-SHA tag: `...:<sha>`).
- PostgreSQL data lives on Worker Node 1's disk (local-path PV). If that node is lost, so is the data.
- Tasks claimed by a dead pod are returned to the queue after 5 minutes (`STALE_SECONDS`), up to 3 attempts.
