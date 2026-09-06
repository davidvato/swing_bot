@echo off
title Swing Trading Bot - Alpaca Paper Trading

setlocal EnableDelayedExpansion
set "BOT_DIR=%~dp0"
cd /d "%BOT_DIR%"

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Python no encontrado en PATH.
    echo  Instala Python 3.10+ desde: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo.
    echo  AVISO: No se encontro el archivo .env
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo  Se creo .env desde .env.example
    )
    echo  IMPORTANTE: Edita .env con tus claves de Alpaca Paper Trading
    echo  Ruta: %BOT_DIR%.env
    echo.
    pause
)

:menu
cls
echo.
echo  ===================================================
echo   SWING TRADING BOT - ALPACA PAPER TRADING
echo   Tickers: AAPL MSFT NVDA GOOG META AMZN TSLA
echo            AVGO PLTR AMD
echo   Estrategia: Mean Reversion  SMA-200 / RSI-4
echo   Riesgo    : Half-Kelly  (max 15%% por operacion)
echo   Cierre    : Viernes 15:45 EST
echo  ===================================================
echo.
echo   [1] Iniciar bot en produccion (loop principal)
echo   [2] Instalar / actualizar dependencias
echo   [3] Ejecutar pruebas unitarias (70 tests)
echo   [4] Verificar conexion con Alpaca
echo   [5] Dry-run: ver senales sin operar
echo   [6] Forzar liquidacion semanal (testing)
echo   [7] Ver ultimas lineas de bot.log
echo   [8] Abrir carpeta del bot en Explorer
echo   [0] Salir
echo.
set "OPT="
set /p "OPT= Elige una opcion [0-8]: "

if "%OPT%"=="1" goto do_run
if "%OPT%"=="2" goto do_setup
if "%OPT%"=="3" goto do_test
if "%OPT%"=="4" goto do_connection
if "%OPT%"=="5" goto do_dryrun
if "%OPT%"=="6" goto do_friday
if "%OPT%"=="7" goto do_log
if "%OPT%"=="8" goto do_explorer
if "%OPT%"=="0" exit /b 0

echo.
echo  Opcion no valida. Intenta de nuevo.
timeout /t 2 /nobreak >nul
goto menu

:do_run
cls
echo.
echo  ===================================================
echo   MODO PRODUCCION - PAPER TRADING (paper=True)
echo   Presiona Ctrl+C para detener el bot.
echo  ===================================================
echo.
python main.py
echo.
echo  Bot detenido. Codigo de salida: %errorlevel%
echo.
pause
goto menu

:do_setup
cls
echo.
echo  Instalando dependencias...
echo.
python -m pip install --upgrade pip
python -m pip install alpaca-py pandas python-dotenv schedule pytz pytest pytest-cov pytest-asyncio
echo.
if %errorlevel%==0 (
    echo  OK: Dependencias instaladas correctamente.
) else (
    echo  ERROR: Fallo la instalacion. Intenta como Administrador.
)
echo.
pause
goto menu

:do_test
cls
echo.
echo  Ejecutando 70 pruebas unitarias...
echo.
python -m pytest tests/ -v --tb=short
echo.
if %errorlevel%==0 (
    echo  OK: Todos los tests pasaron.
) else (
    echo  FALLO: Algunos tests fallaron. Revisa la salida anterior.
)
echo.
pause
goto menu

:do_connection
cls
echo.
echo  Verificando credenciales y equity de cuenta...
echo.
python main.py --test-connection
echo.
pause
goto menu

:do_dryrun
cls
echo.
echo  Analizando senales de mercado sin enviar ordenes...
echo.
python main.py --dry-run
echo.
pause
goto menu

:do_friday
cls
echo.
echo  ===================================================
echo   ADVERTENCIA: LIQUIDACION TOTAL DEL PORTAFOLIO
echo   Esto cerrara TODAS las posiciones abiertas.
echo  ===================================================
echo.
set "CONFIRM="
set /p "CONFIRM= Escribe SI para confirmar: "
if /i not "%CONFIRM%"=="SI" (
    echo  Cancelado.
    timeout /t 2 /nobreak >nul
    goto menu
)
echo.
python main.py --force-friday-close
echo.
pause
goto menu

:do_log
cls
echo.
echo  Ultimas 60 lineas de bot.log:
echo  ===================================================
echo.
if not exist "bot.log" (
    echo  El archivo bot.log no existe aun. Inicia el bot primero.
) else (
    powershell -Command "Get-Content 'bot.log' -Tail 60"
)
echo.
pause
goto menu

:do_explorer
explorer "%BOT_DIR%"
goto menu