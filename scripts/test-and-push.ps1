$ErrorActionPreference='Stop'
$repoName='AlexandrZaharov233/PAD-LAB-3'
$contextName='parkslot-lab3'
$projectRoot=Split-Path -Parent $PSScriptRoot
$runId=[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')+'-'+[guid]::NewGuid().ToString('N').Substring(0,8)
$work=Join-Path $projectRoot ".local/test-runs/$runId"
[void][IO.Directory]::CreateDirectory($work)
$ghPath=$null
$forward=$null
$canPublish=$false
$report=[ordered]@{
    schema_version=1; run_id=$runId; started_at_utc=[DateTime]::UtcNow.ToString('o')
    source='Windows PC / local Minikube'; repository=$repoName; source_commit=$null
    phase='prerequisites'; success=$false; verification=$null
}

function Write-Json($path,$value) {
    [IO.File]::WriteAllText($path,($value | ConvertTo-Json -Depth 16),(New-Object Text.UTF8Encoding($false)))
}
function Get-NativeJson($tool,[string[]]$arguments) {
    $output=@(& $tool @arguments)
    if($LASTEXITCODE -ne 0){throw "$tool failed: $($arguments[0])"}
    ($output -join [Environment]::NewLine) | ConvertFrom-Json -ErrorAction Stop
}
function Gh-Json([string[]]$Arguments) { Get-NativeJson -Tool $ghPath -Arguments $Arguments }
function Kube-Json([string[]]$Arguments) {
    Get-NativeJson -Tool 'kubectl' -Arguments (@('--context',$contextName,'-n','parkslot')+$Arguments)
}
function Ensure-Gh {
    $command=Get-Command gh -ErrorAction SilentlyContinue
    if($command){return $command.Source}
    $known=Join-Path $env:ProgramFiles 'GitHub CLI/gh.exe'
    if(Test-Path -LiteralPath $known){return $known}
    if(-not (Get-Command winget -ErrorAction SilentlyContinue)){
        throw 'Install GitHub CLI from https://cli.github.com/ and run this file again.'
    }
    Write-Host 'Installing GitHub CLI for signing in to GitHub...'
    & winget install --id GitHub.cli --exact --accept-package-agreements --accept-source-agreements --silent
    if($LASTEXITCODE -ne 0){throw 'GitHub CLI installation failed. Install it from https://cli.github.com/.'}
    if(Test-Path -LiteralPath $known){return $known}
    $command=Get-Command gh -ErrorAction SilentlyContinue
    if($command){return $command.Source}
    throw 'GitHub CLI is installed. Close this window and run TEST_AND_PUSH.bat again.'
}
function Format-Report($value) {
    $status=if($value.success){'PASS'}else{'FAIL / incomplete'}
    $lines=@(
        '# Local ParkSlot verification',
        '',
        "**Result: $status**",
        '',
        "| Field | Value |",
        "|---|---|",
        "| Run | $($value.run_id) |",
        "| UTC | $($value.finished_at_utc) |",
        "| Source revision | $($value.source_commit) |",
        "| Environment | Windows PC / local Minikube |",
        '',
        '| Check | Passed |',
        '|---|---|'
    )
    foreach($key in @('api_ready','booking_confirmed','balancing','persistent_recovery')){
        $ok=$false
        if($value.verification){$ok=$value.verification.$key -eq $true}
        $lines+="| $key | $ok |"
    }
    if($value.verification){
        $lines+=@(
            '',
            "Booking: $($value.verification.booking_id)",
            '',
            "Old Pod: $($value.verification.old_pod)",
            '',
            "New Pod: $($value.verification.new_pod)",
            '',
            "Gateway replicas: $($value.verification.gateway_replicas)"
        )
    }
    $lines+=@(
        '',
        'These checks ran on the local PC. They are separate from the Python test suites in GitHub Actions.',
        '',
        "Each report commit starts the CI workflow. Open [Actions](https://github.com/$repoName/actions) for its independent results.",
        '',
        'The report contains structured results only. Credentials and raw local logs are not published.'
    )
    ($lines -join [Environment]::NewLine)+[Environment]::NewLine
}
function Publish-Report($jsonText,$markdown) {
    for($attempt=0;$attempt -lt 3;$attempt++){
        $head=Gh-Json @('api',"repos/$repoName/git/ref/heads/main")
        $commit=Gh-Json @('api',"repos/$repoName/git/commits/$($head.object.sha)")
        $treePayload=@{
            base_tree=$commit.tree.sha
            tree=@(
                @{path='test-results/local/latest.json';mode='100644';type='blob';content=$jsonText},
                @{path='test-results/local/latest.md';mode='100644';type='blob';content=$markdown}
            )
        }
        $inputFile=Join-Path $work 'git-request.json'
        Write-Json $inputFile $treePayload
        $tree=Gh-Json @('api','--method','POST',"repos/$repoName/git/trees",'--input',$inputFile)
        Write-Json $inputFile @{
            message="test: publish local ParkSlot verification $runId"
            tree=$tree.sha
            parents=@($head.object.sha)
        }
        $newCommit=Gh-Json @('api','--method','POST',"repos/$repoName/git/commits",'--input',$inputFile)
        Write-Json $inputFile @{sha=$newCommit.sha;force=$false}
        & $ghPath api --method PATCH "repos/$repoName/git/refs/heads/main" --input $inputFile --silent
        if($LASTEXITCODE -eq 0){
            Remove-Item -LiteralPath $inputFile -Force
            return $newCommit.sha
        }
        Write-Host 'The branch changed during upload. Retrying without overwriting other commits...'
    }
    throw 'Could not update main safely. The local report is saved; run the file again.'
}

try {
    $ghPath=Ensure-Gh
    & $ghPath auth status --hostname github.com
    if($LASTEXITCODE -ne 0){
        Write-Host 'Sign in as AlexandrZaharov233. Follow the browser instructions below.'
        & $ghPath auth login --hostname github.com --git-protocol https --web
        if($LASTEXITCODE -ne 0){throw 'GitHub sign-in was not completed.'}
    }
    $metadata=Gh-Json @('api',"repos/$repoName")
    if(-not $metadata.permissions.push){throw "The signed-in account cannot write to $repoName."}
    $head=Gh-Json @('api',"repos/$repoName/git/ref/heads/main")
    $report.source_commit=if($env:PARKSLOT_SOURCE_COMMIT){$env:PARKSLOT_SOURCE_COMMIT}else{$head.object.sha}
    $canPublish=$true
    if(-not (Get-Command kubectl -ErrorAction SilentlyContinue)){throw 'kubectl is missing. Use the installed Lab 3 environment.'}
    $gateway=Kube-Json @('get','deployment/gateway','-o','json')
    # Preserve extra replicas instead of scaling down a Pod used by the user's browser.
    $replicas=[Math]::Max(3,[int]$gateway.spec.replicas)
    if($replicas -gt 16){throw 'This demonstration supports up to 16 Gateway replicas.'}

    $listener=New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback,0)
    $listener.Start()
    $port=([Net.IPEndPoint]$listener.LocalEndpoint).Port
    $listener.Stop()
    $outLog=Join-Path $work 'port-forward.log'
    $errLog=Join-Path $work 'port-forward-error.log'
    $forward=Start-Process -FilePath (Get-Command kubectl).Source -ArgumentList @(
        '--context',$contextName,'-n','parkslot','port-forward','--address','127.0.0.1',
        'service/gateway',"$($port):8000"
    ) -PassThru -NoNewWindow -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    $ready=$false
    $deadline=(Get-Date).AddSeconds(90)
    do {
        if($forward.HasExited){throw 'Port forwarding stopped. Make sure start-k8s.bat completed.'}
        try {
            $status=Invoke-RestMethod -UseBasicParsing -Uri "http://127.0.0.1:$port/api/status" -TimeoutSec 4
            if($status.ready){$ready=$true;break}
        } catch {}
        Start-Sleep -Seconds 1
    } while((Get-Date) -lt $deadline)
    if(-not $ready){throw 'The application did not become ready. Start Minikube and ParkSlot first.'}
    $report.phase='verification'
    $resultFile=Join-Path $work 'verification.json'
    $psExe=Join-Path $PSHOME 'powershell.exe'
    if(-not (Test-Path -LiteralPath $psExe)){$psExe=Join-Path $PSHOME 'pwsh'}
    Write-Host ''
    Write-Host 'Running real API, payment, Gateway and Pod recovery checks...'
    & $psExe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'verify-k8s.ps1') -Port $port -GatewayReplicas $replicas -ResultPath $resultFile
    $verificationExit=$LASTEXITCODE
    if(Test-Path -LiteralPath $resultFile){
        $report.verification=Get-Content -LiteralPath $resultFile -Raw | ConvertFrom-Json
    }
    $report.success=$verificationExit -eq 0 -and $report.verification.success -eq $true
    $report.phase=if($report.success){'complete'}else{'verification_failed'}
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    if($report.phase -eq 'prerequisites'){$report.phase='blocked'}
} finally {
    if($forward -and -not $forward.HasExited){
        Stop-Process -Id $forward.Id -ErrorAction SilentlyContinue
    }
    $report.finished_at_utc=[DateTime]::UtcNow.ToString('o')
    Write-Json (Join-Path $work 'latest.json') $report
}
$markdown=Format-Report $report
[IO.File]::WriteAllText((Join-Path $work 'latest.md'),$markdown,(New-Object Text.UTF8Encoding($false)))
if(-not $canPublish){
    Write-Host "No report was pushed. Local report: $work"
    exit 1
}
try {
    $publishedSha=Publish-Report ([IO.File]::ReadAllText((Join-Path $work 'latest.json'))) $markdown
    Write-Host ''
    Write-Host "Report pushed: https://github.com/$repoName/commit/$publishedSha" -ForegroundColor Green
    Write-Host "Results: https://github.com/$repoName/blob/main/test-results/local/latest.md"
    Write-Host "CI: https://github.com/$repoName/actions"
    Start-Process "https://github.com/$repoName/blob/main/test-results/local/latest.md"
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host "Report saved locally: $work"
    exit 1
}
if(-not $report.success){
    Write-Host 'The report was uploaded with FAIL / incomplete. Review the local test output.' -ForegroundColor Yellow
    exit 1
}
Write-Host 'PASS. Local verification uploaded. GitHub Actions will run the Python tests independently.' -ForegroundColor Green
