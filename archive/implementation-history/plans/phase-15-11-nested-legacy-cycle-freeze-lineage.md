# Phase 15.11 — Lineage anidado para freeze histórico entre ciclos

## Goal

Corregir el falso negativo de Phase 15.10 y recuperar de forma segura el run
`crypto-sentinel-20260718T115934Z-176634` de CryptoSentinel PR #45.

Phase 15.10 está instalada, pero exige que la `recovery` directa del run actual
sea `external_adjudication`. El run real tiene un único salto adicional:

```text
run actual (recovery=reviewing, source=crypto-sentinel-...-ff42cb)
  -> source terminal (recovery=external_adjudication, ciclo externo 1)
  -> freeze heredado de dos hilos del ciclo 1
```

El actual ya está en ciclo 2 con tres hilos nuevos legítimos; sus dos IDs
esperados son iguales a los del ancestro y ya están procesados/resueltos. La
fase debe aceptar sólo esa cadena durable de un salto. Con un comentario nuevo
exacto `@rojobad /ai-dev-loop continue`, `pr-review continue` limpiará el
freeze del mismo run y el worker normal procesará el ciclo 2 sin republicar
`@codex review`.

## Non-Goals

- No recorrer lineages arbitrarias, no seguir más de un ancestro ni introducir
  adopción genérica basada en `last_error`, `result`, logs o cuerpos de hilos.
- No modificar el source terminal, crear sucesor, abortar, editar estado a mano
  ni cambiar YAML, schemas, A/B, modelos, SSH, hooks o runs nuevos.
- No publicar GitHub, responder/resolver hilos, hacer commit/push, crear Cursor
  ni invocar Codex durante el comando de migración.
- No relajar drift legítimo ni modificar recoveries fuera del patrón histórico
  `reviewing` con artefacto Codex faltante y ancestro externo comprobado.

## Scope

- El clasificador de Phase 15.10 y su carga segura de un ancestro mediante las
  APIs de discovery/estado existentes.
- La ruta histórica de `continue_pr_review_cycle`, pruebas y documentación.

## Out of Scope

- `pr-review recover|resume`, publicación, liveness, no-findings, reacciones
  `eyes`, GitHub schemas y el resto del workflow.
- Llamadas reales a GitHub, Cursor, Codex o SSH durante implementación/tests.
- Comandos o flags nuevos que omitan el comentario GitHub exacto.

## Required Context

- Run actual: `crypto-sentinel-20260718T115934Z-176634`, PR #45, ciclo 2,
  `waiting_for_user_attention`, `eligible_thread_set_drift`, worker stale. Sus
  dos IDs esperados ya están processed/resolved; los tres nuevos no tienen side
  effects.
- Recovery actual: `reviewing`,
  `reason_code=codex_review_result_artifact_missing`, source
  `crypto-sentinel-20260718T024655Z-ff42cb`; no lleva expected IDs.
- Source terminal: estado `failed`, recovery `external_adjudication`,
  `reason_code=github_adjudication_schema_incompatible`, ciclo 1, y el mismo
  snapshot esperado tanto en recovery como en `github_pr_review`.
- Phase 15.9 evita el problema para publicaciones nuevas; Phase 15.10 cubre
  sólo lineage directo. El `continue_comment_id` actual ya consumió el último
  comentario, por lo que se requerirá uno nuevo al terminar esta fase.

## Cursor Rules And Skills

Lee y aplica todas las reglas de `.cursor/rules/`, especialmente:

- `ai-dev-loop-governance.mdc`;
- `ai-dev-loop-orchestrator-contracts.mdc`;
- `ai-dev-loop-state-and-schema-contracts.mdc`;
- `ai-dev-loop-loop-and-resume-contracts.mdc`;
- `ai-dev-loop-codex-review-contracts.mdc`;
- `ai-dev-loop-abort-contracts.mdc`; y
- `ai-dev-loop-docs-acceptance-contracts.mdc`.

Para documentación usa `ai-dev-loop-docs-acceptance-governance`. No uses la
skill staged-review durante implementación. Usa fakes de `gh`, Cursor y Codex;
no ejecutes actividad real de modelos ni GitHub.

## Architecture Guardrails

- El estado y lineage son contratos durables. Decide exclusivamente con campos
  tipados y estado persistido; no con texto, IDs inferidos, fechas heurísticas
  ni contenido de comentarios.
- Conservar el caso directo de Phase 15.10. La ruta anidada sólo puede cargar
  exactamente un `RecoveryState.source_run_id` cuando el actual es
  `recovery=reviewing` y
  `reason_code=codex_review_result_artifact_missing`. Usa `load_run`; no
  construyas rutas ni uses recursión.
- El source debe ser terminal `failed` y coincidir con el `source_status` de la
  recovery actual. Debe tener la misma identidad proyecto/repositorio/PR/branch
  y una recovery directa `external_adjudication` con
  `reason_code=github_adjudication_schema_incompatible`. Su ciclo externo debe
  ser estrictamente menor al actual. Cualquier fallo es no-match seguro: no
  mutar ni programar worker.
- Requerir igualdad de los sets no vacíos de IDs esperados en: run actual,
  `github_pr_review` del ancestro y recovery externa del ancestro. Todos los
  IDs del actual deben estar en processed y resolved. No leer ni filtrar hilos
  actuales durante la migración.
- El comentario exacto, nuevo y autorizado sigue siendo la única autorización.
  Bajo locks, recargar y repetir clasificación antes de mutar. Si hay worker
  live o launcher ambiguo, no consumir comentario ni cambiar el run.
- Un match sólo cambia el actual: limpia expected/outcome/error, restaura
  awaiting, registra continue ID y preserva PR, SHA, marker, ciclo, lineage,
  A/B, Cursor y IDs processed/resolved. El source es read-only.
- Conservar eventos/salida redactados: ciclos, conteos y enums fijos solamente.
  No exponer IDs, rutas sensibles, prompts, cuerpos, token, PID/argv o sesiones.
- Tras persistir, usar el launcher existente para a lo sumo un worker. El
  comando no escribe GitHub ni llama agentes; el worker normal conserva las
  validaciones de PR/SHA/autor/fecha y usa Codex B exacto.
- No hay cambio de schema ni de modelo: la compatibilidad histórica se logra
  por lectura explícita del ancestro validado.

## Implementation Plan

1. Refactorizar el clasificador de Phase 15.10 para separar precondiciones
   locales, evidencia externa directa y evidencia de ancestro anidado. El
   resultado seguro contiene sólo ciclos/conteo, no IDs.
2. Añadir resolvedor de evidencia: aceptar primero el checkpoint externo
   directo; únicamente si la recovery actual es el checkpoint reviewing y razón
   indicados, cargar una vez `source_run_id` con `load_run`, validar su
   terminalidad/identidad/recovery externa y no seguir su source.
3. Validar sets, processed/resolved e invariantes de ciclo antes de devolver
   match. Source ausente, self-reference, error de carga, mismatch de estado,
   identidad, checkpoint/razón, ciclo o snapshot debe conservar el run sin
   mutación, spawn ni datos sensibles en el error.
4. Conectar el resolvedor a `continue_pr_review_cycle` bajo los locks existentes
   y reutilizar la transición/spawn de Phase 15.10. El comentario se consume
   sólo tras rama histórica válida o el flujo normal existente.
5. Actualizar README, CLI, flujo completo, estado/artefactos, troubleshooting y
   trazabilidad: lineage directo o un descendiente reviewing verificado; nuevo
   comentario requerido tras un intento previo; mismo run y sin repost.

## Testing Criteria

Se requieren tests unitarios e integración con fakes.

- Extender `tests/unit/test_legacy_external_cycle_freeze.py` para preservar el
  match directo y aceptar la cadena real de un salto. Rechazar source ausente,
  self-reference, no terminal/status distinto, identidad diferente,
  checkpoint/razón inválidos, ciclo no avanzado, sets diferentes o IDs no
  procesados/resueltos. Probar independencia de `result`/`last_error`.
- Extender `tests/integration/test_phase15_10_legacy_cycle_freeze.py` o crear
  test Phase 15.11: seed source terminal + current reviewing successor; continue
  autorizado limpia sólo current, preserva source byte-for-byte y programa un
  worker. El worker fake envía los tres IDs actuales al Codex B guardado.
- Verificar que `continue` no hace post/reply/resolve/commit/push ni crea chat/
  invoca Codex; que sin comentario nuevo, worker live/launcher unsafe o cadena
  inválida no hay side effects; y que drift real/continue normal conserva freeze.
- Validar que eventos y salidas no exponen IDs, cuerpos, prompts, rutas fuente,
  tokens, PID/argv ni sesiones.
- No modificar schemas. Añadir carga de estados históricos si se toca estado.

## Validation

```bash
TMPDIR=/tmp TEMP=/tmp TMP=/tmp uv run pytest -s \
  tests/unit/test_legacy_external_cycle_freeze.py \
  tests/unit/test_pr_review_worker_liveness.py \
  tests/integration/test_phase15_pr_review.py \
  tests/integration/test_phase15_7_adjudication_recovery.py \
  tests/integration/test_phase15_8_reviewing_recovery.py \
  tests/integration/test_phase15_9_worker_continuity.py \
  tests/integration/test_phase15_10_legacy_cycle_freeze.py
TMPDIR=/tmp TEMP=/tmp TMP=/tmp uv run pytest
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src
uv run mkdocs build --strict
git diff --check
```

No ejecutar `pr-review continue`, `resume`, `recover`, `start`, `launch`,
Cursor, Codex, GitHub ni SSH reales durante implementación/validación.

## Risks Or Recovery Notes

- No modificar/abortar/borrar el run actual ni su source terminal. La corrección
  sólo muta el actual tras una autorización nueva.
- Tras aceptación staged e instalación local, publicar en PR #45:

  ```text
  @rojobad /ai-dev-loop continue
  ```

  Desde A:

  ```bash
  cd ~/Projects/crypto-sentinel
  SSH_AUTH_SOCK="$HOME/.ssh/ai-dev-loop-ssh-agent.sock" \
    ai_dev_loop pr-review continue crypto-sentinel-20260718T115934Z-176634
  ```

  Se retoma la ventana de ciclo 2 ya publicada: no crea PR ni republica
  `@codex review`.
- Si el estado cambia o una precondición falla, detenerse con diagnóstico seguro
  y no forzar otra continuación.

## OpenQuestions

None.
