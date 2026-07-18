# Phase 15.10 — Recuperación del freeze histórico entre ciclos externos

## Goal

Permitir retomar de forma segura el run existente
`crypto-sentinel-20260718T115934Z-176634` (CryptoSentinel PR #45), que quedó
en `waiting_for_user_attention` durante el ciclo externo 2. El marcador del
ciclo 2 y los tres hilos nuevos del bot son válidos, pero el estado conserva
`github_pr_review.expected_eligible_thread_ids` del recovery de adjudicación
del ciclo 1. Ese snapshot fue persistido antes de que Phase 15.9 incorporara
su reset al publicar un trigger nuevo, por lo que el worker detecta
incorrectamente `eligible_thread_set_drift` (esperados 2, observados 3).

La fase debe añadir una recuperación histórica, limitada y autorizada por el
comentario GitHub ya usado para continuar:

1. Detectar únicamente el patrón verificable de freeze heredado de una
   recovery `external_adjudication` de un ciclo anterior.
2. Cuando el usuario publique un nuevo comentario exacto
   `@rojobad /ai-dev-loop continue` y A ejecute `pr-review continue`, limpiar
   sólo ese freeze obsoleto y reanudar el mismo worker/run.
3. Dejar que el polling normal vuelva a leer los hilos del ciclo actual y
   entregue los tres hallazgos al Codex B exacto, sin publicar otro trigger.
4. Mantener la protección de drift para snapshots válidos del ciclo actual.

## Non-Goals

- No crear un sucesor, no abortar el run, no editar `state.json` a mano ni
  reinterpretar genéricamente estados históricos incompletos.
- No publicar `@codex review`, crear PR, hacer commit/push, responder hilos,
  resolver hilos, ejecutar Cursor ni ejecutar Codex durante el comando
  `pr-review continue` que realiza la migración.
- No relajar la detección de drift normal ni aceptar automáticamente un
  conjunto de hilos distinto dentro del mismo ciclo.
- No cambiar la configuración GitHub, modelos, SSH, el protocolo A/B, hooks o
  el comportamiento de Phase 15.9 para runs nuevos.

## Scope

- `continue_pr_review_cycle` y helpers privados de
  `src/ai_dev_loop/commands/pr_review.py`.
- Las validaciones de lineage y de estado necesarias para identificar el único
  caso recuperable.
- Salida segura, evento estructurado y documentación de la acción histórica.
- Tests unitarios e integración/regresión para el run equivalente al incidente.

## Out of Scope

- `pr-review recover`, recovery de staging/reviewing, launcher/liveness,
  publicación de fixes, schemas de adjudicación, no-findings y reacciones
  `eyes`.
- Cualquier acceso de escritura a GitHub desde el comando de migración. El
  único acceso remoto permitido antes de mutar estado es la lectura que ya hace
  `continue` para validar el comentario de autorización.
- Cambios a `ai_dev_loop.yaml`, código o estado de CryptoSentinel, y cualquier
  operación real sobre PR #45 durante implementación y tests.
- Introducir una nueva CLI de adopción amplia (`adopt`, `repair`, etc.); la
  autorización existente por comentario exacto debe seguir siendo la puerta.

## Required Context

- Run: `crypto-sentinel-20260718T115934Z-176634`; PR #45; ciclo actual 2;
  estado/lifecycle `waiting_for_user_attention`; resultado
  `eligible_thread_set_drift`.
- El run es un sucesor con `recovery.recovered_checkpoint ==
  "external_adjudication"` y `recovery.source_iteration == 1`; el
  `github_pr_review.cycle_number == 2`. Los dos IDs esperados pertenecen al
  ciclo recuperado/anterior y ya están procesados/resueltos. Los tres hilos
  observados son elegibles para el marker y SHA del ciclo 2.
- Phase 15.9 ya limpia `expected_eligible_thread_ids` al publicar nuevos
  ciclos. Ese código no pudo afectar esta publicación, ocurrida antes de su
  instalación. No es un fallo del bot, polling, SSH o A/B.
- `pr-review continue` exige el comentario exacto configurado, publicado por
  el usuario configurado y posterior al último comentario de continuación o
  al marker. Un nuevo comentario es la autorización explícita para esta
  migración local; no es un nuevo trigger para Codex bot.
- El controlador A/B, la sesión Codex B, el Cursor chat, PR, marker, SHA y
  artefactos actuales siguen siendo contratos inmutables del run.
- Consultar como contexto histórico Phase 15.7, 15.8 y 15.9, pero usar código,
  tests, schemas y documentación actuales como fuente de verdad.

## Cursor Rules And Skills

Lee y aplica todas las reglas de `.cursor/rules/`, particularmente:

- `ai-dev-loop-governance.mdc`;
- `ai-dev-loop-orchestrator-contracts.mdc`;
- `ai-dev-loop-state-and-schema-contracts.mdc`;
- `ai-dev-loop-loop-and-resume-contracts.mdc`;
- `ai-dev-loop-codex-review-contracts.mdc`;
- `ai-dev-loop-abort-contracts.mdc`; y
- `ai-dev-loop-docs-acceptance-contracts.mdc`.

Para la documentación, usa la skill
`ai-dev-loop-docs-acceptance-governance`. No uses la skill de staged review
durante la implementación. Usa los fakes existentes de `agent`, `codex` y
`gh`; no realices llamadas a modelos, GitHub ni SSH.

## Architecture Guardrails

- El estado persistido y la lineage son contratos públicos: no añadir campos ni
  reinterpretar el contenido de `last_error` para decidir recuperabilidad. Esta
  fase puede resolver el caso sin una migración de schema porque la lineage
  existente contiene el checkpoint y el ciclo fuente necesarios.
- La limpieza está permitida sólo si se cumplen simultáneamente todas estas
  precondiciones durables: estado y lifecycle `waiting_for_user_attention`,
  `worker_outcome == "eligible_thread_set_drift"`, recovery
  `external_adjudication`, `recovery.source_iteration < cycle_number`, ambos
  snapshots esperados son no vacíos e iguales como conjuntos, y todos los IDs
  del snapshot heredado pertenecen a los IDs ya procesados y resueltos. Si una
  falta o es ambigua, conservar el run detenido sin spawn ni mutación.
- La migración debe ocurrir sólo después de que `_find_continue_comment`
  verifique un comentario nuevo, exacto y autorizado. Si no hay comentario
  nuevo, no cambiar estado. No convertir el controller A ni una invocación
  local en autorización implícita.
- Bajo los locks de run y repositorio, releer estado y repetir las
  precondiciones antes de escribir. El único cambio de estado debe limpiar
  `expected_eligible_thread_ids`, limpiar el outcome/error de drift, restaurar
  `awaiting_bot_review` y registrar el nuevo `continue_comment_id`. Preservar
  `processed_thread_ids`, `resolved_thread_ids`, marker, request comment,
  SHA, ciclo, lineage, Cursor chat y sesión Codex.
- Emitir un evento redactado de migración que incluya sólo conteos y números de
  ciclo; nunca IDs de hilo, cuerpos, prompts, token, PID ni IDs completos de
  sesión. La salida humana debe explicar que el worker fue reanudado para la
  ventana ya publicada, sin decir que se reprocesó el bot ni imprimir datos
  sensibles.
- Tras liberar el lock, programar a lo sumo un worker mediante el launcher
  existente. No publicar ni invocar agentes en el comando. El worker normal
  conserva sus validaciones de PR/SHA, autor permitido, fecha posterior al
  marker y exclusión de IDs procesados; él es el único que puede cargar cuerpos
  y llamar Codex B.
- Los estados modernos o snapshots del ciclo actual no deben pasar por esta
  rama. Ante drift real, no modificar el freeze: permanecer en
  `waiting_for_user_attention` y exigir la resolución normal del usuario.
- Mantener `shell=False`, arrays argv, locks, escrituras atómicas, permisos y
  redacción. No ejecutar texto de agentes ni inferir/buscar sesiones; usar
  exactamente `state.codex.session_id` cuando el worker normal llegue a la
  adjudicación.

## Implementation Plan

1. Extraer un helper privado y testeable que clasifique un `RunState` como
   `legacy_external_cycle_freeze` sólo con los campos estructurados indicados
   arriba. Debe devolver un resultado seguro (por ejemplo, booleano/código y
   conteos), nunca cuerpos ni IDs. No usar `result` ni `last_error` como prueba.
2. Ajustar `continue_pr_review_cycle` para distinguir dos rutas después de
   comprobar el comentario GitHub autorizado:
   - la ruta existente para findings no accionables/inciertos, que conserva
     todo snapshot válido; y
   - la ruta de migración histórica, que únicamente ante el helper positivo
     borra `expected_eligible_thread_ids` y limpia el error/outcome antes de
     devolver el mismo run a `awaiting_bot_review`.
   Ambas rutas deben conservar los controles de comentarios repetidos y
   registrar exactamente el comentario consumido.
3. Bajo locks, volver a cargar el estado antes de aplicar la decisión. Si el
   estado cambió, hay worker vivo/launcher ambiguo, falta la precondición, o la
   acción ya fue consumida, fallar seguro o no-op idempotente según el contrato
   existente; no dejar un estado transitorio ni duplicar workers.
4. Reutilizar `_spawn_pr_review_worker` únicamente después de persistir la
   transición. Añadir un evento `pr_review_legacy_cycle_freeze_cleared` (nombre
   equivalente aceptable si es estable) con `source_cycle`, `current_cycle` y
   `expected_thread_count`. No añadir GitHub writes ni artefactos sensibles.
5. Verificar que el loop normal, al ver el freeze en `None`, captura el conjunto
   actual de hilos elegibles y lo persiste como `eligible_thread_ids` antes de
   la adjudicación. No cambiar la semántica de `expected_eligible_thread_ids`:
   sigue siendo un freeze sólo para recoveries que ya lo necesitan, y Phase
   15.9 sigue limpiándolo al publicar el siguiente trigger.
6. Actualizar README, `docs/referencia/cli.md`,
   `docs/guia/flujo-completo.md`, `docs/operacion/estado-artefactos.md`,
   `docs/operacion/troubleshooting.md` y
   `docs/referencia/trazabilidad-fases.md`: explicar que la recuperación es
   excepcional y lineage-bound, el nuevo comentario `continue` requerido, que
   usa el mismo run y que no repite `@codex review`; mantener la recomendación
   de no editar state manualmente. No documentar una automatización que no esté
   verificada.

## Testing Criteria

Automated tests are required because this changes persisted checkpoint recovery,
CLI behavior and process scheduling. Use fakes/mocks only; never invoke Cursor,
Codex, `gh`, SSH or the actual PR.

- Unit tests around the classifier:
  - accepts the exact historical shape: external-adjudication lineage from
    cycle 1, cycle 2 current, identical non-empty inherited snapshot, and each
    expected ID processed and resolved;
  - rejects source/current same cycle, absent/different/empty expected sets,
    unrelated recovery, no recovery, unprocessed/unresolved old ID and any
    lifecycle/status/outcome other than the narrow drift state;
  - does not depend on localized `last_error`/`result` text.
- Integration/regression tests in the Phase 15.9 fixture area:
  - seed a run matching `crypto-sentinel-20260718T115934Z-176634`, including
    two old processed/resolved IDs and three current eligible IDs; after a
    fresh authorized continue, it clears only the inherited freeze, stays on
    cycle 2 and schedules exactly one worker;
  - assert no post/reply/resolve GraphQL write, no Cursor chat creation, no
    Codex call and no commit/push during `continue`; marker, SHA, PR, lineage,
    B session and Cursor chat are byte-for-byte unchanged where applicable;
  - drive the fake worker after that transition and prove its normal
    adjudication payload contains the three current IDs, not the two previous
    IDs, and uses the persisted Codex B session exactly;
  - a second invocation without a newer exact continue comment has no side
    effects; a valid worker/launcher does not create a duplicate worker;
  - normal non-actionable `continue` and a real current-cycle thread-set drift
    keep the expected freeze and do not enter the migration branch.
- Regression tests for status/event output: only safe counts/cycle numbers are
  exposed; no thread IDs, prompts, bodies, token, PID, argv or full session ID.
- Extend model/schema compatibility tests only if code changes a persisted
  model. The intended solution must not require a schema version change.

## Validation

```bash
TMPDIR=/tmp TEMP=/tmp TMP=/tmp uv run pytest -s \
  tests/unit/test_pr_review_worker_liveness.py \
  tests/integration/test_phase15_pr_review.py \
  tests/integration/test_phase15_5_independent_pr_review.py \
  tests/integration/test_phase15_7_adjudication_recovery.py \
  tests/integration/test_phase15_8_reviewing_recovery.py \
  tests/integration/test_phase15_9_worker_continuity.py
TMPDIR=/tmp TEMP=/tmp TMP=/tmp uv run pytest
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src
uv run mkdocs build --strict
git diff --check
```

No ejecutar `pr-review continue`, `resume`, `recover`, `start`, `launch`,
Cursor, Codex, GitHub ni SSH reales durante implementación/validación. La
reanudación del PR #45 ocurre sólo después de review staged, instalación local
de la versión aceptada y autorización explícita del usuario.

## Risks Or Recovery Notes

- No abortar ni recrear `crypto-sentinel-20260718T115934Z-176634`. Conserva un
  marker válido del ciclo 2 y tres observaciones abiertas. La fase debe
  recuperar el mismo run, no un sucesor.
- Tras la implementación aceptada e instalación local, el flujo esperado es:

  1. Publicar en el PR #45 un comentario nuevo y exacto:

     ```text
     @rojobad /ai-dev-loop continue
     ```

  2. Desde A, ejecutar:

     ```bash
     cd ~/Projects/crypto-sentinel
     SSH_AUTH_SOCK="$HOME/.ssh/ai-dev-loop-ssh-agent.sock" \
       ai_dev_loop pr-review continue crypto-sentinel-20260718T115934Z-176634
     ```

  No se publica un segundo `@codex review`. El worker retoma el polling de la
  ventana existente y, si los hallazgos siguen elegibles, reanuda el flujo
  normal de adjudicación/Cursor/publicación.
- Si alguna precondición ya no coincide (PR cerrado, head SHA distinto,
  lineage/snapshot ambiguo, estado cambiado o hilos con efectos parciales), no
  forzar la ruta: detenerse con diagnóstico seguro y preparar una fase nueva.

## OpenQuestions

None.
