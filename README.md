<div align="center">

<pre>
 ██████╗     ████████╗██████╗  █████╗ ██████╗ ███████╗██████╗
██╔═══██╗    ╚══██╔══╝██╔══██╗██╔══██╗██╔══██╗██╔════╝██╔══██╗
██║   ██║       ██║   ██████╔╝███████║██║  ██║█████╗  ██████╔╝
██║▄▄ ██║       ██║   ██╔══██╗██╔══██║██║  ██║██╔══╝  ██╔══██╗
╚██████╔╝       ██║   ██║  ██║██║  ██║██████╔╝███████╗██║  ██║
 ╚══▀▀═╝        ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝ ╚══════╝╚═╝  ╚═╝
</pre>

### Agente de inversión cuantitativa · **solo simulación**

*Un sistema que pierde un 3 % con trazabilidad completa es un éxito.*<br/>
*Uno que gana un 40 % con una orden duplicada sin explicar es un fracaso.*

<br/>

![Simulación](https://img.shields.io/badge/modo-SOLO%20SIMULACI%C3%93N-0b7285?style=for-the-badge&labelColor=1a1a1a)
[![Fases](https://img.shields.io/badge/fases-0--9%20de%2012-2b8a3e?style=for-the-badge&labelColor=1a1a1a)](#estado-actual-fases-0-9-completadas)
[![Tests](https://img.shields.io/badge/tests-620-2b8a3e?style=for-the-badge&labelColor=1a1a1a)](#verificación)

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![mypy](https://img.shields.io/badge/mypy-strict-2b8a3e)
![ruff](https://img.shields.io/badge/ruff-passing-2b8a3e)
![import-linter](https://img.shields.io/badge/import--linter-17%20contratos-2b8a3e)
![Capital](https://img.shields.io/badge/capital-15.000%20%E2%82%AC%20simulados-5c5f66)
![Datos](https://img.shields.io/badge/presupuesto%20datos-0%20%E2%82%AC-5c5f66)

<table>
<tr>
<td align="center" width="25%"><strong>3</strong><br/><sub>capas de defensa<br/>independientes</sub></td>
<td align="center" width="25%"><strong>0</strong><br/><sub>rutas de código<br/>a un broker real</sub></td>
<td align="center" width="25%"><strong>16 ms</strong><br/><sub>ventana TOCTOU<br/>(presupuesto: 100)</sub></td>
<td align="center" width="25%"><strong>SHA-256</strong><br/><sub>auditoría con<br/>hash encadenado</sub></td>
</tr>
</table>

</div>

---

> [!WARNING]
> **Ningún componente de IA puede enviar órdenes.** Un LLM puede como máximo llamar a `propose_order`; `submit_order` no existe en el toolset de ningún agente. La decisión y el envío son deterministas. El paso a dinero real exige una decisión humana firmada y registrada — no un flag.

Plataforma experimental de gestión cuantitativa de cartera. **Opera exclusivamente en simulación.** No existe ninguna ruta de código que envíe una orden a un broker real, y activarla requerirá una decisión humana firmada y registrada.

- **Solo swing.** Barras diarias, holding 15-60 días, rebalanceo semanal. No hay código intradía en este repositorio.
- **Capital de simulación:** 15.000 €. Tamaño mínimo de orden: 1.000 €.
- **Presupuesto de datos:** 0 €. Fuentes gratuitas con cross-validación entre dos proveedores.
- **Universo ETF-céntrico** (~50 instrumentos), congelado y versionado con fecha de declaración.

El objetivo primario es la **corrección operativa, no la rentabilidad**: de ahí el lema de la cabecera. Un resultado sospechosamente bueno se trata como un bug hasta demostrar lo contrario.

> **El backtest genera hipótesis; no valida.** La validación es walk-forward más paper trading sobre datos nunca vistos. Este framing es deliberado y no se suaviza en ningún informe. El benchmark honesto es un ETF indexado global después de costes e impuestos, y lo más probable es no superarlo.

Las reglas que gobiernan el proyecto están en [CLAUDE.md](CLAUDE.md) (invariantes de seguridad y de corrección cuantitativa), el plan completo en [docs/01-PLAN-CLAUDE-CODE.md](docs/01-PLAN-CLAUDE-CODE.md) y las decisiones de arquitectura en [docs/DECISIONS.md](docs/DECISIONS.md).

---

## Estado actual: Fases 0-9 completadas

| Fase | Contenido | Estado |
|---|---|---|
| 0 | Tracer bullet: esqueleto, contratos, camino end-to-end, auditoría | Completada |
| 1 | Capa de datos: proveedores, validación, cross-validación, universo point-in-time | Completada |
| 2 | Backtester event-driven, modelo de costes, golden backtest, walk-forward | Completada |
| 3 | Momentum 12-1 cross-sectional y framework de métricas | Completada |
| 4 | Construcción de cartera, Risk Engine y las tres capas de defensa | Completada |
| 5 | Paper broker con idempotencia, recuperación ante crash y kill switch | Completada |
| 6 | Agente trader determinista como máquina de estados | Completada |
| 7 | Observabilidad: logging estructurado, métricas, alertas, dashboard | Completada |
| 8 | Researcher LLM: toolset offline, generación de hipótesis, sin acceso a producción | Completada |
| 9 | Robustez estadística: sensibilidad, Monte Carlo, PBO/CSCV, DSR, 12 escenarios | Completada |
| 10-12 | Paper trading prolongado, IBKR, producción | Pendientes |

**620 tests** (620 pasan, 1 se omite en Windows), `mypy --strict` en verde, 17 contratos de `import-linter` sin romper. Ningún test accede a la red.

---

## Fase 0 — Tracer bullet

La Fase 0 es un **tracer bullet**: un camino end-to-end mínimo que atraviesa *todas* las capas de la arquitectura antes de construir ninguna en profundidad. Su propósito no es producir un sistema útil, sino validar que las fronteras entre módulos encajan antes de invertir en ellos. Cada capa existe en su versión más simple defendible.

| Tarea | Contenido | Estado |
|---|---|---|
| T0.1 | Esqueleto, tooling y contratos Pydantic congelados | Completada |
| T0.2 | Camino completo end-to-end con datos sintéticos | Completada |
| T0.3 | Auditoría con hash encadenado y persistencia | Completada |

### T0.1 — Esqueleto y contratos

Proyecto Python 3.12 gestionado con `uv` y lockfile, con `ruff`, `mypy --strict` y `pytest` configurados, más un hook `pre-commit` con `gitleaks` para impedir que un secreto entre en el historial.

[src/qtrader/core/types.py](src/qtrader/core/types.py) define los nueve contratos que cruzan fronteras de módulo — `Bar`, `Instrument`, `Signal`, `TargetPosition`, `Order`, `Fill`, `Position`, `RiskDecision`, `AuditRecord` — todos como modelos Pydantic v2 con `frozen=True`. Tres invariantes se aplican en el propio tipo, no en el código que lo usa:

- **Dinero en `Decimal`, nunca `float`.** El alias `StrictDecimal` lleva un `BeforeValidator` que *rechaza* un `float` en tiempo de validación en lugar de convertirlo silenciosamente. Un error de precisión no puede entrar por descuido.
- **Timestamps tz-aware en UTC**, vía `AwareDatetime`. Un `datetime` naive no valida.
- **Validadores de dominio**: `Bar` rechaza `low > high`, precios ≤ 0 y volumen negativo; `Order` y `Fill` rechazan cantidades ≤ 0 (la dirección la lleva `side`, nunca el signo); `Signal.strength` y `TargetPosition.weight` están acotados a [0, 1]; un `RiskDecision` con decisión `REDUCE` exige `adjusted_quantity`.

### T0.2 — Camino completo con datos falsos

Un único comando recorre las seis capas. El flujo, en [src/qtrader/engine.py](src/qtrader/engine.py):

```
datos sintéticos → señal SMA20 → risk engine → paper broker → ledger SQLite → informe
```

- **Datos** — `SyntheticProvider` (`src/qtrader/data/synthetic.py`) genera barras OHLCV deterministas desde una semilla, usando una instancia aislada de `random.Random` en vez del estado global, de modo que la reproducibilidad no depende del resto del programa.
- **Estrategia** — [src/qtrader/strategies/sma.py](src/qtrader/strategies/sma.py): largo si el cierre supera la SMA de 20 sesiones, plano en caso contrario. Devuelve `None` sin historia suficiente en lugar de inventar una señal.
- **Riesgo** — [src/qtrader/risk/engine.py](src/qtrader/risk/engine.py): función **pura**, sin I/O, sin estado global, sin aleatoriedad. Un único límite por ahora (máximo 20 % del equity por posición) que devuelve `APPROVE`, `REDUCE` con cantidad ajustada, o `REJECT` con motivo.
- **Broker** — [src/qtrader/brokers/paper.py](src/qtrader/brokers/paper.py): simula el fill con comisión (mínimo 1,25 € o 0,10 % del nominal).
- **Ledger** — [src/qtrader/ledger/sqlite.py](src/qtrader/ledger/sqlite.py): tablas `fills`, `positions` y `audit_log`. Los `Decimal` se persisten como `TEXT` para no perder precisión al pasar por el tipo `REAL` de SQLite.

**La regla anti-look-ahead está implementada en el bucle, no documentada como intención.** La señal del día `i` se calcula con `bars[:i+1]` y el fill se ejecuta contra `bars[i+1].open` — la apertura de T+1, nunca el cierre que generó la señal.

### T0.3 — Auditoría y persistencia

`audit_log` es un log append-only con **hash encadenado**: cada registro se serializa a JSON canónico (claves ordenadas), se le calcula un SHA-256, y ese hash se guarda junto al registro siguiente como `previous_hash`. Alterar una fila rompe la cadena a partir de ella.

Cada decisión registra el git SHA del código, el hash de la configuración, el hash del snapshot de datos, la estrategia y sus parámetros, la salida del risk engine, la decisión y el motivo — los ingredientes necesarios para reconstruir después *por qué* se hizo una operación.

`verify_chain()` recorre la cadena y aplica dos comprobaciones independientes: que el hash almacenado corresponde a los datos, y que el enlace `previous_hash` apunta al registro anterior. Devuelve los `rowid` corruptos en lugar de un booleano, de modo que el fallo señala dónde está.

---

## Fase 1 — Datos

Capa de proveedor abstracta (`Protocol` en [src/qtrader/data/provider.py](src/qtrader/data/provider.py)) con implementaciones sintética, CSV, Tiingo y yfinance. **Precios sin ajustar** más tabla separada de corporate actions: el parámetro `as_of` de `get_bars()` es obligatorio y aplica solo las corporate actions conocidas en esa fecha, que es lo que impide el look-ahead en los ajustes.

[src/qtrader/data/validation.py](src/qtrader/data/validation.py) implementa ocho detecciones — huecos, duplicados, OHLC inconsistente, precios negativos, volumen cero, saltos de N sigmas, series congeladas y discrepancia entre fuentes. Devuelve una lista de hallazgos con severidad `WARN`, `EXCLUDE` o `HALT`; **no filtra ni "arregla" nada**, la decisión es del llamante. Dato sospechoso → instrumento excluido ese día y registrado en auditoría, nunca interpolado. `HALT` está reservado a corrupción sistemática del proveedor (más del 50 % de barras con precio negativo).

Cross-validación de todo cierre entre dos fuentes, con discrepancia > 0,5 % marcada `SUSPECT`. Universo point-in-time de 45 instrumentos en [config/universe.yaml](config/universe.yaml), filtrado por `declared_on <= as_of` y nunca recalculado retroactivamente.

## Fase 2 — Backtester

Motor event-driven ([src/qtrader/backtesting/engine.py](src/qtrader/backtesting/engine.py)) con la regla dura de ejecución en T+1, verificada por un test **oráculo**: una estrategia que intenta mirar el futuro a través de `SandboxedDataView` provoca una excepción del motor en lugar de ganar dinero.

Modelo de costes completo en [src/qtrader/costs.py](src/qtrader/costs.py) — comisión, spread por categoría de instrumento, slippage proporcional a √(tamaño/ADV) y límite de participación en el volumen. No existe modo "sin costes" salvo como comparación explícita en un test.

Golden backtest con resultados numéricos congelados en [tests/backtesting/golden_reference.json](tests/backtesting/golden_reference.json) como red de seguridad contra regresiones del motor. Walk-forward con purga y embargo, y registro de **cada** configuración evaluada —incluidas las descartadas— en `trials.db`, para poder calcular el Deflated Sharpe Ratio con el N real de intentos.

## Fase 3 — Primera estrategia

Cross-sectional momentum 12-1 ([src/qtrader/strategies/momentum.py](src/qtrader/strategies/momentum.py)), la estrategia más replicada de la literatura, usada como **calibración del sistema y no como fuente de alpha**. Requiere 253 barras de historia y devuelve señales solo para el quintil superior; sin historia suficiente no inventa nada.

Framework de métricas con intervalos de confianza bootstrap en [src/qtrader/backtesting/full_metrics.py](src/qtrader/backtesting/full_metrics.py). Todo informe muestra el Sharpe con y sin el haircut declarado por survivorship bias residual y tracking error proxy→UCITS.

## Fase 4 — Portfolio y Risk Engine

[src/qtrader/portfolio/construction.py](src/qtrader/portfolio/construction.py) construye la cartera objetivo con inverse-vol weighting y caps iterativos por posición, sector y región aplicados hasta punto fijo. Tiene una banda de no-rebalanceo: cuando no hace falta rebalancear devuelve los pesos actuales con `quantity=0`, un detalle que el agente debe tratar con cuidado (ver Fase 6).

[src/qtrader/risk/engine.py](src/qtrader/risk/engine.py) es una **función pura** con niveles `NORMAL → CAUTION → RISK_OFF → HALT` según drawdown desde máximo, cada uno con su multiplicador de tamaño. Nunca lanza excepciones por rechazos de negocio: devuelve siempre un `RiskDecision` con las órdenes aprobadas y las rechazadas con motivo explícito. Sus **property-based tests con `hypothesis`** verifican que ninguna combinación de cartera y órdenes arbitrarias produce una orden que viole un límite duro.

Las **tres capas de defensa** son independientes por diseño, cada una con su config y sus tests: el Risk Engine, el `ExecutionRateLimiter` ([src/qtrader/execution/rate_limiter.py](src/qtrader/execution/rate_limiter.py), con límites deliberadamente distintos y estado en SQLite que sobrevive reinicios) y el `BrokerSanityChecker` ([src/qtrader/execution/sanity.py](src/qtrader/execution/sanity.py), que valida símbolo, desviación de precio, fracción del ADV y calendario de mercado). [tests/execution/test_broken_risk_engine.py](tests/execution/test_broken_risk_engine.py) inyecta un Risk Engine deliberadamente roto que aprueba todo y verifica que las otras dos capas siguen bloqueando la orden absurda.

## Fase 5 — Paper broker y kill switch

[src/qtrader/brokers/interface.py](src/qtrader/brokers/interface.py) define un `Protocol` async diseñado contra la semántica de IBKR para que el swap sea de una línea de config. `PaperBroker` simula fills con **el mismo código de costes que el backtester, no una copia**.

Idempotencia y recuperación: `client_order_id` determinista (SHA-256 de fecha, símbolo, lado, estrategia y secuencia), write-ahead log de intenciones persistido **antes** de llamar al broker, y `reconcile()` al arranque. [tests/brokers/test_idempotency.py](tests/brokers/test_idempotency.py) lanza subprocesos y los mata con `kill()` en cada estado del ciclo de órdenes, verificando que la recuperación no duplica ni pierde nada y que el invariante de caja se mantiene.

**Kill switch** ([src/qtrader/safety/kill_switch.py](src/qtrader/safety/kill_switch.py)) como fichero fuera del proceso del agente. `is_active()` no cachea —cada llamada lee del disco— y ante un error de I/O devuelve `True`: **fail-safe, nunca fail-open**. `deactivate()` exige un token de confirmación literal y está prohibido en modo `production`. En POSIX el test verifica que el agente recibe `PermissionError` al intentar borrar el fichero; en Windows sin admin se usa un backend SQLite con un trigger que aborta cualquier `DELETE`.

El **dead man's switch** ([src/qtrader/safety/watchdog.py](src/qtrader/safety/watchdog.py)) corre en un hilo daemon: si no recibe `heartbeat()` en 180 segundos activa el kill switch con `WATCHDOG_TIMEOUT`. Una vez iniciado, el agente no puede detenerlo.

## Fase 6 — Agente trader (sin LLM)

Orquestador **determinista** del ciclo diario. No añade lógica de negocio: conecta en orden los módulos que ya existen. El LLM llega en la Fase 8, no aquí.

Dos ejecuciones diarias como máquina de estados explícita ([src/qtrader/agents/states.py](src/qtrader/agents/states.py), con las tablas de transición y recuperación como datos, no como `if/elif`):

```
POST-CLOSE:  LOADING_DATA → GENERATING_SIGNALS → EVALUATING_RISK → SLEEPING
PRE-OPEN:    RECONCILING → SUBMITTING_ORDERS → MONITORING → SLEEPING
```

El estado se persiste **antes** de cada handler en `data/agent.db`, que el agente posee con DDL idempotente propio: nunca llama a `SQLiteLedger.initialize()`, que borraría la cadena de auditoría.

**La regla de recuperación más importante: un crash en `SUBMITTING_ORDERS` reanuda en `RECONCILING`, nunca reintentando los envíos.** Si el proceso muere a mitad del lote, el conjunto de órdenes que llegó al broker es desconocido; reintentar a ciegas duplicaría exactamente las que sí llegaron. Solo tras reconciliar —consultando `get_order_status()` por cada `client_order_id`— queda bien definido qué reenviar. Por eso el `client_order_id` y su número de secuencia se commitean **antes** del `await submit_order`. Por el mismo motivo `GENERATING_SIGNALS` y `EVALUATING_RISK` reanudan en `LOADING_DATA`: dependen de datos en memoria que murieron con el proceso.

El kill switch se comprueba al inicio de **cada** estado e inmediatamente antes de **cada** envío, sin ningún `await` entre la comprobación y el `submit_order` para no abrir la ventana TOCTOU (medida y registrada; en las ejecuciones reales queda en 16 ms frente al presupuesto de 100 ms).

El agente **nunca activa el kill switch**: lo tipa como un `Protocol` de solo lectura, así `activate()` no está siquiera en su superficie de tipos. Cuando un estado falla, pide el HALT a través de un *halt requester* inyectado; en producción ese requester es `None` y quien activa el HALT es el Watchdog, que vive fuera del proceso que ha fallado. La justificación está en ADR-009.

## Fase 7 — Observabilidad

Capa de visibilidad operativa completa, sin acceso a ningún componente de producción.

- **Logging estructurado** ([src/qtrader/monitoring/logging.py](src/qtrader/monitoring/logging.py)): JSON lines vía `structlog`, con contexto de ciclo, fase y `correlation_id` propagado. Niveles y formato configurables; en tests el sink es silencioso por defecto.
- **Métricas** ([src/qtrader/monitoring/metrics.py](src/qtrader/monitoring/metrics.py)): contadores y gauges en memoria con snapshot exportable. Mide fills, rechazos por capa, latencia de ciclo, nivel de riesgo, PnL acumulado y slippage real vs modelado.
- **Health checks** ([src/qtrader/monitoring/health.py](src/qtrader/monitoring/health.py)): estado `OK/DEGRADED/DOWN` para cada subsistema (broker, datos, kill switch, watchdog). El agente lo consulta antes de cada ciclo; cualquier `DOWN` impide arrancar.
- **Alertas** ([src/qtrader/monitoring/alerts.py](src/qtrader/monitoring/alerts.py)): reglas configurables que disparan cuando se activa el kill switch, falla la reconciliación, los datos llevan N días congelados, el broker se desconecta o el drawdown cruza un nivel. Las alertas se registran en auditoría —no solo en logs— para mantener la trazabilidad.
- **Dashboard Streamlit** ([src/qtrader/dashboard/app.py](src/qtrader/dashboard/app.py)): interfaz de solo lectura sobre los ficheros SQLite del sistema. Muestra posiciones actuales, PnL diario, historial de fills, nivel de riesgo, alertas activas y botón de kill switch. Escucha **exclusivamente en `127.0.0.1`**; cualquier cambio a `0.0.0.0` es un bug de seguridad.

## Fase 8 — Researcher LLM

El primer punto de entrada de inteligencia artificial al sistema, con aislamiento deliberado de producción: el módulo `research.llm` no puede importar `brokers`, `execution`, `safety` ni `agents` — verificado automáticamente por un contrato de `import-linter`.

- **Toolset** ([src/qtrader/research/llm/toolset.py](src/qtrader/research/llm/toolset.py)): las únicas funciones que el LLM puede llamar. Lee datos históricos, calcula métricas de estrategias existentes, accede a trials pasados y **propone** configuraciones de experimento vía `propose_experiment`. `submit_order` no existe en esta superficie.
- **Schemas Pydantic** ([src/qtrader/research/llm/schemas.py](src/qtrader/research/llm/schemas.py)): toda salida del LLM se valida contra modelos `frozen=True` con `extra="forbid"` antes de usarse. Una respuesta malformada lanza `ValidationError`, nunca pasa silenciosamente.
- **Prompt builder** ([src/qtrader/research/llm/prompt_builder.py](src/qtrader/research/llm/prompt_builder.py)): todo texto externo que llega al LLM se entrega delimitado y etiquetado como datos inertes, nunca como instrucción. Invariante §2.9.
- **Base de datos de propuestas** ([src/qtrader/research/llm/proposals_db.py](src/qtrader/research/llm/proposals_db.py)): las hipótesis generadas se persisten con su estado (`PENDING/ACCEPTED/REJECTED/EVALUATED`) y su link al trial resultante en `trials.db`. Nada se descarta sin registrar.
- **Researcher** ([src/qtrader/research/llm/researcher.py](src/qtrader/research/llm/researcher.py)): orquestador que ejecuta el ciclo proponer → evaluar → registrar. Nunca toca el agente trader ni el broker.

## Fase 9 — Robustez estadística

Esta fase no añade features. Añade **evidencia**: cuatro análisis cuantitativos independientes más doce escenarios de fallo como tests automatizados. El veredicto final es ROBUSTO, CONDICIONAL o FRÁGIL, con explicación detallada de cuál criterio falló y por qué.

### Parte A — Sensibilidad de parámetros (135 configuraciones)

[src/qtrader/research/robustness/sensitivity.py](src/qtrader/research/robustness/sensitivity.py) barre el producto cartesiano de:

- `lookback_months` ∈ {6, 9, 12, 15, 18}
- `skip_months` ∈ {0, 1, 2}
- `rebalance_freq` ∈ {weekly, biweekly, monthly}
- `top_quantile` ∈ {0.10, 0.20, 0.30}

Cada una de las 135 combinaciones se registra en `trials.db` (INSERT-only, nunca UPDATE) con su Sharpe OOS y MaxDD. **Umbral: ≥ 60 % de configs con Sharpe OOS > 0 AND |MaxDD| < 50 %**.

### Parte B — Monte Carlo (block bootstrap)

[src/qtrader/research/robustness/monte_carlo.py](src/qtrader/research/robustness/monte_carlo.py) ejecuta 1.000 muestras con bloques de 21 días para preservar la autocorrelación de los retornos. Calcula percentiles P5/P25/P50/P75/P95 de Sharpe, MaxDD y CAGR sobre la curva de equity, y adicionalmente un bootstrap del orden de trades. **Umbral: P5(Sharpe) ≥ −0,5**.

### Parte C — PBO/CSCV (Probability of Backtest Overfitting)

[src/qtrader/research/robustness/pbo.py](src/qtrader/research/robustness/pbo.py) implementa Combinatorially Symmetric Cross-Validation con S = 8 subperíodos, generando C(8,4) = 70 splits IS/OOS. PBO es la fracción de splits donde la mejor configuración in-sample obtiene Sharpe OOS por debajo de la mediana — una medida directa de cuánto del rendimiento histórico se debe a búsqueda exhaustiva y no a señal real. **Umbral: PBO ≤ 0,70**.

### Parte D — Deflated Sharpe Ratio

Se calcula sobre el N real de trials registrados en `trials.db`. **Umbral: DSR > 0,30**.

### Parte E — 12 escenarios adversos

Tests automatizados en [tests/robustness/test_adverse_scenarios.py](tests/robustness/test_adverse_scenarios.py) que verifican el comportamiento correcto ante fallos reales:

| # | Escenario |
|---|-----------|
| 1 | API del proveedor caída — sin señales, sin crash |
| 2 | Precios marcados SUSPECT — excluidos, no interpolados |
| 3 | Persistencia WAL: cierre y reapertura del broker sin duplicar órdenes |
| 4 | Fill parcial — posición coherente con las acciones efectivamente llenadas |
| 5 | Orden duplicada con mismo `client_order_id` — idempotente |
| 6 | Kill switch activo — agente se detiene en el primer ciclo |
| 7 | Mercado suspendido (datos SUSPECT) — sin fills |
| 8 | Risk Engine en nivel HALT — ninguna orden de compra aprobada |
| 9 | Rate limiter al límite diario — segunda orden rechazada |
| 10 | Crash y recovery — sin duplicados ni pérdidas en el WAL |
| 11 | Restart del broker — posición preservada con exactitud |
| 12 | Risk Engine determinista — mismas entradas → misma salida siempre |

### Informe de robustez

```bash
uv run qtrader research robustness-report
```

Genera `data/reports/robustness_YYYY-MM-DD.md` con veredicto, tabla de criterios, notas de fallo, y **Sharpe bruto vs. neto** con el haircut declarado del 10 % por survivorship bias residual y tracking error proxy→UCITS (invariante §3.11).

**Veredicto con datos sintéticos (esperado):** FRÁGIL — los datos aleatorios producen Sharpe ≈ 0 y DSR ≈ 0. Esto es información útil, no un fracaso: el sistema detecta correctamente que no hay señal en ruido puro.

---

## Uso

```bash
uv sync                                                   # instalar dependencias

# Agente trader — un ciclo completo son dos ejecuciones (señal en T, ejecución en T+1)
uv run qtrader run --once --mode paper --phase post-close --date 2024-06-14
uv run qtrader run --once --mode paper --phase pre-open   --date 2024-06-17

uv run qtrader audit verify --db data/state.db            # verificar la cadena
uv run qtrader backtest --strategy momentum --start 2020-01-01 --end 2023-12-31
uv run qtrader universe show --date 2024-06-14
uv run qtrader demo --days 60                             # tracer bullet de Fase 0
uv run qtrader research robustness-report                 # informe de robustez T9
```

### Verificación

Salida real de los comandos de aceptación tras la Fase 9:

```
$ uv run pytest -q
620 passed, 1 skipped in 98.22s

$ uv run mypy src --strict
Success: no issues found in 76 source files

$ uv run lint-imports
Contracts: 17 kept, 0 broken.

$ uv run qtrader research robustness-report
[robustness-report] Parte A: sweep de sensibilidad (135 configs)...
[robustness-report] Sensibilidad: 0/135 configs robustas (0.0%)
[robustness-report] Parte B: Monte Carlo...
[robustness-report] Monte Carlo: P5(Sharpe)=0.0000
[robustness-report] Parte C: PBO (CSCV)...
[robustness-report] PBO=0.000 (is_overfit=False)
[robustness-report] DSR=0.0044 (N=135)
Veredicto: FRÁGIL
Informe guardado en: data/reports/robustness_2026-08-13.md
```

El veredicto FRÁGIL con datos sintéticos es el resultado correcto: el sistema no inventa señal donde no existe. Con datos reales de mercado se esperan criterios B (Monte Carlo) y C (PBO) en verde, quedando el veredicto en CONDICIONAL o ROBUSTO según la señal histórica del momentum.

---

## Lo que queda por hacer

### Fase 10 — Paper trading prolongado

Mínimo seis meses contra datos de mercado reales (Tiingo + yfinance en modo dual). Los criterios de salida están ponderados hacia lo operativo: incidentes no recuperados (30 %), reconciliación diaria limpia (20 %), slippage real vs modelado (20 %), cobertura de auditoría (10 %), número de trades (10 %) y performance vs backtest (10 %). **El retorno absoluto no aparece en los criterios**, y es deliberado: seis meses no tienen potencia estadística para evaluarlo, y usarlo como criterio de promoción es el error más común del sector.

### Fase 11 — IBKR

Implementar `IBKRBroker` con la misma interfaz async que `PaperBroker`. Paper account de IBKR durante tres meses de validación. `Read-Only API` desactivado como paso manual deliberado, no como flag.

### Fase 12 — Producción

Solo tras firma humana explícita y registrada. Cuenta real segregada. `production` no se activa con un flag: requiere variable de entorno, fichero de autorización con caducidad y confirmación interactiva. El capital real comienza en el mínimo permitido y escala solo si la reconciliación sigue limpia.

---

## Estructura

```
src/qtrader/
  core/types.py              contratos Pydantic congelados
  data/                      proveedores, validación, cross-validación, universo
  strategies/                SMA20 (F0) y momentum 12-1 cross-sectional (F3)
  backtesting/               motor event-driven, métricas, golden backtest
  research/                  walk-forward, trials.db, DSR
  research/llm/              toolset LLM offline, schemas, proposals.db (F8)
  research/robustness/       sensibilidad, Monte Carlo, PBO, informe (F9)
  portfolio/                 construcción inverse-vol con caps iterativos
  risk/                      risk engine puro (capa 1)
  execution/                 rate limiter (capa 2) y sanity checker (capa 3)
  brokers/                   interfaz async, paper broker, client_order_id
  safety/                    kill switch y watchdog
  agents/                    máquina de estados del agente trader
  monitoring/                logging, métricas, alertas, health checks (F7)
  dashboard/                 Streamlit 127.0.0.1 de solo lectura (F7)
  ledger/sqlite.py           persistencia y cadena de auditoría
  costs.py                   modelo de costes compartido backtest/paper
  cli.py                     run, backtest, demo, audit, universe, research
tests/
  robustness/                12 escenarios adversos (F9)
  ...                        617 tests adicionales, sin acceso a red
docs/                        plan, revisión crítica y ADRs
```

Dependencias de capas verificadas con `import-linter` (17 contratos activos):

| Módulo | No puede importar |
|--------|-------------------|
| `risk` | nada fuera de `core` |
| `strategies` | `execution`, `brokers`, `agents` |
| `portfolio` | `brokers`, `agents`, `execution` |
| `execution` | `risk`, `agents`, `brokers` |
| `brokers` | `agents`, `portfolio` |
| `safety` | `brokers`, `agents`, `portfolio` |
| `agents` | `backtesting`, `research` |
| `data` | `execution`, `agents` |
| `backtesting` | `agents`, `brokers` |
| `costs` | `agents`, `brokers`, `execution`, `ledger` |
| `research` | `agents`, `brokers` |
| `research.llm` | `brokers`, `execution`, `safety`, `agents` |
| `research.robustness` | `brokers`, `execution`, `safety`, `agents` |
| `monitoring` | `agents`, `brokers`, `execution` |
| `dashboard` | `agents`, `brokers`, `execution` |

Cada componente con estado posee **su propio fichero SQLite**: `state.db` (ledger y auditoría), `agent.db` (estado del agente y órdenes pendientes), `paper_broker.db` (órdenes, posiciones y caja), `execution.db` (rate limiter), `trials.db` (configuraciones evaluadas) y `proposals.db` (propuestas del LLM). Sin interferencias destructivas entre dueños.

---

## Notas de mantenimiento

- **`.gitignore` excluye módulos `data/` del código fuente.** El patrón `data/` de la línea 21 pretende excluir el directorio de estado en la raíz, pero al no llevar prefijo `/` casa a cualquier profundidad: `src/qtrader/data/` y `tests/data/` siguen sin versionar pese a ser código fuente (`git ls-files src/qtrader/data` no devuelve nada). `git status` sale limpio y el fallo pasa desapercibido hasta que alguien clone el repositorio y nada arranque. Corregir a `/data/` y añadir los ficheros. **Sigue pendiente y ahora afecta a toda la capa de datos de la Fase 1.**
- **Existen dos `RiskLevel` y dos `RiskDecision`.** Los de `qtrader.risk.types` (`NORMAL/CAUTION/RISK_OFF/HALT`) son los vigentes; los de `qtrader.core.types` (`APPROVE/REDUCE/REJECT`) son legacy de la Fase 0 y solo los usa `engine.py`. Unificarlos está pendiente (ADR-013).
- **`ApprovedOrder` no lleva `price` ni `strategy_id`.** El precio se transporta en un mapa lateral indexado por `order_id` en lugar de derivarlo de `notional / quantity`, que perdería precisión. `PaperBroker` registra `strategy_id="unknown"` vía `getattr`.
- **`RiskEngine.evaluate` ignora el nivel de riesgo previo**: fija `current_level=NORMAL`, con lo que la histéresis de `recovery_days` está efectivamente inactiva.
- **`SandboxedDataView` vive en `backtesting`** y `momentum.py` la referencia bajo `TYPE_CHECKING`. El agente define su propia vista duck-typed para no acoplar producción con simulación; el contrato de `import-linter` usa `allow_indirect_imports` por ese import transitivo de solo tipos. Moverla a `data/` sería la solución limpia.
- La configuración del engine de Fase 0 es todavía un stub en el propio módulo (`_CONFIG_STUB`); el resto del sistema ya carga YAML desde `config/`.
- **El researcher LLM (F8) está diseñado para Claude pero desacoplado del modelo.** El `researcher.py` llama a cualquier cliente que implemente la interfaz de mensajes; no hay dependencia dura de la API de Anthropic en el código de producción.
- **Los criterios de robustez (F9) con datos reales esperan CONDICIONAL como mínimo.** Con datos sintéticos el veredicto es FRÁGIL por diseño (ruido puro → Sharpe ≈ 0). Las Partes B y C (Monte Carlo y PBO) están bien calibradas para datos reales con señal débil como el momentum.
