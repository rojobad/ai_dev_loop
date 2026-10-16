# INC-2026-10-12-004: Cursor `resource_exhausted` con cuota disponible, sin clasificar

## Estado y alcance

- Fecha: **2026-10-12**. Todas las horas de esta entrada usan
  **America/La_Paz (UTC−04:00)**.
- Estado operativo: **recuperado a las 22:32** con `cursor-retry --force`.
- Causa inicial: **parcialmente determinada** (análisis con Codex en modo solo
  lectura). El origen exacto del `resource_exhausted` en el backend de Cursor
  es una inferencia: probablemente capacidad transitoria del servicio o del
  modelo, no cuota de la cuenta.
- Relacionado: [INC-2026-10-12-001](2026-10-12-cursor-out-of-usage-unclassified.md)
  (otro mensaje de Cursor sin clasificar) y
  [INC-2026-10-12-003](2026-10-12-codex-review-json-permissions-recurrence.md)
  (bloqueó la recuperación).

## Contexto

| Referencia | Valor |
| --- | --- |
| Run afectado | `ai-dev-loop-hub-8b36e4ea939b` (fase 8/17 `phase-14-8-machine-activity-ui`) |
| Intento Cursor | `att-ca55950dded781e3eccb004c2cb29925` (corrección, iteración 3, mismo chat) |
| Evidencia | `cursor/iterations/03/att-ca55950dded781e3eccb004c2cb29925/stderr.txt`, `metadata.json`; `attempts/att-ca55950dded781e3eccb004c2cb29925/result.json` |
| Cuota de Cursor al revisar (~22:20) | Pro, Included 28%, Auto 29%, API 0%, on-demand apagado, reinicio 12 nov |

## Hechos verificados

- 21:54:09–21:54:39: el intento terminó con exit 1 a los ~30 s, sin señal de
  finalización, con `RetriableError: [resource_exhausted] Error` tras tres
  intentos de reconexión de Cursor. No hubo cambios de corrección.
- El wrapper dejó `failure_code=null`; el run quedó `blocked` /
  `cursor_failure` sin reintento automático.

## Diagnóstico

- El error es marcado por Cursor como reintentable, pero el clasificador no
  lo reconoce, así que no hay reintento automático ni clasificación de
  capacidad.
- Con la cuota de la cuenta disponible, no corresponde tratarlo como límite
  de uso.

## Corrección propuesta (no implementada)

- Clasificar `resource_exhausted` / `RetriableError` como fallo transitorio
  de capacidad, distinto del límite de uso de la cuenta, con reintento
  acotado.
