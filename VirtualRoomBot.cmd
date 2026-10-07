@if not exist "%~dp0runtime\python\python.exe" (echo Run Setup.cmd first. & pause & exit /b 1)
@"%~dp0runtime\python\python.exe" "%~dp0installer\vrb.py" menu & exit /b
