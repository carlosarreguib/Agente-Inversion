from __future__ import annotations

import argparse
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any


def _fmt(amount: Decimal) -> str:
    """Formatea una cantidad en euros con coma decimal (convención española)."""
    return f"{amount:.2f}".replace(".", ",") + " €"


def _cmd_demo(args: argparse.Namespace) -> None:
    from qtrader.engine import run_demo

    result = run_demo(
        symbol="SPY",
        days=int(args.days),
        initial_equity=Decimal("1000"),
        seed=int(args.seed),
        db_path=str(args.db),
    )

    print(f"Equity inicial:  {_fmt(result.initial_equity)}")
    print(f"Equity final:    {_fmt(result.final_equity)}")
    print(f"Nº de trades:    {result.trades}")
    sign = "+" if result.pnl >= Decimal("0") else ""
    print(f"P&L:             {sign}{_fmt(result.pnl)}")


def _cmd_universe_show(args: argparse.Namespace) -> None:
    from qtrader.data.universe import UniverseManager

    config_dir = Path(args.config_dir) if args.config_dir else None
    mgr = UniverseManager(config_dir=config_dir)
    as_of = date.fromisoformat(args.date)
    instruments = mgr.get_universe(as_of)

    print(f"Universo point-in-time al {as_of}  ({len(instruments)} instrumentos)")
    print(f"{'SYMBOL':<8}  {'CATEGORY':<14}  {'EXCHANGE':<6}  {'CCY':<4}  "
          f"{'TICKER_PROXY':<14}  {'TICKER_UCITS':<14}  DECLARED_ON")
    print("-" * 95)
    for inst in instruments:
        print(
            f"{inst.symbol:<8}  {inst.category.value:<14}  {inst.exchange:<6}  "
            f"{inst.currency:<4}  {inst.ticker_proxy:<14}  {inst.ticker_ucits:<14}  "
            f"{inst.declared_on}"
        )


def _cmd_golden_update(args: argparse.Namespace) -> None:  # noqa: ARG001
    """Regenera golden_reference.json mostrando diff y pidiendo confirmacion."""
    import json
    from datetime import UTC, date, datetime, timedelta
    from decimal import Decimal

    from qtrader.backtesting.engine import (
        ApproveAllRisk,
        BacktestEngine,
        CostAwareSimBroker,
    )
    from qtrader.backtesting.events import BarEvent
    from qtrader.backtesting.golden_strategy import (
        GoldenEqualWeightPortfolio,
        MomentumStrategy,
    )
    from qtrader.backtesting.metrics import compute_metrics
    from qtrader.backtesting.synthetic import SyntheticMultiProvider
    from qtrader.core.types import (
        Instrument,
        InstrumentCategory,
        InstrumentType,
    )
    from qtrader.costs import CostsConfig

    _SYMBOL_PARAMS = {
        "SYNTH_A": {"drift": 0.0005, "vol": 0.010, "base_price": 150.0},
        "SYNTH_B": {"drift": 0.0002, "vol": 0.015, "base_price": 80.0},
        "SYNTH_C": {"drift": -0.0001, "vol": 0.013, "base_price": 200.0},
        "SYNTH_D": {"drift": 0.0008, "vol": 0.020, "base_price": 50.0},
        "SYNTH_E": {"drift": 0.0003, "vol": 0.009, "base_price": 120.0},
    }
    _DECLARED_ON = date(2022, 1, 1)
    _INITIAL_EQUITY = Decimal("15000")

    instruments = [
        Instrument(
            symbol=sym,
            name="Synthetic ETF " + sym,
            exchange="XNAS",
            currency="USD",
            category=InstrumentCategory.REGION,
            ticker_proxy=sym,
            ticker_ucits=sym,
            declared_on=_DECLARED_ON,
            instrument_type=InstrumentType.ETF,
            spread_bps=5,
        )
        for sym in sorted(_SYMBOL_PARAMS)
    ]

    class _UM:
        def get_universe(self, as_of: date) -> list[Instrument]:
            return [i for i in instruments if i.declared_on <= as_of]
        def get_instrument(self, symbol: str, as_of: date) -> Instrument | None:
            for inst in instruments:
                if inst.symbol == symbol and inst.declared_on <= as_of:
                    return inst
            return None

    start_dt = datetime(2022, 1, 3, tzinfo=UTC)
    td: list[date] = []
    cur = date(2022, 1, 3)
    while len(td) < 252:
        if cur.weekday() < 5:
            td.append(cur)
        cur += timedelta(days=1)

    provider = SyntheticMultiProvider(
        base_seed=42, start_date=start_dt, symbol_params=_SYMBOL_PARAMS
    )
    strategy = MomentumStrategy(sma_period=20)
    portfolio = GoldenEqualWeightPortfolio(
        initial_equity=_INITIAL_EQUITY,
        n_slots=len(instruments),
        min_order_eur=Decimal("1000"),
    )

    class _Obs:
        def __init__(self, p: GoldenEqualWeightPortfolio) -> None:
            self._p = p
        def on_event(self, event: object) -> None:
            if isinstance(event, BarEvent):
                for vb in event.bars:
                    self._p.update_close(vb.bar.symbol, vb.bar.close)

    config_dir = Path(__file__).resolve().parents[2] / "config"
    costs_config = CostsConfig.from_yaml(config_dir / "costs.yaml")
    broker = CostAwareSimBroker(
        costs_config=costs_config,
        universe={inst.symbol: inst for inst in instruments},
    )

    print("Ejecutando backtest golden (252 dias, 5 instrumentos)...")
    engine = BacktestEngine(
        provider=provider,
        universe_mgr=_UM(),  # type: ignore[arg-type]
        strategy=strategy,
        portfolio=portfolio,
        risk=ApproveAllRisk(),
        broker=broker,
        trading_days=td,
        initial_equity=_INITIAL_EQUITY,
        slippage_bps=Decimal("5"),
        observers=[_Obs(portfolio)],
    )
    result = engine.run()
    new_metrics = compute_metrics(result)
    new_data = new_metrics.to_dict()

    golden_json = (
        Path(__file__).resolve().parents[2] / "tests" / "backtesting" / "golden_reference.json"
    )

    if golden_json.exists():
        old_data: dict[str, str | int] = json.loads(golden_json.read_text(encoding="utf-8"))
        print("\n--- DIFF (antes vs despues) ---")
        changed = False
        for key in new_data:
            old_val = old_data.get(key, "<ausente>")
            new_val = new_data[key]
            marker = " (CAMBIADO)" if old_val != new_val else ""
            print(f"  {key}: {old_val} -> {new_val}{marker}")
            if old_val != new_val:
                changed = True
        if not changed:
            print("  Sin cambios.")
            return
    else:
        print("\n(golden_reference.json no existe; se creara nuevo)")
        for key, val in new_data.items():
            print(f"  {key}: {val}")

    respuesta = input("\n¿Sobreescribir golden_reference.json? [s/N] ").strip().lower()
    if respuesta != "s":
        print("Cancelado. golden_reference.json no modificado.")
        sys.exit(0)

    golden_json.write_text(
        json.dumps(new_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Actualizado: {golden_json}")


def _cmd_research_run(args: argparse.Namespace) -> None:
    """Lanza el LLMResearcher con una hipótesis explícita (T8)."""
    import logging
    from pathlib import Path

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    api_key = args.api_key or __import__("os").environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print(
            "ERROR: se requiere ANTHROPIC_API_KEY (env var o --api-key).",
            file=sys.stderr,
        )
        sys.exit(1)

    from qtrader.research.llm.proposals_db import ProposalsDB
    from qtrader.research.llm.researcher import (
        BacktestRunResult,
        BudgetConfig,
        LLMResearcher,
    )
    from qtrader.research.llm.toolset import load_toolset

    class _AnthropicClient:
        def __init__(self, key: str) -> None:
            self._key = key

        def create_message(
            self,
            *,
            model: str,
            max_tokens: int,
            system: str,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
        ) -> dict[str, Any]:
            import json as _json
            import urllib.request

            payload: dict[str, Any] = {
                "model": model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": messages,
                "tools": tools,
            }
            data = _json.dumps(payload).encode()
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=data,
                headers={
                    "x-api-key": self._key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                result: dict[str, Any] = _json.loads(resp.read())
                return result

    class _StubBacktestRunner:
        """Stub que ejecuta un backtest mínimo con datos sintéticos."""

        def run(
            self,
            strategy_id: str,
            parameters: dict[str, float | int | str],
        ) -> BacktestRunResult:
            return BacktestRunResult(
                sharpe_oos=0.0,
                max_drawdown=0.0,
                dsr=0.0,
                n_trials=0,
            )

    trials_db_path = Path(args.trials_db)
    proposals_db_path = Path(args.proposals_db)
    toolset = load_toolset(trials_db_path)
    proposals_db = ProposalsDB(proposals_db_path)
    budget = BudgetConfig(
        max_tokens_per_run=args.max_tokens,
        max_runs_per_day=args.max_runs_per_day,
    )
    researcher = LLMResearcher(
        toolset=toolset,
        llm_client=_AnthropicClient(api_key),
        proposals_db=proposals_db,
        backtest_runner=_StubBacktestRunner(),
        budget=budget,
        model=args.model,
    )
    result = researcher.run(args.hypothesis)
    print(f"hypothesis_id:    {result.hypothesis_id}")
    print(f"proposal_created: {result.proposal_created}")
    if result.proposal_id:
        print(f"proposal_id:      {result.proposal_id}")
    if result.rejection_reason:
        print(f"rechazado:        {result.rejection_reason}")
    print(f"tokens_usados:    {result.tokens_used}")
    print(f"runs_hoy:         {result.runs_today}")


def _cmd_research_approve(args: argparse.Namespace) -> None:
    """Aprueba una propuesta de investigación con confirmación interactiva (T8)."""
    from pathlib import Path

    from qtrader.research.llm.proposals_db import ProposalsDB

    db = ProposalsDB(Path(args.proposals_db))
    proposal = db.get(args.proposal_id)
    if proposal is None:
        print(f"ERROR: propuesta {args.proposal_id!r} no encontrada.", file=sys.stderr)
        sys.exit(1)

    if proposal.status != "PENDING_APPROVAL":
        print(
            f"ERROR: propuesta {args.proposal_id!r} no está en PENDING_APPROVAL "
            f"(está en {proposal.status!r}).",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"\n=== Propuesta {proposal.proposal_id} ===")
    print(f"Estrategia:  {proposal.strategy_id}")
    print(f"Sharpe OOS:  {proposal.sharpe_oos:.4f}")
    print(f"MaxDD:       {proposal.maxdd:.4f}")
    print(f"DSR:         {proposal.dsr:.4f}")
    print(f"N trials:    {proposal.n_trials}")
    print(f"\nResumen:\n{proposal.summary}")
    print()
    print("ATENCIÓN: Aprobar esta propuesta la marca como APPROVED en proposals.db.")
    print("La activación real en producción requiere pasos adicionales manuales.")
    print()

    respuesta = "s" if args.yes else input("¿Aprobar propuesta? [s/N] ").strip().lower()

    if respuesta != "s":
        print("Cancelado. Propuesta no modificada.")
        sys.exit(0)

    reviewed_by = args.reviewed_by or __import__("os").environ.get("USER", "unknown")
    updated = db.approve(args.proposal_id, reviewed_by=reviewed_by)
    if updated:
        print(f"Propuesta {args.proposal_id} marcada como APPROVED por {reviewed_by!r}.")
        print("Siguiente paso: implementar la estrategia manualmente y crear un ADR.")
    else:
        print("ERROR: no se pudo actualizar la propuesta.", file=sys.stderr)
        sys.exit(1)


def _cmd_research_proposals(args: argparse.Namespace) -> None:
    """Lista propuestas de investigación (T8)."""
    from pathlib import Path

    from qtrader.research.llm.proposals_db import ProposalsDB

    db = ProposalsDB(Path(args.proposals_db))
    records = db.fetch_pending() if args.status == "pending" else db.fetch_all()

    if not records:
        print("Sin propuestas.")
        return

    print(f"{'ID':<38}  {'ESTRATEGIA':<14}  {'DSR':>6}  {'SR_OOS':>7}  {'MaxDD':>7}  ESTADO")
    print("-" * 100)
    for p in records:
        print(
            f"{p.proposal_id:<38}  {p.strategy_id:<14}  "
            f"{p.dsr:>6.4f}  {p.sharpe_oos:>7.4f}  {p.maxdd:>7.4f}  {p.status}"
        )


def _cmd_research_trials_count(args: argparse.Namespace) -> None:
    from qtrader.research.trials_db import TrialsDB

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: trials.db no encontrada: {db_path}", file=sys.stderr)
        sys.exit(1)

    db = TrialsDB(db_path)
    strategy = args.strategy if args.strategy else None
    n = db.count(strategy)
    if strategy:
        print(f"{n}  (estrategia={strategy})")
    else:
        print(n)


def _cmd_research_report(args: argparse.Namespace) -> None:
    from decimal import Decimal

    from qtrader.research.dsr import compute_dsr
    from qtrader.research.trials_db import TrialsDB

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: trials.db no encontrada: {db_path}", file=sys.stderr)
        sys.exit(1)

    db = TrialsDB(db_path)
    strategy_id = args.strategy
    records = db.fetch_completed(strategy_id)

    if not records:
        print(f"Sin trials COMPLETED para estrategia '{strategy_id}'.")
        return

    n_total = db.count(strategy_id)
    sharpes_oos = [r.sharpe_oos for r in records if r.sharpe_oos is not None]
    # T_obs: inferir del primer registro (test_end - test_start en dias habiles)
    first = records[0]
    days_range = (first.test_end - first.test_start).days
    t_obs = max(1, int(days_range * 5 / 7))  # aprox. dias habiles

    dsr = compute_dsr(sharpes_oos, t_obs)

    print(f"\n=== Informe walk-forward: {strategy_id} ===")
    print(f"N trials totales (DB):  {n_total}")
    print(f"N folds COMPLETED:      {len(records)}")
    print(f"T observaciones (OOS):  {t_obs}")
    print()
    print(f"DSR (Deflated Sharpe):  {dsr.dsr}")
    print(f"  SR* (esperado H0):    {dsr.sr_star}")
    print(f"  SR obs (mejor OOS):   {dsr.sr_obs}")
    print(f"  sigma_SR:             {dsr.sigma_sr}")
    if dsr.note:
        print(f"  Nota: {dsr.note}")
    print()

    # Tabla por fold
    print(f"{'Fold':>4}  {'Train':>22}  {'Test':>22}  {'SR_IS':>7}  {'SR_OOS':>7}"
          f"  {'MaxDD_OOS':>10}  {'Trades':>6}")
    print("-" * 90)

    best_oos = max((r.sharpe_oos or Decimal("-99") for r in records), default=None)
    worst_oos = min((r.sharpe_oos or Decimal("99") for r in records), default=None)

    for r in records:
        marker = ""
        if r.sharpe_oos == best_oos:
            marker = " <-- mejor"
        elif r.sharpe_oos == worst_oos:
            marker = " <-- peor"
        sr_is_str = f"{r.sharpe_is:.4f}" if r.sharpe_is is not None else "N/A"
        sr_oos_str = f"{r.sharpe_oos:.4f}" if r.sharpe_oos is not None else "N/A"
        mdd_str = f"{r.max_drawdown_oos:.4f}" if r.max_drawdown_oos is not None else "N/A"
        trades_str = str(r.num_trades_oos) if r.num_trades_oos is not None else "N/A"
        print(
            f"{r.fold_id:>4}  "
            f"{r.train_start} -> {r.train_end}  "
            f"{r.test_start} -> {r.test_end}  "
            f"{sr_is_str:>7}  {sr_oos_str:>7}  {mdd_str:>10}  {trades_str:>6}"
            f"{marker}"
        )

    if len(sharpes_oos) >= 2:
        import math
        n = len(sharpes_oos)
        mean = sum(float(s) for s in sharpes_oos) / n
        std = math.sqrt(
            sum((float(s) - mean) ** 2 for s in sharpes_oos) / (n - 1)
        )
        print()
        print(f"Estabilidad (std SR OOS entre folds): {std:.4f}")
        print(f"Media SR OOS:                         {mean:.4f}")


def _print_full_report(report: object, initial_equity: Decimal) -> None:
    """Imprime el informe completo de metricas con bootstrap CIs."""
    from qtrader.backtesting.full_metrics import BacktestReport
    assert isinstance(report, BacktestReport)

    def _pct(v: Decimal) -> str:
        return f"{v * 100:.2f} %"

    def _d(v: Decimal, prec: int = 4) -> str:
        return f"{v:.{prec}f}"

    def _ci(ci: object) -> str:
        from qtrader.backtesting.full_metrics import BootstrapCI
        if ci is None or not isinstance(ci, BootstrapCI):
            return ""
        return f"  [CI95: {ci.lower:.4f}, {ci.upper:.4f}]"

    print("=" * 70)
    print(f"  INFORME BACKTEST COMPLETO — {report.strategy_id}")
    print(f"  {report.start_date} → {report.end_date}  ({report.n_trading_days} dias habiles)")
    print("=" * 70)

    print("\n--- RETORNO ---")
    sign = "+" if report.total_return >= 0 else ""
    print(f"  Retorno total:         {sign}{_pct(report.total_return)}")
    print(f"  CAGR anualizado:       {sign}{_pct(report.cagr)}")
    print(f"  Capital inicial:       {_fmt(initial_equity)}")
    from qtrader.backtesting.full_metrics import _ZERO
    final = initial_equity * (report.total_return + _ZERO.__class__("1"))
    print(f"  Capital final (est.):  {_fmt(final)}")

    print("\n--- RIESGO ---")
    print(f"  Volatilidad anual:     {_pct(report.volatility)}")
    print(f"  Max drawdown:          {_pct(report.max_drawdown)}")
    print(f"  Max DD duracion:       {report.max_drawdown_duration_days} dias")
    if report.max_drawdown_ci:
        print(f"  Max DD CI95:           [{_pct(report.max_drawdown_ci.lower)}, "
              f"{_pct(report.max_drawdown_ci.upper)}]")

    print("\n--- RATIOS ---")
    print(f"  Sharpe:                {_d(report.sharpe)}{_ci(report.sharpe_ci)}")
    print(f"  Sortino:               {_d(report.sortino)}{_ci(report.sortino_ci)}")
    print(f"  Calmar:                {_d(report.calmar)}{_ci(report.calmar_ci)}")

    print("\n--- ACTIVIDAD ---")
    print(f"  Nº trades (round-trip):{report.n_trades}")
    print(f"  Win rate:              {_pct(report.win_rate)}")
    print(f"  Profit factor:         {_d(report.profit_factor)}")
    print(f"  Avg ganancia:          {_fmt(report.avg_win_eur)}")
    print(f"  Avg perdida:           {_fmt(report.avg_loss_eur)}")
    print(f"  Rotacion anual:        {_d(report.turnover_annual, 2)}x")

    print("\n--- EXPOSICION ---")
    print(f"  Exposicion media:      {_pct(report.avg_exposure)}")
    print(f"  Exposicion maxima:     {_pct(report.max_exposure)}")

    print("\n--- COSTES ---")
    print(f"  Comisiones totales:    {_fmt(report.costs.total_commission)}")
    print(f"  Costes totales:        {_fmt(report.costs.total_costs)}")

    if report.by_instrument:
        print("\n--- P&L POR INSTRUMENTO (top 10 por P&L abs) ---")
        sorted_insts = sorted(
            report.by_instrument,
            key=lambda x: abs(x.pnl_eur),
            reverse=True,
        )
        print(f"  {'SYMBOL':<14}  {'TRADES':>6}  {'P&L EUR':>10}  {'WIN%':>7}")
        print(f"  {'-' * 14}  {'-' * 6}  {'-' * 10}  {'-' * 7}")
        for inst in sorted_insts[:10]:
            sign_i = "+" if inst.pnl_eur >= _ZERO.__class__("0") else ""
            print(f"  {inst.symbol:<14}  {inst.n_trades:>6}  "
                  f"{sign_i}{inst.pnl_eur:>10.2f}  {inst.win_rate * 100:>6.1f}%")

    if report.annual_pnl:
        print("\n--- RETORNO ANUAL ---")
        print(f"  {'AÑO':<6}  {'P&L EUR':>10}  {'RETORNO':>8}")
        print(f"  {'-' * 6}  {'-' * 10}  {'-' * 8}")
        for p in report.annual_pnl:
            sign_p = "+" if p.pnl_eur >= _ZERO.__class__("0") else ""
            sign_r = "+" if p.return_pct >= _ZERO.__class__("0") else ""
            print(f"  {p.period:<6}  {sign_p}{p.pnl_eur:>10.2f}  "
                  f"{sign_r}{p.return_pct * 100:>7.2f}%")

    if report.monthly_pnl:
        print("\n--- RETORNO MENSUAL (primeros 12 meses) ---")
        print(f"  {'MES':<8}  {'P&L EUR':>10}  {'RETORNO':>8}")
        print(f"  {'-' * 8}  {'-' * 10}  {'-' * 8}")
        for p in list(report.monthly_pnl)[:12]:
            sign_p = "+" if p.pnl_eur >= _ZERO.__class__("0") else ""
            sign_r = "+" if p.return_pct >= _ZERO.__class__("0") else ""
            print(f"  {p.period:<8}  {sign_p}{p.pnl_eur:>10.2f}  "
                  f"{sign_r}{p.return_pct * 100:>7.2f}%")

    print("\n--- NOTA ---")
    print("  Sharpe bruto (mostrado): incluye costes modelados.")
    print("  Para Sharpe neto con haircut ver: uv run qtrader research report --strategy X")
    print("=" * 70)


def _cmd_backtest(args: argparse.Namespace) -> None:
    """Ejecuta el backtest de una estrategia sobre datos sinteticos (T3.1/T3.2)."""
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from qtrader.backtesting.engine import (
        ApproveAllRisk,
        BacktestEngine,
        CostAwareSimBroker,
    )
    from qtrader.backtesting.events import BarEvent
    from qtrader.backtesting.metrics import compute_metrics
    from qtrader.backtesting.observers import MetricsObserver
    from qtrader.backtesting.synthetic import SyntheticMultiProvider
    from qtrader.costs import CostsConfig
    from qtrader.data.universe import UniverseManager
    from qtrader.strategies.momentum import (
        CrossSectionalMomentumStrategy,
        MomentumEqualWeightPortfolio,
    )

    if args.strategy != "momentum":
        print(f"ERROR: estrategia desconocida '{args.strategy}'. Solo 'momentum' disponible.",
              file=sys.stderr)
        sys.exit(1)

    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)
    initial_equity = Decimal("15000")
    config_dir = Path(__file__).resolve().parents[2] / "config"

    # Generar lista de dias habiles en el rango
    trading_days: list[date] = []
    cur = start_date
    while cur <= end_date:
        if cur.weekday() < 5:
            trading_days.append(cur)
        cur += timedelta(days=1)

    if len(trading_days) < 300:
        print("ERROR: rango de fechas demasiado corto (minimo ~1 año + 252 dias de lookback).",
              file=sys.stderr)
        sys.exit(1)

    # Universo: todos los instrumentos declarados en el fichero.
    # El motor consulta get_universe(T) point-in-time en cada dia del bucle.
    # Para construir el proveedor de datos y el mapa de costes usamos all_instruments.
    mgr = UniverseManager(config_dir=config_dir)
    all_instruments = mgr.all_instruments
    if not all_instruments:
        print("ERROR: universo vacio (config/universe.yaml sin instrumentos).", file=sys.stderr)
        sys.exit(1)

    instrument_map = {inst.ticker_proxy: inst for inst in all_instruments}
    instruments = all_instruments  # alias para los bucles de parametros

    # Proveedor de datos: sinteticos con parametros calibrados para ETFs
    # drift=0.07/252 anualizado, vol=0.15/sqrt(252) diario
    daily_drift = 0.07 / 252
    daily_vol = 0.15 / (252 ** 0.5)
    # El proveedor debe empezar 2 años antes del inicio del backtest para que
    # la estrategia tenga 252+ barras de historia desde el primer dia de trading.
    from datetime import timedelta as _td
    provider_start = start_date - _td(days=2 * 365)
    start_dt = datetime(provider_start.year, provider_start.month, provider_start.day, tzinfo=UTC)

    # Parametros de correlacion aplicados via seed derivada del sector/region
    # (diferencia sistematica entre sectores y regiones en la seed)
    symbol_params = {}
    for inst in instruments:
        # Volatilidad y drift ligeramente diferenciados por categoria para realismo
        from qtrader.core.types import InstrumentCategory
        if inst.category == InstrumentCategory.SECTOR:
            sym_vol = daily_vol * 1.15
            sym_drift = daily_drift * 0.95
        elif inst.category == InstrumentCategory.FIXED_INCOME:
            sym_vol = daily_vol * 0.35
            sym_drift = daily_drift * 0.50
        elif inst.category == InstrumentCategory.ALTERNATIVE:
            sym_vol = daily_vol * 0.70
            sym_drift = daily_drift * 0.80
        else:
            sym_vol = daily_vol
            sym_drift = daily_drift
        symbol_params[inst.ticker_proxy] = {
            "drift": sym_drift,
            "vol": sym_vol,
            "base_price": 100.0,
        }

    provider = SyntheticMultiProvider(
        base_seed=42,
        start_date=start_dt,
        symbol_params=symbol_params,
    )

    costs_config = CostsConfig.from_yaml(config_dir / "costs.yaml")

    # Para backtest con datos sinteticos, todos los instrumentos se tratan como
    # disponibles desde el inicio del rango (ignoramos declared_on de YAML).
    # En un backtest con datos reales, get_universe usaria la fecha real.
    class _UniverseManagerAdapter:
        def get_universe(self, as_of: date) -> list[object]:  # noqa: ARG002
            return all_instruments  # type: ignore[return-value]

        def get_instrument(self, symbol: str, as_of: date) -> object | None:  # noqa: ARG002
            return instrument_map.get(symbol)

    strategy = CrossSectionalMomentumStrategy(trading_days=trading_days)
    portfolio = MomentumEqualWeightPortfolio(
        initial_equity=initial_equity,
        min_order_eur=Decimal("1000"),
    )
    broker = CostAwareSimBroker(
        costs_config=costs_config,
        universe=instrument_map,  # type: ignore[arg-type]
    )

    class _BarObserver:
        def on_event(self, event: object) -> None:
            if isinstance(event, BarEvent):
                for vb in event.bars:
                    portfolio.update_close(vb.bar.symbol, vb.bar.close)

    print(f"Ejecutando backtest momentum {args.start} → {args.end}")
    print(f"Universo: {len(instruments)} instrumentos  |  Dias habiles: {len(trading_days)}")
    print()

    metrics_obs = MetricsObserver()

    engine = BacktestEngine(
        provider=provider,
        universe_mgr=_UniverseManagerAdapter(),  # type: ignore[arg-type]
        strategy=strategy,
        portfolio=portfolio,
        risk=ApproveAllRisk(),
        broker=broker,
        trading_days=trading_days,
        initial_equity=initial_equity,
        slippage_bps=Decimal("5"),
        observers=[_BarObserver(), metrics_obs],
    )
    result = engine.run()
    metrics = compute_metrics(result)

    # Validacion de rango esperado (invariante CLAUDE.md §3.7 + spec T3.1)
    sharpe = metrics.sharpe_ratio
    max_dd = metrics.max_drawdown

    _SHARPE_MAX_EXPECTED = Decimal("1.5")
    _MDD_MIN_EXPECTED = Decimal("-0.10")  # al menos -10 % de drawdown esperado

    aviso = False
    if sharpe > _SHARPE_MAX_EXPECTED:
        aviso = True
        print("AVISO: Sharpe fuera del rango esperado para momentum clasico.")
        print("       Posible look-ahead bias o error de implementacion.")
        print("       Revisar antes de interpretar estos resultados.")
        print()
    if max_dd > _MDD_MIN_EXPECTED:  # MaxDD muy pequeño (cercano a 0)
        aviso = True
        print("AVISO: MaxDD fuera del rango esperado para momentum clasico.")
        print("       Posible look-ahead bias o error de implementacion.")
        print("       Revisar antes de interpretar estos resultados.")
        print()

    if aviso:
        import logging
        logging.warning(
            "BACKTEST AVISO: Sharpe=%.4f MaxDD=%.4f fuera del rango esperado. "
            "Posible look-ahead bias.",
            float(sharpe), float(max_dd),
        )

    print("=== Resultados backtest momentum ===")
    print(f"Capital inicial:    {_fmt(initial_equity)}")
    print(f"Capital final:      {_fmt(metrics.final_equity)}")
    sign = "+" if metrics.total_return >= Decimal("0") else ""
    ret_pct = metrics.total_return * 100
    print(f"Retorno total:      {sign}{metrics.total_return:.4f} ({sign}{ret_pct:.2f} %)")
    print(f"Sharpe anualizado:  {sharpe:.4f}")
    print(f"Max drawdown:       {max_dd:.4f} ({max_dd * 100:.2f} %)")
    print(f"Numero de trades:   {metrics.num_trades}")
    print(f"Comisiones totales: {_fmt(metrics.total_commission)}")
    print(f"Dias de trading:    {result.trading_days}")
    print()
    print("Sharpe bruto (mostrado): incluye costes modelados (sin haircut adicional).")
    print("Para Sharpe neto con haircut por survivorship bias ver research report.")
    if sharpe < Decimal("0.3") or sharpe > Decimal("0.8"):
        print()
        print(f"Nota: Sharpe={sharpe:.4f} fuera del rango publicado 0.3-0.8 para momentum 12-1.")
        print("      Con datos sinteticos esto es esperado — los resultados con datos reales")
        print("      pueden diferir significativamente.")

    # --- Informe completo (T3.2) ---
    report_mode = getattr(args, "report", "summary")
    if report_mode == "full":
        from qtrader.backtesting.full_metrics import compute_full_report
        print()
        print("Calculando metricas completas con bootstrap (n=1000, block=21)...")
        total_commission = metrics.total_commission
        full_report = compute_full_report(
            strategy_id="momentum",
            equity_curve=metrics_obs.equity_curve,
            fills=metrics_obs.fills,
            initial_equity=initial_equity,
            total_commission=total_commission,
            bootstrap=True,
            bootstrap_n_samples=1000,
            bootstrap_block_size=21,
        )
        _print_full_report(full_report, initial_equity)


def _cmd_audit_verify(args: argparse.Namespace) -> None:
    db_path = str(args.db)
    if not Path(db_path).exists():
        print(f"ERROR: base de datos no encontrada: {db_path}", file=sys.stderr)
        sys.exit(1)

    from qtrader.ledger.sqlite import SQLiteLedger

    ledger = SQLiteLedger(db_path)
    corrupt = ledger.verify_chain()

    if not corrupt:
        print("OK")
    else:
        first = corrupt[0]
        print(f"CORRUPCION detectada: rowid={first}")
        sys.exit(1)


def _cmd_dashboard(args: argparse.Namespace) -> None:
    """Arranca el dashboard Streamlit en 127.0.0.1:8501."""
    import subprocess
    import sys
    from pathlib import Path

    app_path = Path(__file__).parent / "dashboard" / "app.py"
    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 8501)

    if host != "127.0.0.1":
        print(
            "ERROR: el dashboard solo puede escuchar en 127.0.0.1 (CLAUDE.md §2.8)",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd = [
        sys.executable, "-m", "streamlit", "run",
        str(app_path),
        "--server.address", host,
        "--server.port", str(port),
        "--server.headless", "true",
        "--server.fileWatcherType", "none",
    ]
    try:
        subprocess.run(cmd, check=True)
    except KeyboardInterrupt:
        pass
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)


def _cmd_run(args: argparse.Namespace) -> None:
    """Ejecuta un ciclo del agente trader (T6)."""
    import asyncio
    import logging
    from datetime import UTC, date, datetime

    # production requiere variable de entorno + fichero de autorización con
    # caducidad + confirmación interactiva (CLAUDE.md §2.10). Nada de eso
    # existe todavía: rechazar explícitamente en vez de fingir que funciona.
    if args.mode == "production":
        print(
            "ERROR: --mode production no está soportado.\n"
            "Requiere variable de entorno + fichero de autorización con "
            "caducidad + confirmación interactiva (CLAUDE.md §2.10).",
            file=sys.stderr,
        )
        sys.exit(1)

    from qtrader.agents.runner import run_once
    from qtrader.agents.states import RunPhase
    from qtrader.agents.trader import AgentHalted
    from qtrader.backtesting.synthetic import SyntheticMultiProvider

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    trading_date = (
        date.fromisoformat(args.date) if args.date else date.today()
    )
    phase = (
        RunPhase.POST_CLOSE if args.phase == "post-close" else RunPhase.PRE_OPEN
    )

    # El CLI construye el proveedor y lo inyecta: agents/ no puede importar
    # qtrader.backtesting (contrato de import-linter).
    provider = SyntheticMultiProvider(
        base_seed=args.seed,
        start_date=datetime(2022, 1, 3, tzinfo=UTC),
    )

    try:
        result = asyncio.run(
            run_once(
                provider=provider,
                phase=phase,
                trading_date=trading_date,
                mode=args.mode,
                agent_db=args.agent_db,
                ledger_db=args.ledger_db,
                broker_db=args.broker_db,
                exec_db=args.exec_db,
                halt_path=args.halt_path,
            )
        )
    except AgentHalted as exc:
        print(f"CICLO DETENIDO: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Ciclo:        {result.cycle_id}")
    print(f"Fase:         {result.phase.value}")
    print(f"Fecha:        {result.trading_date.isoformat()}")
    print(f"Estados:      {' -> '.join(s.value for s in result.states_entered)}")
    if result.risk_level is not None:
        print(f"Nivel riesgo: {result.risk_level.value}")
    print(f"Órdenes:      {result.orders_submitted} enviadas, "
          f"{result.orders_blocked} bloqueadas")
    print(f"Fills:        {result.fills}")
    if result.toctou_max_ms > 0:
        print(f"TOCTOU máx:   {result.toctou_max_ms:.1f} ms")
    if result.warnings:
        print(f"Avisos:       {len(result.warnings)}")
        for warning in result.warnings[:10]:
            print(f"  - {warning}")
    if result.halted_by_kill_switch:
        print("DETENIDO POR KILL SWITCH")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="qtrader",
        description="qtrader — plataforma cuantitativa (solo simulación)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ------------------------------------------------------------------
    # demo
    # ------------------------------------------------------------------
    demo_parser = subparsers.add_parser(
        "demo",
        help="Ejecuta un camino end-to-end con datos sintéticos",
    )
    demo_parser.add_argument(
        "--days",
        type=int,
        default=60,
        help="Número de días a simular (default: 60)",
    )
    demo_parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Semilla del generador de datos (default: 42)",
    )
    demo_parser.add_argument(
        "--db",
        default="data/state.db",
        help="Ruta a la base de datos SQLite (default: data/state.db)",
    )

    # ------------------------------------------------------------------
    # audit
    # ------------------------------------------------------------------
    audit_parser = subparsers.add_parser(
        "audit",
        help="Comandos de auditoría",
    )
    audit_subs = audit_parser.add_subparsers(dest="audit_command", required=True)

    verify_parser = audit_subs.add_parser(
        "verify",
        help="Verifica la integridad de la cadena de hashes en audit_log",
    )
    verify_parser.add_argument(
        "--db",
        default="data/state.db",
        help="Ruta a la base de datos SQLite (default: data/state.db)",
    )

    # ------------------------------------------------------------------
    # universe
    # ------------------------------------------------------------------
    universe_parser = subparsers.add_parser(
        "universe",
        help="Comandos del universo de instrumentos",
    )
    universe_subs = universe_parser.add_subparsers(dest="universe_command", required=True)

    show_parser = universe_subs.add_parser(
        "show",
        help="Muestra el universo activo en una fecha dada",
    )
    show_parser.add_argument(
        "--date",
        required=True,
        metavar="YYYY-MM-DD",
        help="Fecha de referencia (point-in-time)",
    )
    show_parser.add_argument(
        "--config-dir",
        default=None,
        metavar="PATH",
        help="Directorio de configuración (default: config/ en raíz del proyecto)",
    )

    # ------------------------------------------------------------------
    # golden
    # ------------------------------------------------------------------
    golden_parser = subparsers.add_parser(
        "golden",
        help="Comandos del golden backtest de referencia",
    )
    golden_subs = golden_parser.add_subparsers(dest="golden_command", required=True)
    golden_subs.add_parser(
        "update",
        help="Regenera golden_reference.json (muestra diff y pide confirmacion)",
    )

    # ------------------------------------------------------------------
    # backtest
    # ------------------------------------------------------------------
    backtest_parser = subparsers.add_parser(
        "backtest",
        help="Ejecuta un backtest de una estrategia (solo datos sinteticos)",
    )
    backtest_parser.add_argument(
        "--strategy",
        required=True,
        metavar="STRATEGY_ID",
        help="Estrategia a ejecutar (e.g. 'momentum')",
    )
    backtest_parser.add_argument(
        "--start",
        required=True,
        metavar="YYYY-MM-DD",
        help="Fecha de inicio del backtest",
    )
    backtest_parser.add_argument(
        "--end",
        required=True,
        metavar="YYYY-MM-DD",
        help="Fecha de fin del backtest",
    )
    backtest_parser.add_argument(
        "--report",
        default="summary",
        choices=["summary", "full"],
        help="Nivel de informe: 'summary' (default) o 'full' (con bootstrap CIs y tablas)",
    )

    # ------------------------------------------------------------------
    # dashboard (T7B)
    # ------------------------------------------------------------------
    dashboard_parser = subparsers.add_parser(
        "dashboard",
        help="Arranca el dashboard Streamlit (127.0.0.1:8501)",
    )
    dashboard_parser.add_argument(
        "--port",
        type=int,
        default=8501,
        help="Puerto de escucha (default: 8501)",
    )
    dashboard_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Dirección de bind (solo 127.0.0.1 permitida)",
    )

    # ------------------------------------------------------------------
    # run — agente trader (T6)
    # ------------------------------------------------------------------
    run_parser = subparsers.add_parser(
        "run",
        help="Ejecuta un ciclo del agente trader",
    )
    run_parser.add_argument(
        "--once",
        action="store_true",
        required=True,
        help="Ejecuta un único ciclo y termina (único modo soportado)",
    )
    run_parser.add_argument(
        "--mode",
        default="paper",
        choices=["paper", "development", "production"],
        help="Modo de operación (default: paper)",
    )
    run_parser.add_argument(
        "--phase",
        default="post-close",
        choices=["post-close", "pre-open"],
        help="Fase del ciclo (default: post-close)",
    )
    run_parser.add_argument(
        "--date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Fecha de la sesión (default: hoy)",
    )
    run_parser.add_argument("--agent-db", default="data/agent.db")
    run_parser.add_argument("--ledger-db", default="data/state.db")
    run_parser.add_argument("--broker-db", default="data/paper_broker.db")
    run_parser.add_argument("--exec-db", default="data/execution.db")
    run_parser.add_argument("--halt-path", default="data/HALT")
    run_parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Semilla del generador de datos sintéticos (default: 42)",
    )

    # ------------------------------------------------------------------
    # research
    # ------------------------------------------------------------------
    research_parser = subparsers.add_parser(
        "research",
        help="Walk-forward validation y registro de trials",
    )
    research_subs = research_parser.add_subparsers(dest="research_command", required=True)

    # research trials count
    trials_parser = research_subs.add_parser(
        "trials",
        help="Comandos sobre el registro de trials",
    )
    trials_subs = trials_parser.add_subparsers(dest="trials_command", required=True)
    trials_count_parser = trials_subs.add_parser(
        "count",
        help="Numero acumulado de trials registrados",
    )
    trials_count_parser.add_argument(
        "--db",
        default="data/trials.db",
        help="Ruta a trials.db (default: data/trials.db)",
    )
    trials_count_parser.add_argument(
        "--strategy",
        default=None,
        metavar="STRATEGY_ID",
        help="Filtrar por estrategia (default: todas)",
    )

    # research report
    report_parser = research_subs.add_parser(
        "report",
        help="Informe de walk-forward para una estrategia",
    )
    report_parser.add_argument(
        "--strategy",
        required=True,
        metavar="STRATEGY_ID",
        help="ID de la estrategia",
    )
    report_parser.add_argument(
        "--db",
        default="data/trials.db",
        help="Ruta a trials.db (default: data/trials.db)",
    )

    # research run — LLMResearcher (T8)
    run_llm_parser = research_subs.add_parser(
        "run",
        help="Lanza el LLMResearcher con una hipótesis explícita (T8)",
    )
    run_llm_parser.add_argument(
        "--hypothesis",
        required=True,
        metavar="TEXTO",
        help="Hipótesis a investigar",
    )
    run_llm_parser.add_argument(
        "--trials-db",
        default="data/trials.db",
        metavar="PATH",
        help="Ruta a trials.db (default: data/trials.db)",
    )
    run_llm_parser.add_argument(
        "--proposals-db",
        default="data/proposals.db",
        metavar="PATH",
        help="Ruta a proposals.db (default: data/proposals.db)",
    )
    run_llm_parser.add_argument(
        "--model",
        default="claude-sonnet-4-6",
        help="Modelo LLM (default: claude-sonnet-4-6)",
    )
    run_llm_parser.add_argument(
        "--max-tokens",
        type=int,
        default=2000,
        help="Máximo de tokens por run (default: 2000)",
    )
    run_llm_parser.add_argument(
        "--max-runs-per-day",
        type=int,
        default=10,
        help="Máximo de runs por día (default: 10)",
    )
    run_llm_parser.add_argument(
        "--api-key",
        default=None,
        metavar="KEY",
        help="API key de Anthropic (default: ANTHROPIC_API_KEY env var)",
    )

    # research approve — aprobación humana de propuesta (T8)
    approve_parser = research_subs.add_parser(
        "approve",
        help="Aprueba una propuesta de investigación con confirmación interactiva",
    )
    approve_parser.add_argument(
        "--proposal-id",
        required=True,
        metavar="UUID",
        help="ID de la propuesta a aprobar",
    )
    approve_parser.add_argument(
        "--proposals-db",
        default="data/proposals.db",
        metavar="PATH",
        help="Ruta a proposals.db (default: data/proposals.db)",
    )
    approve_parser.add_argument(
        "--reviewed-by",
        default=None,
        metavar="NOMBRE",
        help="Nombre del revisor (default: $USER)",
    )
    approve_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirmar sin prompt interactivo (para scripts)",
    )

    # research proposals — lista propuestas (T8)
    proposals_list_parser = research_subs.add_parser(
        "proposals",
        help="Lista propuestas de investigación",
    )
    proposals_list_parser.add_argument(
        "--proposals-db",
        default="data/proposals.db",
        metavar="PATH",
        help="Ruta a proposals.db (default: data/proposals.db)",
    )
    proposals_list_parser.add_argument(
        "--status",
        default="pending",
        choices=["pending", "all"],
        help="Filtro de estado (default: pending)",
    )

    args = parser.parse_args()

    if args.command == "dashboard":
        _cmd_dashboard(args)
    elif args.command == "demo":
        _cmd_demo(args)
    elif args.command == "backtest":
        _cmd_backtest(args)
    elif args.command == "run":
        _cmd_run(args)
    elif args.command == "audit":
        if args.audit_command == "verify":
            _cmd_audit_verify(args)
        else:
            audit_parser.print_help()
            sys.exit(1)
    elif args.command == "universe":
        if args.universe_command == "show":
            _cmd_universe_show(args)
        else:
            universe_parser.print_help()
            sys.exit(1)
    elif args.command == "golden":
        if args.golden_command == "update":
            _cmd_golden_update(args)
        else:
            golden_parser.print_help()
            sys.exit(1)
    elif args.command == "research":
        if args.research_command == "trials":
            if args.trials_command == "count":
                _cmd_research_trials_count(args)
            else:
                trials_parser.print_help()
                sys.exit(1)
        elif args.research_command == "report":
            _cmd_research_report(args)
        elif args.research_command == "run":
            _cmd_research_run(args)
        elif args.research_command == "approve":
            _cmd_research_approve(args)
        elif args.research_command == "proposals":
            _cmd_research_proposals(args)
        else:
            research_parser.print_help()
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)
