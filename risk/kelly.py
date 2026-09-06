"""
risk/kelly.py — Criterio de Kelly Fraccional (Half-Kelly)
==========================================================
Implementa la variante Half-Kelly del Criterio de Kelly para calcular
el tamano optimo de posicion en funcion del capital disponible.

Formula del Criterio de Kelly:
    f* = (b*p - q) / b
    donde:
        p = probabilidad de ganar (win rate)
        q = 1 - p (probabilidad de perder)
        b = ratio ganancia/perdida (win/loss ratio)

Half-Kelly aplicado:
    half_kelly = 0.5 * f*

Con los parametros base del sistema (p=0.60, b=2.0):
    f* = (2.0 * 0.60 - 0.40) / 2.0 = 0.80 / 2.0 = 0.40
    half_kelly = 0.5 * 0.40 = 0.20  (20% del capital)
    Techo duro aplicado → max 15% → notional = equity * 0.15

Referencia: Kelly, J.L. (1956). "A New Interpretation of Information Rate."
Bell System Technical Journal, 35(4), 917-926.
"""

import logging

logger = logging.getLogger(__name__)


def half_kelly_fraction(
    p: float,
    b: float,
    kelly_multiplier: float = 0.5,
) -> float:
    """
    Calcula la fraccion de Kelly fraccional (Half-Kelly por defecto).

    Args:
        p: Probabilidad de ganar (win rate). Debe estar en (0, 1).
        b: Ratio de ganancia/perdida (win/loss ratio). Debe ser > 0.
        kelly_multiplier: Factor de reduccion de Kelly (default=0.5 para Half-Kelly).

    Returns:
        Fraccion del capital a arriesgar (valor entre 0.0 y 1.0).
        Retorna 0.0 si el edge es negativo o nulo (no conviene operar).

    Raises:
        ValueError: Si los parametros estan fuera de rango valido.

    Ejemplo:
        >>> half_kelly_fraction(p=0.60, b=2.0)
        0.2  # 20% del capital
    """
    if not (0 < p < 1):
        raise ValueError(f"El win rate 'p' debe estar en (0, 1). Recibido: p={p}")
    if b <= 0:
        raise ValueError(f"El win/loss ratio 'b' debe ser > 0. Recibido: b={b}")
    if not (0 < kelly_multiplier <= 1):
        raise ValueError(
            f"kelly_multiplier debe estar en (0, 1]. Recibido: {kelly_multiplier}"
        )

    q = 1.0 - p  # Probabilidad de perder

    # Formula de Kelly: f* = (b*p - q) / b
    full_kelly = (b * p - q) / b

    if full_kelly <= 0:
        logger.warning(
            f"Kelly negativo o nulo (f*={full_kelly:.4f}). "
            f"El edge es negativo con p={p}, b={b}. No se opera."
        )
        return 0.0

    fractional_kelly = kelly_multiplier * full_kelly

    logger.debug(
        f"Kelly calculado: p={p}, b={b} → f*={full_kelly:.4f}, "
        f"Half-Kelly={fractional_kelly:.4f} ({fractional_kelly*100:.1f}%)"
    )

    return fractional_kelly


def compute_notional(
    account_equity: float,
    p: float,
    b: float,
    max_pct: float,
    kelly_multiplier: float = 0.5,
) -> tuple[float, float]:
    """
    Calcula el importe nocional (en dolares) a invertir en una operacion.

    El calculo aplica Half-Kelly para determinar la fraccion optima del capital,
    luego impone un techo duro (max_pct) para prevenir sobreexposicion.

    Args:
        account_equity: Valor total de la cuenta en dolares (equity).
        p: Win rate historico base.
        b: Win/loss ratio.
        max_pct: Techo duro maximo como fraccion del equity (e.g., 0.15 para 15%).
        kelly_multiplier: Fraccion de Kelly a usar (default=0.5 para Half-Kelly).

    Returns:
        Tupla (notional, applied_fraction):
            - notional: Importe en USD a invertir (redondeado a 2 decimales).
            - applied_fraction: Fraccion efectivamente aplicada (despues del techo).

    Raises:
        ValueError: Si account_equity es negativo o cero.

    Ejemplo:
        >>> compute_notional(100_000.0, p=0.60, b=2.0, max_pct=0.15)
        (15000.0, 0.15)  # Kelly=20% → recortado al techo del 15%

        >>> compute_notional(10_000.0, p=0.50, b=1.5, max_pct=0.15)
        (833.33, 0.0833...)  # Kelly < 15% → sin recorte
    """
    if account_equity <= 0:
        raise ValueError(
            f"El equity de la cuenta debe ser positivo. Recibido: {account_equity}"
        )
    if not (0 < max_pct <= 1):
        raise ValueError(
            f"max_pct debe estar en (0, 1]. Recibido: {max_pct}"
        )

    # Calcular fraccion Half-Kelly
    kelly_frac = half_kelly_fraction(p=p, b=b, kelly_multiplier=kelly_multiplier)

    if kelly_frac == 0.0:
        logger.info("Kelly = 0: No se genera notional. La operacion no es rentable.")
        return 0.0, 0.0

    # Aplicar techo duro: nunca superar max_pct del equity
    applied_fraction = min(kelly_frac, max_pct)

    if kelly_frac > max_pct:
        logger.info(
            f"Techo duro aplicado: Kelly={kelly_frac*100:.1f}% recortado a "
            f"{max_pct*100:.1f}% (MAX_POSITION_PCT)."
        )

    # Calcular importe nocional en dolares
    notional = round(account_equity * applied_fraction, 2)

    logger.info(
        f"Notional calculado: equity=${account_equity:,.2f} × "
        f"{applied_fraction*100:.1f}% = ${notional:,.2f}"
    )

    return notional, applied_fraction
