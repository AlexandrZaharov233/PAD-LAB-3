param(
    [ValidateRange(2,32)][int]$Cpu = 4,
    [ValidateRange(3072,32768)][int]$MemoryMB = 5120
)
$ErrorActionPreference = 'Stop'
$profileName = 'parkslot-lab3'
$namespaceName = 'parkslot'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

function Run-Mini {
    & minikube -p $profileName @args
    if ($LASTEXITCODE -ne 0) { throw "Minikube failed: $($args[0])" }
}
function Run-Kube {
    & kubectl --context $profileName -n $namespaceName @args
    if ($LASTEXITCODE -ne 0) { throw "kubectl failed: $($args[0])" }
}
function Run-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "Docker failed: $($args[0])" }
}
function Seed-Secret($name, $payloadPath) {
    $found = Run-Kube get secret $name --ignore-not-found -o name
    if ($found) {
        Write-Host "Using existing Secret: $name"
        return
    }
    Run-Kube create secret generic $name "--from-env-file=$payloadPath"
}

try {
    foreach ($command in @('docker','minikube','kubectl')) {
        if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
            throw "$command is not installed or not on PATH. See README.md."
        }
    }
    if ($env:MINIKUBE_ACTIVE_DOCKERD) {
        throw 'Use a new PowerShell window without minikube docker-env.'
    }
    $osType = Run-Docker info --format '{{.OSType}}'
    if ($osType -ne 'linux') { throw 'Start Docker Desktop in Linux containers mode.' }
    Run-Docker compose version
    Run-Mini start --driver=docker --container-runtime=docker "--cpus=$Cpu" "--memory=$MemoryMB" --keep-context
    Run-Kube apply -f k8s/namespace.yaml

    $services = @('auth-service','profile-service','parking-service','booking-service',
        'payment-service','notification-service','promotion-service','waitlist-service',
        'maintenance-service','reporting-service','gateway')
    Run-Docker compose build @services

    foreach ($name in $services) {
        $image = "parkslot/" + $name + ":lab3"
        Write-Host "Loading $image into $profileName"
        Run-Mini image load --daemon $image
    }

    $privateDir = Join-Path $projectRoot '.local'
    $seedFile = Join-Path $privateDir 'k8s.env'
    if (-not (Test-Path -LiteralPath $seedFile)) {
        $existing = Run-Kube get secrets parkslot-shared parkslot-auth --ignore-not-found -o name
        if ($existing) { throw 'Cluster credentials exist, but .local/k8s.env is missing. Restore that file before redeploying.' }
        New-Item -ItemType Directory -Path $privateDir -Force | Out-Null
        Run-Docker run --rm --user 0 --mount "type=bind,source=$projectRoot,target=/project" --workdir /project --entrypoint python parkslot/auth-service:lab3 scripts/configure.py .local/k8s.env --k8s-only
    }
    $seed = @{}
    Get-Content -LiteralPath $seedFile | ForEach-Object {
        if ($_ -match '^([A-Z_]+)=(.+)$') { $seed[$Matches[1]] = $Matches[2] }
    }
    $shared = @('JWT_PUBLIC_KEY','INTERNAL_API_KEY','NATS_PASSWORD')
    $auth = @('JWT_PRIVATE_KEY','ADMIN_PASSWORD')
    foreach ($key in ($shared + $auth)) {
        if (-not $seed.ContainsKey($key)) { throw "Missing $key in .local/k8s.env" }
    }
    if ($seed['INTERNAL_API_KEY'].Length -lt 32 -or $seed['ADMIN_PASSWORD'].Length -lt 12) {
        throw 'The Secret input is invalid. Check .local/k8s.env.'
    }
    $sharedPath = Join-Path $privateDir 'k8s-shared.env'
    $authPath = Join-Path $privateDir 'k8s-auth.env'
    try {
        $shared | ForEach-Object { "$_=$($seed[$_])" } | Set-Content -LiteralPath $sharedPath -Encoding Ascii
        $auth | ForEach-Object { "$_=$($seed[$_])" } | Set-Content -LiteralPath $authPath -Encoding Ascii
        Seed-Secret 'parkslot-shared' $sharedPath
        Seed-Secret 'parkslot-auth' $authPath
    } finally {
        foreach ($path in @($sharedPath,$authPath)) {
            if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
        }
    }

    $running = Run-Kube get deployments -l app.kubernetes.io/part-of=parkslot -o name
    Run-Kube apply -k k8s --dry-run=server
    Run-Kube apply -k k8s
    if ($running) {
        Run-Kube rollout restart deployment -l app.kubernetes.io/part-of=parkslot
    }
    Run-Kube rollout status statefulset/nats --timeout=300s
    foreach ($name in $services) {
        Run-Kube rollout status "deployment/$name" --timeout=300s
    }
    Run-Kube get 'pods,services,pvc'
    Write-Host ''
    Write-Host 'Minikube is ready. Run open-k8s.bat to open http://127.0.0.1:8082.' -ForegroundColor Green
    Write-Host 'Admin: admin@parking.local. Password: ADMIN_PASSWORD in .local/k8s.env.'
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host 'Inspect: kubectl --context parkslot-lab3 -n parkslot get pods,pvc'
    Write-Host 'Run again after resolving the issue. Existing databases and credentials are retained.'
    exit 1
}
