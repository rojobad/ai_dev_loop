# INC-2026-10-12-001: agotamiento de cuota de Cursor no clasificado ("out of usage")

## Estado y alcance

- Fecha: **2026-10-12**. Todas las horas de esta entrada usan
  **America/La_Paz (UTC−04:00)**.
- Estado operativo: **recuperado a las 11:23** mediante
  `cursor-retry --force`, después de restablecer la cuota de Cursor.
- Causa inicial: **determinada** (análisis de causa raíz hecho con Codex en
  modo solo lectura).
- Decisión del operador: registrar el incidente. Esta entrada no incorpora
  safeguards, parches ni cambios de política.
- Relacionado: [INC-2026-10-12-002](2026-10-12-codex-review-json-permissions-false-tamper.md),
  que bloqueó la recuperación de este mismo run.

## Contexto

| Referencia | Valor |
| --- | --- |
| Proyecto | `ai_dev_loop_hub` (corre en `main` de forma intencional) |
| Secuencia | `ai-dev-loop-hub-seq-1da48112f32b`, fase 3/17 `phase-14-3-resource-request-audit` |
| Run afectado | `ai-dev-loop-hub-c78a3ad37d89` |
| Intento Cursor afectado | `att-c35e6c719ee5626e8c19f31fd31a1015` (corrección, iteración 2) |
| Run sucesor de la recuperación | `ai-dev-loop-hub-20261012T152338Z-621ac3` |
| Entorno | WSL del NucBox; Cursor Agent `2026.10.01-e373342`, plan Pro |

## Síntomas e impacto

Durante la corrección de la iteración 2, Cursor terminó con exit 1 y este
mensaje en stderr:

```text
ActionRequiredError: Increase limits ... You're out of usage. Switch to Auto...
```

El run y la secuencia pasaron a `blocked` con `cursor_failure`, sin
`failure_code`. No se emitió `cursor_usage_limit_detected` ni se creó la
continuación acotada por límite de uso. El `Next action` indicaba que no había
reintento automático. La fase estuvo detenida unas **7 horas y 40 minutos**
(03:42–11:23).

## Cronología verificada

| Hora | Hecho |
| --- | --- |
| 02:01:39 | Se envía y autoriza la fase 3/17. |
| 02:02–02:41 | El turno inicial de Cursor termina bien. |
| 02:42–02:48 | La primera revisión de Codex sale con un resultado inválido. |
| 03:26–03:29 | La revisión de Codex reintentada a mano sale bien y pide correcciones. |
| 03:30:29 | Arranca la corrección de Cursor (iteración 2) en el mismo chat. |
| 03:42:17 | Después de 707 s y varias ediciones, Cursor emite `You're out of usage`. |
| 03:42:22 | Se registra el intento con exit 1; el run queda `blocked` / `cursor_failure`. |
| ~03:50 | El operador confirma con `/usage` que el plan está al 100% (Included, Auto y API) y que on-demand está apagado. |
| 11:23 | Con cuota nueva (otra cuenta de Cursor) y tras resolver INC-2026-10-12-002, `cursor-retry --force` crea el run sucesor. |

## Diagnóstico

- Disparador: **agotamiento real de la cuota de Cursor**, confirmado por el
  stderr, la hora y `/usage`.
- Defecto: el clasificador de fallos de Cursor
  (`src/ai_dev_loop/runners/cursor_failure.py`) reconoce frases como
  "usage limit", pero no la redacción actual de Cursor, "out of usage" /
  "Switch to Auto". Por eso el fallo quedó sin clasificar, en vez de
  `cursor_usage_limit`.
- Por qué no se detectó: los fixtures y las pruebas usan las redacciones
  anteriores del mensaje.
- Riesgo de recurrencia: **alto**. Cada vez que se agote la cuota con esta
  redacción, el fallo terminará como `cursor_failure` sin clasificar.

## Recuperación realizada

1. Se esperó a que hubiera cuota (el dueño inició sesión con otra cuenta de
   Cursor; `/usage` mostró 0%).
2. Se resolvió INC-2026-10-12-002, que impedía la recuperación.
3. `ai_dev_loop scheduler cursor-retry --check` devolvió
   `authenticated_cursor_failure`, `recovery_supported=true`.
4. `ai_dev_loop scheduler cursor-retry --force ai-dev-loop-hub-c78a3ad37d89`
   creó el sucesor `ai-dev-loop-hub-20261012T152338Z-621ac3` con el intento
   `att-95ebbf4ac155a18b4d512531f9b0cb96` activo. No se abortó ni el run ni la
   secuencia, y no se editó el ledger.

## Corrección propuesta (no implementada)

- Clasificar "out of usage" / "Switch to Auto" (`ActionRequiredError`) como
  `cursor_usage_limit`.
- Agregar una prueba con el texto exacto observado.

## Seguimiento

Si se repite, crear una nueva entrada enlazada a esta y conservar el stderr del
intento antes de recuperar.
