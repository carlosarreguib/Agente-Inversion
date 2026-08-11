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
`math.erfinv` (stdlib Python 3.12) en lugar de scipy para mantener el
presupuesto de dependencias bajo. El DSR se calcula sobre todos los trials
COMPLETED de la misma estrategia en la DB, no solo sobre los del run actual.
Esto penaliza correctamente el data-mining acumulado a lo largo de varias
sesiones de investigación.

### Consecuencias

- El informe `uv run qtrader research report --strategy X` siempre muestra
  el DSR basado en el histórico completo de la DB, no en el run aislado.
- Cualquier cambio en la fórmula DSR (e.g. sigma_SR corregido por sesgo)
  debe documentarse aquí y regenerar los informes.
- La invariante de no-look-ahead (§3.1) es verificable leyendo `Fold.train_days`:
  el último elemento siempre es al menos `embargo_days` antes de `test_start`.
