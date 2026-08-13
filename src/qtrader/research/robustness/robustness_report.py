"""Generador del informe de robustez — T9.

Orquesta las Partes A, B y C y produce un informe Markdown con veredicto
ROBUSTO / CONDICIONAL / FRÁGIL según los criterios de CLAUDE.md §3.

Criterios cuantitativos (4 en total):
  A. Sensibilidad: >= 60 % de las 135 configs con Sharpe OOS > 0 Y MaxDD < 50 %
  B. Monte Carlo: P5(Sharpe) del block bootstrap >= -0.5
  C. PBO: PBO <= 0.70 (no sobreajustado)
  D. DSR global: DSR > 0.3 sobre el conjunto de trials

Veredicto:
  ROBUSTO:     4/4 criterios pass + todos los escenarios adversos pass
  CONDICIONAL: 3/4 criterios pass + todos los escenarios adversos pass
  FRÁGIL:      < 3 criterios pass O algún escenario adverso falla

El informe detalla CUÁL criterio falló y POR QUÉ.

Nota: el informe muestra Sharpe bruto Y Sharpe neto (con haircut por
survivorship bias residual), tal como exige la invariante §3.11.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from qtrader.research.robustness.monte_carlo import MonteCarloResult
from qtrader.research.robustness.pbo import PBOResult
from qtrader.research.robustness.sensitivity import SensitivityReport

_log = logging.getLogger(__name__)

# Haircut por survivorship bias residual + tracking error proxy→UCITS
# Declarado explícitamente per invariante §3.11.
SURVIVORSHIP_BIAS_HAIRCUT = Decimal("0.10")  # 10 % de reducción del Sharpe

DSR_THRESHOLD = Decimal("0.3")


@dataclass(frozen=True)
class CriterionResult:
    """Resultado de un criterio cuantitativo individual."""

    name: str
    passed: bool
    value: str           # valor observado (para mostrar en informe)
    threshold: str       # umbral requerido
    note: str = ""


@dataclass(frozen=True)
class RobustnessVerdict:
    """Veredicto final del análisis de robustez."""

    verdict: str                           # ROBUSTO | CONDICIONAL | FRÁGIL
    criteria: tuple[CriterionResult, ...]  # 4 criterios cuantitativos
    n_criteria_passed: int
    all_scenarios_passed: bool
    explanation: str                       # texto explicativo del veredicto


def _compute_verdict(
    criteria: list[CriterionResult],
    all_scenarios_passed: bool,
) -> RobustnessVerdict:
    n_passed = sum(1 for c in criteria if c.passed)
    failed_names = [c.name for c in criteria if not c.passed]

    if n_passed >= 4 and all_scenarios_passed:
        verdict = "ROBUSTO"
        explanation = "Todos los criterios cuantitativos y escenarios adversos pasan."
    elif n_passed >= 3 and all_scenarios_passed:
        verdict = "CONDICIONAL"
        failed_str = ", ".join(failed_names)
        explanation = (
            f"3 de 4 criterios cuantitativos pasan. "
            f"Criterio fallido: {failed_str}. "
            "Los escenarios adversos pasan."
        )
    else:
        verdict = "FRÁGIL"
        reasons: list[str] = []
        if n_passed < 3:
            reasons.append(f"Solo {n_passed}/4 criterios cuantitativos pasan ({', '.join(failed_names)})")
        if not all_scenarios_passed:
            reasons.append("Algún escenario adverso falla")
        explanation = ". ".join(reasons) + "."

    return RobustnessVerdict(
        verdict=verdict,
        criteria=tuple(criteria),
        n_criteria_passed=n_passed,
        all_scenarios_passed=all_scenarios_passed,
        explanation=explanation,
    )


def compute_verdict(
    sensitivity: SensitivityReport,
    monte_carlo: MonteCarloResult,
    pbo: PBOResult,
    dsr_value: Decimal,
    all_scenarios_passed: bool,
) -> RobustnessVerdict:
    """Calcula el veredicto final a partir de los 4 análisis."""
    criteria: list[CriterionResult] = []

    # Criterio A: Sensibilidad
    criteria.append(CriterionResult(
        name="A. Sensibilidad",
        passed=sensitivity.is_robust,
        value=f"{sensitivity.pass_fraction:.1%} ({sensitivity.n_passed}/{sensitivity.n_configs})",
        threshold=f">= 60% con Sharpe>0 y |MaxDD|<50%",
        note="" if sensitivity.is_robust else (
            f"Solo {sensitivity.n_passed}/{sensitivity.n_configs} configs pasan los filtros. "
            "La estrategia es sensible a los parámetros."
        ),
    ))

    # Criterio B: Monte Carlo — P5(Sharpe)
    from qtrader.research.robustness.monte_carlo import P5_SHARPE_MIN
    p5_sharpe = monte_carlo.sharpe_pctls.p5
    criteria.append(CriterionResult(
        name="B. Monte Carlo P5(Sharpe)",
        passed=monte_carlo.p5_sharpe_passes and monte_carlo.has_enough_data,
        value=f"P5={p5_sharpe:.4f}" if monte_carlo.has_enough_data else "N/A (datos insuficientes)",
        threshold=f">= {P5_SHARPE_MIN}",
        note="" if monte_carlo.has_enough_data else "Datos insuficientes para bootstrap.",
    ))

    # Criterio C: PBO
    criteria.append(CriterionResult(
        name="C. PBO (overfitting)",
        passed=not pbo.is_overfit,
        value=f"PBO={pbo.pbo:.3f} ({pbo.n_overfit_splits}/{pbo.n_splits} splits)",
        threshold="<= 0.70",
        note=pbo.note if pbo.is_overfit else "",
    ))

    # Criterio D: DSR
    criteria.append(CriterionResult(
        name="D. DSR global",
        passed=dsr_value >= DSR_THRESHOLD,
        value=f"DSR={float(dsr_value):.4f}",
        threshold=f"> {float(DSR_THRESHOLD):.2f}",
        note="" if dsr_value >= DSR_THRESHOLD else (
            f"DSR={float(dsr_value):.4f} < {float(DSR_THRESHOLD):.2f}. "
            "El Sharpe OOS no es estadísticamente significativo dado el número de trials."
        ),
    ))

    return _compute_verdict(criteria, all_scenarios_passed)


def render_report(
    verdict: RobustnessVerdict,
    sensitivity: SensitivityReport,
    monte_carlo: MonteCarloResult,
    pbo: PBOResult,
    dsr_value: Decimal,
    as_of: date,
    sharpe_bruto: Decimal | None = None,
    notes: str = "",
) -> str:
    """Genera el Markdown del informe de robustez."""
    lines: list[str] = []
    lines.append(f"# Informe de Robustez — {as_of}")
    lines.append("")
    lines.append(f"**Veredicto: {verdict.verdict}**")
    lines.append("")
    lines.append(f"> {verdict.explanation}")
    lines.append("")

    # --- Criterios cuantitativos ---
    lines.append("## Criterios cuantitativos")
    lines.append("")
    lines.append(f"Criterios pasados: **{verdict.n_criteria_passed}/4**")
    lines.append("")
    lines.append("| # | Criterio | Valor | Umbral | ¿Pasa? |")
    lines.append("|---|----------|-------|--------|--------|")
    for c in verdict.criteria:
        emoji = "✅" if c.passed else "❌"
        lines.append(f"| {c.name} | {c.value} | {c.threshold} | {emoji} |")
    lines.append("")

    # Notas de criterios fallidos
    failed_notes = [(c.name, c.note) for c in verdict.criteria if not c.passed and c.note]
    if failed_notes:
        lines.append("### Por qué falló cada criterio")
        lines.append("")
        for name, note in failed_notes:
            lines.append(f"**{name}**: {note}")
        lines.append("")

    # --- Sharpe bruto vs neto ---
    lines.append("## Sharpe bruto vs. neto con haircut")
    lines.append("")
    lines.append(
        f"Haircut declarado por survivorship bias residual y tracking error "
        f"proxy→UCITS: **{float(SURVIVORSHIP_BIAS_HAIRCUT):.0%}**"
    )
    lines.append("")
    if sharpe_bruto is not None:
        sharpe_neto = sharpe_bruto * (_ZERO_COPY := Decimal("1") - SURVIVORSHIP_BIAS_HAIRCUT)
        lines.append(f"- Sharpe bruto (mejor config OOS): **{float(sharpe_bruto):.4f}**")
        lines.append(f"- Sharpe neto (después del haircut): **{float(sharpe_neto):.4f}**")
    else:
        lines.append("- Sharpe bruto: N/A (sin backtest ejecutado en esta sesión)")
        lines.append("- Sharpe neto: N/A")
    lines.append("")
    lines.append(
        "> ⚠️ El backtest genera hipótesis; no valida. La validación real es "
        "walk-forward + paper trading sobre datos nunca vistos."
    )
    lines.append("")

    # --- Parte A: Sensibilidad ---
    lines.append("## A. Análisis de sensibilidad (135 configuraciones)")
    lines.append("")
    lines.append(f"- Configuraciones evaluadas: **{sensitivity.n_configs}**")
    lines.append(f"- Configuraciones que pasan (Sharpe>0 ∩ |MaxDD|<50%): **{sensitivity.n_passed}**")
    lines.append(f"- Fracción de paso: **{sensitivity.pass_fraction:.1%}**")
    lines.append(f"- Robusto (≥60%): **{'Sí' if sensitivity.is_robust else 'No'}**")
    if sensitivity.sharpes_oos:
        sharpes_float = [float(s) for s in sensitivity.sharpes_oos]
        lines.append(f"- Sharpe OOS medio: **{sum(sharpes_float)/len(sharpes_float):.4f}**")
        lines.append(f"- Sharpe OOS mínimo: **{min(sharpes_float):.4f}**")
        lines.append(f"- Sharpe OOS máximo: **{max(sharpes_float):.4f}**")
    lines.append("")

    # --- Parte B: Monte Carlo ---
    lines.append("## B. Monte Carlo (block bootstrap, n=1000, block_size=21)")
    lines.append("")
    if monte_carlo.has_enough_data:
        sp = monte_carlo.sharpe_pctls
        mp = monte_carlo.maxdd_pctls
        cp = monte_carlo.cagr_pctls
        lines.append("### Sharpe ratio")
        lines.append(f"| P5 | P25 | P50 | P75 | P95 |")
        lines.append(f"|---|---|---|---|---|")
        lines.append(
            f"| {float(sp.p5):.4f} | {float(sp.p25):.4f} | {float(sp.p50):.4f} "
            f"| {float(sp.p75):.4f} | {float(sp.p95):.4f} |"
        )
        lines.append("")
        lines.append("### Max Drawdown")
        lines.append(f"| P5 | P25 | P50 | P75 | P95 |")
        lines.append(f"|---|---|---|---|---|")
        lines.append(
            f"| {float(mp.p5):.4f} | {float(mp.p25):.4f} | {float(mp.p50):.4f} "
            f"| {float(mp.p75):.4f} | {float(mp.p95):.4f} |"
        )
        lines.append("")
        lines.append("### CAGR")
        lines.append(f"| P5 | P25 | P50 | P75 | P95 |")
        lines.append(f"|---|---|---|---|---|")
        lines.append(
            f"| {float(cp.p5):.4f} | {float(cp.p25):.4f} | {float(cp.p50):.4f} "
            f"| {float(cp.p75):.4f} | {float(cp.p95):.4f} |"
        )
        lines.append("")
        if monte_carlo.trade_order_sharpe_pctls is not None:
            tp = monte_carlo.trade_order_sharpe_pctls
            lines.append("### Bootstrap del orden de trades")
            lines.append(
                f"P5={float(tp.p5):.4f}  P50={float(tp.p50):.4f}  P95={float(tp.p95):.4f}"
            )
            lines.append("")
    else:
        lines.append("*Datos insuficientes para ejecutar el bootstrap.*")
        lines.append("")

    # --- Parte C: PBO ---
    lines.append("## C. Probability of Backtest Overfitting (CSCV)")
    lines.append("")
    lines.append(f"- Subperíodos (S): **{pbo.s_subperiods}**")
    lines.append(f"- Splits IS/OOS evaluados: **{pbo.n_splits}**")
    lines.append(f"- Splits donde el ganador IS pierde OOS: **{pbo.n_overfit_splits}**")
    lines.append(f"- PBO: **{pbo.pbo:.3f}**")
    lines.append(f"- Sobreajustado (PBO > 0.70): **{'Sí ⚠️' if pbo.is_overfit else 'No ✅'}**")
    if pbo.note:
        lines.append(f"- Nota: {pbo.note}")
    lines.append("")

    # --- DSR ---
    lines.append("## D. Deflated Sharpe Ratio (DSR)")
    lines.append("")
    lines.append(f"- DSR: **{float(dsr_value):.4f}**")
    lines.append(f"- Umbral: **> {float(DSR_THRESHOLD):.2f}**")
    lines.append(
        f"- ¿Significativo?: **{'Sí ✅' if dsr_value >= DSR_THRESHOLD else 'No ❌'}**"
    )
    lines.append("")

    # --- Escenarios adversos ---
    lines.append("## E. Escenarios adversos")
    lines.append("")
    lines.append(
        f"Estado: **{'Todos pasan ✅' if verdict.all_scenarios_passed else 'Alguno falla ❌'}**"
    )
    lines.append("")
    lines.append("Ejecutar: `uv run pytest tests/robustness/ -v` para ver detalle.")
    lines.append("")

    # --- Notas adicionales ---
    if notes:
        lines.append("## Notas")
        lines.append("")
        lines.append(notes)
        lines.append("")

    lines.append("---")
    lines.append(
        "*Este informe fue generado automáticamente. El veredicto es orientativo: "
        "la decisión de operar en papel o real requiere firma humana explícita.*"
    )
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helper para cálculo del Sharpe bruto de referencia
# ---------------------------------------------------------------------------

_ZERO_COPY = Decimal("1") - SURVIVORSHIP_BIAS_HAIRCUT


def best_sharpe_oos(sensitivity: SensitivityReport) -> Decimal | None:
    """Devuelve el mejor Sharpe OOS de la sweep (para el informe bruto/neto)."""
    sharpes = sensitivity.sharpes_oos
    if not sharpes:
        return None
    return max(sharpes)
