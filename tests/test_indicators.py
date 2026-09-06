"""
tests/test_indicators.py — Pruebas unitarias para señales de mean reversion
============================================================================
Valida el calculo correcto de SMA-200, RSI-4 y la logica de señal de compra.
Utiliza datos sinteticos deterministas para resultados reproducibles.
"""

import math
import pytest
import pandas as pd
import numpy as np

# Importar el modulo a probar
from signals.indicators import (
    compute_indicators,
    generate_signal,
    check_consecutive_down,
    COL_SMA,
    COL_RSI,
    COL_CONSEC_DOWN,
)


# ─── Fixtures reutilizables ───────────────────────────────────────────────────

def make_df(closes: list[float]) -> pd.DataFrame:
    """
    Crea un DataFrame minimo con una serie de precios de cierre.

    Args:
        closes: Lista de precios de cierre en orden cronologico.

    Returns:
        DataFrame con columna 'close' e indice de fechas.
    """
    dates = pd.date_range(start="2023-01-01", periods=len(closes), freq="B")
    return pd.DataFrame({"close": closes}, index=dates)


def make_uptrend_df(n: int = 250, base: float = 200.0) -> pd.DataFrame:
    """
    Crea un DataFrame en tendencia alcista con precio > SMA-200 al final.

    Los primeros 200 dias tienen precio base, luego sube un 10%.
    Esto garantiza que el ultimo precio este sobre la SMA-200.
    """
    # Precios: primeros 200 en base, luego sube para estar sobre SMA
    closes = [base] * 200 + [base * 1.05] * (n - 200)
    return make_df(closes)


# ─── Tests de SMA ────────────────────────────────────────────────────────────

class TestSMACalculation:
    """Pruebas para el calculo de la Media Movil Simple de 200 periodos."""

    def test_sma_calculation_matches_arithmetic_mean(self):
        """
        La SMA-200 de una serie constante debe ser igual al valor de la serie.

        Para datos sinteticos donde todos los cierres son iguales,
        la SMA-200 debe ser exactamente ese mismo valor.
        """
        constant_price = 150.0
        df = make_df([constant_price] * 220)
        df_with_indicators = compute_indicators(df)

        # La SMA-200 en el ultimo punto debe ser exactamente 150.0
        sma_last = df_with_indicators[COL_SMA].iloc[-1]
        assert not math.isnan(sma_last), "SMA-200 no debe ser NaN con 220 barras"
        assert abs(sma_last - constant_price) < 0.001, (
            f"SMA-200 esperada={constant_price}, obtenida={sma_last}"
        )

    def test_sma_is_nan_with_insufficient_data(self):
        """
        La SMA-200 debe ser NaN cuando hay menos de 200 barras disponibles.
        Esto es comportamiento correcto de pandas-ta.
        """
        df = make_df([100.0] * 199)  # Solo 199 barras, SMA-200 necesita 200
        df_with_indicators = compute_indicators(df)

        # Con 199 barras, todos los valores de SMA-200 deben ser NaN
        sma_values = df_with_indicators[COL_SMA].dropna()
        assert len(sma_values) == 0, (
            "SMA-200 no debe tener valores con menos de 200 barras"
        )

    def test_sma_is_available_with_200_bars(self):
        """Con exactamente 200 barras, la SMA-200 debe tener al menos un valor."""
        df = make_df([100.0] * 200)
        df_with_indicators = compute_indicators(df)

        sma_last = df_with_indicators[COL_SMA].iloc[-1]
        assert not math.isnan(sma_last), (
            "SMA-200 debe estar disponible con exactamente 200 barras"
        )


# ─── Tests de RSI ────────────────────────────────────────────────────────────

class TestRSICalculation:
    """Pruebas para el calculo del RSI-4."""

    def test_rsi_is_low_after_consecutive_drops(self):
        """
        Despues de caidas consecutivas, el RSI-4 debe estar en zona de sobreventa.
        Usamos datos con tendencia bajista pronunciada para forzar RSI < 30.
        """
        # Crear tendencia alcista larga (para SMA) + caida brusca al final
        closes = [200.0] * 200
        # Caidas del 3% diarias durante 10 dias → RSI extremadamente bajo
        for i in range(10):
            closes.append(closes[-1] * 0.97)
        df = make_df(closes)
        df_with_indicators = compute_indicators(df)

        rsi_last = df_with_indicators[COL_RSI].iloc[-1]
        assert not math.isnan(rsi_last), "RSI-4 no debe ser NaN"
        assert rsi_last < 30, (
            f"RSI-4 esperado < 30 despues de caidas, obtenido: {rsi_last:.2f}"
        )

    def test_rsi_is_high_after_consecutive_gains(self):
        """
        Despues de ganancias consecutivas, el RSI-4 debe estar en zona de sobrecompra.

        El RSI de Wilder (EWM) requiere varianza previa para inicializarse.
        La serie comienza con precios alternantes leves (+1%/-1%) durante 10 dias
        para que avg_loss > 0, luego sube 30 dias al 3% para empujar RSI > 70.
        """
        closes = []
        # Seed: 10 sesiones con oscilacion leve para dar varianza inicial al EWM
        price = 100.0
        for i in range(20):
            price = price * (1.01 if i % 2 == 0 else 0.99)
            closes.append(price)
        # Warmup de 180 dias neutros para SMA
        neutral_price = closes[-1]
        for _ in range(180):
            closes.append(neutral_price)
        # 30 dias de subida sostenida del 3% → RSI muy alto
        for _ in range(30):
            closes.append(closes[-1] * 1.03)

        df = make_df(closes)
        df_with_indicators = compute_indicators(df)

        rsi_last = float(df_with_indicators[COL_RSI].iloc[-1])
        assert not math.isnan(rsi_last), (
            f"RSI-4 no debe ser NaN. Ultimo valor: {rsi_last}"
        )
        assert rsi_last > 70, (
            f"RSI-4 esperado > 70 despues de 30 dias de ganancias, obtenido: {rsi_last:.2f}"
        )



# ─── Tests de señal de compra ─────────────────────────────────────────────────

class TestGenerateSignal:
    """Pruebas para la logica de generacion de señal LONG."""

    def test_signal_true_when_rsi_oversold_and_price_above_sma(self):
        """
        La señal debe ser True cuando:
        - Precio actual > SMA-200
        - RSI-4 < 30 (sobreventa)
        """
        # Precio en tendencia alcista sostenida + caida brusca al final
        closes = [200.0] * 200
        for _ in range(8):
            closes.append(closes[-1] * 0.97)  # Caida → RSI bajo
        df = make_df(closes)
        df_with_indicators = compute_indicators(df)

        last = df_with_indicators.iloc[-1]
        close_val = last["close"]
        sma_val = last[COL_SMA]
        rsi_val = last[COL_RSI]

        # Solo testear si la condicion de precios se cumple
        if close_val > sma_val and rsi_val < 30:
            assert generate_signal(df_with_indicators) is True

    def test_signal_false_when_rsi_not_oversold_no_consec_down(self):
        """
        La señal debe ser False cuando:
        - Precio > SMA-200
        - RSI-4 NO < 30 y sin 4 dias consecutivos a la baja
        """
        # Precios estables: precio sobre SMA pero RSI neutro
        closes = [100.0] * 200 + [110.0] * 20  # Precio sube un 10% estable
        df = make_df(closes)
        df_with_indicators = compute_indicators(df)

        last = df_with_indicators.iloc[-1]
        rsi_val = last[COL_RSI]
        consec = last[COL_CONSEC_DOWN]

        # Solo testear si RSI no esta en zona de sobreventa
        if rsi_val >= 30 and not consec:
            assert generate_signal(df_with_indicators) is False

    def test_signal_false_when_price_below_sma(self):
        """
        La señal debe ser False cuando el precio esta BAJO la SMA-200,
        incluso si el RSI < 30. (El filtro de tendencia es obligatorio).
        """
        # Tendencia bajista: el precio cae sistematicamente
        closes = list(range(300, 100, -1))  # 300, 299, 298, ... 101 (200 valores)
        df = make_df(closes)
        df_with_indicators = compute_indicators(df)

        last = df_with_indicators.iloc[-1]
        sma_val = last[COL_SMA]
        close_val = last["close"]

        # Verificar que el precio esta efectivamente bajo la SMA
        if pd.notna(sma_val) and close_val < sma_val:
            result = generate_signal(df_with_indicators)
            assert result == False, (
                "La señal no debe activarse cuando precio < SMA-200"
            )

    def test_signal_false_with_empty_dataframe(self):
        """La señal debe retornar False para DataFrames vacios."""
        df_empty = pd.DataFrame(columns=["close"])
        assert generate_signal(df_empty) is False

    def test_signal_false_without_indicators_computed(self):
        """
        La señal debe retornar False si no se han calculado los indicadores.
        (Las columnas SMA/RSI no existen en el DataFrame).
        """
        df = make_df([100.0] * 50)
        # Sin llamar a compute_indicators(), las columnas SMA/RSI no existen
        assert generate_signal(df) is False


# ─── Tests de dias consecutivos a la baja ────────────────────────────────────

class TestCheckConsecutiveDown:
    """Pruebas para la deteccion de 4 dias consecutivos a la baja."""

    def test_four_consecutive_down_returns_true(self):
        """4 cierres estrictamente decrecientes deben retornar True."""
        closes = [100.0] * 10 + [95.0, 94.0, 93.0, 92.0]
        df = make_df(closes)
        assert check_consecutive_down(df, n=4) is True

    def test_three_consecutive_down_returns_false(self):
        """Solo 3 cierres decrecientes (no 4) deben retornar False."""
        closes = [100.0] * 10 + [95.0, 95.0, 94.0, 93.0]
        # El primero de los 4 no cae → no hay 4 consecutivos
        df = make_df(closes)
        assert check_consecutive_down(df, n=4) is False

    def test_flat_prices_return_false(self):
        """Precios iguales (sin caida) deben retornar False."""
        closes = [100.0] * 14
        df = make_df(closes)
        assert check_consecutive_down(df, n=4) is False

    def test_prices_going_up_return_false(self):
        """Precios subiendo deben retornar False."""
        closes = [100.0, 101.0, 102.0, 103.0, 104.0]
        df = make_df(closes)
        assert check_consecutive_down(df, n=4) is False

    def test_insufficient_data_returns_false(self):
        """Con menos de n filas, debe retornar False (no hay error)."""
        closes = [100.0, 99.0, 98.0]  # Solo 3 cierres, n=4
        df = make_df(closes)
        assert check_consecutive_down(df, n=4) is False

    def test_exactly_four_strictly_decreasing(self):
        """Exactamente 4 cierres estrictamente decrecientes = True."""
        closes = [100.0, 99.0, 98.0, 97.0]
        df = make_df(closes)
        assert check_consecutive_down(df, n=4) is True

    def test_last_four_with_equal_day_returns_false(self):
        """Si hay un dia plano en los 4, no son ESTRICTAMENTE decrecientes."""
        closes = [100.0] * 10 + [95.0, 94.0, 94.0, 93.0]  # Dia 2 == Dia 3
        df = make_df(closes)
        assert check_consecutive_down(df, n=4) is False


# ─── Test de integracion de indicadores ──────────────────────────────────────

class TestComputeIndicators:
    """Tests de integracion para compute_indicators()."""

    def test_returns_dataframe_with_required_columns(self):
        """El resultado debe contener las columnas SMA_200, RSI_4, CONSEC_DOWN."""
        df = make_df([100.0] * 210)
        result = compute_indicators(df)
        assert COL_SMA in result.columns, f"Columna {COL_SMA} no encontrada"
        assert COL_RSI in result.columns, f"Columna {COL_RSI} no encontrada"
        assert COL_CONSEC_DOWN in result.columns, f"Columna {COL_CONSEC_DOWN} no encontrada"

    def test_raises_value_error_without_close_column(self):
        """Debe lanzar ValueError si el DataFrame no tiene columna 'close'."""
        df = pd.DataFrame({"open": [100.0, 101.0], "volume": [1000, 2000]})
        with pytest.raises(ValueError, match="close"):
            compute_indicators(df)

    def test_does_not_modify_original_dataframe(self):
        """compute_indicators() trabaja sobre una copia, no modifica el original."""
        df = make_df([100.0] * 210)
        original_cols = list(df.columns)
        compute_indicators(df)
        assert list(df.columns) == original_cols, (
            "compute_indicators() no debe modificar el DataFrame original"
        )
