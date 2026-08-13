# Estado del proyecto — inicio de Fase 10 (paper trading)

Fecha: 2026-08-13
Tests: 620 passed, 1 skipped
Contratos import-linter: 17
mypy strict: 73 ficheros, 0 errores
Fases completadas: 0–9

---

## Criterios de salida de la Fase 10

La fase 10 termina cuando se cumplen TODOS los criterios siguientes.
El retorno absoluto NO es un criterio de salida — 6 meses no tienen
potencia estadística para evaluarlo.

### Criterios operativos (ponderación 80 %)

| Criterio | Umbral | Peso |
|---|---|---|
| Incidentes operativos no recuperados | 0 | 30 % |
| Reconciliación diaria limpia | 100 % de sesiones | 20 % |
| Slippage real vs modelado en backtest | dentro de ± 5 bps | 20 % |
| Cobertura de auditoría | 100 % de decisiones trazables | 10 % |

### Criterios de actividad (ponderación 20 %)

| Criterio | Umbral | Peso |
|---|---|---|
| Número de trades ejecutados | ≥ 100 | 10 % |
| Performance vs backtest | dentro de 1.5 σ del intervalo simulado | 10 % |

### Duración mínima

6 meses de mercado real (no días calendario).
Inicio: primer día hábil tras activar el agente en modo paper.

### Qué hacer si se activa el kill switch

- Registrar el incidente completo en docs/INCIDENTS.md
- Diagnosticar la causa raíz antes de reanudar
- Si la causa es un bug de código: corregir, añadir test,
  reiniciar el contador de "incidentes no recuperados"
- Si la causa es un evento de mercado extremo: documentar,
  reanudar sin penalización en el contador

### Qué NO hacer durante la Fase 10

- No modificar parámetros de la estrategia en producción
- No añadir nuevas estrategias al agente (eso va al Researcher)
- No cambiar umbrales del Risk Engine
- No "ayudar" al sistema en decisiones de trading

Cualquier cambio de código durante la Fase 10 requiere:
1. Un ADR documentado
2. Reinicio del contador de días si afecta a la lógica de trading

---

## Comando de arranque

```bash
uv run qtrader run --mode paper
```

El agente corre en background. El dashboard en:

```bash
uv run qtrader dashboard
# → http://127.0.0.1:8501
```

Métricas diarias en data/metrics.csv.
Alertas en data/alerts.log + Telegram si configurado.

---

## Checklist diario (5 minutos)

- [ ] `uv run qtrader audit verify` → OK
- [ ] Dashboard: reconciliación limpia
- [ ] Dashboard: nivel de riesgo NORMAL
- [ ] data/alerts.log: sin alertas críticas nuevas
- [ ] data/metrics.csv: NAV actualizado

## Checklist semanal (15 minutos)

- [ ] `uv run qtrader research robustness-report` → guardar en docs/weekly/
- [ ] Revisar slippage real vs modelado en el informe
- [ ] Revisar turnover vs presupuesto de costes
- [ ] `git tag week-N` para trazabilidad