# INC-2026-10-29-001 — Cursor SDK rechaza `grok-4.7` ("AI Model Not Found") a mitad de turno y el sucesor falla en `resume`

- Fecha: 2026-10-29 (03:36–03:51 La Paz / 07:36–07:51 UTC)
- Detectado por: Brusy (supervisión)
- Secuencia: `ai-dev-loop-seq-34a21c365f4f` (phase-30-sqlite-statistics-and-historical-backfill), fase 3/3 `phase-30-3-database-only-restore-acceptance`
- Run origen: `ai-dev-loop-934e259b28b7`: estado `blocked`, block reason `cursor_failure`, 2/7 revisiones
- Run sucesor: `ai-dev-loop-20261029T074954Z-f8bde5` (creado con `implementer-retry --force`): estado `blocked`, block reason `cursor_sdk_preflight_failed`
- Repo/rama: `~/project/ai_dev_loop`, `codex/phase-30-sqlite-statistics`, HEAD `a9c519b` (checkpoint de fase 2); índice con 17 archivos staged de iter01/02 de fase 3 (SHA-256 de `git diff --cached` = `da81a227…a16e0`, igual al parche registrado tras iter02)
- Implementer: Cursor SDK `sdk-local` 1.0.37, `grok-4.7` (context 256k, fast=false, reasoning high); reviewer `gpt-6.1-sol` xhigh
- Estado: Abierto (decisión de Rodrigo). Fases 1 y 2 aceptadas y checkpointeadas (`ec51b7e899ef`, `a9c519b09377`).

## Síntoma
En la iteración 03 (turno de corrección tras la review 2), Cursor trabajó ~4 min y terminó con `AI Model Not Found Unknown model ID: grok-4.7` (`RUN_LIFECYCLE_STATUS_ERROR`). El mismo modelo y parámetros habían funcionado en iter01 e iter02 de esta fase y en las fases 1 y 2. El sucesor publicado con `implementer-retry --force` falló a los 31 s con `sdk_failure {code: invalid_request, operation: resume}`, sin llegar a enviar el turno.

## Cronología (UTC; La Paz = UTC-4)
| Hora UTC | La Paz | Etapa | Evidencia |
|---|---|---|---|
| 06:01:53 | 02:01:53 | Materialización de fase 3 (run origen) | `scheduler status` |
| 06:02:55–06:03:26 | 02:02:55–02:03:26 | Creación del agente SDK | timeline, `cursor/sdk-identity.json` |
| 06:03:57–06:51:14 | 02:03:57–02:51:14 | Cursor iter01 correcto | timeline |
| 06:52:16–07:01:32 | 02:52:16–03:01:32 | Codex review 1: pide correcciones | timeline |
| 07:02:02–07:29:49 | 03:02:02–03:29:49 | Cursor iter02 correcto con `grok-4.7` | `cursor/iterations/02/…/sdk-metadata.json` (`status=finished`) |
| 07:30:51–07:36:00 | 03:30:51–03:36:00 | Codex review 2: pide correcciones (F-04, F-05) | timeline |
| 07:36:31 | 03:36:31 | Lanza Cursor iter03; preflight, resume y send correctos | `cursor/sdk-attempts/att-3bdad2bf…/dispatch-journal.jsonl` |
| ~07:40:31 | ~03:40:31 | Error nativo "AI Model Not Found Unknown model ID: grok-4.7" (createdAt 07:36:38.598Z + durationMs 232426) | `cursor/iterations/03/att-3bdad2bf…/sdk-envelopes.jsonl:1178-1180` |
| 07:40:35 | 03:40:35 | Worker termina con exit 1 | `attempts/att-3bdad2bf…/result.json` |
| 07:41:06–07:41:37 | 03:41:06–03:41:37 | Reconciliación `continuation_ready`; origen pasa a `blocked` (`cursor_failure`) | `cursor/sdk-reconciliation/…/record-v1.json` |
| 07:49:54 | 03:49:54 | Operador ejecuta `implementer-retry --force`: sucesor con mismo agente y misma config congelada | `cursor/sdk-recovery/record-v1.json` (run sucesor) |
| 07:50:24–07:50:55 | 03:50:24–03:50:55 | Sucesor falla en `resume` (`invalid_request`) → `cursor_sdk_preflight_failed`; secuencia `blocked` | `attempts/att-5d406ef8…/stdout.txt` |

Artefactos:
- Origen: `~/.local/state/ai_dev_loop/artifacts/runs/5ce6dcf4ee8e17212490f376e1d375c52e987dddef100727c6fd1b9171d2545f/`
- Sucesor: `~/.local/state/ai_dev_loop/artifacts/runs/5a6ea72340e9a76e952ff55d51795e6f6f2c69961080e88c3163ca50eea2c0f0/`

## RCA (Codex, solo lectura, 2026-10-29 ~08:05–08:17 La Paz)
Hechos verificados:
- Cursor rechazó `grok-4.7` durante la ejecución del turno iter03, después de que el preflight del adaptador (`me` + `models.list` + coincidencia exacta de ID y variante, `src/ai_dev_loop/integrations/cursor_sdk/adapter.py:313`) hubiera pasado.
- La recuperación (`implementer-retry --force`) reutiliza por diseño el mismo agente, la conversación y la selección de modelo congelada; no hay comando soportado para cambiar modelo, parámetros o proveedor de un run existente.
- El adaptador vuelve a pasar el modelo en `AgentOptions` al reanudar (`adapter.py:372`) y convierte tanto `BadRequestError` como `ConfigurationError` del SDK en `invalid_request`, por lo que el mensaje original del fallo del sucesor no quedó registrado.

Inferencias (no demostradas):
- Causa raíz más probable: inconsistencia del lado de Cursor entre el catálogo de modelos y la ruta de ejecución (despliegue parcial o enrutamiento), no un retiro global del modelo: la documentación pública sigue listando Grok 4.7 y Cursor ya reconoció el 22 sep un error idéntico ("AI Model Not Found" para Grok 4.7 en Cloud Agents) corregido con un despliegue ([foro de Cursor](https://forum.cursor.com/t/grok-4-7-not-found-in-cloud-agent/172633)). No hay evidencia de cambio de cuenta, plan o cuota.
- El `invalid_request` del sucesor es probablemente el SDK al re-resolver la misma selección `grok-4.7` en `resumeAgent`. No está demostrado que el agente quede "atado" al modelo; el SDK admite otra selección al reanudar.

Causa pendiente: la causa interna en Cursor requiere trazas del proveedor de la ventana 07:36–07:51 UTC.

Factores contribuyentes:
- El clasificador (`src/ai_dev_loop/runners/cursor_failure.py`) no tiene categoría de "modelo desconocido"; cae en `cursor_failure` genérico.
- `continuation_ready` solo indica que es seguro continuar la conversación, no que el modelo vuelva a estar disponible; el recovery reintentó con la misma selección que fallaba y se consumió.
- Pérdida de detalle diagnóstico en el fallo de `resume`.

Por qué no se detectó: el preflight existe y pasó; valida el catálogo en un instante, no garantiza la resolución en `resume`/`send` ni la ejecución durante todo el turno. No hay pruebas con catálogos divergentes ni rechazo de modelo posterior al preflight.

Riesgo de recurrencia: abierto para cualquier run Cursor con `grok-4.7` mientras persista la discrepancia en Cursor; sin datos para estimar tasa. Segundo caso conocido del mismo mensaje con Grok 4.7 (el primero en otro runtime de Cursor).

## Recuperación
- `ai_dev_loop scheduler cursor-retry ai-dev-loop-934e259b28b7 --check` → `ineligible_reservation_conflict` (el origen ya no posee la reserva; está `released`), `recovery_supported=no`.
- `ai_dev_loop scheduler cursor-retry ai-dev-loop-20261029T074954Z-f8bde5 --check` → `ineligible_block_reason` (no es `cursor_failure`), `recovery_supported=no`.
- Relanzar con `implementer-retry` ya no es posible (`changed=false`).
- Opciones (Codex; ninguna ejecutada):
  1. Run nuevo independiente de fase 3 sobre HEAD `a9c519b` y el staged actual, con implementer Codex (`gpt-6.1-sol`) y reviewer separado, config externa con `require_clean_worktree=false` (con `true` el staged actual se rechaza como `dirty_worktree`). Recomendado por Codex.
  2. Run nuevo Cursor (agente nuevo) con un modelo/variante confirmados antes en el catálogo autenticado (candidatos documentados: `composer-2.5`, `auto-smart`, Grok 4.6, o `grok-4.7` si Cursor lo corrige).
  3. Secuencia nueva de una sola fase con el plan de fase 3 (manifest v4 Cursor / v3 Codex).
  4. Continuación manual con revisión independiente (menos trazabilidad).
- En todos los casos la secuencia original queda bloqueada en el histórico; las fases 1 y 2 siguen aceptadas y el nuevo run debe vincularse operacionalmente.
- Ningún modelo alternativo quedó certificado para esta cuenta: no se consultó el catálogo autenticado.

## Correcciones propuestas (no aplicadas)
| Corrección | Esfuerzo |
|---|---|
| Conservar clase, código nativo y mensaje sanitizado de errores del SDK | S |
| Distinguir modelo ausente, variante inválida y fallo de `resume` en la proyección operativa | M |
| Clasificar terminales "Unknown model ID" con categoría propia | M |
| Guardar evidencia mínima del catálogo validado (instante, ID, variante, huella) | M |
| Que `cursor-retry --check` avise que el recovery continúa con el mismo modelo | S |
| Recovery explícito con cambio de modelo (nueva identidad y config congelada, preservando parche y linaje) | L |
| Pruebas con catálogos divergentes y rechazo de modelo tras preflight satisfactorio | M |

(S ≤ 2 días; M 3–5 días; L ≥ 1 semana)

## Acciones de supervisión
Solo lectura: `scheduler status/timeline`, `sequence status`, `cursor-retry --check` y artefactos (sin credenciales). Codex ejecutó el RCA con `codex exec` en sandbox `workspace-write` limitado a un directorio temporal en `/tmp`, eliminado al terminar. `git status` y el hash de `git diff`/`git diff --cached` verificados idénticos antes y después del RCA. Sin retry adicional, abort, cambios de git, auth ni servicios.

Advertencia: este archivo y la entrada en `index.md` quedan sin stage en el mismo checkout que contiene el trabajo staged de fase 3. Cualquier turno de corrección o run nuevo sobre este checkout ejecuta `git add -A` e incluiría estos dos cambios en el snapshot de fase 3; conviene apartarlos antes de reanudar.

## Relacionados
- [INC-2026-10-19-001](2026-10-19-cursor-sdk-busy-first-send-ambiguous-missing.md): mismo implementer Cursor SDK 1.0.37 / `grok-4.7`, otra causa.
- [INC-2026-10-12-001](2026-10-12-cursor-out-of-usage-unclassified.md) y [INC-2026-10-12-004](2026-10-12-cursor-resource-exhausted-unclassified.md): errores de Cursor sin clasificación específica que terminan en `cursor_failure`.
