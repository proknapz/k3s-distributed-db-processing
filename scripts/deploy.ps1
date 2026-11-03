param(
    # Kubernetes node names (kubectl get nodes): Worker 1 hosts PostgreSQL, Worker 2 only runs workers
    [Parameter(Mandatory)][string]$Node1,
    [Parameter(Mandatory)][string]$Node2,
    # Use a prebuilt image (e.g. ghcr.io/<owner>/dbp-platform:latest) pulled by the nodes.
    # If omitted, the image is built locally and copied to the workers over SSH (-Workers required).
    [string]$Image,
    [string[]]$Workers,   # SSH targets, e.g. user@100.64.0.11,user@100.64.0.12
    # Only for private GHCR packages
    [string]$RegistryUser,
    [string]$RegistryToken
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
function Run($cmd) { Write-Host ">> $cmd" -ForegroundColor Cyan; Invoke-Expression $cmd; if ($LASTEXITCODE) { throw "failed: $cmd" } }

if (-not $Image) {
    if (-not $Workers) { throw "Provide -Image <registry image> or -Workers <ssh targets>" }
    $Image = "dbp-platform:1.0"
    Run "docker build -t $Image `"$root\app`""
    $tar = Join-Path $env:TEMP "dbp-platform.tar"
    Run "docker save -o `"$tar`" $Image"
    foreach ($w in $Workers) {
        Run "scp `"$tar`" ${w}:/tmp/dbp-platform.tar"
        Run "ssh $w `"sudo k3s ctr images import /tmp/dbp-platform.tar && rm /tmp/dbp-platform.tar`""
    }
}

Run "kubectl label node $Node1 $Node2 dbp/worker=true --overwrite"
Run "kubectl label node $Node1 dbp/db=true --overwrite"
Run "kubectl apply -f `"$root\k8s\00-namespace-config.yaml`""

# Create the DB secret once; never overwrite an existing password (the DB volume keeps the old one).
kubectl -n dbp get secret postgres-credentials *> $null
if ($LASTEXITCODE) {
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $pw = [Convert]::ToBase64String($bytes) -replace '[^A-Za-z0-9]', 'x'
    Run "kubectl -n dbp create secret generic postgres-credentials --from-literal=POSTGRES_DB=dbp --from-literal=POSTGRES_USER=dbp --from-literal=POSTGRES_PASSWORD=$pw"
}

if ($RegistryUser) {
    kubectl -n dbp delete secret regcred --ignore-not-found | Out-Null
    Run "kubectl -n dbp create secret docker-registry regcred --docker-server=ghcr.io --docker-username=$RegistryUser --docker-password=$RegistryToken"
}

Run "kubectl apply -f `"$root\k8s`""
Run "kubectl -n dbp set image deployment/api api=$Image"
Run "kubectl -n dbp set image deployment/worker worker=$Image"
if ($RegistryUser) {
    $patch = Join-Path $env:TEMP "dbp-pullsecret.json"
    '{"spec":{"template":{"spec":{"imagePullSecrets":[{"name":"regcred"}]}}}}' | Set-Content -Encoding ascii $patch
    foreach ($d in 'api', 'worker') { Run "kubectl -n dbp patch deployment $d --patch-file `"$patch`"" }
}
Run "kubectl -n dbp rollout status statefulset/postgres --timeout=300s"
Run "kubectl -n dbp rollout status deployment/api --timeout=300s"
Run "kubectl -n dbp rollout status deployment/worker --timeout=300s"
Run "kubectl -n dbp get pods -o wide"
Write-Host "`nOpen http://<node-ip-or-tailscale-ip>:30080" -ForegroundColor Green
