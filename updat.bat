@echo off
:: Atualiza o Skin Searcher para a versao mais recente do main no GitHub.
:: Clone git: guarda de lado (git stash) o que tiveres por fazer commit e nunca apaga commits teus.
:: Pasta vinda de um ZIP: descarrega o ZIP mais recente e copia por cima.
:: A pasta catalog (chave, definicoes, avatares) nunca e tocada.

if /i "%~1"=="--from-temp" (
    set "REPO=%~2"
    goto :start
)
set "REPO=%~dp0"

:: O cmd le um .bat aos bocados enquanto o corre, e a atualizacao pode reescrever este
:: proprio ficheiro (quando o updat.bat muda no main). Por isso corre de uma copia em %TEMP%.
copy /y "%~f0" "%TEMP%\skinsearcher-update.bat" >nul 2>nul && "%TEMP%\skinsearcher-update.bat" --from-temp "%~dp0"
:: So chega aqui se a copia falhar: continua a partir deste ficheiro.

:start
setlocal
set "STASHED="
cd /d "%REPO%"
title Skin Searcher - Atualizar

echo ===================================================
echo   Skin Searcher - Atualizar para o main mais recente
echo ===================================================
echo.

:: 0. E um clone git? Se nao for (pasta vinda de um ZIP), usa o ZIP.
set "ISREPO="
where git >nul 2>nul
if not errorlevel 1 (
    git rev-parse --is-inside-work-tree >nul 2>nul
    if not errorlevel 1 set "ISREPO=1"
)
if not defined ISREPO goto :zipupdate

for /f %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%T"
for /f "delims=" %%B in ('git rev-parse --abbrev-ref HEAD') do set "BRANCH=%%B"

:: 1. Alteracoes por fazer commit: guardar de lado antes de mudar de branch
set "DIRTY="
for /f "delims=" %%L in ('git status --porcelain --untracked-files=no') do set "DIRTY=1"
if not defined DIRTY goto :fetch

echo Tens alteracoes que ainda nao estao em nenhum commit:
git status --short --untracked-files=no
echo.
choice /c GC /n /m "[G] Guardar de lado e continuar   [C] Cancelar: "
if errorlevel 2 goto :cancel
git stash push -m "updat.bat %STAMP% (branch %BRANCH%)"
if errorlevel 1 goto :fail
set "STASHED=1"
echo [OK] Guardadas no stash.
echo.

:: 2. Ir buscar as novidades
:fetch
echo [*] A ir buscar as novidades ao GitHub...
git fetch origin --prune
if errorlevel 1 (
    echo [ERRO] Nao consegui falar com o GitHub. Tens internet e acesso ao repositorio?
    goto :fail
)
git rev-parse --verify --quiet origin/main >nul
if errorlevel 1 (
    echo [ERRO] Nao encontrei o branch main no GitHub.
    goto :fail
)

:: 3. Passar para o main
if /i not "%BRANCH%"=="main" (
    echo [*] A mudar do branch %BRANCH% para o main...
    git checkout main
    if errorlevel 1 goto :fail
)

:: 4. Avancar o main ate ao do GitHub
for /f %%H in ('git rev-parse HEAD') do set "BEFORE=%%H"
git merge-base --is-ancestor HEAD origin/main
if errorlevel 1 goto :diverged
git merge --ff-only origin/main
if errorlevel 1 goto :fail
goto :done

:diverged
echo.
echo O teu main local tem commits que nao estao no GitHub:
git log --oneline origin/main..HEAD
echo.
echo [B] Guardar esses commits no branch backup/main-%STAMP% e por o main igual ao do GitHub
echo [C] Cancelar e nao mexer em mais nada
choice /c BC /n /m "Escolhe: "
if errorlevel 2 goto :cancel
git branch "backup/main-%STAMP%"
if errorlevel 1 goto :fail
git reset --hard origin/main
if errorlevel 1 goto :fail
echo [OK] Os teus commits ficaram no branch backup/main-%STAMP%.

:done
for /f %%H in ('git rev-parse HEAD') do set "AFTER=%%H"
echo.
if "%BEFORE%"=="%AFTER%" (
    echo [OK] Ja tinhas a versao mais recente do main.
) else (
    echo [OK] Atualizado. Novidades:
    git log --oneline %BEFORE%..%AFTER%
)
echo.
git log -1 --format="Agora em: %%h %%s"
call :pipinstall
call :stashnote
echo.
echo Para abrir a versao nova: start.bat
echo.
pause
exit /b 0

:: --- Pasta vinda de um ZIP: descarregar o ZIP do main e copiar por cima ---
:zipupdate
echo Esta pasta nao e um clone git, por isso vou descarregar o ZIP mais recente.
echo A pasta catalog (chave, definicoes, avatares) nao e tocada.
echo.
where powershell >nul 2>nul
if errorlevel 1 (
    echo [ERRO] O PowerShell nao foi encontrado. Descarrega o ZIP em github.com/tomasfi2006-del/skin-searcher
    goto :fail
)
set "PS=$ErrorActionPreference='Stop'; $tmp=Join-Path $env:TEMP 'skin-searcher-update'; if(Test-Path $tmp){Remove-Item $tmp -Recurse -Force}; New-Item -ItemType Directory $tmp | Out-Null; $zip=Join-Path $tmp 'main.zip'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest 'https://codeload.github.com/tomasfi2006-del/skin-searcher/zip/refs/heads/main' -OutFile $zip -UseBasicParsing; Expand-Archive $zip -DestinationPath $tmp -Force; Copy-Item (Join-Path $tmp 'skin-searcher-main\*') -Destination '.' -Recurse -Force; Remove-Item $tmp -Recurse -Force"
powershell -NoProfile -ExecutionPolicy Bypass -Command "%PS%"
if errorlevel 1 (
    echo [ERRO] Nao consegui descarregar ou copiar. Tens internet?
    goto :fail
)
echo [OK] Atualizado a partir do ZIP.
call :pipinstall
echo.
echo Para abrir a versao nova: start.bat
echo.
pause
exit /b 0

:cancel
echo.
echo Cancelado.
call :stashnote
echo.
pause
exit /b 1

:fail
echo.
echo [ERRO] A atualizacao parou (ve a mensagem acima). Nada foi apagado.
call :stashnote
echo.
pause
exit /b 1

:pipinstall
where python >nul 2>nul
if errorlevel 1 exit /b 0
python -m pip install -q -r "%REPO%requirements.txt" >nul 2>nul
exit /b 0

:stashnote
if not defined STASHED exit /b 0
echo.
echo As alteracoes que tinhas por fazer commit (no branch %BRANCH%) estao no stash.
echo Para as trazer de volta: git checkout %BRANCH% e depois git stash pop
exit /b 0
