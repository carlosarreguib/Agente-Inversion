"""Probability of Backtest Overfitting (PBO) via CSCV — T9 Parte C.

Implementa el método CSCV (Combinatorially Symmetric Cross-Validation) de
Bailey & Lopez de Prado (2014) con S=8 subperíodos y C(8,4)=70 splits.

Definición de PBO:
  PBO = fracción de splits IS/OOS donde la mejor config IS obtiene
        Sharpe OOS por debajo del Sharpe OOS mediano del split.

Umbral de robustez (criterio cuantitativo D):
  PBO <= 0.70  →  no sobreajustado
  PBO >  0.70  →  probable overfitting

Referencia: Bailey, D.H. & Lopez de Prado, M. (2014).
  "The Probability of Backtest Overfitting". Journal of Computational Finance.

Sin dependencias externas. Solo stdlib.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from decimal import Decimal

_ZERO = Decimal("0")

S = 8                  # número de subperíodos
PBO_OVERFIT_THRESHOLD = 0.70   # PBO > 0.70 → sobreajustado


@dataclass(frozen=True)
class PBOResult:
    """Resultado del análisis CSCV."""

    pbo: float                      # fracción de splits con Sharpe OOS < mediana IS
    n_splits: int                   # C(S, S//2) splits evaluados
    n_overfit_splits: int           # splits donde el ganador IS pierde OOS
    s_subperiods: int               # S usado
    is_overfit: bool                # True si PBO > PBO_OVERFIT_THRESHOLD
    note: str = ""


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _generate_cscv_splits(
    n_configs: int,
    s: int = S,
) -> list[tuple[list[int], list[int]]]:
    """Genera los C(S, S//2) splits IS/OOS sobre índices de subperíodos.

    Cada split: (is_subperiod_indices, oos_subperiod_indices).
    Devuelve lista de hasta C(S, S//2) splits (70 para S=8).
    """
    half = s // 2
    all_periods = list(range(s))
    splits = []
    for is_combo in itertools.combinations(all_periods, half):
        is_set = set(is_combo)
        oos_set = set(all_periods) - is_set
        splits.append((list(is_combo), list(oos_set)))
    return splits


def compute_pbo(
    sharpe_matrix: list[list[float]],
    s: int = S,
) -> PBOResult:
    """Calcula el PBO usando CSCV.

    Args:
        sharpe_matrix: matriz [config_idx][subperiod_idx] de Sharpe ratios.
                       shape: (n_configs, s_subperiods).
                       Si un subperíodo no tiene datos, usar 0.0.
        s:             número de subperíodos (por defecto 8).

    Returns:
        PBOResult con la fracción de overfitting y estadísticas.
    """
    n_configs = len(sharpe_matrix)
    if n_configs == 0:
        return PBOResult(
            pbo=0.0, n_splits=0, n_overfit_splits=0,
            s_subperiods=s, is_overfit=False,
            note="Sin configuraciones para evaluar",
        )

    actual_s = len(sharpe_matrix[0]) if sharpe_matrix else s
    if actual_s < 2:
        return PBOResult(
            pbo=0.0, n_splits=0, n_overfit_splits=0,
            s_subperiods=actual_s, is_overfit=False,
            note="Insuficientes subperíodos para CSCV",
        )

    splits = _generate_cscv_splits(n_configs=n_configs, s=actual_s)
    n_overfit = 0

    for is_indices, oos_indices in splits:
        # Sharpe IS promedio por config sobre subperíodos IS
        is_sharpes: list[float] = []
        for cfg_idx in range(n_configs):
            avg_is = sum(sharpe_matrix[cfg_idx][p] for p in is_indices) / len(is_indices)
            is_sharpes.append(avg_is)

        # Config ganadora en IS
        best_cfg = max(range(n_configs), key=lambda i: is_sharpes[i])

        # Sharpe OOS del ganador IS sobre subperíodos OOS
        best_oos = sum(
            sharpe_matrix[best_cfg][p] for p in oos_indices
        ) / len(oos_indices)

        # Sharpe OOS de todas las configs en subperíodos OOS
        all_oos: list[float] = []
        for cfg_idx in range(n_configs):
            avg_oos = sum(sharpe_matrix[cfg_idx][p] for p in oos_indices) / len(oos_indices)
            all_oos.append(avg_oos)

        median_oos = _median(all_oos)

        if best_oos < median_oos:
            n_overfit += 1

    n_splits = len(splits)
    pbo = n_overfit / n_splits if n_splits > 0 else 0.0

    note = ""
    if n_configs < 5:
        note = f"n_configs={n_configs} < 5: PBO estimado con pocas configuraciones"

    return PBOResult(
        pbo=pbo,
        n_splits=n_splits,
        n_overfit_splits=n_overfit,
        s_subperiods=actual_s,
        is_overfit=pbo > PBO_OVERFIT_THRESHOLD,
        note=note,
    )


def build_sharpe_matrix_from_subperiods(
    sharpes_by_config: list[list[float]],
    s: int = S,
) -> list[list[float]]:
    """Construye la matriz [config][subperiod] dividiendo el histórico en S partes.

    Args:
        sharpes_by_config: lista de listas de Sharpe ratios.
                           sharpes_by_config[i] = lista de Sharpes para config i
                           (uno por cada observación/fold).
        s:                 número de subperíodos.

    Returns:
        Matriz shape (n_configs, s) con el Sharpe promedio por subperíodo.
        Las observaciones se reparten uniformemente entre subperíodos.
    """
    if not sharpes_by_config:
        return []

    matrix: list[list[float]] = []
    for config_sharpes in sharpes_by_config:
        n_obs = len(config_sharpes)
        if n_obs == 0:
            matrix.append([0.0] * s)
            continue
        subperiod_size = max(1, math.ceil(n_obs / s))
        row: list[float] = []
        for period_idx in range(s):
            start = period_idx * subperiod_size
            end = min(start + subperiod_size, n_obs)
            chunk = config_sharpes[start:end]
            avg = sum(chunk) / len(chunk) if chunk else 0.0
            row.append(avg)
        matrix.append(row)

    return matrix
