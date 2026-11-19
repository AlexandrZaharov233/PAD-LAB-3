param(
    [ValidateRange(1024,65535)][int]$Port = 8082,
    [ValidateRange(3,16)][int]$GatewayReplicas = 3,
    [string]$ResultPath = ''
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$baseUrl = "http://127.0.0.1:$Port"
$runId = [guid]::NewGuid().ToString('N')
$requestId = "lab3-$runId"
$result = [ordered]@{
    schema_version=1; success=$false; phase='startup'; request_id=$requestId
    api_ready=$false; booking_confirmed=$false; balancing=$false; persistent_recovery=$false
    gateway_replicas=$GatewayReplicas; booking_id=$null; old_pod=$null; new_pod=$null
}

function Run-Kube {
    & kubectl --context parkslot-lab3 -n parkslot @args
    if ($LASTEXITCODE -ne 0) { throw "kubectl failed: $($args[0])" }
}
function Call-Api($method, $path, $body=$null, $token='') {
    $headers = @{'X-Request-ID' = $requestId}
    if ($token) { $headers.Authorization = "Bearer $token" }
    $options = @{
        Uri = "$baseUrl/api$path"
        Method = $method
        Headers = $headers
        TimeoutSec = 15
        UseBasicParsing = $true
    }
    if ($null -ne $body) {
        $options.ContentType = 'application/json; charset=utf-8'
        $options.Body = [Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Depth 6 -Compress))
    }
    try { Invoke-RestMethod @options }
    catch { throw "$method $path failed: $($_.Exception.Message)" }
}
function Wait-Until($label, [scriptblock]$check, [int]$timeout = 80) {
    $deadline = (Get-Date).AddSeconds($timeout)
    do {
        $result = & $check
        if ($result) { return $result }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "Timed out: $label"
}
function Ready-Pods($appName) {
    $payload = Run-Kube get pods -l "app=$appName" -o json
    # kubectl emits separate text lines. PowerShell does not interpret \n as a
    # newline escape; use the platform newline to preserve valid JSON.
    $pods = ($payload -join [Environment]::NewLine) | ConvertFrom-Json -ErrorAction Stop
    $pods.items | Where-Object {
        -not $_.metadata.deletionTimestamp -and
        ($_.status.conditions | Where-Object { $_.type -eq 'Ready' -and $_.status -eq 'True' })
    }
}

try {
    $result.phase='api'
    Write-Host '=== 1. Kubernetes objects ==='
    Run-Kube get 'pods,services,deployments,statefulsets,pvc'
    Run-Kube get 'configmaps,secrets'
    $status = Call-Api 'GET' '/status'
    if (-not $status.ready -or $status.services.Count -ne 10) {
        throw 'Expected all ten business services ready. Inspect /api/status.'
    }
    Write-Host 'API: all 10 services ready.' -ForegroundColor Green
    $result.api_ready=$true

    Write-Host '=== 2. Live API: account, parking, promo, booking and payment ==='
    $seedFile = Join-Path $projectRoot '.local/k8s.env'
    $adminPassword = ''
    if (Test-Path -LiteralPath $seedFile) {
        $passwordLine = Get-Content -LiteralPath $seedFile | Where-Object { $_ -match '^ADMIN_PASSWORD=' } | Select-Object -First 1
        if ($passwordLine) { $adminPassword=$passwordLine.Substring('ADMIN_PASSWORD='.Length) }
    }
    if (-not $adminPassword) {
        $encoded = Run-Kube get secret parkslot-auth -o 'jsonpath={.data.ADMIN_PASSWORD}'
        $adminPassword = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(($encoded -join '')))
    }
    $result.phase='booking_and_payment'
    $admin = Call-Api 'POST' '/auth/login' @{
        email='admin@parking.local'; password=$adminPassword
    }
    $lot = Call-Api 'POST' '/parking/lots' @{
        name="K8s demo $($runId.Substring(0,8))"; address='Demo parking street 1'; hourly_rate_cents=3000; capacity=1
    } $admin.access_token
    $account = Call-Api 'POST' '/auth/register' @{
        name='Kubernetes demo'; email="k8s-$runId@example.test"; password=([guid]::NewGuid().ToString('N') + 'Az4')
    }
    $token = $account.access_token
    $vehicle = Call-Api 'POST' '/profiles/me/vehicles' @{
        plate="K8S $($runId.Substring(0,5).ToUpperInvariant())"; label='Demo car'
    } $token
    $spaces = Call-Api 'GET' "/parking/lots/$($lot.id)/spaces"
    $spaceIds = @($spaces.id)
    if ($spaceIds.Count -ne 1 -or -not $spaceIds[0]) { throw 'The demo needs one parking space.' }
    $startsAt = [DateTime]::UtcNow.AddDays(1)
    $book = Call-Api 'POST' '/bookings' @{
        space_id=$spaceIds[0]; vehicle_id=$vehicle.id
        start_at=$startsAt.ToString('o'); end_at=$startsAt.AddHours(1).ToString('o')
        promo_code='PARK10'; idempotency_key=[guid]::NewGuid().ToString()
    } $token
    if ($book.amount_cents -ne 2700) { throw "Expected PARK10: 30.00 -> 27.00 MDL, got $($book.amount_cents) cents" }
    $payment = Call-Api 'POST' '/payments' @{
        booking_id=$book.id; idempotency_key=[guid]::NewGuid().ToString()
    } $token
    if ($payment.status -ne 'SUCCEEDED' -or $payment.amount_cents -ne 2700) { throw 'The demo payment was not accepted.' }
    Wait-Until 'BookingConfirmed event' {
        (Call-Api 'GET' "/bookings/$($book.id)" $null $token).status -eq 'CONFIRMED'
    } | Out-Null
    $result.booking_confirmed=$true
    $result.booking_id=$book.id
    Write-Host "PARK10, payment 27.00 MDL and confirmed booking $($book.id)." -ForegroundColor Green
    $bookingLogs = Run-Kube logs deployment/booking-service --tail=80
    $bookingLogs | Select-String -SimpleMatch $requestId | Select-Object -First 5 | ForEach-Object { Write-Host $_.Line }

    $result.phase='scaling'
    Write-Host "=== 3. Gateway replicas: $GatewayReplicas and in-cluster balancing ==="
    Run-Kube scale deployment/gateway "--replicas=$GatewayReplicas"
    Run-Kube rollout status deployment/gateway --timeout=180s
    Run-Kube get pods -l app=gateway -o wide
    $clusterProbe = 'from collections import Counter; from urllib.request import urlopen; s=Counter(urlopen("http://gateway:8000/health",timeout=5).headers["X-ParkSlot-Instance"] for i in range(96)); print(dict(s)); assert len(s)==__REPLICAS__, s'
    $clusterProbe = $clusterProbe.Replace('__REPLICAS__',[string]$GatewayReplicas)
    # Send the code on stdin; Windows PowerShell 5.1 rewrites quotes in native args.
    $clusterProbe | kubectl --context parkslot-lab3 -n parkslot exec -i deployment/gateway -- python -
    if ($LASTEXITCODE -ne 0) { throw 'Expected HTTP traffic to reach all Gateway Pods.' }
    $result.balancing=$true

    Write-Host '=== 4. Recreate Booking Pod and compare stored booking ==='
    $result.phase='recovery'
    $old = @(Ready-Pods 'booking-service')
    if ($old.Count -ne 1) { throw 'Expected one ready Booking Pod before demo.' }
    $oldUid = $old[0].metadata.uid
    $oldName = $old[0].metadata.name
    $result.old_pod=$oldName
    Run-Kube delete pod $oldName --wait=true --timeout=100s
    $replacement = Wait-Until 'Replacement Booking Pod' {
        Ready-Pods 'booking-service' | Where-Object { $_.metadata.uid -ne $oldUid } | Select-Object -First 1
    } 160
    Wait-Until 'Gateway ready after Booking Pod recovery' {
        try { (Call-Api 'GET' '/status').ready } catch { $false }
    } | Out-Null
    $saved = Call-Api 'GET' "/bookings/$($book.id)" $null $token
    if ($saved.id -ne $book.id -or $saved.status -ne 'CONFIRMED' -or $saved.amount_cents -ne 2700) {
        throw 'Booking data did not survive Pod deletion.'
    }
    $result.persistent_recovery=$true
    $result.new_pod=$replacement.metadata.name
    $result.success=$true
    $result.phase='complete'
    Write-Host "$oldName -> $($replacement.metadata.name). Booking $($saved.id) preserved." -ForegroundColor Green
    Write-Host "PASS: application API, $GatewayReplicas Gateway replicas and persistent recovery."
    Write-Host 'The test leaves the demo booking and Gateway Pods visible for review.'
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host 'Verification stopped. Review the error above; API access uses open-k8s.bat.'
    exit 1
} finally {
    if ($ResultPath) {
        $result.finished_at_utc=[DateTime]::UtcNow.ToString('o')
        $fullPath=[IO.Path]::GetFullPath($ResultPath)
        [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($fullPath))
        [IO.File]::WriteAllText($fullPath,($result | ConvertTo-Json -Depth 5),(New-Object Text.UTF8Encoding($false)))
    }
}
