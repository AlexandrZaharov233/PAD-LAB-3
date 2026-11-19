$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location $taskRoot
try {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Install Docker Desktop, start it, then run start.bat again. See README.md.' }
    docker info --format '{{.ServerVersion}}'
    if ($LASTEXITCODE -ne 0) { throw 'Docker Desktop is not ready. Start Docker Desktop and wait for the engine.' }
    docker compose version
    if ($LASTEXITCODE -ne 0) { throw 'Docker Compose is not available. Update Docker Desktop.' }
    if (-not (Test-Path '.env')) {
        docker compose build auth-service
        if ($LASTEXITCODE -ne 0) { throw 'Could not build the initialization image.' }
        docker run --rm --user 0 --mount "type=bind,source=$taskRoot,target=/project" --workdir /project --entrypoint python parkslot/auth-service:lab3 scripts/configure.py
        if ($LASTEXITCODE -ne 0) { throw 'Could not create .env.' }
    }
    docker compose up -d --build --wait --wait-timeout 180
    if ($LASTEXITCODE -ne 0) {
        docker compose ps
        docker compose logs --tail 30
        throw 'A service did not become ready. Review the errors above.'
    }
    Write-Host ''
    Write-Host 'ParkSlot Compose is ready: http://127.0.0.1:8081' -ForegroundColor Green
    Get-Content '.env' | Where-Object { $_ -match '^ADMIN_(EMAIL|PASSWORD)=' } | ForEach-Object { Write-Host $_ }
    Write-Host 'Or create a user account in the browser. Keep .env private.'
    Start-Process 'http://127.0.0.1:8081'
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
