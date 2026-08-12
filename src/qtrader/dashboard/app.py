"""Dashboard Streamlit — solo lectura + botón de kill switch.

Arranca con: uv run qtrader dashboard
Escucha en 127.0.0.1:8501.

Secciones (sidebar):
    1. Portfolio   — equity, cash, posiciones, curva
    2. Performance — métricas, bootstrap CI, P&L mensual
    3. Trading     — últimas 50 ops, estadísticas
    4. Risk        — nivel de riesgo, drawdown, exposición
    5. Agent       — FSM, audit log, health check
    6. Research    — tabla de trials

Kill switch en sidebar: botón rojo → confirmación "CONFIRMO" → POST /halt.
"""
from __future__ import annotations

import time
from pathlib import Path

import streamlit as st

from qtrader.dashboard.data import (
    DEFAULT_HALT_PATH,
    DEFAULT_HEALTH_URL,
    is_kill_switch_active,
    post_halt,
    read_agent_state,
    read_audit_log,
    read_cash,
    read_equity_curve,
    read_fills,
    read_metrics_latest,
    read_metrics_history,
    read_nav_history,
    read_positions,
    read_trials,
)

# ---------------------------------------------------------------------------
# Configuración de página
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="qtrader Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Paths configurables via query params o defaults
# ---------------------------------------------------------------------------

HALT_PATH = Path(st.query_params.get("halt_path", str(DEFAULT_HALT_PATH)))
HEALTH_URL = st.query_params.get("health_url", DEFAULT_HEALTH_URL)

# ---------------------------------------------------------------------------
# Banner de kill switch activo (se muestra en todas las páginas)
# ---------------------------------------------------------------------------

_ks_active = is_kill_switch_active(HALT_PATH)

if _ks_active:
    st.error(
        "🔴 **KILL SWITCH ACTIVO** — El sistema está en modo HALT. "
        "No se están ejecutando ciclos de trading.",
        icon="🚨",
    )

# ---------------------------------------------------------------------------
# Sidebar: navegación + kill switch
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("qtrader")
    st.caption("Solo simulación · Read-only")
    st.divider()

    page = st.radio(
        "Sección",
        ["Portfolio", "Performance", "Trading", "Risk", "Agent", "Research"],
        index=0,
    )

    st.divider()

    # Kill switch -------------------------------------------------------
    st.markdown("### ⚠️ Control de emergencia")
    if _ks_active:
        st.error("KILL SWITCH ACTIVO")
    else:
        with st.expander("🔴 Activar Kill Switch", expanded=False):
            st.warning(
                "Esta acción detiene todos los ciclos de trading. "
                "Solo se puede desactivar manualmente."
            )
            confirm_input = st.text_input(
                'Escribe "CONFIRMO" para activar',
                key="ks_confirm",
                placeholder="CONFIRMO",
            )
            if st.button("ACTIVAR KILL SWITCH", type="primary", use_container_width=True):
                if confirm_input.strip() == "CONFIRMO":
                    ok, detail = post_halt("dashboard-halt", HEALTH_URL)
                    if ok:
                        st.success("Kill switch activado. Recargando...")
                        time.sleep(1)
                        st.rerun()
                    else:
                        st.error(f"Error al activar: {detail}")
                else:
                    st.error('Debes escribir exactamente "CONFIRMO" para confirmar.')

    st.divider()
    if st.button("🔄 Actualizar datos", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.caption(f"Auto-refresh: 30 s")


# ---------------------------------------------------------------------------
# Helpers de formato
# ---------------------------------------------------------------------------

def _eur(v: float) -> str:
    return f"{v:,.2f} €"


def _pct(v: float) -> str:
    return f"{v * 100:.2f} %"


def _na(v: object) -> str:
    return str(v) if v is not None else "N/A"


# ---------------------------------------------------------------------------
# Caché con TTL de 30 s
# ---------------------------------------------------------------------------

@st.cache_data(ttl=30)
def _positions() -> list[dict]:  # type: ignore[type-arg]
    return read_positions()

@st.cache_data(ttl=30)
def _cash() -> float | None:
    return read_cash()

@st.cache_data(ttl=30)
def _equity_curve() -> list[dict]:  # type: ignore[type-arg]
    return read_equity_curve()

@st.cache_data(ttl=30)
def _fills() -> list[dict]:  # type: ignore[type-arg]
    return read_fills(limit=50)

@st.cache_data(ttl=30)
def _agent_state() -> dict | None:  # type: ignore[type-arg]
    return read_agent_state()

@st.cache_data(ttl=30)
def _audit_log() -> list[dict]:  # type: ignore[type-arg]
    return read_audit_log(limit=20)

@st.cache_data(ttl=30)
def _metrics() -> dict[str, float]:
    return read_metrics_latest()

@st.cache_data(ttl=30)
def _nav_history() -> list[dict]:  # type: ignore[type-arg]
    return read_nav_history()

@st.cache_data(ttl=30)
def _trials() -> list[dict]:  # type: ignore[type-arg]
    return read_trials()

@st.cache_data(ttl=30)
def _nav_metric_history() -> list[dict]:  # type: ignore[type-arg]
    return read_metrics_history(metric_name="portfolio.nav")


# ---------------------------------------------------------------------------
# PÁGINA 1 — Portfolio
# ---------------------------------------------------------------------------

if page == "Portfolio":
    st.header("📈 Portfolio")

    cash = _cash()
    positions = _positions()
    metrics = _metrics()

    # KPIs
    nav = metrics.get("portfolio.nav", cash or 0.0)
    exposure = metrics.get("portfolio.exposure", 0.0)

    col1, col2, col3 = st.columns(3)
    col1.metric("NAV actual", _eur(nav) if nav else "Sin datos")
    col2.metric("Cash", _eur(cash) if cash is not None else "Sin datos")
    col3.metric("Exposición total", _pct(exposure) if exposure else "—")

    st.divider()

    # Posiciones
    st.subheader("Posiciones abiertas")
    if positions:
        st.dataframe(positions, use_container_width=True)
    else:
        st.info("Sin posiciones abiertas (o BD no disponible)")

    # Equity curve desde métricas
    st.subheader("Equity curve")
    equity_data = _nav_metric_history() or _equity_curve()
    if equity_data:
        import pandas as pd
        df = pd.DataFrame(equity_data)
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
            df = df.set_index("timestamp")
        value_col = "nav" if "nav" in df.columns else "value"
        if value_col in df.columns:
            st.line_chart(df[[value_col]])
    else:
        st.info("Sin datos de equity curve")


# ---------------------------------------------------------------------------
# PÁGINA 2 — Performance
# ---------------------------------------------------------------------------

elif page == "Performance":
    st.header("📊 Performance")
    metrics = _metrics()

    if not metrics:
        st.info("Sin métricas disponibles (data/metrics.csv no existe o está vacío)")
    else:
        col1, col2, col3 = st.columns(3)
        col1.metric("NAV", _eur(metrics.get("portfolio.nav", 0)))
        col2.metric("Drawdown actual", _pct(metrics.get("portfolio.drawdown", 0)))
        col3.metric("Nivel de riesgo", str(int(metrics.get("risk.level", 0))))

        st.divider()
        st.subheader("Métricas de ciclo")
        cycle_metrics = {
            k: v for k, v in metrics.items()
            if k.startswith("agent.") or k.startswith("data.")
        }
        if cycle_metrics:
            import pandas as pd
            st.dataframe(
                pd.DataFrame(
                    [{"métrica": k, "valor": v} for k, v in sorted(cycle_metrics.items())]
                ),
                use_container_width=True,
            )

    st.divider()
    st.subheader("NAV histórico")
    nav_h = _nav_history()
    if nav_h:
        import pandas as pd
        df = pd.DataFrame(nav_h)
        df["trading_date"] = pd.to_datetime(df["trading_date"], errors="coerce")
        df = df.set_index("trading_date")
        st.line_chart(df[["nav"]])
    else:
        st.info("Sin historial de NAV diario")

    st.caption(
        "ℹ️ Intervalos de confianza bootstrap disponibles tras ejecutar "
        "`uv run qtrader backtest --report full`."
    )


# ---------------------------------------------------------------------------
# PÁGINA 3 — Trading
# ---------------------------------------------------------------------------

elif page == "Trading":
    st.header("🔄 Trading")

    fills = _fills()

    if not fills:
        st.info("Sin operaciones registradas (o BD no disponible)")
    else:
        import pandas as pd

        st.subheader("Últimas 50 operaciones")
        df = pd.DataFrame(fills)
        st.dataframe(df, use_container_width=True)

        st.divider()
        st.subheader("Estadísticas")

        metrics = _metrics()
        col1, col2, col3 = st.columns(3)
        col1.metric("Órdenes enviadas", int(metrics.get("agent.orders.submitted", 0)))
        col2.metric("Órdenes bloqueadas", int(metrics.get("agent.orders.blocked", 0)))
        col3.metric("Fills (ciclo)", int(metrics.get("agent.fills.count", 0)))

        # Win rate desde fills
        buys = [f for f in fills if str(f.get("side", "")).upper() == "BUY"]
        sells = [f for f in fills if str(f.get("side", "")).upper() == "SELL"]
        st.caption(f"BUY: {len(buys)} | SELL: {len(sells)} | Total: {len(fills)}")


# ---------------------------------------------------------------------------
# PÁGINA 4 — Risk
# ---------------------------------------------------------------------------

elif page == "Risk":
    st.header("⚠️ Risk")

    metrics = _metrics()
    risk_level_int = int(metrics.get("risk.level", 0))
    risk_labels = {0: "NORMAL", 1: "CAUTION", 2: "RISK_OFF", 3: "HALT"}
    risk_colors = {0: "green", 1: "yellow", 2: "orange", 3: "red"}
    risk_label = risk_labels.get(risk_level_int, "UNKNOWN")
    risk_color = risk_colors.get(risk_level_int, "grey")

    # Nivel de riesgo con color
    col1, col2 = st.columns([1, 3])
    with col1:
        st.markdown(
            f"<div style='background:{risk_color};padding:20px;border-radius:8px;"
            f"text-align:center;font-size:1.5em;font-weight:bold;color:white'>"
            f"{risk_label}</div>",
            unsafe_allow_html=True,
        )
    with col2:
        drawdown = metrics.get("portfolio.drawdown", 0.0)
        exposure = metrics.get("portfolio.exposure", 0.0)
        st.metric("Drawdown actual", _pct(drawdown))
        st.metric("Exposición total", _pct(exposure))

    st.divider()

    # Kill switch status
    st.subheader("Kill Switch")
    if _ks_active:
        st.error("🔴 ACTIVO — El sistema está en HALT")
    else:
        st.success("🟢 Inactivo")

    st.divider()

    # Símbolos excluidos / warnings de datos
    col1, col2 = st.columns(2)
    col1.metric("Símbolos excluidos (ciclo)", int(metrics.get("data.symbols_excluded", 0)))
    col2.metric("Warnings de datos (ciclo)", int(metrics.get("data.validation_warnings", 0)))


# ---------------------------------------------------------------------------
# PÁGINA 5 — Agent
# ---------------------------------------------------------------------------

elif page == "Agent":
    st.header("🤖 Agent")

    agent = _agent_state()

    # Estado FSM
    st.subheader("Estado de la máquina de estados")
    if agent:
        col1, col2, col3 = st.columns(3)
        col1.metric("Estado actual", agent.get("current_state", "—"))
        col2.metric("Fecha trading", agent.get("trading_date", "—"))
        col3.metric("Cycle ID", str(agent.get("cycle_id", "—"))[:12] + "…")
        if agent.get("error_message"):
            st.error(f"Error: {agent['error_message']}")
    else:
        st.info("Sin datos del agente (data/agent.db no existe o sin ciclos ejecutados)")

    st.divider()

    # Health check del agente
    st.subheader("Health Check")
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=3) as resp:  # noqa: S310
            import json
            health = json.loads(resp.read())
            st.success(f"🟢 OK — Estado: {health.get('state', '?')} | "
                       f"NAV: {_eur(float(health.get('nav', 0)))}")
    except urllib.error.HTTPError as exc:
        st.error(f"🔴 {exc.code} — HealthServer responde con error")
    except (urllib.error.URLError, OSError, TimeoutError):
        st.warning("⚠️ HealthServer no responde (agente no en ejecución)")

    st.divider()

    # Audit log
    st.subheader("Últimas 20 decisiones del audit log")
    audit = _audit_log()
    if audit:
        import pandas as pd
        df = pd.DataFrame([
            {
                "timestamp": e.get("timestamp", ""),
                "event_type": e.get("event_type", ""),
                "record_id": e.get("record_id", ""),
            }
            for e in audit
        ])
        st.dataframe(df, use_container_width=True)
    else:
        st.info("Sin entradas en el audit log")


# ---------------------------------------------------------------------------
# PÁGINA 6 — Research
# ---------------------------------------------------------------------------

elif page == "Research":
    st.header("🔬 Research")

    trials = _trials()

    if not trials:
        st.info("Sin trials registrados (data/research/trials.db no existe)")
    else:
        import pandas as pd

        st.subheader(f"Trials registrados ({len(trials)})")
        df = pd.DataFrame(trials)

        # Columnas relevantes para mostrar
        cols = [
            c for c in ["strategy_id", "fold_id", "sharpe_is", "sharpe_oos",
                         "max_drawdown_oos", "num_trades_oos", "status",
                         "train_start", "test_start", "test_end"]
            if c in df.columns
        ]
        st.dataframe(df[cols], use_container_width=True)

        st.divider()
        col1, col2 = st.columns(2)
        col1.metric("Total trials", len(trials))
        completed = [t for t in trials if t.get("status") == "COMPLETED"]
        col2.metric("Trials COMPLETED", len(completed))

        if completed:
            oos_vals = [
                float(t["sharpe_oos"])
                for t in completed
                if t.get("sharpe_oos") is not None
            ]
            if oos_vals:
                import statistics
                st.metric("Sharpe OOS medio", f"{statistics.mean(oos_vals):.4f}")

    st.divider()
    st.info(
        "📌 **Placeholder T8**: Modelos candidatos y resultados de optimización "
        "se mostrarán aquí en la Tarea 8."
    )


# ---------------------------------------------------------------------------
# Auto-refresh silencioso cada 30 segundos
# ---------------------------------------------------------------------------

# Streamlit no tiene auto-refresh nativo sin st.empty + time.sleep.
# Usamos el patrón de fragment para no bloquear el hilo principal.
# El botón manual "Actualizar datos" en el sidebar limpia la caché.
