@echo off
if defined ORCEXE_FAKE_PYTHON (
  "%ORCEXE_FAKE_PYTHON%" "%~dp0agent" %*
) else (
  python "%~dp0agent" %*
)
exit /b %errorlevel%
