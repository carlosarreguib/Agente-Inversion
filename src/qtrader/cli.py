from __future__ import annotations

import argparse
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


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

    args = parser.parse_args()

    if args.command == "demo":
        _cmd_demo(args)
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
        else:
            research_parser.print_help()
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)
