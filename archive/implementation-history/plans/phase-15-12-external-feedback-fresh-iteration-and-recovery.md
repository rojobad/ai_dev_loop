# Phase 15.12 — Corrección externa en iteración nueva y recovery de staging vacío

## Goal

Corregir la transición desde observaciones accionables de Codex-bot hacia Cursor y desbloquear con un sucesor inmutable el run de CryptoSentinel `crypto-sentinel-20260718T115934Z-176634` (PR #45).

El incidente no fue que Cursor no hiciera cambios. El run ya tenía una iteración local `01` completa y publicada en `c29e15e`. Tras la adjudicación del ciclo externo 2, con tres observaciones nuevas y accionables, el orquestador puso `workflow.current_review_iteration = 0`. El planificador vio `cursor/iterations/01` completo, saltó a staging y falló con `no staged changes after git add -A`; Cursor nunca recibió `prompts/fixes/github-02.txt`.

Esta fase debe:

1. Programar cada corrección externa en una iteración nueva, monotónica y sin colisión con artefactos anteriores.
2. Enviar a Cursor el prompt externo exacto persistido del ciclo actual.
3. Añadir un `pr-review recover` estrecho, tipado e inmutable para el caso terminal en que el run falló en `fixing_external_feedback` antes de que esa nueva iteración de Cursor empezara.
4. Retomar el sucesor de este incidente sin repostear `@codex review`, sin re-adjudicar los mismos threads y sin mutar el run fuente.

## Non-Goals

- No cambiar qué observaciones son accionables ni relajar validaciones de PR, SHA, autor, fecha, threads, locks o publicación.
- No recuperar estados por texto de `result`, `last_error`, logs o respuestas de agentes.
- No editar, resetear, abortar, borrar ni mutar manualmente el run fallido o sus ancestros.
- No cambiar A/B, modelos, SSH, hooks, `gh`, el comando `continue` ni máximos configurados de ciclos externos.

## Scope

- `src/ai_dev_loop/commands/pr_review.py`: planificación de feedback externo y reanudación controlada.
- `src/ai_dev_loop/resume_planner.py`, `src/ai_dev_loop/iterations.py` y, cuando sea necesario, `src/ai_dev_loop/workflow_engine.py`: selección de iteración y prompt externo.
- `src/ai_dev_loop/commands/pr_review_recover.py` y `src/ai_dev_loop/state.py`: clasificación, lineage y sucesor de recovery.
- Pruebas, documentación operativa y trazabilidad de fase.

## Out of Scope

- Ejecutar operaciones reales `continue`, `resume`, `recover`, `start`, `launch`, Cursor, Codex, GitHub, SSH, Docker, commit o push durante implementación/validación.
- Cambiar `ai_dev_loop.yaml`, configurar CryptoSentinel o modificar su código.
- Reescribir recoveries de Phase 15.7–15.11.
- Responder, resolver o cerrar threads durante `recover`; sólo la publicación normal puede hacerlo tras una corrección aceptada.

## Required Context

- Fuente: `crypto-sentinel-20260718T115934Z-176634`, PR #45, `status=failed`, `lifecycle=fixing_external_feedback`, ciclo 2 y worktree limpio. Conserva `last_external_result_path=github/cycles/02/result.json` y `external_fix_prompt_path=prompts/fixes/github-02.txt`.
- El resultado externo del ciclo 2 marca tres threads actuales como accionables. Los dos threads ya processed/resolved pertenecen al ciclo histórico anterior y no deben mezclarse con el conjunto actual.
- `iterations[01]` es un artefacto local heredado, completo. No prueba que Cursor haya recibido o corregido el prompt `github-02`.
- La ruta actual fija el contador a cero, llama a `resume_run` y `active_cursor_iteration` cae a `max(iterations)`. Si `01` terminó, el planner selecciona staging.
- `pr-review recover` actual trata el run como recovery reviewing por tener progreso local y lo rechaza por `review_result_already_present`; no existe hoy una ruta segura de recuperación para este fallo.

## Cursor Rules And Skills

Lee y aplica todas las reglas de `.cursor/rules/`, en especial:

- `ai-dev-loop-governance.mdc`;
- `ai-dev-loop-orchestrator-contracts.mdc`;
- `ai-dev-loop-state-and-schema-contracts.mdc`;
- `ai-dev-loop-loop-and-resume-contracts.mdc`;
- `ai-dev-loop-codex-review-contracts.mdc`;
- `ai-dev-loop-abort-contracts.mdc`; y
- `ai-dev-loop-docs-acceptance-contracts.mdc`.

Para documentación usa `ai-dev-loop-docs-acceptance-governance`. No uses la skill staged-review durante implementación. Emplea sólo fakes de `agent`, `codex` y `gh`; nunca servicios o credenciales reales.

## Architecture Guardrails

- Los artefactos Cursor/Codex son estado durable. Una corrección externa nunca puede reutilizar ni sobrescribir una iteración completa. Su número es mayor que cualquier iteración persistida y queda guardado antes de invocar Cursor.
- Con lifecycle `fixing_external_feedback`, el prompt procede únicamente de `external_fix_prompt_path` validado y se entrega byte por byte. No puede caer al prompt inicial ni a un fix prompt local por presencia de artefactos.
- Preservar la separación de ciclo externo y revisión local. Si es necesario modelar el intento externo para evitar colisiones o cambiar la cuenta local, usar campos/checkpoints tipados, validados, serializables y compatibles hacia atrás; nunca offsets, timestamps o heurísticas de archivos.
- El nuevo recovery es elegible sólo con evidencia estructurada: fuente terminal failed, lifecycle externo correcto, PR/head/branch/repo enlazados, resultado y prompt externos válidos del ciclo/threads actuales, todos los findings accionables, baseline limpio, cero publicación y cero efectos parciales sobre el conjunto actual, y ningún artefacto de la nueva ejecución de Cursor. `last_error` no es prueba de elegibilidad.
- El sucesor usa un checkpoint/reason code específico para “external feedback Cursor not started”, con validadores explícitos. Copiar sólo identidad, chat, resultado/snapshot/prompt externos referenciados y artefactos locales necesarios. El source es byte-a-byte inmutable.
- Congelar el conjunto actual de threads en el sucesor y revalidar PR, SHA, threads, chat, sesión B, prompt y baseline antes de Cursor. Ante mismatch, detenerse sin GitHub writes ni Cursor.
- Reutilizar la adjudicación externa ya validada: no volver a ejecutar Codex B para esos mismos threads durante recovery. Después de Cursor se mantienen las rutas normales de revisión local, publicación y siguiente trigger.
- Mantener argv estructurados, `shell=False`, locks, escrituras atómicas, permisos sensibles y redacción. Eventos/CLI/manifest sólo exponen enums, conteos, números y SHA abreviado permitido; nunca IDs de threads, prompts, cuerpos, sesiones, tokens, PID/argv ni rutas sensibles.
- `state.py` y recovery son control-plane work: la aceptación manual de la revisión staged es obligatoria.

## Implementation Plan

1. Definir el contrato de corrección externa: cuando una adjudicación sea completamente accionable, elegir y persistir una iteración fresca basada en el máximo durable. Reemplazar el reset ambiguo a cero por una transición que fuerce Cursor antes de staging aun si `01` existe y está completa.
2. Ajustar planificador, `active_cursor_iteration` y resolución de prompt para que la iteración fresca use el prompt externo exacto. Los artefactos nuevos quedan bajo el nuevo número y los históricos no se alteran. Conservar Cursor → staging → revisión local → publicación.
3. Revisar el presupuesto de revisiones locales para que la numeración durable no altere sus límites ni los máximos de ciclos externos. Si hace falta estado adicional para separar ambas cuentas, añadirlo tipado, validado y compatible con estados históricos.
4. Añadir al análisis y modelo de `pr-review recover` un checkpoint dedicado `external_feedback_cursor` (u otro nombre estable equivalente). Debe aceptar sólo el caso previo a Cursor, verificar remoto/threads/baseline y rechazar workers vivos, prompt/resultado inválido, findings no accionables, publicación/side effects actuales, intento Cursor parcial o lineage ambiguo.
5. Crear el sucesor idempotente: preservar proyecto, repo, rama, PR, SHA, chat Cursor y sesión Codex; copiar contexto externo allowlisted; congelar threads actuales; dejar estado `interrupted` reanudable. Una repetición reutiliza sólo el mismo sucesor íntegramente compatible; fuente y ancestros no cambian.
6. Extender `pr-review resume` para el checkpoint nuevo: revalidar PR/head y freeze de threads, y después abrir Cursor con la nueva iteración y prompt adoptado. No exigir otro `continue`; `recover` + `resume` son la autorización explícita de reintentar la corrección local ya adjudicada.
7. Actualizar README, `docs/referencia/cli.md`, `docs/guia/flujo-completo.md`, `docs/operacion/estado-artefactos.md`, `docs/operacion/troubleshooting.md` y `docs/referencia/trazabilidad-fases.md`: explicar staging vacío antes de Cursor, el recovery inmutable, precondiciones, ausencia de repost y los comandos posteriores a aceptación manual.

## Testing Criteria

Se requieren tests unitarios, integración y regresión con fakes; nunca servicios reales.

- Planificador/prompt: con `01` completa y feedback externo accionable, la siguiente acción debe ser Cursor en una iteración superior; el texto debe ser exactamente `github-02`, no el inicial ni un fix local. Probar prompt vacío, lifecycle erróneo y metadata parcial como fallos seguros.
- Flujo end-to-end: seed de iteración `01` aceptada, ciclo 2 con tres findings accionables y worker real con fakes. Comprobar Cursor en una iteración nueva, patch staged, revisión local y handoff a publicación. No re-adjudicar ni repostear el marker al recuperar el mismo resultado.
- Recovery: source equivalente al incidente crea un único sucesor, deja source byte-a-byte intacto, conserva identidad/chat/sesión/PR/SHA/prompt/resultado, y `resume` abre Cursor antes de staging. Rechazar baseline sucio, resultado/prompt corrupto, findings no accionables, SHA/PR/branch/threads distintos, worker vivo, artefacto Cursor parcial, publicación o efectos parciales, y lineage ambiguo.
- Reintentos: reutilizar sólo sucesor compatible; no duplicar workers, comentarios, commits, pushes, replies ni resolves.
- Ejecutar regresiones Phase 15.7–15.11 y compatibilidad de estados históricos; verificar redacción de CLI/eventos/manifest.

## Validation

```bash
TMPDIR=/tmp TEMP=/tmp TMP=/tmp uv run pytest -s \
  tests/unit/test_pr_review_worker_liveness.py \
  tests/integration/test_phase15_pr_review.py \
  tests/integration/test_phase15_5_independent_pr_review.py \
  tests/integration/test_phase15_7_adjudication_recovery.py \
  tests/integration/test_phase15_8_reviewing_recovery.py \
  tests/integration/test_phase15_9_worker_continuity.py \
  tests/integration/test_phase15_10_legacy_cycle_freeze.py \
  tests/integration/test_phase15_11_nested_legacy_cycle_freeze.py
TMPDIR=/tmp TEMP=/tmp TMP=/tmp uv run pytest
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src
uv run mkdocs build --strict
git diff --check
```

No ejecutar operaciones reales sobre PR #45 durante implementación o validación.

## Risks Or Recovery Notes

- El plan no autoriza retomar la PR hasta revisión staged, aceptación, instalación local y publicación de esta versión.
- Tras aceptación, el flujo debe ser crear un sucesor desde el run fallido y ejecutar el comando `resume` que entregue `pr-review recover`. No se publica un `@codex review` adicional; el próximo trigger sólo viene de una publicación normal de corrección aceptada.
- Si PR, SHA o threads cambian, detenerse y preparar otra fase: no forzar estado ni adoptar comentarios nuevos.

## OpenQuestions

None.
