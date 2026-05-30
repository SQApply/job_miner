@echo off
setlocal EnableExtensions
cd /d "%~dp0\.."

set "EMAIL=%~1"
if "%EMAIL%"=="" set "EMAIL=abhinav.mg.aws@gmail.com"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0reset_candidate_resume_test_data.ps1" -Email "%EMAIL%" -RepoRoot "%CD%"
exit /b %ERRORLEVEL%
