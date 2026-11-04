#!/bin/bash
set -e

NODE1=$1
NODE2=$2
IMAGE=$3

# If only two arguments are provided, the second one might be the image
if [ -z "$3" ]; then
    IMAGE=$2
    NODE2=""
fi

if [ -z "$NODE1" ] || [ -z "$IMAGE" ]; then
    echo "Usage: ./deploy.sh <node1> [node2] <image>"
    echo "Example: ./deploy.sh laptop-r10q787a desktop-g8j5afn ghcr.io/proknapz/k3s-distributed-db-processing/dbp-platform:latest"
    exit 1
fi

echo ">> Labeling nodes..."
sudo kubectl label node $NODE1 dbp/worker=true --overwrite
if [ ! -z "$NODE2" ]; then
    sudo kubectl label node $NODE2 dbp/worker=true --overwrite
fi
sudo kubectl label node $NODE1 dbp/db=true --overwrite

echo ">> Applying namespace config..."
sudo kubectl apply -f ./k8s/00-namespace-config.yaml

echo ">> Managing DB secrets..."
if ! sudo kubectl -n dbp get secret postgres-credentials > /dev/null 2>&1; then
    PW=$(openssl rand -base64 24 | tr -dc 'a-zA-Z0-9' | head -c 24)
    sudo kubectl -n dbp create secret generic postgres-credentials --from-literal=POSTGRES_DB=dbp --from-literal=POSTGRES_USER=dbp --from-literal=POSTGRES_PASSWORD=$PW
fi

echo ">> Deploying platform..."
sudo kubectl apply -f ./k8s
sudo kubectl -n dbp set image deployment/api api=$IMAGE
sudo kubectl -n dbp set image deployment/worker worker=$IMAGE

echo ">> Waiting for rollout..."
sudo kubectl -n dbp rollout status statefulset/postgres --timeout=300s
sudo kubectl -n dbp rollout status deployment/api --timeout=300s
sudo kubectl -n dbp rollout status deployment/worker --timeout=300s

sudo kubectl -n dbp get pods -o wide
echo -e "\nOpen http://<node-ip>:30080"
