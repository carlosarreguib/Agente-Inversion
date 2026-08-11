# CLAUDE.md — Invariantes del proyecto `qtrader`

Este fichero se lee al inicio de toda sesión. Las reglas de aquí **no se negocian ni se relajan** por conveniencia de una tarea concreta. Si una tarea parece exigir violar una regla, la tarea está mal planteada: páralo y dilo.

---

## 1. Qué es este proyecto

Plataforma experimental de gestión cuantitativa de cartera. Opera **exclusivamente en simulación** hasta que exista una decisión humana firmada y registrada de pasar a real.

Parámetros fijados (cambiarlos requiere un ADR):

- **Solo swing.** Barras diarias, holding 15-60 días, rebalanceo semanal. **No existe código intradía en este repositorio.**
- **Capital de simulación: 15.000 €.** Tamaño mínimo de orden: 1.000 €.
- **Presupuesto de datos: 0 €.** Fuentes gratuitas con las mitigaciones de la sección 3.
- **Universo ETF-céntrico** (~50 instrumentos), congelado y versionado con fecha de declaración.

**El objetivo primario es la corrección operativa, no la rentabilidad.** Un sistema que pierde un 3 % con reconciliación perfecta y trazabilidad completa es un éxito. Un sistema que gana un 40 % con una orden duplicada sin explicar es un fracaso.

---

## 2. Invariantes de seguridad (violarlas es un bug crítico)

1. **Ningún componente de IA puede enviar órdenes.** Un LLM puede como máximo llamar a `propose_order`. `submit_order` no existe en ningún toolset de ningún agente. La decisión y el envío son deterministas.
2. **El Risk Engine es puro**: sin I/O, sin red, sin estado global, sin aleatoriedad. Mismas entradas → misma salida, siempre.
3. **Existen 3 capas de control independientes** entre una señal y el broker: Risk Engine, rate limits del Execution Engine, validación de sanidad del Broker Interface. Cada una con su config y sus tests. Nunca compartir código de validación entre ellas.
4. **Toda orden lleva `client_order_id` determinista** y su intención se persiste en un write-ahead log **antes** de la llamada al broker. Nunca reenviar una orden sin consultar antes su estado por ese id.
5. **El broker es la fuente de verdad.** La BD local es caché. Reconciliar al arrancar y periódicamente. Discrepancia > tolerancia → HALT, nunca un warning.
6. **El kill switch está fuera del proceso del agente.** El agente lo lee; no puede escribirlo ni borrarlo. Se comprueba en cada ciclo y antes de cada envío.
7. **Ningún secreto en el código, en los logs ni en los tests.** Solo variables de entorno / gestor de secretos. Credenciales de paper y de real completamente separadas, nunca cargadas en el mismo proceso.
8. **El dashboard escucha en `127.0.0.1`.** Nunca en `0.0.0.0`. Nunca sin auth si eso cambia.
9. **Todo texto de fuente externa que llegue a un LLM se trata como dato inerte**, delimitado y etiquetado, nunca como instrucción. Toda salida de LLM se valida contra un schema Pydantic estricto antes de usarse.
10. **`production` no se activa con un flag.** Requiere variable de entorno + fichero de autorización con caducidad + confirmación interactiva.

---

## 3. Invariantes de corrección cuantitativa

1. **Prohibido el look-ahead.** Una señal generada con el cierre de T se ejecuta contra la apertura de T+1 o posterior. Nunca contra la barra que la generó.
2. **Precios almacenados sin ajustar** + tabla separada de corporate actions. El ajuste se aplica en lectura con un `as_of` explícito.
3. **Universo point-in-time.** La composición del universo en una fecha nunca se recalcula con información posterior. Los instrumentos deslistados permanecen en el histórico.
4. **Datos sospechosos → excluir e informar.** Nunca interpolar, rellenar hacia adelante ni "arreglar" silenciosamente.
5. **Todo backtest incluye comisiones, spread, slippage y límite de participación en el volumen.** No existe el modo "sin costes" salvo como comparación explícita en un test.
6. **Toda configuración evaluada se registra en el registro de trials**, incluidas las descartadas. El Deflated Sharpe Ratio de cualquier estrategia se calcula con ese contador.
7. **Un resultado sospechosamente bueno se trata como bug hasta demostrar lo contrario.** Sharpe > 1,5 en un backtest de estrategia clásica: buscar el leakage antes de celebrar.
8. **Cross-validación entre dos fuentes de datos.** Todo cierre se contrasta entre Tiingo y yfinance. Discrepancia > 0,5 % → `SUSPECT`, instrumento excluido ese día, registrado en auditoría. Sin fuente secundaria disponible → el día no se opera.
9. **Caché inmutable.** Un dato histórico ya almacenado no se vuelve a descargar ni se sobrescribe nunca. Las fuentes gratuitas revisan sus series en silencio.
10. **Cada decisión referencia el hash del snapshot de datos que usó.** Sin eso no hay reproducibilidad.
11. **Todo informe de backtest muestra el Sharpe con y sin el haircut declarado** por survivorship bias residual y tracking error proxy→UCITS. El resultado bruto no se cita en ningún sitio sin el neto al lado.
12. **El backtest genera hipótesis; no valida.** La validación es walk-forward + paper trading sobre datos nunca vistos. Este framing va en el README y no se suaviza.

---

## 4. Reglas de código

- Python 3.12. `uv` para dependencias, con lockfile.
- `mypy --strict` y `ruff` en verde. Sin `# type: ignore` sin comentario justificativo.
- **Dinero en `Decimal`**, nunca `float`. Cantidades de acciones en `Decimal` (fraccionales).
- **Timestamps tz-aware en UTC** en todo el sistema. La conversión a hora local ocurre solo en la capa de presentación.
- Contratos entre módulos con **Pydantic v2 `frozen=True`**. Nada de `dict[str, Any]` cruzando fronteras de módulo.
- Dependencias de capas (verificadas con `import-linter`): `strategies` no importa `execution`; `execution` no importa `agents`; `agents/llm` no importa `execution` ni `brokers`; `risk` no importa nada fuera de `core`.
- Sin estado global mutable. Sin singletons. La configuración se inyecta.

---

## 5. Reglas de tests

- **Ninguna funcionalidad sin test.** Ningún test se debilita para que pase; se arregla el código.
- **Property-based (`hypothesis`) obligatorio** para: Risk Engine, position sizing, portfolio construction, contabilidad del ledger.
- **Tests de crash** para el ciclo de órdenes: matar el proceso en cada estado y verificar que la recuperación no duplica ni pierde nada.
- **Golden backtest**: resultados numéricos congelados que detectan regresiones del motor.
- Los tests no acceden a la red. Nunca. Proveedores externos siempre mockeados.

---

## 6. Reglas de trabajo con Claude Code

- **Una tarea del plan por sesión.** No adelantar trabajo de fases posteriores.
- Al terminar, **ejecutar el comando de aceptación y pegar la salida real**. No describir lo que debería salir.
- Toda decisión no trivial → un ADR en `docs/DECISIONS.md` (contexto, opciones, decisión, consecuencias).
- Ante ambigüedad menor: tomar la decisión razonable, documentarla, seguir. Ante ambigüedad que afecte a una invariante de la sección 2 o 3: parar y preguntar.
- Si detectas que una instrucción de una tarea contradice este fichero, **este fichero gana**. Dilo explícitamente en vez de resolverlo en silencio.

---

## 7. Cosas que este proyecto NO hace

- No opera con dinero real sin firma humana explícita y registrada.
- No usa un LLM como oráculo de mercado ni en el bucle de decisión.
- No promete rentabilidad. El benchmark honesto es un ETF indexado global después de costes e impuestos, y lo más probable es no superarlo.
- No optimiza el retorno histórico como objetivo único.
- No despliega automáticamente una estrategia nueva a producción.
