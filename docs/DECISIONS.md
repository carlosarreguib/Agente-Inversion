# Registro de decisiones de arquitectura (ADRs)

Formato: contexto → opciones → decisión → consecuencias.

---

## ADR-001 — `AuditRecord.payload` usa `dict[str, str]`

**Fecha**: 2024-01-15  
**Estado**: Aceptado

### Contexto

`AuditRecord` necesita un campo de payload flexible para almacenar metadatos
de auditoría (parámetros de estrategia, hashes, IDs de ciclo, etc.).
La opción natural sería `dict[str, Any]`, pero CLAUDE.md §4 prohíbe cruzar
fronteras de módulo con `dict[str, Any]` y mypy strict rechaza `Any` implícito.

### Opciones consideradas

1. `dict[str, Any]` — flexible pero viola el invariante y mypy strict.
2. `dict[str, str]` — restringido pero satisface mypy strict; los valores
   complejos se serializan a string antes de almacenar.
3. Un modelo Pydantic especializado por `event_type` — correcto pero prematuro
   en T0.1; añade complejidad sin beneficio inmediato.

### Decisión

`dict[str, str]`. Los valores complejos (cantidades, hashes, parámetros)
se convierten a `str` en el punto de creación del registro.

### Consecuencias

- La capa de persistencia (T0.3) serializa directamente a JSON sin coerciones.
- Si en una fase posterior se necesitan valores tipados en el payload,
  se crea un modelo `AuditPayload` con campos explícitos y se reemplaza
  este campo. Eso requiere un nuevo ADR.

---

## ADR-002 — Python 3.12 instalado vía uv con Python 3.13 en el sistema

**Fecha**: 2024-01-15  
**Estado**: Aceptado

### Contexto

El sistema tiene Python 3.13 instalado. CLAUDE.md fija Python 3.12 como
versión del proyecto. Cambiar la versión requiere un ADR (§1 de CLAUDE.md).

### Decisión

Instalar Python 3.12 vía `uv python install 3.12` y fijar
`requires-python = "==3.12.*"` en `pyproject.toml`. uv gestiona el
intérprete del proyecto de forma independiente al intérprete del sistema.

### Consecuencias

- `uv run` y `uv sync` usan siempre Python 3.12.13 (cpython).
- El entorno virtual en `.venv/` está aislado del Python de sistema.
- La CI (cuando exista) debe instalar uv y dejar que uv gestione Python.

---

## ADR-003 — Golden backtest con `SyntheticMultiProvider` y hash MD5 para determinismo

**Fecha**: 2026-08-11  
**Estado**: Aceptado

### Contexto

El golden backtest (T2.3) necesita datos de mercado deterministas entre
procesos Python separados. El uso de `hash(symbol)` es insuficiente porque
Python randomiza el hash interno por proceso (PYTHONHASHSEED). Cualquier
generación de seed basada en `hash()` produce series distintas en cada
ejecución del proceso.

### Opciones consideradas

1. `hash(symbol)` — no determinista entre procesos (PYTHONHASHSEED).
2. `hashlib.md5(symbol.encode()).hexdigest()` — determinista, disponible en
   stdlib, no requiere dependencias externas.
3. Datos de mercado reales en fixtures — viola el invariante §5 ("los tests
   no acceden a la red") y hace el golden test frágil a revisiones upstream.

### Decisión

`hashlib.md5(symbol.encode(), usedforsecurity=False)`. El parámetro
`usedforsecurity=False` suprime la advertencia de FIPS y deja claro que MD5
se usa aquí como función hash determinista, no criptográfica.

### Consecuencias

- `SyntheticMultiProvider._seed_for()` produce siempre el mismo seed para
  el mismo símbolo, independientemente de PYTHONHASHSEED.
- El fichero `tests/backtesting/golden_reference.json` es reproducible en
  cualquier máquina con las mismas dependencias.
- Si se cambia la lógica de generación (drift, vol, make_bar), se debe
  regenerar el JSON con `uv run qtrader golden update` y confirmar el diff.

---

## ADR-004 — Walk-forward con purga por embargo y registro INSERT-only en SQLite

**Fecha**: 2026-08-11  
**Estado**: Aceptado

### Contexto

La validación temporal (T2.4) requiere:

1. Separar estrictamente los datos de entrenamiento de los de test para evitar
   look-ahead (invariante §3.1).
2. Registrar toda configuración evaluada, incluidas las descartadas, con el
   Deflated Sharpe Ratio (invariante §3.6).
3. Mantener la historia de experimentos inmutable para reproducibilidad y
   auditoría.

### Decisión — Purga y embargo

Los últimos `embargo_days` de la ventana de entrenamiento se eliminan del
conjunto de datos que ve la estrategia IS. El test comienza en el día
inmediatamente posterior al fin original (no purgado) del train. Este diseño
sigue a Bailey & López de Prado (2018): evita que las etiquetas solapadas al
final del periodo de train contaminen el test a través de features de largo
plazo (e.g. medias móviles, momentum de varias semanas).

Los parámetros por defecto (train=756, test=252, step=63, embargo=5) son
configurables en `config/research.yaml`. Cambiarlos no requiere un ADR pero
sí documentar la razón en el commit.

### Decisión — SQLite INSERT-only

`trials.db` usa SQLite con una tabla INSERT-only. La garantía INSERT-only se
impone a nivel de API (`TrialsDB`): la clase no expone métodos `update` ni
`delete`. El esquema incluye una tabla `_schema_version` para migraciones
futuras.

Se registran cuatro eventos por fold: RUNNING (antes de ejecutar), y luego
COMPLETED o FAILED (con `error_message`). Esto permite auditar cuántos trials
fallaron y por qué, y recuperar el estado si el proceso muere a mitad.

### Opciones descartadas

- **UPDATE de status**: haría la historia mutable, violando el principio de
  inmutabilidad del registro de trials.
- **PostgreSQL**: presupuesto de datos = 0 €; SQLite es suficiente para un
  universo de ~50 ETFs con folds anuales.
- **Parquet / CSV**: sin ACID, sin PKs, sin garantía de inserción atómica.

### Deflated Sharpe Ratio

Se implementa la ecuación 8 de Bailey & López de Prado (2014) usando
la aproximación racional de Abramowitz & Stegun 26.2.17 (en lugar de
`math.erfinv`, que no está disponible en la build Windows de Python 3.12)
en lugar de scipy para mantener el
presupuesto de dependencias bajo. El DSR se calcula sobre todos los trials
COMPLETED de la misma estrategia en la DB, no solo sobre los del run actual.
Esto penaliza correctamente el data-mining acumulado a lo largo de varias
sesiones de investigación.

### Consecuencias

- El informe `uv run qtrader research report --strategy X` siempre muestra
  el DSR basado en el histórico completo de la DB, no en el run aislado.
- Cualquier cambio en la fórmula DSR (e.g. sigma_SR corregido por sesgo)
  debe documentarse aquí y regenerar los informes.

---

## ADR-005 — Mark-to-market en el motor y estrategia momentum cross-sectional 12-1

**Fecha**: 2026-08-12
**Estado**: Aceptado

### Contexto

El motor de backtesting (T2.1) registraba en la equity curve solo el cash disponible,
no el NAV (Net Asset Value = cash + valor de mercado de posiciones). Para estrategias
multi-activo como el momentum cross-sectional, casi todo el capital queda invertido en
posiciones tras el primer rebalanceo. La equity curve de cash caía a ~30 de 15000,
generando métricas erróneas (Sharpe -10, retorno -99%).

### Opciones consideradas

1. Mantener equity=cash y gestionar el portfolio solo con cash disponible sin
   reinversión. Produce comportamiento buy-and-hold parcial, no momentum real.
2. Modificar el motor para calcular NAV en cada ciclo usando los cierres del día.
3. Calcular NAV en el portfolio y limitar compras al cash disponible con ajuste manual.

### Decisión

Opción 2: el motor calcula `nav = cash + sum(pos.qty * bar.close)` al final de cada
ciclo antes de registrar en `equity_curve`. `final_equity` en `BacktestResult` refleja
el NAV del último día. El golden backtest cambió sus métricas (de -60.65% a -0.53% de
retorno) — cambio legítimo y correcto. `MomentumEqualWeightPortfolio` recalcula el NAV
localmente y limita las compras nuevas al cash disponible para evitar posiciones
descubiertas (sin apalancamiento implícito).

### Consecuencias

1. El golden reference (`tests/backtesting/golden_reference.json`) se actualizó con los
   valores correctos post mark-to-market.
2. `test_equity_decreases_on_buy` reformulado: comprar acciones transfiere cash a
   posición (el NAV permanece ≥ 0); el test verifica `final_equity >= 0`.
3. `SyntheticMultiProvider` añadió caché por símbolo (dict interno): evita regenerar
   la serie completa en cada llamada, reduciendo el backtest de 14 años de >300 s a ~60 s.
4. La estrategia momentum declara `declared_on` en el universo para el proveedor
   sintético como la fecha de inicio del proveedor (no la del YAML) para disponer de
   historia suficiente de lookback sin depender de datos reales.
- La invariante de no-look-ahead (§3.1) es verificable leyendo `Fold.train_days`:
  el último elemento siempre es al menos `embargo_days` antes de `test_start`.

---

## ADR-006 — Risk Engine como función pura con tipos propios e independencia de capas

**Fecha**: 2026-08-12
**Estado**: Aceptado

### Contexto

CLAUDE.md §2.2 exige que el Risk Engine sea puro (sin I/O, sin red, sin estado global,
sin aleatoriedad) y §2.3 exige tres capas de control independientes entre una señal y el
broker, con código de validación nunca compartido entre capas.

El diseño debía elegir entre:
- Reutilizar los tipos de `core.types` (`RiskDecision`, `RiskLevel`) ya existentes.
- Crear tipos propios en `risk/types.py`.

Los tipos existentes en `core.types` modelaban una decisión por orden con semántica
APPROVE/REDUCE/REJECT (legacy de T0.1). El Risk Engine de T4.2 evalúa múltiples órdenes
en lote con niveles de portfolio, rate limits, métricas agregadas y warnings.

### Opciones consideradas

1. Extender `core.types.RiskDecision` con campos adicionales. Viola la independencia de
   capas: todo el sistema dependería del mismo tipo, haciendo imposible cambiar el
   contrato del Risk Engine sin afectar a otras capas.

2. Crear tipos propios en `risk/types.py` que importan solo de `core.types` para enums
   compartidos (`Side`, `InstrumentCategory`). El módulo `risk` no importa `portfolio`,
   `brokers`, `agents` ni `execution`.

### Decisión

Opción 2. `risk/types.py` define `CurrentPortfolio`, `ProposedOrder`, `MarketState`,
`ApprovedOrder`, `RejectedOrder`, `PortfolioRiskMetrics`, `RiskDecision`, `RiskLevel`
(con semántica distinta a `core.types.RiskLevel`). La independencia se verifica con
`test_independence.py::test_import_risk_engine_does_not_import_portfolio()` en un
subprocess limpio.

Las exposiciones sectoriales y regionales son `tuple[tuple[str, Decimal], ...]` (no
`dict`) para garantizar determinismo byte a byte en la comparación Pydantic == y
compatibilidad con `frozen=True` en mypy strict.

### Resolución de conflictos entre límites

Los límites se aplican en orden fijo y secuencial:

1. `max_order_notional` → 2. `max_position_weight`/`max_single_position` →
3. `max_total_exposure` → 4. `max_sector_exposure` → 5. `max_region_exposure` →
6. multiplicador de nivel

El multiplicador de nivel se aplica **después** de todos los límites absolutos (reduce
sin violar límites). Las ventas (`SELL`) bypasan el multiplicador de nivel en HALT y
RISK_OFF para permitir el cierre de posiciones. HALT por pérdida diaria tiene prioridad
sobre HALT por drawdown (se evalúa primero y retorna inmediatamente).

### Consecuencias

- `core.types` mantiene los tipos legacy (`RiskDecision`, `RiskLevel`) hasta que
  se eliminen los tests que los usan. Coexisten sin conflicto de nombres porque
  pertenecen a módulos distintos.
- El import-linter contract "risk does not import outside core" garantiza el
  aislamiento en CI.
- Cualquier cambio en los umbrales de riesgo (drawdown_caution, recovery_days, etc.)
  se hace exclusivamente en `RiskConfig`, no en `engine.py`.
- Los property tests P1-P8 (hypothesis, 100-200 ejemplos) cubren monotonía de niveles,
  rate limits, HALT sin BUY, cantidad aprobada ≤ propuesta, clasificación completa de
  órdenes, inmutabilidad y determinismo.

---

## ADR-007 — Capas 2 y 3 de defensa: límites propios, SQLite y validación de mercado

**Fecha**: 2026-08-12
**Estado**: Aceptado

### Contexto

CLAUDE.md §2.3 exige tres capas de control independientes entre una señal y el broker,
con código de validación nunca compartido entre ellas. T4.2 implementó la capa 1
(Risk Engine puro). T4.3 implementa las capas 2 y 3, que deben funcionar aunque la
capa 1 esté deliberadamente rota.

### Capa 2 — ExecutionRateLimiter

**Fichero**: `src/qtrader/execution/rate_limiter.py`

Los límites son deliberadamente distintos a los del Risk Engine para que ninguna capa
sea redundante de la otra:

| Parámetro               | Risk Engine (capa 1) | RateLimiter (capa 2) |
|-------------------------|----------------------|----------------------|
| `max_orders_per_day`    | 20                   | 30                   |
| `max_notional_per_day`  | 15.000 €             | 20.000 €             |
| `max_orders_per_minute` | —                    | 5                    |

**Persistencia en SQLite**: la tabla `execution_log` (WAL mode) registra cada orden
aprobada con `order_id`, `symbol`, `notional` y `timestamp`. Las órdenes bloqueadas
**no se registran** — solo el estado real cuenta. La atomicidad se garantiza con
`BEGIN EXCLUSIVE` + `INSERT` en una sola transacción; `ROLLBACK` si se bloquea.

**Razón para SQLite y no memoria**: los rate limits deben sobrevivir reinicios del
proceso para prevenir ventanas de abuso. Un rate limiter en memoria resetea con cada
reinicio y podría explotarse reiniciando el proceso deliberadamente.

### Capa 3 — BrokerSanityChecker

**Fichero**: `src/qtrader/execution/sanity.py`

Cuatro validaciones secuenciales; la primera que falla retorna inmediatamente:

1. **Símbolo en universo activo**: bloquea cualquier símbolo no declarado. Primer
   filtro — si falla, ninguna otra validación se ejecuta.
2. **Precio dentro de ±10% del último cierre conocido**: protege contra errores de
   conversión de unidades, ticks erróneos o manipulación. Sin cierre disponible →
   bloquear (no asumir nada).
3. **Tamaño ≤ 10% del ADV estimado** (solo BUY): el fallback conservador de 2.000 €
   bloquea cualquier BUY > 2.000 € si no hay ADV disponible. Las ventas (`SELL`)
   bypasan este check para no bloquear el cierre de posiciones por falta de datos.
4. **Mercado abierto**: usa `exchange_calendars` con el calendario del exchange
   declarado en `SanityMarketState`. Los días festivos reales (NYSEHolidays, etc.) se
   verifican correctamente. Error al consultar el calendario → bloquear (fail-closed).

**Opciones descartadas para la validación de mercado**:
- Calendario hardcodeado: no scalable, incorrecto para festivos infrecuentes.
- Consulta a red: viola el invariante de tests sin red (§5 de CLAUDE.md) y crea
  dependencia en el path crítico. `exchange_calendars` es una biblioteca local con
  datos de calendario estáticos — no hace llamadas de red.

### Test de capa rota (el más importante de T4.3)

**Fichero**: `tests/execution/test_broken_risk_engine.py`

`BrokenRiskEngine.approve_everything()` aprueba cualquier orden sin evaluación.
Se construye una orden absurda (símbolo `"AAAA"`, precio 99.999 €, cantidad 10.000,
notional ≈ 1.000 M€) y se verifica que:

- La capa 1 rota la aprueba (el test lo afirma explícitamente).
- La capa 2 la bloquea por `RATE_LIMIT_NOTIONAL_DAY` (1.000 M€ >> 20.000 €/día).
- La capa 3 la bloquea por `UNKNOWN_SYMBOL` (`"AAAA"` no está en el universo).
- Cada capa bloquea de forma independiente aunque la otra sea bypaseada.
- Subprocess tests confirman que `execution/` no importa `risk/` ni `agents/`.

### Consecuencias

- El import-linter contract "execution does not import risk or agents" garantiza el
  aislamiento en CI (10 contratos activos, 0 rotos).
- Los límites distintos entre capas son intencionales: cambiarlos para que coincidan
  con los de otra capa requiere un ADR que justifique la razón.
- `exchange_calendars` necesita `# type: ignore[import-untyped]` por falta de stubs;
  el comportamiento está cubierto por tests de integración.
- El date de la sesión NYSE en los tests debe ser una sesión real (no festivo):
  2024-01-15 es MLK Day (festivo NYSE); se usa 2024-01-16 (martes ordinario).

---

## ADR-008 — Idempotencia y recuperación ante crash: WAL-first con order_intentions

**Fecha**: 2026-08-12  
**Estado**: Aceptado

### Contexto

CLAUDE.md §2.4 exige que toda orden lleve un `client_order_id` determinista y que
su intención se persista en un write-ahead log **antes** de la llamada al broker.
T5.2 implementó `PaperBroker` con estado en SQLite pero sin esta garantía: si el
proceso moría entre el `submit_order` y el registro del fill, podía haber ambigüedad
sobre si la orden había llegado al broker o no.

T5.3 implementa la garantía completa en `PaperBroker`.

### Decisiones

**`client_order_id` determinista**:

```
client_order_id = SHA256(
    trading_date.isoformat() + symbol + side.value +
    strategy_id + str(sequence_number)
)[:16]
```

`sequence_number` se persiste en `order_sequence` (tabla SQLite, 1 fila).
Se incrementa en la misma transacción que el INSERT en `order_intentions` — si
el proceso muere antes de este punto, al arrancar el seq ya está incrementado
y el id nunca se reutiliza. Mismo input → mismo id, siempre.

**Flujo WAL-first** (orden garantizado, sin excepción):

1. `BEGIN` → `INSERT order_intentions` (status=`PENDING_UNKNOWN`) + `COMMIT`
2. Llamada al broker (`INSERT OR IGNORE` en `orders`, status=`SUBMITTED`)
3. `UPDATE order_intentions → SUBMITTED`
4. `advance_to()` ejecuta el fill en `orders` + `cash_ledger`
5. `UPDATE order_intentions → CONFIRMED`

**Tabla `order_intentions`** (nueva en T5.3):

```sql
CREATE TABLE order_intentions (
    client_order_id TEXT PRIMARY KEY,
    symbol TEXT, side TEXT, quantity TEXT, strategy_id TEXT, trading_date TEXT,
    status TEXT,   -- PENDING_UNKNOWN | SUBMITTED | CONFIRMED | FAILED
    created_at TEXT, updated_at TEXT,
    broker_order_id TEXT, error_message TEXT
);
```

`INSERT OR IGNORE` en todos los pasos: reenviar un `client_order_id` ya existente
no sobreescribe el registro anterior.

**`reconcile()`** en `__init__` de `PaperBroker`:

Carga intenciones con `status NOT IN ('CONFIRMED', 'FAILED')` y para cada una
consulta el estado en la tabla `orders` (sin I/O de red — el paper broker es local).
Según el estado encontrado:

| orders status | Acción |
|---|---|
| `UNKNOWN` (sin fila) < 24h | Dejar `PENDING_UNKNOWN` — el llamador reenvía |
| `UNKNOWN` ≥ 24h | Marcar `FAILED (TIMEOUT)` |
| `SUBMITTED/PARTIAL/PENDING` | Actualizar a `SUBMITTED` |
| `FILLED` | Marcar `CONFIRMED` sin duplicar el fill |
| `CANCELLED/REJECTED` | Marcar `FAILED` |

### Alternativas rechazadas

- **Sin WAL, confiar en idempotencia del broker**: el paper broker no tiene un
  broker externo real que pueda consultar — el estado está en SQLite local.
  La tabla `order_intentions` es el WAL que permite distinguir "la orden llegó al
  broker" de "el proceso murió antes de enviarla".

- **`asyncio.run()` en `reconcile()`**: `get_order_status` es async pero en el
  paper broker no tiene I/O real. Llamar `asyncio.run()` desde `__init__` falla
  si hay un event loop activo (tests async). Solución: `reconcile()` consulta
  SQLite directamente (sync), igual que `get_order_status` pero sin el overhead
  de asyncio.

- **`_update_intention` sin `with self._conn:`**: el context manager es necesario
  para hacer commit. Sin él, las actualizaciones de estado quedaban en una
  transacción abierta no visible para el siguiente proceso que abriera la misma BD.
  Bug encontrado en tests de integración: la intención quedaba en `PENDING_UNKNOWN`
  tras `submit_order` porque el UPDATE a `SUBMITTED` nunca se comitteaba.

### Tests de crash

4 tests en `tests/brokers/test_idempotency.py` que verifican los 4 puntos de fallo:

- **crash_1**: Proceso muerto tras `INSERT order_intentions` pero antes de
  `INSERT orders`. Recovery: `PENDING_UNKNOWN`, 0 órdenes en `orders`. Sin duplicado.
- **crash_2**: Proceso muerto tras `INSERT orders` pero antes de
  `UPDATE intentions → SUBMITTED`. Recovery: intentions actualiza a `SUBMITTED`.
  La orden no se reenvía (broker ya la tiene).
- **crash_3**: Proceso muerto tras fill en `orders` pero antes de
  `UPDATE intentions → CONFIRMED`. Recovery: intención marcada `CONFIRMED`.
  El fill no se duplica. Cash invariant = 15000 − 1000 = 14000.
- **crash_4**: Proceso muerto a mitad de `advance_to()` (AAA filled, BBB+CCC SUBMITTED).
  Recovery: AAA queda `CONFIRMED`, BBB+CCC quedan `SUBMITTED` para el siguiente
  `advance_to()`. Sin duplicado de AAA.

El mecanismo de kill es `process.kill()` (= `TerminateProcess` en Windows), que
equivale a `SIGKILL` en Unix: sin posibilidad de limpieza, idéntico a un corte de
corriente desde el punto de vista de la integridad de la BD.

### Consecuencias

- Cualquier reinicio de `PaperBroker` reconcilia el estado sin intervención humana.
- El import-linter contract "brokers does not import agents or portfolio" se mantiene
  (11 contratos, 0 rotos).
- En el broker IBKR real (fase 11), `reconcile()` deberá llamar a la API del broker
  para `get_order_status` — el mismo patrón, distinta implementación del paso de I/O.

---

## ADR-009 — El agente pide el HALT, no lo activa

**Fecha**: 2026-08-12
**Estado**: Aceptado

### Contexto

La especificación de T6 describe el estado `ERROR` como «loggear, activar kill
switch, detenerse». Esto contradice dos invariantes ya establecidas:

- CLAUDE.md §2.6: «El kill switch está fuera del proceso del agente. **El agente
  lo lee; no puede escribirlo ni borrarlo.**»
- El docstring de `KillSwitch.activate()` (`src/qtrader/safety/kill_switch.py:85-88`):
  «Puede llamarse desde cualquier componente **excepto el agente**.»

Además, en un despliegue real donde el fichero HALT tiene permisos de solo
lectura para el usuario del agente, `activate()` fallaría con `PermissionError`:
la ruta ni siquiera es ejecutable de forma fiable.

Por CLAUDE.md §6, ante una contradicción entre una tarea y este fichero, gana el
fichero, y hay que decirlo explícitamente en vez de resolverlo en silencio.

### Opciones consideradas

1. **Implementar el spec literalmente**: el agente llama a `activate()`. Viola
   §2.6 y falla en producción con permisos correctos.
2. **No activar nunca**: el agente solo deja de latir y el Watchdog activa el
   HALT a los 180 s. Máxima pureza, pero deja una ventana de hasta 3 minutos sin
   HALT tras un error fatal.
3. **Halt requester inyectado** (elegida).

### Decisión

El agente recibe un `halt_requester: Callable[[str], None] | None` inyectado:

- `--mode paper` / `development`: el CLI inyecta `kill_switch.activate`, útil en
  un bucle local monousuario.
- `--mode production`: `None`. El Watchdog, que vive fuera del proceso, activa el
  HALT al no recibir heartbeat.

El agente tipa el kill switch como `KillSwitchReader`, un `Protocol` con un solo
método `is_active()`. `activate()` y `deactivate()` **no están en la superficie de
tipos del agente**: la invariante queda garantizada por estructura, no por
convención. Hay un test que lo comprueba
(`test_agent_never_calls_activate_directly`).

### Consecuencias

- Se cumple la intención del spec (el sistema deja de operar tras un error fatal)
  sin que el agente escriba el kill switch en producción.
- El modo de producción depende del Watchdog para el HALT automático, con la
  latencia de `max_silence_seconds`. Es el comportamiento correcto: el actor que
  detiene el sistema está fuera del proceso que ha fallado.
- Un `AgentHalted` lanzado dentro de un handler también pasa por `_to_error`:
  persiste `ERROR` y pide el HALT. Sin eso, un `VALIDATION_HALT` se propagaría sin
  dejar rastro en `agent_state`.

---

## ADR-010 — El agente posee su propia base de datos (`data/agent.db`)

**Fecha**: 2026-08-12
**Estado**: Aceptado

### Contexto

La especificación de T6 dice «Tabla `agent_state` en `state.db`». Pero
`SQLiteLedger.initialize()` ejecuta `_DROP + _SCHEMA`
(`src/qtrader/ledger/sqlite.py:82-86`), es decir, **borra todas las tablas**, y
`qtrader demo` la invoca con el default `data/state.db`.

Co-localizar `agent_state` con el ledger significaría que un `qtrader demo`
accidental destruiría la cadena de auditoría dejando `agent_state` huérfano,
apuntando a un `cycle_id` cuyos registros ya no existen.

### Decisión

El agente posee `data/agent.db` con DDL idempotente (`CREATE TABLE IF NOT EXISTS`)
para `agent_state`, `agent_pending_orders` y `agent_nav_history`, y **nunca llama
a `initialize()`**. Para el ledger usa `ensure_ledger_schema()`, que crea el
esquema sin borrar nada.

Sigue el patrón ya establecido en el repositorio: `PaperBroker`,
`ExecutionRateLimiter` y `trials_db` tienen cada uno su propio fichero.

### Consecuencias

- Cuatro ficheros SQLite, cuatro dueños, sin interferencias destructivas.
- Todas las rutas son configurables por CLI, lo que hace los tests triviales.
- `agent_state` es append-only en vez de un UPDATE sobre una fila única: encaja
  con la cultura de auditoría del proyecto y hace inspeccionables los tests de
  crash. `load()` devuelve la última fila.
- La clave de `agent_pending_orders` es `(trading_date, order_id)` y **no incluye
  `cycle_id`**: las órdenes las aprueba el ciclo POST_CLOSE del día T y las envía
  el ciclo PRE_OPEN del día siguiente, que es otro proceso con su propio
  `cycle_id`. Incluirlo haría que PRE_OPEN nunca encontrase nada que enviar.

---

## ADR-011 — Tras un crash en `SUBMITTING_ORDERS` se reanuda en `RECONCILING`

**Fecha**: 2026-08-12
**Estado**: Aceptado

### Contexto

Si el proceso muere dentro de `SUBMITTING_ORDERS`, el conjunto de órdenes que
llegó al broker es **desconocido para el agente**. `PaperBroker.submit_order`
tiene tres pasos durables (`paper_broker.py:240-291`) y el crash puede caer entre
cualquiera de ellos; además, el bucle puede morir entre la orden *k* y la *k+1*.

El estado real es una partición del lote en {nunca enviada, enviada-desconocida,
enviada-confirmada}. Como `agent_pending_orders.submitted_at` se escribe *después*
de que retorne el `await`, una fila que parece no enviada puede haber llegado al
broker.

### Decisión

`RESUME_MAP[SUBMITTING_ORDERS] = RECONCILING`. Nunca se reintentan los submits
directamente. El conjunto a reintentar solo queda bien definido **después** de
reconciliar:

```
retryable = filas con submitted_at IS NULL AND client_order_id IS NULL
          ∪ filas con submitted_at IS NULL AND client_order_id IS NOT NULL
            AND get_order_status(coid) == UNKNOWN
```

La segunda rama es el «consultar antes de reenviar» que exige CLAUDE.md §2.4.
Para que el `client_order_id` sea reconstruible, `sequence_number` y
`client_order_id` se escriben **commiteados antes** del `await submit_order`, en la
misma transacción que `next_sequence()` — que deliberadamente no hace commit
(`order_id.py:40-52`) precisamente para permitir esta composición.

Por el mismo motivo, `GENERATING_SIGNALS` y `EVALUATING_RISK` reanudan en
`LOADING_DATA` (dependen de datos en memoria que murieron con el proceso; la
estrategia es *stateful* y reanudar ahí produciría cero señales en silencio), y
`MONITORING` reanuda en `RECONCILING` (`advance_to()` muta cash y posiciones).

### Consecuencias

- Reintentar directamente duplicaría exactamente el subconjunto
  «enviada-desconocida», violando §2.4 y §1 («un sistema que gana un 40 % con una
  orden duplicada sin explicar es un fracaso»).
- Un `ERROR` persistido **rechaza arrancar** (exit 2) y exige intervención humana:
  un agente que se auto-reanuda tras un error fatal anula el propósito del estado.
- Verificado con un test de subprocess y `kill()` real, además de los seis tests
  en proceso, uno por estado.

---

## ADR-012 — `WARNING` es una severidad, no un estado

**Fecha**: 2026-08-12
**Estado**: Aceptado

### Contexto

La especificación lista `WARNING` junto a `ERROR` y `SLEEPING` como «estado
especial»: «loggear, continuar con precaución, no detener».

### Decisión

No se modela como `AgentState`. Un estado en el que nunca permaneces y del que
siempre sales hacia donde venías no es un estado: es una severidad. Modelarlo
como estado obligaría a cada estado a recordar su predecesor y, sobre todo,
haría que `agent_state.current_state = 'WARNING'` fuera **irreanudable** tras un
crash: ¿reanudar dónde?

Se maneja dentro de cada handler: log a nivel WARNING, registro `AGENT_WARNING`
en auditoría, y el ciclo continúa. `CycleResult.warnings` los acumula.

### Consecuencias

- La tabla de recuperación cubre exactamente los seis estados reanudables.
- Los avisos siguen siendo visibles y auditables, sin contaminar la máquina de
  estados.

---

## ADR-013 — Deuda registrada: acoplamientos que T6 no arregla

**Fecha**: 2026-08-12
**Estado**: Aceptado

### Contexto

Al cablear el agente aparecieron tres asperezas preexistentes. Arreglarlas
excede el alcance de «conectar lo que ya existe», así que se registran en vez de
resolverse en silencio.

### Observaciones

1. **`ApprovedOrder` no tiene `price` ni `strategy_id`** (`risk/types.py:89-100`).
   `PaperBroker` lo sortea con `getattr(order, "strategy_id", "unknown")`
   (`paper_broker.py:255`), así que toda orden del agente queda registrada con
   `strategy_id="unknown"` en `order_intentions`. El precio **no se deriva** de
   `notional / approved_quantity` (perdería precisión en divisiones no exactas):
   se transporta en un `price_map` lateral indexado por `order_id` y se persiste
   en `agent_pending_orders.price`.

2. **`RiskEngine.evaluate` ignora el nivel de riesgo previo**: hardcodea
   `current_level=RiskLevel.NORMAL` (`engine.py:475`), con lo que la histéresis de
   `recovery_days` está efectivamente muerta. El agente no lo compensa.

3. **`SandboxedDataView` vive en `qtrader.backtesting.engine`** y `momentum.py` la
   referencia bajo `TYPE_CHECKING` (`momentum.py:28-29`). El agente no puede
   importar `backtesting`, así que define su propia `_InMemoryDataView`
   duck-typed, con la guarda anti-look-ahead incluida. El contrato de
   import-linter usa `allow_indirect_imports = true` porque el import transitivo
   vía `momentum` es solo de tipos y no existe en runtime. Moverla a `qtrader/data/`
   sería la solución limpia y requiere su propio ADR.

### Consecuencias

- Existen **dos** `RiskLevel` y **dos** `RiskDecision` en el repositorio: los de
  `qtrader.risk.types` (NORMAL/CAUTION/RISK_OFF/HALT) y los legacy de
  `qtrader.core.types` (APPROVE/REDUCE/REJECT). El agente usa **solo** los de
  `risk.types`. Unificarlos es trabajo pendiente.
- El CLI construye el proveedor sintético y lo inyecta, de modo que `agents/`
  nunca importa `backtesting` de forma directa.
