"""Deflated Sharpe Ratio (DSR) — Bailey & Lopez de Prado (2014).

Referencia: Bailey, D.H. & Lopez de Prado, M. (2014).
  "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting
  and Non-Normality". Journal of Portfolio Management, 40(5), 94-107.

Formula implementada (ecuacion 8 del paper):
  PSR(SR*) = Phi( (SR_hat - SR*) * sqrt(T-1) / sigma_hat_SR )

donde:
  SR_hat    = Sharpe observado del trial a evaluar.
  SR*       = Sharpe esperado bajo H0 para el mejor de N trials independientes.
              Aproximado como:
              SR* ≈ ((1-gamma)*Phi^{-1}(1-1/N) + gamma*Phi^{-1}(1-1/(N*e))) * sigma_SR/sqrt(T)
              donde gamma = constante de Euler-Mascheroni ≈ 0.5772.
  sigma_SR  = desviacion estandar de los Sharpes OOS observados.
  T         = numero de observaciones del periodo de test.
  Phi       = CDF normal estandar.

El DSR es PSR(SR*): probabilidad de que el Sharpe IS sea positivo despues de
descontar la seleccion entre N configuraciones.

Nota: cuando N=1 no hay seleccion; DSR = PSR(0) (probabilidad de SR > 0).
Cuando N es grande, SR* crece y DSR cae, penalizando el overfitting.

Invariante de correcto uso: DSR < Sharpe_IS para N > 1 cuando sigma_SR > 0.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

_GAMMA = 0.5772156649   # constante de Euler-Mascheroni
_SQRT_2PI = math.sqrt(2 * math.pi)
_ZERO = Decimal("0")
_FOUR = Decimal("4")


def _phi(x: float) -> float:
    """CDF normal estandar (usando math.erfc para precision)."""
    return 0.5 * math.erfc(-x / math.sqrt(2))


def _phi_inv(p: float) -> float:
    """Inversa de la CDF normal estandar — racional A&S 26.2.17, |error| < 4.5e-4."""
    if p <= 0.0:
        return -8.0
    if p >= 1.0:
        return 8.0
    # Abramowitz & Stegun 26.2.17: approximation valid for p in (0, 1)
    # Sign flip for p < 0.5
    sign = 1.0 if p >= 0.5 else -1.0
    q = p if p >= 0.5 else 1.0 - p
    t = math.sqrt(-2.0 * math.log(1.0 - q))
    c0, c1, c2 = 2.515517, 0.802853, 0.010328
    d1, d2, d3 = 1.432788, 0.189269, 0.001308
    num = c0 + c1 * t + c2 * t * t
    den = 1.0 + d1 * t + d2 * t * t + d3 * t * t * t
    return sign * (t - num / den)


@dataclass(frozen=True)
class DSRResult:
    """Resultado del calculo del Deflated Sharpe Ratio."""

    dsr: Decimal                # Deflated Sharpe Ratio en [0, 1]
    sr_star: Decimal            # Sharpe esperado bajo H0 para N trials
    sr_obs: Decimal             # Sharpe OOS observado del mejor trial
    n_trials: int               # numero total de trials usados
    sigma_sr: Decimal           # desviacion estandar de Sharpes OOS
    t_obs: int                  # observaciones del periodo de test
    note: str = ""              # advertencia si hay pocos datos


def compute_dsr(
    sharpes_oos: list[Decimal],
    t_obs: int,
) -> DSRResult:
    """Calcula el DSR para una lista de Sharpes OOS.

    Args:
        sharpes_oos: lista de Sharpe ratios OOS de todos los trials
                     COMPLETED para la misma estrategia.
        t_obs:       numero de observaciones del periodo de test
                     (tipicamente test_days para series diarias).

    Returns:
        DSRResult con el DSR y los inputs intermedios para trazabilidad.
    """
    n = len(sharpes_oos)
    note = ""

    if n == 0:
        return DSRResult(
            dsr=_ZERO, sr_star=_ZERO, sr_obs=_ZERO,
            n_trials=0, sigma_sr=_ZERO, t_obs=t_obs,
            note="Sin trials disponibles",
        )

    sr_obs = max(sharpes_oos)
    sr_obs_f = float(sr_obs)

    if n == 1:
        # Sin seleccion: PSR(0) = probabilidad de que SR > 0
        dsr_f = 0.0 if t_obs < 2 else _phi(sr_obs_f * math.sqrt(t_obs - 1))
        return DSRResult(
            dsr=Decimal(str(round(dsr_f, 6))),
            sr_star=_ZERO,
            sr_obs=sr_obs,
            n_trials=1,
            sigma_sr=_ZERO,
            t_obs=t_obs,
            note="N=1: DSR = PSR(0), sin correccion por seleccion",
        )

    # --- sigma_SR: desviacion estandar de los Sharpes OOS ---
    sr_floats = [float(s) for s in sharpes_oos]
    mean_sr = sum(sr_floats) / n
    variance_sr = sum((s - mean_sr) ** 2 for s in sr_floats) / (n - 1)
    sigma_sr_f = math.sqrt(variance_sr)

    if sigma_sr_f < 1e-12:
        note = "sigma_SR ~ 0: todos los trials tienen el mismo Sharpe"
        sigma_sr_f = 1e-12

    # --- SR* esperado bajo H0 (Bailey & Lopez de Prado, ec. 8) ---
    # SR* = sigma_SR / sqrt(T) * [(1-gamma)*Phi^{-1}(1-1/N) + gamma*Phi^{-1}(1-1/(N*e))]
    if t_obs < 2:
        sr_star_f = 0.0
    else:
        sqrt_t = math.sqrt(t_obs)
        p1 = 1.0 - 1.0 / n
        p2 = 1.0 - 1.0 / (n * math.e)
        # Clamp para evitar Phi_inv en limites
        p1 = max(1e-9, min(1 - 1e-9, p1))
        p2 = max(1e-9, min(1 - 1e-9, p2))
        z1 = _phi_inv(p1)
        z2 = _phi_inv(p2)
        sr_star_f = (sigma_sr_f / sqrt_t) * ((1 - _GAMMA) * z1 + _GAMMA * z2)

    # --- PSR(SR*): probabilidad de que SR_hat > SR* ---
    # PSR = Phi( (SR_hat - SR*) * sqrt(T-1) / sigma_SR )
    if t_obs < 2:
        dsr_f = 0.0
    else:
        z = (sr_obs_f - sr_star_f) * math.sqrt(t_obs - 1) / sigma_sr_f
        dsr_f = _phi(z)

    if n < 5:
        note = note + ("; " if note else "") + f"N={n} < 5: DSR estimado con pocos trials"

    return DSRResult(
        dsr=Decimal(str(round(dsr_f, 6))),
        sr_star=Decimal(str(round(sr_star_f, 6))),
        sr_obs=sr_obs,
        n_trials=n,
        sigma_sr=Decimal(str(round(sigma_sr_f, 6))),
        t_obs=t_obs,
        note=note,
    )
