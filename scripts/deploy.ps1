param(
    [Parameter(Mandatory)][string]$Node1,
    [Parameter(Mandatory=$false)][string]$Node2 = "",
    [string]$Image,
    [string[]]$Workers,
    [string]$RegistryUser,
    [string]$RegistryToken
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent

# FORCED CONFIG PATH - Change this if your k3s.yaml is elsewhere
$KubeConfig = "/etc/rancher/k3s/k3s.yaml"

function Run($cmd) { 
    # Inject --kubeconfig into every kubectl command
    if ($cmd -like "kubectl*") {
        $cmd = $cmd.Replace("kubectl ", "kubectl --kubeconfig=$KubeConfig ")
    }
    Write-Host ">> $cmd" -ForegroundColor Cyan
    Invoke-Expression $cmd
    if ($LASTEXITCODE) { throw "failed: $cmd" } 
}

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

Run "kubectl label node $Node1 dbp/worker=true --overwrite"
if ($Node2) {
    Run "kubectl label node $Node2 dbp/worker=true --overwrite"
}
Run "kubectl label node $Node1 dbp/db=true --overwrite"
Run "kubectl apply -f `"$root\k8s\00-namespace-config.yaml`""

# Handle secrets separately because they use a direct call
$secretCheck = "kubectl --kubeconfig=$KubeConfig -n dbp get secret postgres-credentials"
Invoke-Expression $secretCheck *> $null
if ($LASTEXITCODE) {
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $pw = [Convert]::ToBase64String($bytes) -replace "[^A-Za-z0-9]", "x"
    Run "kubectl -n dbp create secret generic postgres-credentials --from-literal=POSTGRES_DB=dbp --from-literal=POSTGRES_USER=dbp --from-literal=POSTGRES_PASSWORD=$pw"
}

if ($RegistryUser) {
    Run "kubectl -n dbp delete secret regcred --ignore-not-found"
    Run "kubectl -n dbp create secret docker-registry regcred --docker-server=ghcr.io --docker-username=$RegistryUser --docker-password=$RegistryToken"
}

Run "kubectl apply -f `"$root\k8s`""
Run "kubectl -n dbp set image deployment/api api=$Image"
Run "kubectl -n dbp set image deployment/worker worker=$Image"
if ($RegistryUser) {
    $patch = Join-Path $env:TEMP "dbp-pullsecret.json"
    '{"spec":{"template":{"spec":{"imagePullSecrets":[{"name":"regcred"}]}}}}' | Set-Content -Encoding ascii $patch
    foreach ($d in "api", "worker") { Run "kubectl -n dbp patch deployment $d --patch-file `"$patch`"" }
}
Run "kubectl -n dbp rollout status statefulset/postgres --timeout=300s"
Run "kubectl -n dbp rollout status deployment/api --timeout=300s"
Run "kubectl -n dbp rollout status deployment/worker --timeout=300s"
Run "kubectl -n dbp get pods -o wide"
Write-Host "`nOpen http://<node-ip-or-tailscale-ip>:30080" -ForegroundColor Green
