@echo off
if defined ORCEXE_FAKE_PYTHON (
  "%ORCEXE_FAKE_PYTHON%" "%~dp0claude" %*
) else (
  python "%~dp0claude" %*
)
exit /b %errorlevel%
