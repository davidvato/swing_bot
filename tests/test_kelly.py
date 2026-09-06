"""
tests/test_kelly.py — Pruebas unitarias para el Criterio de Kelly Fraccional
=============================================================================
Valida la formula matematica del Half-Kelly, el techo duro del 15%,
y los casos limite (edge negativo, parametros invalidos).
"""

import pytest

from risk.kelly import half_kelly_fraction, compute_notional


class TestHalfKellyFraction:
    """Pruebas para la formula del Criterio de Kelly Fraccional."""

    def test_half_kelly_base_parameters(self):
        """
        Con p=0.60 y b=2.0, el Half-Kelly debe ser exactamente 0.20.

        Calculo verificado manualmente:
            f* = (2.0 * 0.60 - 0.40) / 2.0 = 0.80 / 2.0 = 0.40
            half_kelly = 0.5 * 0.40 = 0.20
        """
        result = half_kelly_fraction(p=0.60, b=2.0)
        assert abs(result - 0.20) < 1e-9, (
            f"Half-Kelly esperado=0.20, obtenido={result}"
        )

    def test_full_kelly_formula_correctness(self):
        """
        Verifica que la formula f* = (b*p - q) / b es implementada correctamente.
        La full_kelly con kelly_multiplier=1.0 debe ser exactamente f*.
        """
        p, b = 0.60, 2.0
        q = 1.0 - p
        expected_full_kelly = (b * p - q) / b  # = 0.40
        result_full_kelly = half_kelly_fraction(p=p, b=b, kelly_multiplier=1.0)
        assert abs(result_full_kelly - expected_full_kelly) < 1e-9, (
            f"Full Kelly esperado={expected_full_kelly}, obtenido={result_full_kelly}"
        )

    def test_quarter_kelly(self):
        """Quarter-Kelly (kelly_multiplier=0.25) debe ser f* * 0.25."""
        p, b = 0.60, 2.0
        expected = 0.25 * 0.40  # = 0.10
        result = half_kelly_fraction(p=p, b=b, kelly_multiplier=0.25)
        assert abs(result - expected) < 1e-9

    def test_kelly_returns_zero_when_edge_is_negative(self):
        """
        Cuando el edge de Kelly es negativo (f* <= 0), debe retornar 0.0.
        Con p=0.30 y b=0.5: f* = (0.5*0.30 - 0.70)/0.5 = (0.15-0.70)/0.5 = -1.10 < 0
        """
        result = half_kelly_fraction(p=0.30, b=0.5)
        assert result == 0.0, (
            f"Kelly negativo debe retornar 0.0, obtenido: {result}"
        )

    def test_kelly_at_breakeven_returns_zero(self):
        """
        En el punto de equilibrio (edge = 0), debe retornar 0.0.
        Con p=0.50 y b=1.0: f* = (1.0*0.50 - 0.50)/1.0 = 0.0
        """
        result = half_kelly_fraction(p=0.50, b=1.0)
        assert result == 0.0, (
            f"Kelly en breakeven debe retornar 0.0, obtenido: {result}"
        )

    def test_kelly_raises_on_invalid_win_rate_zero(self):
        """p=0 debe lanzar ValueError."""
        with pytest.raises(ValueError, match="win rate"):
            half_kelly_fraction(p=0.0, b=2.0)

    def test_kelly_raises_on_invalid_win_rate_one(self):
        """p=1 debe lanzar ValueError."""
        with pytest.raises(ValueError, match="win rate"):
            half_kelly_fraction(p=1.0, b=2.0)

    def test_kelly_raises_on_invalid_win_rate_negative(self):
        """p=-0.1 debe lanzar ValueError."""
        with pytest.raises(ValueError, match="win rate"):
            half_kelly_fraction(p=-0.1, b=2.0)

    def test_kelly_raises_on_invalid_ratio_zero(self):
        """b=0 debe lanzar ValueError."""
        with pytest.raises(ValueError, match="win/loss ratio"):
            half_kelly_fraction(p=0.60, b=0.0)

    def test_kelly_raises_on_invalid_ratio_negative(self):
        """b=-1 debe lanzar ValueError."""
        with pytest.raises(ValueError, match="win/loss ratio"):
            half_kelly_fraction(p=0.60, b=-1.0)

    def test_kelly_with_high_edge(self):
        """
        Con edge muy alto (p=0.80, b=5.0), el Half-Kelly debe ser positivo
        pero no mayor a 1.0 (limite matematico).
        f* = (5.0*0.80 - 0.20)/5.0 = (4.0 - 0.20)/5.0 = 3.80/5.0 = 0.76
        half_kelly = 0.38
        """
        result = half_kelly_fraction(p=0.80, b=5.0)
        assert 0.0 < result <= 1.0, f"Half-Kelly fuera de rango: {result}"
        assert abs(result - 0.38) < 1e-9


class TestComputeNotional:
    """Pruebas para el calculo del importe nocional con techo duro."""

    def test_notional_capped_at_15_percent_with_base_params(self):
        """
        Equity=$100,000, p=0.60, b=2.0 → Kelly=20% > techo 15%
        El notional debe ser $15,000 (techo duro aplicado).
        """
        equity = 100_000.0
        notional, applied_frac = compute_notional(
            account_equity=equity,
            p=0.60,
            b=2.0,
            max_pct=0.15,
        )
        assert notional == 15_000.0, (
            f"Notional esperado=$15,000 (techo 15%), obtenido=${notional}"
        )
        assert applied_frac == 0.15, (
            f"Fraccion aplicada esperada=0.15, obtenida={applied_frac}"
        )

    def test_notional_not_capped_when_kelly_below_max(self):
        """
        Equity=$10,000, p=0.51, b=1.1 → Kelly muy pequeño < 15%
        El notional no debe ser recortado por el techo.
        """
        p, b = 0.51, 1.1
        from risk.kelly import half_kelly_fraction
        kelly = half_kelly_fraction(p=p, b=b)

        if kelly > 0 and kelly < 0.15:
            notional, applied_frac = compute_notional(
                account_equity=10_000.0,
                p=p,
                b=b,
                max_pct=0.15,
            )
            expected_notional = round(10_000.0 * kelly, 2)
            assert abs(notional - expected_notional) < 0.01, (
                f"Notional esperado=${expected_notional}, obtenido=${notional}"
            )
            assert abs(applied_frac - kelly) < 1e-9

    def test_notional_zero_when_kelly_is_negative_edge(self):
        """
        Con edge negativo (p=0.30, b=0.5), el notional debe ser $0.
        """
        notional, applied_frac = compute_notional(
            account_equity=50_000.0,
            p=0.30,
            b=0.5,
            max_pct=0.15,
        )
        assert notional == 0.0, (
            f"Notional debe ser $0 con edge negativo, obtenido=${notional}"
        )
        assert applied_frac == 0.0

    def test_notional_raises_on_zero_equity(self):
        """Equity=0 debe lanzar ValueError."""
        with pytest.raises(ValueError, match="equity"):
            compute_notional(account_equity=0.0, p=0.60, b=2.0, max_pct=0.15)

    def test_notional_raises_on_negative_equity(self):
        """Equity negativo debe lanzar ValueError."""
        with pytest.raises(ValueError, match="equity"):
            compute_notional(account_equity=-1000.0, p=0.60, b=2.0, max_pct=0.15)

    def test_notional_exactly_two_decimal_places(self):
        """El notional siempre debe estar redondeado a 2 decimales."""
        notional, _ = compute_notional(
            account_equity=99_999.99,
            p=0.60,
            b=2.0,
            max_pct=0.15,
        )
        # Verificar que no tiene mas de 2 decimales
        rounded = round(notional, 2)
        assert notional == rounded, (
            f"Notional debe tener exactamente 2 decimales: ${notional}"
        )

    def test_hard_cap_prevents_overexposure(self):
        """
        Independientemente de los parametros de Kelly, el notional
        nunca debe superar max_pct del equity.
        """
        equity = 100_000.0
        max_pct = 0.15
        # Parametros con Kelly muy alto
        notional, _ = compute_notional(
            account_equity=equity,
            p=0.90,
            b=10.0,
            max_pct=max_pct,
        )
        assert notional <= equity * max_pct + 0.01, (
            f"Notional ${notional} supera el techo de ${equity * max_pct}"
        )

    def test_applied_fraction_never_exceeds_max_pct(self):
        """La fraccion aplicada nunca debe superar max_pct."""
        _, applied_frac = compute_notional(
            account_equity=100_000.0,
            p=0.95,
            b=20.0,  # Kelly extremadamente alto
            max_pct=0.15,
        )
        assert applied_frac <= 0.15, (
            f"Fraccion aplicada {applied_frac} supera el maximo 0.15"
        )
