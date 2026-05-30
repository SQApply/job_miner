@echo off
setlocal EnableExtensions
cd /d "%~dp0\.."

if not exist ".env" (
  echo [ERROR] .env file not found. Copy .env.example to .env and add Google OAuth credentials first.
  exit /b 1
)

for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
  if /I "%%A"=="KEYCLOAK_ADMIN" set "KEYCLOAK_ADMIN=%%B"
  if /I "%%A"=="KEYCLOAK_ADMIN_PASSWORD" set "KEYCLOAK_ADMIN_PASSWORD=%%B"
  if /I "%%A"=="GOOGLE_CLIENT_ID" set "GOOGLE_CLIENT_ID=%%B"
  if /I "%%A"=="GOOGLE_CLIENT_SECRET" set "GOOGLE_CLIENT_SECRET=%%B"
  if /I "%%A"=="JOB_MINER_KEYCLOAK_REALM" set "JOB_MINER_KEYCLOAK_REALM=%%B"
  if /I "%%A"=="JOB_MINER_KEYCLOAK_CLIENT_ID" set "JOB_MINER_KEYCLOAK_CLIENT_ID=%%B"
  if /I "%%A"=="JOB_MINER_KEYCLOAK_AUDIENCE" set "JOB_MINER_KEYCLOAK_AUDIENCE=%%B"
)

if "%KEYCLOAK_ADMIN%"=="" set "KEYCLOAK_ADMIN=admin"
if "%KEYCLOAK_ADMIN_PASSWORD%"=="" set "KEYCLOAK_ADMIN_PASSWORD=admin"
if "%JOB_MINER_KEYCLOAK_REALM%"=="" set "JOB_MINER_KEYCLOAK_REALM=job-miner"
if "%JOB_MINER_KEYCLOAK_CLIENT_ID%"=="" set "JOB_MINER_KEYCLOAK_CLIENT_ID=job-miner-web"
if "%JOB_MINER_KEYCLOAK_AUDIENCE%"=="" set "JOB_MINER_KEYCLOAK_AUDIENCE=job-miner-api"

if "%GOOGLE_CLIENT_ID%"=="" echo [WARN] GOOGLE_CLIENT_ID is empty in .env
if "%GOOGLE_CLIENT_SECRET%"=="" echo [WARN] GOOGLE_CLIENT_SECRET is empty in .env

where docker >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Docker is not available in PATH.
  exit /b 1
)

powershell -NoProfile -Command "(Get-Content -Raw 'infra\keycloak\bootstrap\apply_realm_config.sh') -replace '`r`n', '`n' | Set-Content -NoNewline 'infra\keycloak\bootstrap\apply_realm_config.sh'"

docker ps --format "{{.Names}}" | findstr /X "job_miner_keycloak" >nul
if errorlevel 1 (
  echo [INFO] Starting Keycloak through Docker Compose...
  docker compose --env-file .env -f docker-compose.mvp.yml up -d postgres keycloak
)

echo [INFO] Waiting for Keycloak container to accept admin commands...
set ATTEMPT=0
:wait_loop
set /a ATTEMPT+=1
docker exec job_miner_keycloak /opt/keycloak/bin/kcadm.sh config credentials --server http://localhost:8080 --realm master --user "%KEYCLOAK_ADMIN%" --password "%KEYCLOAK_ADMIN_PASSWORD%" >nul 2>nul
if not errorlevel 1 goto configured
if %ATTEMPT% GEQ 30 (
  echo [ERROR] Keycloak did not become ready after 30 attempts.
  docker compose -f docker-compose.mvp.yml logs --tail=80 keycloak
  exit /b 1
)
timeout /t 3 /nobreak >nul
goto wait_loop

:configured
echo [INFO] Applying realm configuration inside Keycloak...
docker exec -e KEYCLOAK_ADMIN="%KEYCLOAK_ADMIN%" -e KEYCLOAK_ADMIN_PASSWORD="%KEYCLOAK_ADMIN_PASSWORD%" -e GOOGLE_CLIENT_ID="%GOOGLE_CLIENT_ID%" -e GOOGLE_CLIENT_SECRET="%GOOGLE_CLIENT_SECRET%" -e JOB_MINER_KEYCLOAK_REALM="%JOB_MINER_KEYCLOAK_REALM%" -e JOB_MINER_KEYCLOAK_CLIENT_ID="%JOB_MINER_KEYCLOAK_CLIENT_ID%" -e JOB_MINER_KEYCLOAK_AUDIENCE="%JOB_MINER_KEYCLOAK_AUDIENCE%" job_miner_keycloak bash /opt/keycloak/bootstrap/apply_realm_config.sh
if errorlevel 1 (
  echo [ERROR] Failed to apply Keycloak realm configuration.
  exit /b 1
)

echo [SUCCESS] Keycloak is configured. Sign out once or clear localhost site data if your browser still has an old token.
endlocal
