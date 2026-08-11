# qtrader

Plataforma experimental de gestión cuantitativa de cartera. **Opera exclusivamente en simulación.** No existe ninguna ruta de código que envíe una orden a un broker real, y activarla requerirá una decisión humana firmada y registrada.

- **Solo swing.** Barras diarias, holding 15-60 días, rebalanceo semanal. No hay código intradía en este repositorio.
- **Capital de simulación:** 15.000 €. Tamaño mínimo de orden: 1.000 €.
- **Presupuesto de datos:** 0 €. Fuentes gratuitas con cross-validación entre dos proveedores.
- **Universo ETF-céntrico** (~50 instrumentos), congelado y versionado con fecha de declaración.

El objetivo primario es la **corrección operativa, no la rentabilidad**. Un sistema que pierde un 3 % con reconciliación perfecta y trazabilidad completa es un éxito; uno que gana un 40 % con una orden duplicada sin explicar es un fracaso.

> **El backtest genera hipótesis; no valida.** La validación es walk-forward más paper trading sobre datos nunca vistos. Este framing es deliberado y no se suaviza en ningún informe. El benchmark honesto es un ETF indexado global después de costes e impuestos, y lo más probable es no superarlo.

Las reglas que gobiernan el proyecto están en [CLAUDE.md](CLAUDE.md) (invariantes de seguridad y de corrección cuantitativa), el plan completo en [docs/01-PLAN-CLAUDE-CODE.md](docs/01-PLAN-CLAUDE-CODE.md) y las decisiones de arquitectura en [docs/DECISIONS.md](docs/DECISIONS.md).

---

## Estado actual: Fase 0 completada

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

## Uso

```bash
uv sync                                    # instalar dependencias
uv run qtrader demo --days 60              # camino end-to-end completo
uv run qtrader audit verify                # verificar la cadena de auditoría
```

`demo` acepta `--days`, `--seed` y `--db`. Ejecutarlo dos veces con la misma semilla produce resultados idénticos.

### Verificación

Salida real de los comandos de aceptación en el commit `38c8883`:

```
$ uv run pytest -q
..............................................................           [100%]
62 passed in 0.96s

$ uv run mypy src --strict
Success: no issues found in 15 source files

$ uv run ruff check .
All checks passed!

$ uv run qtrader demo --days 60
Equity inicial:  1000,00 €
Equity final:    996,27 €
Nº de trades:    4
P&L:             -3,73 €

$ uv run qtrader audit verify
OK
```

La demo pierde 3,73 € sobre datos sintéticos aleatorios en 60 sesiones. Eso es exactamente lo esperado y no es un problema a corregir: una SMA20 sobre un paseo aleatorio no tiene edge, y las cuatro operaciones pagan comisión. **Un resultado bueno aquí sería la señal de alarma**, no este.

---

## Lo que queda por hacer

La Fase 0 valida la forma de la arquitectura, no su contenido. Prácticamente todo el sistema está por construir: de los diez componentes que atraviesa la demo, cada uno existe en la versión más simple que permitía cerrar el circuito. Lo que sigue.

### Fase 1 — Datos

Capa de proveedor abstracta (`Protocol`) con implementaciones sintética, CSV y de proveedor real. Almacenamiento en Parquet particionado por año con índice DuckDB, **precios sin ajustar** más tabla separada de corporate actions. El parámetro `as_of` de `get_bars()` aplica solo las corporate actions conocidas en esa fecha — es lo que impide el look-ahead en los ajustes y no es opcional.

Validación de datos con ocho detecciones (huecos, duplicados, OHLC inconsistente, precios cero, volumen cero, saltos de N sigmas sin corporate action, series congeladas). Política: dato sospechoso → instrumento excluido ese día y registrado en auditoría; **nunca interpolar ni rellenar hacia adelante en silencio**. Cross-validación de todo cierre entre Tiingo y yfinance, con discrepancia > 0,5 % marcada `SUSPECT`. Universo point-in-time con calendario de mercado, guardado con fecha y nunca recalculado retroactivamente.

### Fase 2 — Backtester

Motor event-driven con la regla dura de ejecución en T+1, verificada por un test "oráculo": una estrategia que intenta hacer trampa mirando el futuro debe provocar una excepción del motor, no ganar dinero. Modelo de costes completo — comisión, spread, slippage proporcional a √(tamaño/ADV) y límite de participación en el volumen. Backtest "golden" con resultados numéricos congelados como red de seguridad contra regresiones. Walk-forward con purga y embargo, y registro obligatorio de **cada** configuración evaluada, incluidas las descartadas, para poder calcular el Deflated Sharpe Ratio con el N real de intentos.

### Fase 3 — Primera estrategia

Cross-sectional momentum 12-1, la estrategia más replicada de la literatura, usada como **calibración del sistema y no como fuente de alpha**. El criterio de aceptación es que caiga dentro del rango publicado (Sharpe 0,3-0,7, MaxDD 30-50 %); si sale Sharpe 2,5 hay un bug o leakage y se busca antes de continuar. Framework de métricas con intervalos de confianza bootstrap.

### Fase 4 — Portfolio y Risk Engine

Construcción de cartera con volatility targeting y equal risk contribution simplificado (mean-variance queda descartado en fase inicial: con este capital y covarianzas ruidosas, optimiza el ruido). Risk Engine completo con niveles `NORMAL → CAUTION → RISK_OFF → HALT` según drawdown desde máximo. Sus **property-based tests con `hypothesis` son el test más importante del repositorio**: ninguna combinación de cartera y órdenes arbitrarias puede producir una orden que viole un límite duro.

Aquí se construyen las capas 2 y 3 de defensa — rate limits en el Execution Engine y validación de sanidad en el Broker Interface — con config y tests propios, **independientes del Risk Engine**. El test que las valida inyecta un Risk Engine deliberadamente roto que aprueba todo y verifica que las otras dos capas siguen bloqueando la orden absurda.

### Fase 5 — Paper broker

Interfaz común diseñada ya contra la semántica de IBKR para que el swap sea de una línea de config. El paper broker simula fills con **el mismo código de costes que el backtester, no una copia**; correr el mismo periodo en ambos debe dar resultados idénticos, y si divergen uno de los dos está mal.

Idempotencia y recuperación: `client_order_id` determinista, write-ahead log de intenciones antes de llamar al broker, reconciliación al arranque. Los tests matan el proceso con `SIGKILL` en cada estado del ciclo de órdenes y verifican que la recuperación no duplica ni pierde nada. Kill switch como fichero fuera del proceso del agente, que el agente puede leer pero **no puede borrar** — con un test que lo intenta y espera `PermissionError`.

### Fase 6 — Agente trader (aún sin LLM)

Orquestador determinista del ciclo diario como máquina de estados explícita con estados persistidos, de modo que un reinicio a mitad de ciclo se reanude correctamente en lugar de repetir trabajo.

### Fase 7 — Observabilidad

Logging estructurado en JSON lines, métricas, health checks y alertas para HALT activado, reconciliación fallida, datos congelados, broker desconectado y cruce de nivel de drawdown. Dashboard Streamlit de solo lectura con botón de kill switch, **atado a `127.0.0.1`**.

### Fase 8 — Researcher (aquí entra el LLM, y solo aquí)

Herramientas offline de generación y evaluación de hipótesis, sin acceso alguno al broker ni a producción. El LLM propone hipótesis, escribe configuraciones de experimento y redacta informes; **nunca decide ni envía una orden**. Un contrato de `import-linter` verifica automáticamente que el módulo del LLM no importa nada de `execution/` ni de `brokers/`.

### Fase 9 — Robustez

Sensibilidad de parámetros, Monte Carlo por bootstrap de retornos y de orden de trades, probabilidad de overfitting vía CSCV, Deflated Sharpe, análisis por régimen de mercado y los doce escenarios de fallo como tests automatizados.

### Fase 10 — Paper trading prolongado

Mínimo seis meses. Los criterios de salida están ponderados hacia lo operativo: incidentes no recuperados (30 %), reconciliación diaria limpia (20 %), slippage real vs modelado (20 %), cobertura de auditoría (10 %), número de trades (10 %) y performance vs backtest (10 %). **El retorno absoluto no aparece**, y es deliberado: seis meses no tienen potencia estadística para evaluarlo, y usarlo como criterio de promoción es el error más común del sector.

### Fases 11-12 — IBKR y producción

Solo tras firma humana explícita y registrada. Paper account de IBKR → tres meses de validación → cuenta real segregada, con `Read-Only API` desactivado como paso manual deliberado. `production` no se activa con un flag: requiere variable de entorno, fichero de autorización con caducidad y confirmación interactiva.

---

## Estructura

```
src/qtrader/
  core/types.py        contratos Pydantic congelados
  data/synthetic.py    proveedor de barras deterministas
  strategies/sma.py    señal SMA20
  risk/engine.py       risk engine puro
  brokers/paper.py     simulación de fills
  ledger/sqlite.py     persistencia y cadena de auditoría
  engine.py            orquestación end-to-end
  cli.py               comandos demo y audit
tests/                 62 tests, sin acceso a red
docs/                  plan, revisión crítica y ADRs
```

Dependencias de capas (se verificarán con `import-linter` en Fase 8): `strategies` no importa `execution`; `execution` no importa `agents`; `agents/llm` no importa `execution` ni `brokers`; `risk` no importa nada fuera de `core`.

---

## Notas de mantenimiento

- **`.gitignore` excluye los módulos `data/` del código fuente.** El patrón `data/` de la línea 21 pretende excluir el directorio de estado en la raíz, pero al no llevar prefijo `/` casa a cualquier profundidad: `src/qtrader/data/` y `tests/data/` están sin versionar pese a ser código fuente. `git status` sale limpio y el fallo pasa desapercibido hasta que alguien clone el repositorio y la demo no arranque. Corregir a `/data/` y añadir los ficheros.
- Los `AuditRecord` de T0.3 llevan defaults en los campos nuevos (`git_sha`, `config_hash`, `parameters`, `risk_output`, `decision`, `reason`) para no romper los tests de T0.1. Conviene revisarlo cuando el formato de auditoría se estabilice.
- La configuración del engine es todavía un stub en el propio módulo (`_CONFIG_STUB`); pasa a fichero YAML en fases posteriores.
