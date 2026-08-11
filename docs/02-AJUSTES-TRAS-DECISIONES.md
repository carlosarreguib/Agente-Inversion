# Addendum: ajustes tras las decisiones de diseño

Decisiones tomadas (fijadas, no reabrir sin ADR):

| Decisión | Valor |
|---|---|
| Horizonte | **Solo swing.** Intradía fuera del alcance del proyecto |
| Presupuesto de datos | **0 €/mes** |
| Capital de simulación | **15.000 €** (configurable en rango 10.000–25.000) |

---

## 1. Capital 15.000 € — la economía ahora sí cierra

| Parámetro | Valor |
|---|---|
| Capital inicial | 15.000 € |
| Posiciones simultáneas | 6–10 |
| Tamaño de posición | 1.500–2.500 € |
| Comisión mín. estimada (IBKR) | ~1,00–1,25 € por lado |
| **Coste de comisión por round-trip** | **~0,10–0,17 % del nominal** |
| Coste total con spread y slippage (ETFs líquidos) | ~0,15–0,25 % |
| MaxDD objetivo 15 % | 2.250 € |
| Tamaño mínimo de orden (regla dura) | 1.000 € — por debajo, la comisión mínima supera el 0,12 % y la orden se descarta |

Con holding objetivo de 15–60 días y un turnover de 200–400 % anual, el coste anual queda en 0,4–1,0 %. Eso es un lastre asumible frente a movimientos esperados del 3–8 % por operación. **Esta es la diferencia entre un sistema evaluable y uno matemáticamente muerto.**

Efectos colaterales positivos: la regla PDT deja de aplicar (no hay day trades), y ya no dependes de acciones fraccionadas para diversificar.

---

## 2. Solo swing — qué desaparece del alcance

**Fuera**: datos intradía, tick data, latencia como requisito, order books, ejecución algorítmica, PDT, gestión de posiciones dentro de sesión.

**Diseño resultante:**

| Aspecto | Decisión |
|---|---|
| Barra base | Diaria (OHLCV) |
| Frecuencia de decisión | 1 vez al día, tras el cierre del mercado |
| Frecuencia de rebalanceo | Semanal (viernes) por defecto; mensual como alternativa configurable |
| Ejecución | Órdenes limitadas al día siguiente contra la apertura, con banda de precio; fallback a `MOO` si no ejecuta |
| Holding objetivo | 15–60 días |
| Overnight risk | Asumido por diseño. Se gestiona con tamaño de posición y stops por volatilidad (ATR), no con cierres intradía |
| Gaps | Modelados explícitamente: el stop se evalúa contra el precio de apertura, no contra el nivel teórico. El backtester debe simular el gap-through |
| Ciclo del agente | 2 ejecuciones diarias: `post-close` (decidir) y `pre-open` (reconciliar + enviar) |

---

## 3. Datos a coste 0 € — la restricción estructural

Esta es la decisión que más condiciona el resultado. Lo que hay disponible gratis (verificado):

| Fuente | Qué da | Límite |
|---|---|---|
| **Tiingo free** | EOD US equities+ETFs, historia larga, splits y dividendos | 500 símbolos/mes, ~50 símbolos/hora. Uso personal e interno únicamente |
| **yfinance** | EOD global, `auto_adjust=False` + serie de `actions` | Sin SLA, API inestable, ToS gris |
| **Stooq** | EOD CSV, US + EU | Sin garantías, útil como tercera fuente |
| Alpha Vantage / EODHD free | ~20–25 llamadas/día | Inservible para un universo |

**Lo que NINGUNA fuente gratuita da: universo point-in-time con deslistados y eventos corporativos fechados.** No es un problema de esfuerzo, es que ese dato es el producto que venden los proveedores de pago.

### 3.1 Consecuencia y mitigación arquitectónica: universo ETF-céntrico

Si no puedes eliminar el survivorship bias, elige un universo donde sea pequeño y acotable. **Los ETFs grandes y consolidados casi no mueren**; las acciones individuales sí. Un universo ETF resuelve de un golpe cuatro problemas: survivorship bias, liquidez, spread y la restricción PRIIPs/UCITS.

Universo propuesto (~50 instrumentos, congelado y declarado por escrito):

- Regiones: US, Europa, Japón, emergentes, world
- Sectores: los 11 GICS
- Factores: momentum, value, quality, min-vol, small cap
- Renta fija: treasuries corto/medio/largo, corporate, high yield
- Alternativos: oro, materias primas, REITs

Esto además hace que la estrategia natural sea **rotación sectorial/regional por momentum**, que es de las mejor documentadas de la literatura.

### 3.2 El truco del proxy — declarado, no escondido

Los ETFs UCITS europeos tienen historia corta (muchos post-2010) y peor calidad de datos en fuentes gratuitas. Solución estándar:

> **Backtest sobre los proxies US** (SPY, XLK, IEF, GLD, EFA…), que tienen 20-30 años de historia limpia y gratuita. **Operar los equivalentes UCITS** (CSPX, IUIT, IDTM, SGLN, IWRD…).

Obligatorio: mantener `config/universe_mapping.yaml` con el mapeo proxy→UCITS, medir el tracking error entre ambos en el periodo solapado, y **restar ese tracking error de los resultados del backtest**. Si el TE anualizado supera 50 bps para un par, ese instrumento sale del universo.

### 3.3 Reglas nuevas de calidad de datos (sustituyen a "pagar por calidad")

1. **Cross-validación obligatoria entre 2 fuentes.** Todo cierre se descarga de Tiingo y de yfinance. Discrepancia > 0,5 % → dato marcado `SUSPECT`, instrumento excluido ese día y registrado en auditoría. Es la única forma de tener control de calidad sin pagar.
2. **Caché local Parquet inmutable.** Se descarga una vez y nunca se vuelve a pedir un dato histórico ya almacenado. Protege contra los rate limits, contra revisiones silenciosas del proveedor y contra que yfinance deje de funcionar un martes.
3. **Snapshot con hash por sesión.** Cada decisión referencia el hash del snapshot de datos que usó. Sin esto no hay reproducibilidad, y con fuentes gratuitas los datos cambian bajo tus pies.
4. **Universo congelado con fecha de declaración.** El fichero `config/universe.yaml` lleva un campo `declared_on`. Añadir un instrumento crea una versión nueva; los backtests anteriores a esa fecha usan la versión anterior. Esto es lo más cerca que puedes estar de point-in-time sin pagar.
5. **Haircut declarado.** Todo informe de backtest incluye la penalización estimada por sesgo residual (para universo ETF: ~10-30 bps anuales; para acciones: 100-400 bps) y muestra el Sharpe antes y después. No se discute el resultado bruto en ningún sitio.

### 3.4 Reencuadre honesto

Con datos gratuitos, **el backtest es un generador de hipótesis y un detector de bugs, no una validación**. Debe aparecer así en el README. La validación real la da el walk-forward y el paper trading prolongado sobre datos que el sistema ve por primera vez.

Licencias: Tiingo free es de uso personal e interno; yfinance opera en una zona gris de los ToS de Yahoo. **No redistribuir datos, no publicar el caché, no hacer el repo público con `data/` dentro.** Añadirlo a `.gitignore` en el commit 1.

---

## 4. Cambios concretos al plan de fases

| Tarea | Cambio |
|---|---|
| T1.1 | Proveedores: `TiingoProvider`, `YFinanceProvider`, `StooqProvider`, `SyntheticProvider`. Caché Parquet inmutable con hash. |
| T1.2 | **Nueva regla**: cross-validación entre 2 fuentes con umbral 0,5 %. |
| T1.3 | Universo ETF congelado con `declared_on` + `universe_mapping.yaml` proxy→UCITS + test de tracking error. |
| T2.1 | Solo barras diarias. Fill contra apertura de T+1. **Simulación explícita de gap-through en stops.** |
| T2.2 | Costes calibrados a 15.000 €: comisión mín. 1,25 €, tamaño mínimo de orden 1.000 €. |
| T3.1 | **Rotación por momentum sobre ETFs** (12-1, quintil superior, rebalanceo semanal) en lugar de momentum sobre acciones. Rango esperado de validación: Sharpe 0,4–0,8, MaxDD 20–35 %. |
| T4.1 | 6–10 posiciones, vol targeting, banda de no-rebalanceo del 20 % para controlar turnover. |
| — | Eliminadas todas las referencias a intradía en fases 1–12. |

Todo lo demás del plan original (fases 0, 5, 6, 7, 8, 9, 10) se mantiene sin cambios.

---

## 5. Coste mensual real del sistema

| Concepto | Coste |
|---|---|
| Datos | 0 € |
| LLM (solo Researcher, offline, uso ocasional) | 5–20 € |
| Infraestructura (local, sin cloud) | 0 € |
| Broker paper (IBKR) | 0 € |
| **Total** | **5–20 €/mes** |

Sobre 15.000 € eso es 0,4–1,6 % anual. Sigue siendo un lastre real —comparable al TER de un fondo activo— pero ya no es absurdo. Es una razón más para mantener el LLM fuera del bucle diario: si se ejecutara en cada decisión, el coste se multiplicaría por 20.
