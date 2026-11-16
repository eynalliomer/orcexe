@echo off
@where py >nul 2>&1
@if errorlevel 1 goto python
@py -3 "%~dp0orcexe" %*
@exit /b %errorlevel%
:python
@python "%~dp0orcexe" %*
@exit /b %errorlevel%
