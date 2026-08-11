"""Walk-forward validation engine (T2.4).

Genera folds train/test sobre una lista de dias habiles con purga y embargo.

Terminologia (Bailey & Lopez de Prado, 2018):
  - train:   periodo de entrenamiento (in-sample).
  - test:    periodo de validacion (out-of-sample). Nunca solapado con train.
  - purga:   se eliminan los ultimos embargo_days del train para evitar que
             labels del final del train contaminen el test (overlap de features).
  - embargo: los primeros embargo_days del periodo inmediatamente posterior
             al train tambien se saltan. En nuestro caso los absorbemos en
             la purga: el test comienza exactamente en el dia siguiente al
             ultimo dia de train (tras purga), sin gap adicional.

Invariante §3.1: el backtest de cada fold recibe como trading_days solo los
dias del intervalo [train_start, train_end - embargo_days], garantizando que
ninguna barra del test se filtra al entrenamiento.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date  # noqa: TCH003
from pathlib import Path  # noqa: TCH003
from typing import Any

import yaml


@dataclass(frozen=True)
class WalkForwardConfig:
    """Parametros del walk-forward, cargables desde config/research.yaml."""

    train_days: int = 756
    test_days: int = 252
    step_days: int = 63
    embargo_days: int = 5

    @classmethod
    def from_yaml(cls, path: Path) -> WalkForwardConfig:
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        wf = raw.get("walk_forward", {})
        return cls(
            train_days=int(wf.get("train_days", 756)),
            test_days=int(wf.get("test_days", 252)),
            step_days=int(wf.get("step_days", 63)),
            embargo_days=int(wf.get("embargo_days", 5)),
        )


@dataclass(frozen=True)
class Fold:
    """Un fold de walk-forward con fechas exactas.

    train_days:  lista de dias habiles de entrenamiento (ya purgada).
    test_days:   lista de dias habiles de test (out-of-sample).
    fold_id:     indice del fold (0-based).

    train_start / train_end: primer/ultimo dia del intervalo de train original
                             (antes de purga), para trazabilidad en trials.db.
    test_start / test_end:   primer/ultimo dia del intervalo de test.
    embargo_days_applied:    numero de dias eliminados del final del train.
    """

    fold_id: int
    train_days: tuple[date, ...]
    test_days: tuple[date, ...]
    train_start: date
    train_end: date         # ultimo dia del train original (antes de purga)
    test_start: date
    test_end: date
    embargo_days_applied: int


def generate_folds(
    trading_days: list[date],
    config: WalkForwardConfig,
) -> list[Fold]:
    """Genera todos los folds posibles sobre la lista de dias habiles.

    Args:
        trading_days: lista ordenada de dias de trading disponibles.
        config:       parametros de ventana.

    Returns:
        Lista de Fold ordenada cronologicamente. Puede ser vacia si no hay
        suficientes dias para al menos un fold completo.
    """
    n = len(trading_days)
    min_required = config.train_days + config.test_days
    if n < min_required:
        return []

    folds: list[Fold] = []
    fold_id = 0
    train_start_idx = 0

    while True:
        train_end_idx = train_start_idx + config.train_days  # exclusive
        test_end_idx = train_end_idx + config.test_days       # exclusive

        if test_end_idx > n:
            break

        # Indice del ultimo dia de train TRAS purga (inclusive)
        purged_train_end_idx = train_end_idx - config.embargo_days
        if purged_train_end_idx <= train_start_idx:
            # Purga deja el train vacio — config invalida
            break

        train_slice = trading_days[train_start_idx:purged_train_end_idx]
        test_slice = trading_days[train_end_idx:test_end_idx]

        folds.append(Fold(
            fold_id=fold_id,
            train_days=tuple(train_slice),
            test_days=tuple(test_slice),
            train_start=trading_days[train_start_idx],
            train_end=trading_days[train_end_idx - 1],
            test_start=trading_days[train_end_idx],
            test_end=trading_days[test_end_idx - 1],
            embargo_days_applied=config.embargo_days,
        ))

        train_start_idx += config.step_days
        fold_id += 1

    return folds


def business_days_range(start: date, end: date) -> list[date]:
    """Genera dias habiles (lun-vie) entre start y end inclusivos."""
    from datetime import timedelta

    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days
