@echo off
setlocal
title ParkSlot - tests and GitHub report
echo Downloading the current test runner from AlexandrZaharov233/PAD-LAB-3...
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; try { [Net.ServicePointManager]::SecurityProtocol=[Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12; $headers=@{'User-Agent'='ParkSlot-Lab3-Launcher'}; $head=Invoke-RestMethod -UseBasicParsing -Headers $headers -Uri 'https://api.github.com/repos/AlexandrZaharov233/PAD-LAB-3/git/ref/heads/main'; $revision=$head.object.sha; $folder=Join-Path $env:LOCALAPPDATA ('ParkSlotLab3\runner-'+[guid]::NewGuid().ToString('N')); [void][IO.Directory]::CreateDirectory($folder); $archive=Join-Path $folder 'source.zip'; Invoke-WebRequest -UseBasicParsing -Headers $headers -Uri ('https://github.com/AlexandrZaharov233/PAD-LAB-3/archive/'+$revision+'.zip') -OutFile $archive; Expand-Archive -LiteralPath $archive -DestinationPath $folder; $source=Get-ChildItem -LiteralPath $folder -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'scripts\test-and-push.ps1') } | Select-Object -First 1; if(-not $source){ throw 'Test runner was not found in the repository archive.' }; $env:PARKSLOT_SOURCE_COMMIT=$revision; & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $source.FullName 'scripts\test-and-push.ps1'); exit $LASTEXITCODE } catch { Write-Host $_.Exception.Message -ForegroundColor Red; exit 1 }"
if errorlevel 1 (
  echo.
  echo The run did not complete successfully. Review the message above.
) else (
  echo.
  echo Done. Results were pushed to GitHub.
)
pause
