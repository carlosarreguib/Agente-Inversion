# Plan de ejecución para Claude Code — proyecto `qtrader`

Cómo usar este documento:

1. Crea el repo vacío y copia `CLAUDE.md` (fichero aparte) a la raíz.
2. Copia este plan a `docs/PLAN.md`.
3. Abre Claude Code en el repo y arranca con el prompt de la sección **"Prompt de arranque"**.
4. Una tarea por sesión. No pasar a la siguiente sin que el criterio de aceptación se cumpla ejecutando el comando indicado.

---

## Prompt de arranque (pégalo tal cual en Claude Code)

```
Lee CLAUDE.md y docs/PLAN.md completos antes de escribir nada.

Vamos a construir el proyecto por fases. Hoy trabajamos SOLO en la Tarea 0.1.

Reglas de esta sesión:
- No escribas código de fases posteriores, ni siquiera "de aviso".
- Ninguna funcionalidad sin su test. Un test que falla se arregla en el código,
  nunca debilitando el test.
- Cuando termines, ejecuta el comando de aceptación de la tarea y pégame la salida real.
- Si una decisión es ambigua, toma la razonable, escríbela en docs/DECISIONS.md
  con formato ADR y sigue. No me preguntes por detalles menores.

Empieza confirmándome en 5 líneas qué vas a hacer en la Tarea 0.1 y qué ficheros
vas a crear. Luego hazlo.
```

---

## Fase 0 — Tracer bullet (1 semana)

Objetivo: un camino end-to-end mínimo que atraviese **todas** las capas antes de construir ninguna en profundidad. Esto valida la arquitectura antes de invertir en ella. Es la desviación más importante respecto al plan original (que construye capa por capa y descubre los problemas de integración al final).

### T0.1 — Esqueleto y contratos
- `uv` + `pyproject.toml`, Python 3.12, ruff + mypy strict, pytest.
- `src/qtrader/core/types.py`: modelos Pydantic v2 congelados para `Bar`, `Instrument`, `Signal`, `TargetPosition`, `Order`, `Fill`, `Position`, `RiskDecision`, `AuditRecord`. Todos inmutables (`frozen=True`), todo dinero en `Decimal`, todo timestamp `datetime` tz-aware en UTC.
- `.gitignore`, `.env.example`, hook pre-commit con `gitleaks`.
- **Aceptación**: `uv run pytest && uv run mypy src --strict && uv run ruff check .` sale en verde. `uv run python -c "from qtrader.core.types import Order; Order(...)"` valida y rechaza un `Order` con cantidad negativa.

### T0.2 — Camino completo con datos falsos
Un único comando que recorre: datos sintéticos → señal trivial ("comprar SPY si cierre > SMA20") → risk engine trivial (un solo límite: máx 20 % por posición) → paper broker → ledger SQLite → informe por consola.
- **Aceptación**: `uv run qtrader demo --days 60` imprime equity inicial 1000,00 €, equity final, nº de trades y P&L, y `sqlite3 data/state.db "select count(*) from fills"` devuelve > 0. Ejecutarlo dos veces con la misma semilla da resultados idénticos byte a byte.

### T0.3 — Auditoría y persistencia desde el minuto 1
- Log append-only con hash encadenado en SQLite. Cada decisión registra: timestamp, versión de código (git sha), versión de config (hash), datos usados (hash del snapshot), estrategia, parámetros, riesgo calculado, decisión, motivo.
- **Aceptación**: `uv run qtrader audit verify` recorre la cadena y devuelve OK. Si modificas manualmente una fila con `sqlite3`, devuelve el índice del registro corrupto.

---

## Fase 1 — Datos

### T1.1 — Capa de proveedor abstracta
`MarketDataProvider` como Protocol. Implementaciones: `SyntheticProvider` (para tests), `CSVProvider`, `<ProveedorReal>Provider`.
- Almacenamiento: **Parquet particionado por año** + índice DuckDB. Precios **sin ajustar** + tabla separada `corporate_actions`.
- `get_bars(symbols, start, end, as_of)` — el parámetro `as_of` aplica solo las corporate actions conocidas en esa fecha. **Este parámetro es el que impide el look-ahead en los ajustes; no es opcional.**
- **Aceptación**: test que carga una serie con un split en fecha T, pide `as_of=T-1d` y `as_of=T+1d`, y verifica que los precios anteriores a T difieren entre ambas llamadas.

### T1.2 — Validación de datos
Detectar: huecos, barras duplicadas, OHLC inconsistente (low > high), precios cero/negativos, volumen cero, saltos > N sigmas sin corporate action asociada, series congeladas (mismo precio N días).
- Política: dato sospechoso → instrumento excluido del universo ese día, registrado en auditoría. **Nunca** interpolar silenciosamente.
- **Aceptación**: `uv run pytest tests/data/test_validation.py` cubre los 8 casos con fixtures que los reproducen.

### T1.3 — Universo y calendario
Filtros configurables (liquidez, ADV, precio mínimo, spread, divisa, bolsa). Calendario de mercado con `exchange_calendars`. Composición del universo **guardada con fecha** — nunca recalculada retroactivamente.
- **Aceptación**: `uv run qtrader universe show --date 2023-06-15` lista los instrumentos y no incluye ninguno que empezara a cotizar después.

---

## Fase 2 — Backtester

### T2.1 — Motor event-driven
Bucle: `for each bar → publicar evento → estrategias generan señales → portfolio → risk → órdenes → simulación de fill en la barra SIGUIENTE`.
- Regla dura: **una señal generada con la barra de cierre de T se ejecuta contra la apertura de T+1**, con slippage. Nunca contra el cierre de T.
- **Aceptación**: test "oráculo": una estrategia que intenta hacer trampa (usa el cierre de T+1 para decidir en T) debe hacer que el motor lance excepción, no que gane dinero.

### T2.2 — Modelo de costes
Comisión (mínimo + variable + máximo), spread (half-spread estimado por instrumento), slippage (modelo de impacto ∝ √(tamaño/ADV)), límite de participación (máx 10 % del volumen de la barra), gaps y huecos de liquidez.
- **Aceptación**: `uv run qtrader backtest --strategy buyhold --no-costs` vs `--with-costs` sobre un año; la diferencia debe coincidir con el cálculo manual de comisiones documentado en el test.

### T2.3 — Backtest de referencia ("golden")
Un backtest fijo con datos versionados cuyos resultados numéricos quedan congelados en un fichero de referencia.
- **Aceptación**: `uv run pytest tests/golden/` detecta cualquier cambio de comportamiento del motor. Es la red de seguridad contra regresiones durante el resto del proyecto.

### T2.4 — Validación temporal
Walk-forward con purga y embargo. Splits: train 3 años / test 1 año, rolling. Registro obligatorio de **cada** configuración evaluada en `experiments/trials.db`.
- **Aceptación**: `uv run qtrader research trials count` devuelve el número acumulado de configuraciones probadas, y el informe de cualquier estrategia incluye su Deflated Sharpe Ratio calculado con ese N.

---

## Fase 3 — Primera estrategia (una sola)

### T3.1 — Cross-sectional momentum
12-1 (retorno de 12 meses excluyendo el último), ranking transversal, quintil superior, rebalanceo mensual. Es la estrategia más replicada de la literatura; sirve de calibración del sistema, no de fuente de alpha.
- **Aceptación**: el backtest 2010–2024 sobre el universo definido produce métricas dentro del rango publicado en la literatura (Sharpe 0,3–0,7, MaxDD 30–50 %). Si sale Sharpe 2,5, **hay un bug o leakage**: buscarlo antes de continuar.

### T3.2 — Framework de métricas
Retorno, Sharpe, Sortino, Calmar, MaxDD y duración, volatilidad, turnover, hit rate, profit factor, exposición media, P&L por instrumento y por estrategia. Todo con intervalos de confianza bootstrap.
- **Aceptación**: comparación contra `pandas`/`empyrical` calculado a mano en el test para 3 métricas clave.

---

## Fase 4 — Portfolio y Risk Engine

### T4.1 — Portfolio construction
Empezar con **volatility targeting + equal risk contribution simplificado**. Mean-variance queda descartado en fase inicial: con 1.000 € y estimaciones ruidosas de covarianza, optimiza el ruido. Justificación en ADR.
- Restricciones: máx posiciones, máx peso por posición, máx exposición sectorial, mínimo tamaño de orden (por la comisión mínima), banda de no-rebalanceo (no rebalancear si la desviación < X %, para controlar turnover).
- **Aceptación**: property test — para cualquier vector de señales aleatorio, la cartera resultante nunca viola ninguna restricción configurada.

### T4.2 — Risk Engine
Componente **puro y determinista**: `RiskEngine.evaluate(portfolio, proposed_orders, market_state) -> RiskDecision`. Sin I/O, sin estado oculto, sin llamadas a red. Devuelve `APPROVE | REDUCE(size) | REJECT(reason)`.
- Niveles: `NORMAL → CAUTION → RISK_OFF → HALT`, con transiciones basadas en drawdown desde máximo (p.ej. 0-5 % / 5-9 % / 9-13 % / >13 %) y reducción progresiva del riesgo por operación en cada nivel. Objetivo: llegar al 15 % de MaxDD debería ser un evento excepcional, no el umbral de reacción.
- **Aceptación crítica**: property-based tests con `hypothesis` que generan carteras y órdenes arbitrarias y verifican que **ninguna combinación de entradas produce una orden que viole un límite duro**. Este test es el más importante del repositorio.

### T4.3 — Capas 2 y 3 de defensa
Rate limit en Execution Engine (máx N órdenes/min, M/día, X € nocional/día) y validación de sanidad en Broker Interface (precio dentro de ±10 % del último close, tamaño ≤ 10 % del ADV). **Independientes del Risk Engine, con su propia config y sus propios tests.**
- **Aceptación**: test que inyecta un Risk Engine deliberadamente roto (aprueba todo) y verifica que las capas 2 y 3 siguen bloqueando la orden absurda.

---

## Fase 5 — Paper broker

### T5.1 — Broker Interface
Protocol común: `submit_order`, `cancel_order`, `get_positions`, `get_account`, `get_open_orders`, `get_fills_since`. Diseñado ya contra la semántica de IBKR (order types, TIF, estados) para que el swap sea de una línea de config.

### T5.2 — Paper broker
Simula fills contra datos reales con el mismo modelo de costes que el backtester (**el mismo código, no una copia**), comisiones, dividendos, splits, cash, P&L realizado y no realizado.
- **Aceptación**: correr el mismo periodo en backtest y en paper broker con datos idénticos debe dar resultados idénticos. Si divergen, uno de los dos está mal.

### T5.3 — Idempotencia y recuperación
`client_order_id` determinista + write-ahead log de intenciones + reconciliación al arranque.
- **Aceptación**: test que mata el proceso (`SIGKILL`) entre el WAL y la confirmación del broker, lo reinicia, y verifica que **no se duplica la orden**. Repetir para: matar durante el fill, matar durante la actualización del ledger, matar durante el rebalanceo.

### T5.4 — Kill switch
Fichero `HALT` propiedad de otro usuario/permisos read-only para el agente + supervisor con dead man's switch.
- **Aceptación**: `touch $(HALT_PATH)` mientras el agente corre → el agente deja de enviar órdenes en < 1 ciclo y lo registra en auditoría. Y: el agente **no puede** borrar ese fichero (test que lo intenta y espera `PermissionError`).

---

## Fase 6 — Agente Trader (aún sin LLM)

Orquestador determinista del ciclo diario: datos → validación → señales → portfolio → risk → órdenes → ejecución → reconciliación → auditoría. Máquina de estados explícita con estados persistidos, para que un reinicio a mitad de ciclo se reanude correctamente.
- **Aceptación**: `uv run qtrader run --once --mode paper` completa un ciclo y deja traza auditable completa. Matarlo en cada uno de sus estados y reiniciarlo converge siempre a un estado consistente.

---

## Fase 7 — Observabilidad y dashboard

- structlog → JSON lines, métricas, health checks, alertas (Telegram/email) para: HALT activado, reconciliación fallida, datos congelados, broker desconectado, drawdown cruzando nivel.
- Dashboard Streamlit read-only + botón de kill switch. **Bind a `127.0.0.1` únicamente.**
- **Aceptación**: matar la fuente de datos → alerta en < 2 min. El dashboard muestra todas las secciones del §29 del prompt original.

---

## Fase 8 — Researcher (aquí entra el LLM, y solo aquí)

- Herramientas offline de generación y evaluación de hipótesis. Sin acceso alguno al broker ni a producción.
- El LLM: propone hipótesis, escribe configuraciones de experimento, redacta informes, explica decisiones ya tomadas. Todo output validado contra schema.
- **Aceptación**: auditoría del código que confirme que el módulo del LLM no importa nada de `execution/` ni de `brokers/`. Test que lo verifica automáticamente (`import-linter` con contrato de capas).

---

## Fase 9 — Robustez

Sensibilidad de parámetros, Monte Carlo (bootstrap de retornos y de orden de trades), PBO vía CSCV, Deflated Sharpe, análisis por régimen de mercado, simulación de fallos (los 12 escenarios del §28 del prompt como tests automatizados).

---

## Fase 10 — Paper trading prolongado

Mínimo 6 meses. **Criterios de salida ponderados hacia lo operativo:**

| Criterio | Peso | Umbral |
|---|---|---|
| Incidentes operativos no recuperados | 30 % | 0 |
| Reconciliación diaria limpia | 20 % | 100 % de sesiones |
| Slippage real vs modelado | 20 % | dentro de ±5 bps |
| Cobertura de auditoría | 10 % | 100 % de decisiones trazables |
| Nº de trades | 10 % | ≥ 100 |
| Performance vs backtest | 10 % | dentro de 1,5 σ del intervalo simulado |

Nótese que el **retorno absoluto no aparece**. Es deliberado: 6 meses no tienen potencia estadística para evaluarlo, y usarlo como criterio de promoción es el error más común del sector.

---

## Fases 11–12 — IBKR y producción

Solo tras firma humana explícita. Orden: `ib_insync`/`ib_async` contra paper account de IBKR → validación de 3 meses → cuenta real con capital de la fase, `Read-Only API` desactivado como paso manual, Trusted IPs, cuenta segregada.

---

## Reglas para Claude Code durante todo el proyecto

1. **Una tarea por sesión.** El contexto se degrada; tareas grandes producen código que compila y no funciona.
2. **Test primero en el Risk Engine y en la idempotencia.** En el resto, test junto al código.
3. **Prohibido debilitar un test para que pase.** Si Claude Code propone cambiar un assert para que el test pase, rechazar y hacer que arregle el código.
4. **Cada decisión no trivial → un ADR** en `docs/DECISIONS.md`.
5. **Al final de cada tarea, pedir la salida real del comando de aceptación**, no un resumen de lo que debería salir.
6. Cuando aparezca una métrica sospechosamente buena, **la hipótesis por defecto es que hay un bug**, no que la estrategia es genial.
