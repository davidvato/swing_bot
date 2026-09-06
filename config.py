"""
config.py — Constantes globales del Swing Trading Bot
=====================================================
Centraliza TODOS los parametros configurables del sistema.
Para modificar el comportamiento del bot, editar UNICAMENTE este archivo.
Las credenciales sensibles se cargan exclusivamente desde .env via dotenv.
"""

# ─── Universo de Trading (Fallback Estatico) ─────────────────────────────────
# IMPORTANTE: Esta lista es el FALLBACK de emergencia del bot.
# El universo operativo real es calculado dinamicamente cada lunes
# por universe/screener.py (UniverseScreener). Solo se usa esta lista
# si el screener falla O si no existe cache previo al arrancar el bot.
TICKERS: list[str] = [
    "AAPL",   # Apple Inc.
    "MSFT",   # Microsoft Corporation
    "NVDA",   # NVIDIA Corporation
    "GOOG",   # Alphabet Inc.
    "META",   # Meta Platforms Inc.
    "AMZN",   # Amazon.com Inc.
    "TSLA",   # Tesla Inc.
    "AVGO",   # Broadcom Inc.
    "PLTR",   # Palantir Technologies
    "AMD",    # Advanced Micro Devices
]

# ─── Seleccion Dinamica de Universo (Universe Screener) ───────────────────────
UNIVERSE_SIZE: int = 10               # Numero de tickers a seleccionar (Top-N)
UNIVERSE_MIN_PRICE: float = 10.0      # Precio minimo de cierre ($) para filtrar
UNIVERSE_MIN_AVG_VOLUME: int = 5_000_000  # Volumen promedio minimo (30d) por dia
UNIVERSE_LOOKBACK_DAYS: int = 30      # Dias de historico para calcular metricas
UNIVERSE_CACHE_FILE: str = "universe_cache.json"  # Cache JSON del universo activo
# Hora de actualizacion del universo (lunes, 5 min despues de apertura)
UNIVERSE_UPDATE_TIME_EST: str = "09:35"  # Formato HH:MM (hora EST)

# ─── Parametros de Datos Historicos ──────────────────────────────────────────
LOOKBACK_DAYS: int = 200        # Sesiones de mercado a descargar (para SMA-200)
BATCH_SIZE: int = 5             # Tickers por lote (control de rate limit)
MARKET_REGIME_TICKER: str = "SPY" # Ticker para el filtro de régimen de mercado

# ─── Parametros de Indicadores Tecnicos ──────────────────────────────────────
SMA_PERIOD: int = 200           # Periodo de la Media Movil Simple (SMA)
RSI_PERIOD: int = 4             # Periodo del Indice de Fuerza Relativa (RSI)
RSI_OVERSOLD: float = 30.0      # Umbral RSI para condicion de sobreventa
CONSEC_DOWN_DAYS: int = 4       # Dias consecutivos a la baja (señal alternativa)

# ─── Criterio de Kelly Fraccional (Half-Kelly) ───────────────────────────────
KELLY_WIN_RATE: float = 0.60    # Tasa de ganancia historica base (p)
KELLY_WIN_LOSS_RATIO: float = 1.0   # Ratio Ganancia/Perdida promedio (b) ajustado por TP/SL 1:1
KELLY_FRACTION: float = 0.5     # Fraccion de Kelly aplicada (0.5 = Half-Kelly)
MAX_POSITION_PCT: float = 0.15  # Techo duro maximo por operacion (15% del equity)

# ─── Parametros de Salida (TP/SL emulados) ───────────────────────────────────
TAKE_PROFIT_PCT: float = 0.05   # Take-Profit: salida con +5% de ganancia
STOP_LOSS_PCT: float = 0.05     # Stop-Loss: salida con -5% de perdida
MAX_HOLD_DAYS: int = 5          # Dias maximos de holding antes de salida forzada
SL_GRACE_PERIOD_MINS: int = 15  # Periodo de gracia (minutos) en la apertura para SL

# ─── Planificador Semanal ─────────────────────────────────────────────────────
WEEKLY_CLOSE_TIME_EST: str = "15:45"    # Hora de liquidacion viernes (EST)
MARKET_OPEN_TIME_EST: str = "09:30"    # Apertura del mercado (EST)
MARKET_CLOSE_TIME_EST: str = "16:00"   # Cierre del mercado (EST)
TIMEZONE_EST: str = "America/New_York"  # Zona horaria de referencia

# ─── Rate Limiting de la API de Alpaca ───────────────────────────────────────
API_RATE_LIMIT_RPM: int = 200           # Limite maximo de requests por minuto
REQUEST_INTERVAL_SEC: float = 0.35      # Pausa minima entre requests (segundos)
MAX_RETRY_ATTEMPTS: int = 3             # Reintentos maximos ante HTTP 429
SUPERVISOR_POLL_INTERVAL_SEC: int = 60  # Intervalo de monitoreo TP/SL (segundos)

# ─── Almacenamiento de Datos ──────────────────────────────────────────────────
DB_PATH: str = "trades.db"              # Ruta de la base de datos SQLite
LOG_FILE: str = "bot.log"              # Archivo de log del sistema
# Nota: UNIVERSE_CACHE_FILE definido en la seccion Universe Screener

# ─── Tipos de Operacion (para el Trade Log) ──────────────────────────────────
TRADE_TYPE_BUY: str = "BUY"            # Compra inicial por señal
TRADE_TYPE_SELL_TP: str = "SELL_TP"   # Venta por Take-Profit (+10%)
TRADE_TYPE_SELL_SL: str = "SELL_SL"   # Venta por Stop-Loss (-5%)
TRADE_TYPE_SELL_EOW: str = "SELL_EOW" # Venta del viernes (End-Of-Week)
TRADE_TYPE_SELL_5D: str = "SELL_5D"   # Venta por maximo de 5 dias de holding
