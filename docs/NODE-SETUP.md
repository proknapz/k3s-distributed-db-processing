# Node setup: what to run on each machine

Three Linux machines (Ubuntu/Debian assumed), all with Tailscale. Replace the `<...>` placeholders.

| Machine | Role | Runs |
|---|---|---|
| Master | K3s server (control plane) | Kubernetes only, no app pods |
| Worker 1 | K3s agent | PostgreSQL + worker pods (+ maybe API) |
| Worker 2 | K3s agent | worker pods (+ maybe API) |

## 0. All three machines

Give each a unique hostname. Kubernetes uses it as the node name.

```bash
sudo hostnamectl set-hostname k3s-master      # k3s-worker1 / k3s-worker2 on the others
sudo apt update && sudo apt install -y curl

# Tailscale
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4                                # note this address (100.x.y.z)
```

Skip Tailscale if the machines share a LAN and use their LAN IPs instead (and drop `--flannel-iface`).

## 1. Master node

```bash
curl -sfL https://get.k3s.io | sh -s - server \
  --node-ip <master-ts-ip> --advertise-address <master-ts-ip> --tls-san <master-ts-ip> \
  --flannel-iface tailscale0 \
  --node-taint node-role.kubernetes.io/control-plane=true:NoSchedule

sudo systemctl status k3s                      # should be active (running)
sudo cat /var/lib/rancher/k3s/server/node-token    # copy this token for the workers
```

The taint keeps ordinary pods off the master. Our manifests already target only the worker nodes.

## 2. Worker nodes (run on BOTH worker 1 and worker 2)

```bash
curl -sfL https://get.k3s.io | K3S_URL=https://<master-ts-ip>:6443 K3S_TOKEN=<token-from-master> sh -s - agent \
  --node-ip <this-worker-ts-ip> --flannel-iface tailscale0

sudo systemctl status k3s-agent                # should be active (running)
```

## 3. Back on the master: check the cluster and label the nodes

```bash
sudo kubectl get nodes -o wide                 # all 3 should be Ready
```

```bash
# Both workers run the CPU workloads
sudo kubectl label node k3s-worker1 k3s-worker2 dbp/worker=true --overwrite
# Only worker 1 hosts PostgreSQL
sudo kubectl label node k3s-worker1 dbp/db=true --overwrite
```

## 4. Deploy the application

**Option A: from the master (Linux)**

```bash
git clone https://github.com/<you>/<repo>.git && cd <repo>

sudo kubectl apply -f k8s/00-namespace-config.yaml
sudo kubectl -n dbp create secret generic postgres-credentials \
  --from-literal=POSTGRES_DB=dbp --from-literal=POSTGRES_USER=dbp \
  --from-literal=POSTGRES_PASSWORD="$(openssl rand -hex 16)"

IMAGE=ghcr.io/<you>/<repo>/dbp-platform:latest
sudo kubectl apply -f k8s/
sudo kubectl -n dbp set image deployment/api api=$IMAGE
sudo kubectl -n dbp set image deployment/worker worker=$IMAGE
```

A private GHCR package also needs a pull secret (or just make the package public):

```bash
sudo kubectl -n dbp create secret docker-registry regcred --docker-server=ghcr.io \
  --docker-username=<github-user> --docker-password=<PAT with read:packages>
sudo kubectl -n dbp patch deployment api    -p '{"spec":{"template":{"spec":{"imagePullSecrets":[{"name":"regcred"}]}}}}'
sudo kubectl -n dbp patch deployment worker -p '{"spec":{"template":{"spec":{"imagePullSecrets":[{"name":"regcred"}]}}}}'
```

**Option B: from your Windows PC**: copy `/etc/rancher/k3s/k3s.yaml` from the master, replace
`127.0.0.1` with `<master-ts-ip>`, set it as `KUBECONFIG`, then run
`.\scripts\deploy.ps1 -Node1 k3s-worker1 -Node2 k3s-worker2 -Image <IMAGE>`.

## 5. Verify

```bash
sudo kubectl -n dbp get pods -o wide           # postgres on worker1; worker pods on both nodes
sudo kubectl -n dbp logs deploy/worker --tail=20
sudo kubectl top nodes
```

Open `http://<any-worker-ts-ip>:30080` from a device on your tailnet.

## Useful commands

| Where | Command | Purpose |
|---|---|---|
| Master | `sudo journalctl -u k3s -f` | control-plane logs |
| Worker | `sudo journalctl -u k3s-agent -f` | agent logs |
| Master | `sudo kubectl -n dbp scale deployment/worker --replicas=8` | more workers |
| Master | `sudo kubectl -n dbp describe pod <pod>` | why a pod is Pending |
| Worker | `sudo /usr/local/bin/k3s-agent-uninstall.sh` | remove K3s from a worker |
| Master | `sudo /usr/local/bin/k3s-uninstall.sh` | remove K3s from the master |

## Troubleshooting

- **Node stays NotReady / pods on different nodes can't talk**: check `tailscale status` on all nodes,
  that tailnet ACLs allow node-to-node traffic, and that `--flannel-iface tailscale0` was given on every node.
- **Worker pod Pending**: the spread rule needs both workers Ready and labeled `dbp/worker=true`.
- **ImagePullBackOff**: wrong image name, or the GHCR package is private without a `regcred` secret.
- **Postgres Pending**: worker 1 is missing the `dbp/db=true` label, or the PVC can't bind.
