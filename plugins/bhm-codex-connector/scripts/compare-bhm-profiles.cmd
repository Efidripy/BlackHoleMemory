@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bhm-profile.ps1" -Action compare
