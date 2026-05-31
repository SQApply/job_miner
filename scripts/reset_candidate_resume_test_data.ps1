param(
    [Parameter(Mandatory = $false)]
    [string]$Email = "abhinav.mg.aws@gmail.com",

    [Parameter(Mandatory = $false)]
    [string]$RepoRoot = ".",

    [Parameter(Mandatory = $false)]
    [string]$PostgresContainer = "job_miner_postgres",

    [Parameter(Mandatory = $false)]
    [string]$MongoContainer = "job_miner_mongo",

    [Parameter(Mandatory = $false)]
    [string]$PostgresUser = "job_miner_app",

    [Parameter(Mandatory = $false)]
    [string]$PostgresDb = "job_miner_control",

    [Parameter(Mandatory = $false)]
    [string]$MongoUser = "job_miner",

    [Parameter(Mandatory = $false)]
    [string]$MongoPassword = "job_miner",

    [Parameter(Mandatory = $false)]
    [string]$MongoDb = "job_miner",

    [Parameter(Mandatory = $false)]
    [switch]$KeepDebugFiles,

    [Parameter(Mandatory = $false)]
    [switch]$KeepFailedFiles,

    [Parameter(Mandatory = $false)]
    [switch]$KeepProcessedFiles,

    [Parameter(Mandatory = $false)]
    [switch]$CleanLogs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "[INFO] $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "[OK] $Message" -ForegroundColor Green
}

function Write-WarnMsg {
    param([string]$Message)
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
}

function Remove-PathIfExists {
    param([string]$PathToRemove)

    if ([string]::IsNullOrWhiteSpace($PathToRemove)) {
        return
    }

    if (Test-Path -LiteralPath $PathToRemove) {
        Remove-Item -LiteralPath $PathToRemove -Recurse -Force
        Write-Ok "Deleted: $PathToRemove"
    }
    else {
        Write-WarnMsg "Not found, skipped: $PathToRemove"
    }
}

function Invoke-Docker {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$DockerArgs
    )

    $output = & docker @DockerArgs 2>&1
    $exitCode = $LASTEXITCODE

    if ($exitCode -ne 0) {
        $message = ($output | Out-String).Trim()
        throw "Docker command failed: docker $($DockerArgs -join ' ')`n$message"
    }

    return $output
}

function Invoke-DockerWithInputFile {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$DockerArgs,

        [Parameter(Mandatory = $true)]
        [string]$InputFile,

        [Parameter(Mandatory = $false)]
        [string]$FailureMessage = "Docker command with input file failed."
    )

    if (-not (Test-Path -LiteralPath $InputFile)) {
        throw "Input file not found: $InputFile"
    }

    $output = Get-Content -Raw -LiteralPath $InputFile | & docker @DockerArgs 2>&1
    $exitCode = $LASTEXITCODE

    if ($exitCode -ne 0) {
        $message = ($output | Out-String).Trim()
        throw "$FailureMessage`nDocker command failed: docker $($DockerArgs -join ' ')`n$message"
    }

    return $output
}

$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
Set-Location -LiteralPath $RepoRoot

Write-Host "============================================================"
Write-Host "Job Miner Resume Test Data Reset"
Write-Host "RepoRoot : $RepoRoot"
Write-Host "Email    : $Email"
Write-Host "============================================================"

Write-Step "Checking Docker containers"
Invoke-Docker @("ps", "--format", "{{.Names}}") | Out-Null

$runningContainers = @(Invoke-Docker @("ps", "--format", "{{.Names}}"))

if ($runningContainers -notcontains $PostgresContainer) {
    throw "Postgres container '$PostgresContainer' is not running."
}

if ($runningContainers -notcontains $MongoContainer) {
    throw "Mongo container '$MongoContainer' is not running."
}

Write-Ok "Docker containers are running"

Write-Step "Finding app user in Postgres"

$emailSql = $Email.Replace("'", "''")

$appUserIdRaw = Invoke-Docker @(
    "exec", "-i", $PostgresContainer,
    "psql", "-U", $PostgresUser, "-d", $PostgresDb,
    "-t", "-A",
    "-c", "SELECT id FROM job_miner_control.app_users WHERE lower(email)=lower('$emailSql') LIMIT 1;"
)

$appUserId = ($appUserIdRaw | Out-String).Trim()

if ([string]::IsNullOrWhiteSpace($appUserId)) {
    Write-WarnMsg "No app_users row found for email '$Email'. Continuing with Mongo cleanup only."
}
else {
    Write-Ok "APP_USER_ID=$appUserId"
}

Write-Step "Deleting Mongo candidate/resume/recommendation records"

$emailJson = $Email | ConvertTo-Json -Compress

$mongoJs = @"
const email = $emailJson;

const candidateDocs = db.candidate_tower_records.find({
  `$or: [
    { email: email },
    { "contact.email": email },
    { "raw_payload.email": email }
  ]
}).toArray();

const candidateIds = [...new Set(candidateDocs.map(x => x.candidate_id).filter(Boolean))];
const resumeIds = [...new Set(candidateDocs.map(x => x.resume_id).filter(Boolean))];

const resumeDocs = db.resume_profiles_current.find({
  `$or: [
    { "contact.email": email },
    { resume_id: { `$in: resumeIds } }
  ]
}).toArray();

resumeDocs.forEach(x => {
  if (x.resume_id) {
    resumeIds.push(x.resume_id);
  }
});

const finalResumeIds = [...new Set(resumeIds.filter(Boolean))];

function deleteMany(collectionName, query) {
  return db.getCollection(collectionName).deleteMany(query).deletedCount;
}

const result = {
  email,
  candidateIds,
  resumeIds: finalResumeIds,
  deleted: {}
};

result.deleted.candidate_job_matches = deleteMany("candidate_job_matches", {
  candidate_id: { `$in: candidateIds }
});

result.deleted.candidate_job_matches_llm_reranked = deleteMany("candidate_job_matches_llm_reranked", {
  candidate_id: { `$in: candidateIds }
});

result.deleted.candidate_tower_records = deleteMany("candidate_tower_records", {
  `$or: [
    { candidate_id: { `$in: candidateIds } },
    { email: email },
    { "contact.email": email },
    { "raw_payload.email": email }
  ]
});

result.deleted.resume_profiles_current = deleteMany("resume_profiles_current", {
  `$or: [
    { resume_id: { `$in: finalResumeIds } },
    { "contact.email": email }
  ]
});

print(JSON.stringify(result, null, 2));
"@

$tempJs = Join-Path $env:TEMP ("job_miner_cleanup_" + [Guid]::NewGuid().ToString("N") + ".js")
Set-Content -LiteralPath $tempJs -Value $mongoJs -Encoding UTF8

try {
    $mongoOutput = Invoke-DockerWithInputFile `
        -InputFile $tempJs `
        -DockerArgs @(
            "exec", "-i", $MongoContainer,
            "mongosh",
            "-u", $MongoUser,
            "-p", $MongoPassword,
            "--authenticationDatabase", "admin",
            $MongoDb,
            "--quiet"
        ) `
        -FailureMessage "Failed to delete Mongo candidate/resume/recommendation records."

    $mongoOutput | ForEach-Object { Write-Host $_ }
}
finally {
    Remove-Item -LiteralPath $tempJs -Force -ErrorAction SilentlyContinue
}

Write-Step "Deleting Postgres candidate links, saved jobs, and applications"

Invoke-Docker @(
    "exec", "-i", $PostgresContainer,
    "psql", "-U", $PostgresUser, "-d", $PostgresDb,
    "-c", "WITH target_user AS (SELECT id FROM job_miner_control.app_users WHERE lower(email)=lower('$emailSql')) DELETE FROM job_miner_control.candidate_user_links l USING target_user u WHERE l.app_user_id = u.id RETURNING l.app_user_id, l.candidate_id, l.resume_id;"
) | ForEach-Object { Write-Host $_ }

Invoke-Docker @(
    "exec", "-i", $PostgresContainer,
    "psql", "-U", $PostgresUser, "-d", $PostgresDb,
    "-c", "WITH target_user AS (SELECT id FROM job_miner_control.app_users WHERE lower(email)=lower('$emailSql')) DELETE FROM job_miner_control.candidate_saved_jobs s USING target_user u WHERE s.app_user_id = u.id RETURNING s.id;"
) | ForEach-Object { Write-Host $_ }

Invoke-Docker @(
    "exec", "-i", $PostgresContainer,
    "psql", "-U", $PostgresUser, "-d", $PostgresDb,
    "-c", "WITH target_user AS (SELECT id FROM job_miner_control.app_users WHERE lower(email)=lower('$emailSql')) DELETE FROM job_miner_control.candidate_job_applications a USING target_user u WHERE a.app_user_id = u.id RETURNING a.id;"
) | ForEach-Object { Write-Host $_ }

Write-Step "Deleting local resume upload and extraction files"

if (-not [string]::IsNullOrWhiteSpace($appUserId)) {
    Remove-PathIfExists (Join-Path $RepoRoot "data\resumes\candidate_uploads\$appUserId")
}

if (-not $KeepDebugFiles) {
    Remove-PathIfExists (Join-Path $RepoRoot "data\resumes\llm_debug")
}

if (-not $KeepFailedFiles) {
    Remove-PathIfExists (Join-Path $RepoRoot "data\resumes\failed")
    Remove-PathIfExists (Join-Path $RepoRoot "data\failed\resume_ocr")
}

if (-not $KeepProcessedFiles) {
    Remove-PathIfExists (Join-Path $RepoRoot "data\resumes\processed")
    Remove-PathIfExists (Join-Path $RepoRoot "data\processed\resume_ocr")
    Remove-PathIfExists (Join-Path $RepoRoot "data\processed\resumes")
}

if ($CleanLogs) {
    Remove-PathIfExists (Join-Path $RepoRoot "data\resumes\logs\resume_ocr")
    Remove-PathIfExists (Join-Path $RepoRoot "data\logs\resume_ocr")
}

Write-Step "Verifying Postgres cleanup"

Invoke-Docker @(
    "exec", "-i", $PostgresContainer,
    "psql", "-U", $PostgresUser, "-d", $PostgresDb,
    "-c", "SELECT u.id AS app_user_id, u.email, l.candidate_id, l.resume_id FROM job_miner_control.app_users u LEFT JOIN job_miner_control.candidate_user_links l ON l.app_user_id = u.id WHERE lower(u.email)=lower('$emailSql');"
) | ForEach-Object { Write-Host $_ }

Write-Step "Verifying Mongo cleanup"

$verifyJs = @"
const email = $emailJson;

print("candidate_tower_records=" + db.candidate_tower_records.countDocuments({
  `$or: [
    { email: email },
    { "contact.email": email },
    { "raw_payload.email": email }
  ]
}));

print("resume_profiles_current=" + db.resume_profiles_current.countDocuments({
  "contact.email": email
}));
"@

$tempVerifyJs = Join-Path $env:TEMP ("job_miner_verify_" + [Guid]::NewGuid().ToString("N") + ".js")
Set-Content -LiteralPath $tempVerifyJs -Value $verifyJs -Encoding UTF8

try {
    $verifyOutput = Invoke-DockerWithInputFile `
        -InputFile $tempVerifyJs `
        -DockerArgs @(
            "exec", "-i", $MongoContainer,
            "mongosh",
            "-u", $MongoUser,
            "-p", $MongoPassword,
            "--authenticationDatabase", "admin",
            $MongoDb,
            "--quiet"
        ) `
        -FailureMessage "Failed to verify Mongo cleanup."

    $verifyOutput | ForEach-Object { Write-Host $_ }
}
finally {
    Remove-Item -LiteralPath $tempVerifyJs -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "============================================================"
Write-Ok "Cleanup complete. Refresh http://localhost:5173 and upload the resume again."
Write-Host "============================================================"