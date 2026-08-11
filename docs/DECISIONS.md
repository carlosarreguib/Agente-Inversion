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
