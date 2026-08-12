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
