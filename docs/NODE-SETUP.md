# Node Setup Guide

Set up a 3-machine K3s cluster: 1 master and 2 workers. Follow the steps in order.
Each step says **where** to run it.

## Your machines (fill this in first)

| Role | Hostname to use | Tailscale IP |
|---|---|---|
| Master | `k3s-master` | `__________` |
| Worker 1 (runs PostgreSQL) | `k3s-worker1` | `__________` |
| Worker 2 | `k3s-worker2` | `__________` |

Commands below use `<MASTER_IP>`, `<WORKER_IP>` and `<TOKEN>`. Replace them with your values.
Assumes Ubuntu/Debian. K3s ships its own container runtime, so Docker is not needed on the nodes.

## Using a Mac as a node

K3s only runs on Linux, so a Mac node must run a **Linux VM**, and everything in Steps 1-3 is done *inside that VM*.

1. Install a VM tool: [Multipass](https://multipass.run) is the easiest (`brew install --cask multipass`).
   UTM, OrbStack or Lima also work.
2. Create an Ubuntu VM with enough resources, then open a shell in it:
   ```bash
   multipass launch --name k3s-worker2 --cpus 4 --memory 6G --disk 30G
   multipass shell k3s-worker2
   ```
3. Inside the VM, follow Step 1 (Tailscale) and Step 3 (agent). The VM gets its own Tailscale IP;
   use that as `<WORKER_IP>`. Do **not** use the Mac's own Tailscale IP.
4. Keep the Mac awake and plugged in. If it sleeps, the node goes `NotReady`.

**CPU architecture:** Apple Silicon Macs (M1/M2/M3...) run **arm64** VMs, while the other machines are probably **amd64**.
The image workflow now builds both, so a mixed cluster works if you pull the image from GHCR.
The `-Workers` SSH copy mode in `deploy.ps1` builds for only one architecture, so use `-Image` instead.
Check with `sudo kubectl get nodes -L kubernetes.io/arch`.

**Which worker hosts PostgreSQL:** make the always-on Linux machine Worker 1 (`dbp/db=true`) and the Mac VM Worker 2.
The database should not sit on a laptop that sleeps.

## What each machine runs

| Machine | K3s part | Our app pods |
|---|---|---|
| Master | server | none |
| Worker 1 | agent | PostgreSQL + worker pods |
| Worker 2 | agent | worker pods |

---

## Step 1. Prepare all 3 machines

**Run on: master, worker 1 and worker 2.** Use that machine's own hostname from the table.

```bash
sudo hostnamectl set-hostname <HOSTNAME>
sudo apt update && sudo apt install -y curl
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4
```

The last command prints the Tailscale IP (100.x.y.z). Write it in the table above.

Machines on the same LAN can use their LAN IPs instead. In that case, leave out `--flannel-iface tailscale0` below.

## Step 2. Install the master

**Run on: master only.**

```bash
curl -sfL https://get.k3s.io | sh -s - server \
  --node-ip <MASTER_IP> --advertise-address <MASTER_IP> --tls-san <MASTER_IP> \
  --flannel-iface tailscale0 \
  --node-taint node-role.kubernetes.io/control-plane=true:NoSchedule
```

Check it:

```bash
sudo systemctl status k3s
```

Get the join token and copy it:

```bash
sudo cat /var/lib/rancher/k3s/server/node-token
```

## Step 3. Install the workers

**Run on: worker 1, then worker 2.** Use that worker's own Tailscale IP for `<WORKER_IP>`.

```bash
curl -sfL https://get.k3s.io | K3S_URL=https://<MASTER_IP>:6443 K3S_TOKEN=<TOKEN> sh -s - agent \
  --node-ip <WORKER_IP> --flannel-iface tailscale0
```

Check it:

```bash
sudo systemctl status k3s-agent
```

## Step 4. Check the cluster and label the nodes

**Run on: master.**

```bash
sudo kubectl get nodes -o wide
```

All 3 nodes should show `Ready`. Then add the labels our manifests use:

```bash
sudo kubectl label node k3s-worker1 k3s-worker2 dbp/worker=true --overwrite
sudo kubectl label node k3s-worker1 dbp/db=true --overwrite
```

| Label | Nodes | Meaning |
|---|---|---|
| `dbp/worker=true` | worker 1, worker 2 | may run the API and worker pods |
| `dbp/db=true` | worker 1 only | hosts PostgreSQL |

## Step 5. Deploy the application

Choose **one** option.

### Option A. From the master

**Run on: master.**

```bash
git clone https://github.com/<you>/<repo>.git
cd <repo>

sudo kubectl apply -f k8s/00-namespace-config.yaml
sudo kubectl -n dbp create secret generic postgres-credentials \
  --from-literal=POSTGRES_DB=dbp --from-literal=POSTGRES_USER=dbp \
  --from-literal=POSTGRES_PASSWORD="$(openssl rand -hex 16)"

IMAGE=ghcr.io/<you>/<repo>/dbp-platform:latest
sudo kubectl apply -f k8s/
sudo kubectl -n dbp set image deployment/api api=$IMAGE
sudo kubectl -n dbp set image deployment/worker worker=$IMAGE
```

If the GHCR package is private, you also need a pull secret. Making the package public is simpler.

```bash
sudo kubectl -n dbp create secret docker-registry regcred --docker-server=ghcr.io \
  --docker-username=<github-user> --docker-password=<PAT with read:packages>
sudo kubectl -n dbp patch deployment api    -p '{"spec":{"template":{"spec":{"imagePullSecrets":[{"name":"regcred"}]}}}}'
sudo kubectl -n dbp patch deployment worker -p '{"spec":{"template":{"spec":{"imagePullSecrets":[{"name":"regcred"}]}}}}'
```

### Option B. From your Windows PC

1. On the master, copy `/etc/rancher/k3s/k3s.yaml` to your PC.
2. In that file, replace `127.0.0.1` with `<MASTER_IP>`.
3. Point kubectl at it and run the deploy script:

```powershell
$env:KUBECONFIG = "C:\path\to\k3s.yaml"
kubectl get nodes
.\scripts\deploy.ps1 -Node1 k3s-worker1 -Node2 k3s-worker2 -Image ghcr.io/<you>/<repo>/dbp-platform:latest
```

## Step 6. Verify

**Run on: master.**

```bash
sudo kubectl -n dbp get pods -o wide
sudo kubectl -n dbp logs deploy/worker --tail=20
sudo kubectl top nodes
```

Then open `http://<WORKER_IP>:30080` from any device on your tailnet.

### Checklist

- [ ] All 3 nodes show `Ready`
- [ ] `postgres-0` is Running on `k3s-worker1`
- [ ] Worker pods are Running on both worker nodes
- [ ] The web page loads and a job completes

---

## Command reference

| Where | Command | Purpose |
|---|---|---|
| Master | `sudo kubectl -n dbp scale deployment/worker --replicas=8` | add more workers |
| Master | `sudo kubectl -n dbp describe pod <pod>` | find out why a pod is Pending |
| Master | `sudo journalctl -u k3s -f` | master logs |
| Worker | `sudo journalctl -u k3s-agent -f` | worker logs |
| Master | `sudo /usr/local/bin/k3s-uninstall.sh` | remove K3s from the master |
| Worker | `sudo /usr/local/bin/k3s-agent-uninstall.sh` | remove K3s from a worker |

## Troubleshooting

| Problem | Likely cause and fix |
|---|---|
| Node is `NotReady`, or pods on different nodes can't talk | Run `tailscale status` on every node. Check that tailnet ACLs allow node-to-node traffic and that every node used `--flannel-iface tailscale0`. |
| Worker pod stays `Pending` | Both workers must be Ready and labeled `dbp/worker=true`. |
| `ImagePullBackOff` | The image name is wrong, or the GHCR package is private and there is no `regcred` secret. |
| `postgres-0` stays `Pending` | Worker 1 is missing the `dbp/db=true` label, or the storage volume can't bind. |
