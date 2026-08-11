from __future__ import annotations

import argparse
import sys
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

    args = parser.parse_args()

    if args.command == "demo":
        _cmd_demo(args)
    elif args.command == "audit":
        if args.audit_command == "verify":
            _cmd_audit_verify(args)
        else:
            audit_parser.print_help()
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)
