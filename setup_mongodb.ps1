# One-time local MongoDB setup for GI-Craft-Tracing.
# Run from an ADMINISTRATOR PowerShell:   powershell -ExecutionPolicy Bypass -File .\setup_mongodb.ps1
# It installs MongoDB (server, shell, Compass) with winget, creates the gi_trace database,
# three users, collections and indexes, turns on authentication, and writes .env.
# Passwords are generated randomly and written only to .env; they are never printed.
$ErrorActionPreference = 'Stop'

function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }
function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "Open PowerShell with 'Run as administrator' and run this again."
}

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path $projectDir ".env"
if (Test-Path $envFile) { Fail ".env already exists at $envFile. Move it away first so it is not overwritten." }

# 1. Install -------------------------------------------------------------------------------
Step "1/6 Installing MongoDB Server, mongosh and Compass (about 5-10 minutes)"
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Fail "winget not found. Install 'App Installer' from the Microsoft Store, or install the three programs by hand from mongodb.com/try/download (Server, Shell, Compass) and re-run."
}
foreach ($id in @("MongoDB.Server", "MongoDB.Shell", "MongoDB.Compass.Full")) {
    $installed = winget list --id $id -e --accept-source-agreements 2>$null | Select-String $id
    if ($installed) { Write-Host "$id already installed" ; continue }
    winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { Fail "winget could not install $id (exit $LASTEXITCODE). Install it by hand from mongodb.com/try/download and re-run." }
}

# 2. Find the service, config and mongosh ---------------------------------------------------
Step "2/6 Locating the service and mongosh"
$svc = Get-CimInstance Win32_Service -Filter "Name='MongoDB'"
if (-not $svc) { Fail "Windows service 'MongoDB' not found after install." }
if ($svc.PathName -notmatch '--config\s+(?:"([^"]+)"|(\S+))') { Fail "Could not read the mongod.cfg path from the service: $($svc.PathName)" }
$cfgPath = if ($Matches[1]) { $Matches[1] } else { $Matches[2] }
if (-not (Test-Path $cfgPath)) { Fail "Config file not found: $cfgPath" }

$mongosh = $null
$cmd = Get-Command mongosh -ErrorAction SilentlyContinue
if ($cmd) { $mongosh = $cmd.Source }
if (-not $mongosh) {
    foreach ($dir in @("$env:ProgramFiles\mongosh", "$env:LOCALAPPDATA\Programs\mongosh", "${env:ProgramFiles(x86)}\mongosh")) {
        $hit = Get-ChildItem -Path $dir -Filter mongosh.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { $mongosh = $hit.FullName; break }
    }
}
if (-not $mongosh) { Fail "mongosh.exe not found. Open a new terminal and re-run, or install mongosh by hand." }
Write-Host "mongosh: $mongosh"
Write-Host "config:  $cfgPath"

if ((Get-Content $cfgPath -Raw) -match '(?m)^\s*authorization:\s*enabled') {
    Fail "Authentication is already enabled in $cfgPath, so this looks like a finished setup. Stopping so nothing is overwritten."
}

if ($svc.State -ne 'Running') { Start-Service MongoDB }
for ($i = 0; $i -lt 20; $i++) {
    & $mongosh "mongodb://127.0.0.1:27017" --quiet --eval "db.runCommand({ping:1}).ok" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 2
}
if ($LASTEXITCODE -ne 0) { Fail "MongoDB is not answering on 127.0.0.1:27017." }

# 3. Passwords ----------------------------------------------------------------------------
Step "3/6 Generating passwords"
function New-Password {
    $chars = [char[]]'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    $bytes = New-Object byte[] 24
    $rng = New-Object Security.Cryptography.RNGCryptoServiceProvider
    $rng.GetBytes($bytes)
    -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
}
$env:GI_PW_ADMIN  = New-Password
$env:GI_PW_WRITER = New-Password
$env:GI_PW_READER = New-Password

# 4. Users, collections, indexes (no secrets in this file: it reads environment variables) -
Step "4/6 Creating database, users, collections and indexes"
$js = Join-Path $env:TEMP "gi_trace_setup.js"
@'
const env = process.env;
let d = db.getSiblingDB("admin");
d.createUser({user: "dbadmin", pwd: env.GI_PW_ADMIN, roles: ["userAdminAnyDatabase", "readWriteAnyDatabase"]});
d = db.getSiblingDB("gi_trace");
d.createUser({user: "gi_writer", pwd: env.GI_PW_WRITER, roles: [{role: "readWrite", db: "gi_trace"}]});
d.createUser({user: "gi_reader", pwd: env.GI_PW_READER, roles: [{role: "read", db: "gi_trace"}]});
["certificates", "documents", "audit_log"].forEach(c => d.createCollection(c));
d.certificates.createIndex({batch_id: 1});
d.certificates.createIndex({cert_type: 1, compliance_status: 1});
d.documents.createIndex({batch_id: 1, doc_type: 1});
print("setup ok");
'@ | Set-Content -Path $js -Encoding ASCII
& $mongosh "mongodb://127.0.0.1:27017" --quiet --file $js
$code = $LASTEXITCODE
Remove-Item $js -Force
if ($code -ne 0) { Fail "mongosh setup script failed (exit $code). Nothing else was changed." }

# 5. Turn on authentication -----------------------------------------------------------------
Step "5/6 Turning on authentication"
Copy-Item $cfgPath "$cfgPath.bak" -Force
$lines = Get-Content $cfgPath
if ($lines -match '^\s*#\s*security:') {
    $lines = $lines | ForEach-Object { if ($_ -match '^\s*#\s*security:') { "security:`n  authorization: enabled" } else { $_ } }
} elseif ($lines -match '^\s*security:') {
    $lines = $lines | ForEach-Object { if ($_ -match '^\s*security:') { "security:`n  authorization: enabled" } else { $_ } }
} else {
    $lines = @($lines) + @("", "security:", "  authorization: enabled")
}
Set-Content -Path $cfgPath -Value $lines -Encoding ASCII
Restart-Service MongoDB
Start-Sleep -Seconds 5

# 6. .env, .gitignore, verification ---------------------------------------------------------
Step "6/6 Writing .env and verifying"
$write = "mongodb://gi_writer:$($env:GI_PW_WRITER)@127.0.0.1:27017/gi_trace"
$read  = "mongodb://gi_reader:$($env:GI_PW_READER)@127.0.0.1:27017/gi_trace"
@("MONGO_WRITE_URI=$write", "MONGO_READ_URI=$read") | Set-Content -Path $envFile -Encoding ASCII
$gi = Join-Path $projectDir ".gitignore"
if (-not (Test-Path $gi) -or -not (Select-String -Path $gi -Pattern '^\.env$' -Quiet)) { Add-Content -Path $gi -Value ".env" }

& $mongosh "mongodb://127.0.0.1:27017/gi_trace" --quiet --eval "db.certificates.countDocuments({})" 2>$null | Out-Null
$anonBlocked = ($LASTEXITCODE -ne 0)
$names = & $mongosh $read --quiet --eval "db.getCollectionNames().sort().join(',')"
$readerOk = ($LASTEXITCODE -eq 0 -and $names -match 'certificates')
& $mongosh $read --quiet --eval "db.certificates.insertOne({x:1})" 2>$null | Out-Null
$readerReadOnly = ($LASTEXITCODE -ne 0)

Write-Host ""
Write-Host ("anonymous access refused : " + $anonBlocked)
Write-Host ("reader sees collections  : " + $readerOk + "  ($names)")
Write-Host ("reader cannot write      : " + $readerReadOnly)
Remove-Item Env:GI_PW_ADMIN, Env:GI_PW_WRITER, Env:GI_PW_READER
if ($anonBlocked -and $readerOk -and $readerReadOnly) {
    Write-Host "`nDONE. Credentials are in $envFile (keep it private). The admin password is not stored anywhere:" -ForegroundColor Green
    Write-Host "if you need dbadmin later, reset it from a local shell before enabling auth, or recreate the user." -ForegroundColor Green
} else {
    Fail "One of the three checks failed. Copy the lines above and send them to me."
}
