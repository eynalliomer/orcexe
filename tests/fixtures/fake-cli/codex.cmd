@echo off
if defined ORCEXE_FAKE_PYTHON (
  "%ORCEXE_FAKE_PYTHON%" "%~dp0codex" %*
) else (
  python "%~dp0codex" %*
)
exit /b %errorlevel%
