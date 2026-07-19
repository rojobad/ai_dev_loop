# Phase 15.16 — Baseline limpio para feedback externo y recovery pre-Cursor

## Goal

Corregir el contrato pre-Cursor de las rondas externas de PR. El primer turno
Cursor después de publicar un fix debe arrancar desde el commit limpio recién
publicado; una corrección local posterior a un finding de Codex debe conservar y
verificar el patch staged de su iteración anterior.

La fase debe desbloquear el run CryptoSentinel
crypto-sentinel-20260719T002147Z-a0f030 (PR #45). Publicó el fix anterior,
resolvió cinco threads históricos y recibió dos observaciones nuevas en el ciclo
3. El worktree está limpio y HEAD coincide con la PR, pero Cursor 03 nunca
arrancó: el orquestador comparó el índice limpio con git/diffs/02.patch.

También debe hacer recuperable ese checkpoint. El directorio vacío
cursor/iterations/03 creado antes del preflight no es evidencia de una
ejecución parcial Cursor ni debe clasificar el source como reviewing.

## Non-Goals

- No relajar el control de drift de patch staged entre una revisión Codex local
  y su siguiente corrección Cursor.
- No aceptar índice sucio, branch/HEAD/PR/thread drift, publicación parcial,
  ejecución Cursor parcial ni evidencia ambigua.
- No editar manualmente state.json, artefactos del run fuente, patches
  históricos ni CryptoSentinel.
- No modificar SSH, IdentityAgent, gh, modelos, A/B, hooks, ai_dev_loop.yaml,
  límites de ciclos ni la semántica de continue.
- No ejecutar recover, resume, launch, Cursor, Codex, GitHub, SSH, commit ni
  push reales durante implementación o validación.

## Scope

- workflow_engine.py, iterations.py y helpers Git estrictamente necesarios:
  selección semántica del baseline antes de Cursor.
- commands/pr_review_recover.py: clasificación y sucesor inmutable cuando el
  feedback externo falla antes de que Cursor realmente inicie.
- Tests unitarios, integración y regresión Phase 15.12–15.15.
- Documentación de CLI, flujo, artefactos, troubleshooting y trazabilidad.

## Out of Scope

- Añadir schema o estado persistido si el campo existente
  github_pr_review.external_cursor_iteration basta para distinguir el turno
  externo. Si Cursor concluye que es imprescindible, debe detenerse y anotarlo
  en OpenQuestions antes de un cambio dependiente.
- Cambiar publicación normal, salvo el uso de fakes necesario para probar dos
  ciclos completos.
- Reescribir recoveries de staging, usage limit, reviewing,
  external_adjudication o publication_pre_commit, salvo evitar que este
  checkpoint se clasifique falsamente como reviewing.
- Llamadas a modelos, GitHub, Docker, SSH o agentes reales desde tests.

## Required Context

- workflow_engine._run_cursor_turn aplica validate_correction_pre_cursor a
  cualquier iteración mayor a 1. Esto es correcto sólo para una corrección
  local que conserva un patch staged.
- is_external_cursor_prompt_iteration(state, iteration_number) ya identifica
  de forma tipada el primer turno de cada ronda externa mediante
  external_cursor_iteration, lifecycle fixing_external_feedback y el prompt
  externo durable.
- Después de publicar, state.repository.initial_head y
  github_pr_review.bound_head_sha se actualizan al commit. El baseline del
  próximo feedback externo debe ser: identidad Git, branch y HEAD enlazados;
  índice, worktree y untracked limpios.
- El run afectado tiene external_cursor_iteration=3, prompt y resultado externo
  válidos, dos threads actuales, y sólo un directorio 03 vacío: no existen
  execution_started, metadata, status, fingerprint, diff, review ni
  iterations[03].
- Phase 15.15 refresca staged_patch_sha256 antes de publicar. Tras publicar
  comienza una nueva ronda limpia; sólo un finding Codex dentro de la misma
  ronda debe exigir el patch staged anterior.

## Cursor Rules And Skills

Leer y aplicar todas las reglas de .cursor/rules/, especialmente:

- ai-dev-loop-governance.mdc;
- ai-dev-loop-orchestrator-contracts.mdc;
- ai-dev-loop-state-and-schema-contracts.mdc;
- ai-dev-loop-loop-and-resume-contracts.mdc;
- ai-dev-loop-codex-review-contracts.mdc;
- ai-dev-loop-abort-contracts.mdc;
- ai-dev-loop-global-integrations-contracts.mdc; y
- ai-dev-loop-docs-acceptance-contracts.mdc.

Para documentación usar ai-dev-loop-docs-acceptance-governance. No usar la
skill staged-review durante implementación. Usar sólo fakes de agent, codex,
gh, SSH y publicación; nunca servicios, claves, sockets ni agentes reales.

## Architecture Guardrails

- Elegir el baseline por semántica durable, nunca por iteration_number, textos
  de error, timestamps, directorios o logs.
- Para la iteración external_cursor_iteration exigir, bajo locks de run y repo:
  root/git dirs correctos, branch de PR, HEAD igual a initial_head y
  bound_head_sha, e índice/worktree/untracked limpios. No comparar con un patch
  publicado anterior.
- Para correcciones locales posteriores, preservar exactamente
  validate_correction_pre_cursor: patch de la iteración anterior, sin tracked
  unstaged ni untracked.
- Mantener el binding remoto existente de independent_pr antes de programar
  Cursor. El nuevo preflight local debe cubrir también source_run y no debe
  añadir llamadas GitHub desde el runner Cursor.
- Crear cursor/iterations/NN y artefactos de intento sólo después de superar el
  preflight. Por compatibilidad, recovery puede ignorar un directorio vacío
  sólo si no hay entrada durable NN ni archivos/evidencia de ejecución.
- Recovery usa sólo estado y artefactos estructurados. Debe congelar los dos
  threads actuales, prompt exacto, PR/SHA/rama, Cursor chat, sesión Codex y
  controlador; no puede re-adjudicar, responder, resolver, publicar, hacer
  commit/push ni repostear @codex review.
- El source terminal es byte-a-byte inmutable. recover --dry-run no escribe;
  recover crea un único sucesor transaccional e idempotente; sólo resume puede
  iniciar Cursor.
- Mantener argv arrays, shell=False, locks, escrituras atómicas, permisos
  sensibles y redacción. No exponer prompts, patches, cuerpos, IDs completos,
  tokens, sockets ni entornos en outputs normales.
- No cambiar modelos, schemas, transiciones ni configuración sin necesidad
  probada. Ante esa necesidad, detenerse y registrar OpenQuestions.

## Implementation Plan

1. Mapear y probar la ruta publicación -> awaiting_bot_review -> adjudicación
   accionable -> fixing_external_feedback -> Cursor externo, y la continuación
   Cursor -> staging -> Codex -> publicación. Confirmar que
   external_cursor_iteration identifica exclusivamente el primer turno externo.
2. Implementar un preflight específico para ese turno. Debe validar identidad
   del repositorio, branch, HEAD local/estado/PR y baseline absolutamente
   limpio. Invocarlo en _run_cursor_turn cuando
   is_external_cursor_prompt_iteration sea verdadero, antes de crear
   directorios, metadata o invocar Cursor.
3. Reordenar _run_cursor_turn: la rama de usage-limit conserva prioridad; la
   rama externa usa baseline limpio; toda corrección local posterior usa el
   patch staged anterior. Conservar el binding remoto de independent_pr y
   cubrir source_run sin llamadas remotas adicionales.
4. Corregir external_feedback_cursor recovery. Un directorio NN vacío no es un
   intento parcial; una entrada iterations[NN], metadata, events, status,
   fingerprint, diff, review o cualquier archivo sí lo es. La clasificación
   externa debe ocurrir antes de reviewing y nunca depender de last_error.
5. Confirmar o ajustar mínimamente el sucesor existente para copiar sólo el
   contexto allowlisted, no copiar el directorio vacío, congelar el set actual,
   conservar external_cursor_iteration y planificar Cursor 03. Mantener fuente
   inmutable y reuso idempotente.
6. Añadir una prueba integral de dos rondas externas con fakes: publicar patch,
   dejar repo limpio, recibir feedback, ejecutar turno externo fresco,
   stage/review/publicar y programar la siguiente ronda. Probar que no hay
   comparación del índice limpio con patch publicado ni duplicación de
   trigger, commits, pushes, replies o resolves.
7. Actualizar docs/referencia/cli.md, docs/guia/flujo-completo.md,
   docs/operacion/estado-artefactos.md, docs/operacion/troubleshooting.md y
   docs/referencia/trazabilidad-fases.md para describir ambos baselines y el
   recovery pre-Cursor ya implementado.

## Testing Criteria

Se requieren tests unitarios, integración y regresión con repositorios
temporales y fakes; nunca usar el estado real de CryptoSentinel como fixture
mutante.

- Unit: una iteración externa 03 con patch histórico 02 y repo limpio usa el
  baseline limpio; drift de identidad, branch, HEAD, staged, unstaged o
  untracked falla antes de Cursor.
- Unit: un finding Codex local sobre la iteración externa 03 programa 04 y
  exige exactamente git/diffs/03.patch. Un índice vacío o distinto sigue
  fallando.
- Integration Phase 15.16: reproducir el incidente con 01/02 publicadas,
  ciclo 3, dos findings accionables, repo limpio y directorio 03 vacío.
  recover --dry-run debe ser elegible como external_feedback_cursor; recover
  crea/reusa un sucesor inmutable y resume ejecuta sólo Cursor 03 con el prompt
  externo exacto.
- Recovery negative: directorio 03 no vacío, entrada durable 03,
  metadata/event/status/fingerprint/diff/review, baseline sucio, worker vivo,
  prompt/resultado inválido, findings no accionables, PR/SHA/rama/freeze
  distinto, side effects actuales, o sucesor ambiguo/fallido rechazan sin
  writes.
- Two-cycle integration: fakes de agent, codex, gh, publicación y SSH verifican
  prompts exactos, misma identidad de chat/sesión, fingerprint Phase 15.15,
  commits/push no-force y resoluciones únicamente del ciclo publicado.
- Conservar regresiones Phase 15.12–15.15, staging y publicación.

## Validation

```bash
TMPDIR=/tmp TEMP=/tmp TMP=/tmp uv run pytest -q \
  tests/unit/test_phase15_12_external_feedback_iteration.py \
  tests/unit/test_staging_correction.py \
  tests/integration/test_phase15_12_external_feedback_recovery.py \
  tests/integration/test_phase15_13_publication_pre_commit_recovery.py \
  tests/integration/test_phase15_15_publication_patch_fingerprint.py \
  tests/integration/test_phase15_16_external_feedback_clean_baseline.py \
  tests/integration/test_phase15_pr_review.py \
  tests/unit/test_publish_and_github_state.py \
  tests/unit/test_recovery_planner.py
TMPDIR=/tmp TEMP=/tmp TMP=/tmp uv run pytest -q
uv run python -m ruff format --check src tests
uv run python -m ruff check src tests
uv run python -m mypy src
uv run mkdocs build --strict
git diff --check
```

No ejecutar operaciones reales sobre PR #45 durante implementación o validación.

## Risks Or Recovery Notes

- Esta fase no autoriza retomar todavía el run. Requiere revisión staged,
  aceptación, instalación local y publicación.
- Después, ejecutar primero recover --dry-run sobre
  crypto-sentinel-20260719T002147Z-a0f030. Debe indicar
  external_feedback_cursor, no reviewing; después crear/reusar el sucesor y
  ejecutar su resume_command desde A. B queda inactiva.
- Resume abre Cursor sólo para el prompt externo del ciclo 3. Sólo después de
  una corrección aceptada la publicación normal resolverá los dos threads y
  pedirá la ronda siguiente.
- Si dry-run posterior encuentra drift o evidencia parcial real, no forzar ni
  editar estado: preparar otra fase con la evidencia actual.

## OpenQuestions

None.
