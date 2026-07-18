# Phase 15.13 — Interrupción SSH de publicación y recovery desde `pre_commit`

## Goal

Hacer reanudable una publicación de correcciones de PR que se detiene antes del commit porque `ssh-agent` no tiene una clave utilizable. Debe desbloquear de forma segura el run de CryptoSentinel `crypto-sentinel-20260718T212910Z-9488fe` (PR #45).

El incidente ya completó Cursor y revisión local Codex: la iteración externa `02` está staged y su resultado local no tiene hallazgos accionables. El worker llegó a `publication_phase: pre_commit`, pero `verify_ssh_push_ready()` lanzó un `ValidationError`, que el clasificador convirtió en `failed`. Por eso `pr-review resume` no puede usar el checkpoint durable que ya existe.

Esta fase debe clasificar el SSH-agent sin identidad como interrupción tipada para runs futuros, crear un recovery inmutable para runs históricos terminales en `pre_commit`, y corregir la ayuda de `pr-review recover` para que incluya `external_feedback_cursor` y el nuevo checkpoint.

## Non-Goals

- No cargar claves, solicitar passphrases, cambiar SSH por HTTPS/PAT ni guardar credenciales.
- No relajar validaciones de PR, SHA, rama, patch staged, locks, threads, A/B o publicación.
- No usar `last_error`, logs, stderr, texto de Git o texto de agentes como prueba de recuperabilidad.
- No mutar el run fuente, reintentar automáticamente, ni ejecutar operaciones reales sobre PR #45 durante implementación.

## Scope

- `src/ai_dev_loop/errors.py` y `src/ai_dev_loop/runners/publish.py`: error de dominio estrecho para `ssh-add -l` sin identidad utilizable.
- `src/ai_dev_loop/commands/pr_review.py`: clasificación de worker y checkpoint de publicación reanudable.
- `src/ai_dev_loop/commands/pr_review_recover.py`, `state.py` y schemas aplicables: análisis, lineage y sucesor de `publication_pre_commit`.
- `src/ai_dev_loop/cli.py`, `docs/` y tests de publicación, recovery PR y CLI help.

## Out of Scope

- Cursor, Codex, GitHub, `gh`, SSH, commit, push, `recover`, `resume`, `launch` o `continue` reales.
- Cambiar recoveries existentes (`external_adjudication`, `reviewing`, `external_feedback_cursor`) excepto para integrarlos compatiblemente en selector, documentación y help.
- Convertir todos los `ValidationError` en recuperables o implementar una segunda vía de commit/push.
- Cambiar `ai_dev_loop.yaml`, CryptoSentinel, hooks, modelos o configuración SSH del usuario.

## Required Context

- Run afectado: `crypto-sentinel-20260718T212910Z-9488fe`, sucesor de `crypto-sentinel-20260718T115934Z-176634`, PR #45. Conserva `publication_phase: pre_commit`, patch staged `b15e530a...`, `github/cycles/02/publication-text.json`, resultado local Codex 02 sin hallazgos y freeze de tres threads actuales.
- No hubo commit, push ni nueva escritura GitHub en ese intento. El repositorio objetivo conserva el patch staged en la rama del PR.
- `verify_ssh_push_ready()` usa `ssh-add -l` y hoy lanza un `ValidationError` genérico. `_classify_worker_outcome()` trata primero cualquier `ValidationError` como terminal, aunque la publicación esté en curso.
- `pr-review resume` ya puede retomar un run `interrupted` con checkpoint de publicación. Falta conservar esa clasificación y una vía de sucesor para el run histórico ya terminal.
- El docstring de `pr_review_recover_command` sólo lista schema/reviewing; la documentación de Phase 15.12 ya documenta `external_feedback_cursor`.

## Cursor Rules And Skills

Leer y aplicar todas las reglas de `.cursor/rules/`: `ai-dev-loop-governance.mdc`, `ai-dev-loop-orchestrator-contracts.mdc`, `ai-dev-loop-state-and-schema-contracts.mdc`, `ai-dev-loop-loop-and-resume-contracts.mdc`, `ai-dev-loop-codex-review-contracts.mdc`, `ai-dev-loop-abort-contracts.mdc`, `ai-dev-loop-global-integrations-contracts.mdc` y `ai-dev-loop-docs-acceptance-contracts.mdc`.

Para documentación usar `ai-dev-loop-docs-acceptance-governance`. No usar la skill staged-review durante implementación. Usar sólo fakes de `agent`, `codex`, `gh`, Git y `ssh-add`; nunca servicios, claves o credenciales reales.

## Architecture Guardrails

- La falta de identidad del agente SSH debe ser una excepción tipada desde `verify_ssh_push_ready`, nunca una coincidencia de strings. Sólo esa excepción bajo `_publication_in_progress(gpr)` puede producir `interrupted` con outcome estable y seguro.
- URL remota inválida, remoto HTTP/no SSH, SHA, patch, PR, baseline y cualquier `ValidationError` distinto siguen siendo terminales. No generalizar la recuperación por contexto de publicación.
- El recovery histórico debe basarse en evidencia durable: source `failed`; lifecycle/phase `failed` + `pre_commit`; patch actual igual al hash durable; `HEAD`, local/bound SHA y fase prueban que no hubo commit posterior; resultado local Codex válido sin hallazgos; texto de publicación válido; freeze de threads actual intacto; PR remoto abierto en el SHA enlazado. Nunca usar `last_error`.
- Rechazar worker vivo, baseline sucio, drift de patch/SHA/PR/rama/remoto, resultado local faltante/accionable, texto corrupto, commit/push/trigger parcial, side effect sobre threads actuales o lineage ambigua. Ante duda no crear sucesor ni escribir GitHub.
- El source es inmutable. Crear un único sucesor transaccional `interrupted` con checkpoint/reason code nuevos y tipados, por ejemplo `publication_pre_commit` / `publication_pre_commit_interrupted`; para historial la elegibilidad es por checkpoint, no por el mensaje de error.
- Conservar PR, repo, rama, SHA, controlador A, sesión Codex B, chat Cursor, freeze de threads, patch hash y artefactos de publicación allowlisted. Mantener rutas relativas, permisos sensibles, escrituras atómicas y locks.
- Tras `pr-review resume`, el único trabajo permitido es la publicación existente. No abrir Cursor/Codex, re-adjudicar, ejecutar `continue`, responder/resolver hilos ni repostear `@codex review` antes del flujo normal posterior a una publicación exitosa.
- No usar `shell=True` ni exponer salida de `ssh-add`, prompts, patch, credenciales o IDs completos. `state.py` y recovery son control-plane work y requieren aceptación manual staged.

## Implementation Plan

1. Crear una excepción de dominio estrecha para `ssh-add -l` sin clave. Conservar `ValidationError` para remotos y preflights no relacionados. Añadir outcome seguro sin stderr de `ssh-add`.
2. Ajustar `_classify_worker_outcome()` y `_apply_worker_failure()` para que esa excepción tipada durante publicación deje el estado `interrupted`, preserve `pre_commit` y restaure `publishing_initial` o `publishing_external_fix`. Confirmar que `pr-review resume` reusa el pipeline de publicación y no duplica artefactos/agentes.
3. Extender `PrReviewRecoveryAnalysis` y su selector con `publication_pre_commit`. Validar estado/artefactos, patch, HEAD/SHA, resultado local, texto de publicación, PR remoto, thread set, ausencia de side effects y worker. Distinguir explícitamente `pre_commit` de `committed`, `pushed` y `pr_bound`.
4. Implementar creación/reuso idempotente de sucesor inmutable. Copiar sólo identidad y publicación requeridas; preservar A/B y el freeze; emitir `resume_command` con controlador A cuando corresponde. No crear una ruta paralela de push.
5. Extender `pr-review resume` sólo si es necesario para que el checkpoint nuevo restaure `publishing_external_fix` y ejecute exclusivamente `_publish_external_fix`. Si la clave sigue ausente, volver a interrumpir sin duplicar commits, pushes ni comentarios.
6. Corregir el docstring/help de `pr-review recover` para listar `external_adjudication`, `reviewing`, `external_feedback_cursor` y `publication_pre_commit`. Actualizar CLI, configuración, troubleshooting, flujo completo y trazabilidad con precondiciones, `--dry-run`, sucesor inmutable y carga manual de clave.
7. Si se añade un enum/campo persistente, actualizar modelo Pydantic, schema, writers/readers, renderizadores y compatibilidad histórica juntos. No sobrecargar reason codes existentes.

## Testing Criteria

- Fake `ssh-add -l` sin identidad genera error tipado. En `pre_commit`, el worker queda `interrupted`, conserva patch y no llama commit/push/trigger. Con clave fake, `pr-review resume` publica una vez y no llama Cursor, Codex ni adjudicación.
- Un `ValidationError` de URL, SHA, patch, baseline o preflight no SSH sigue terminal y no obtiene recovery por tener fase de publicación.
- Un fixture equivalente a `...212910Z-9488fe` produce con `--dry-run` un análisis sin writes y con `recover` un solo sucesor `interrupted`; el source queda byte-a-byte inmutable y el resume sólo publica.
- Rechazar fases `committed`/`pushed`/`pr_bound`, review local faltante/accionable, patch/HEAD drift, worktree sucio, PR cerrado/SHA distinto, threads modificados, texto corrupto, worker vivo, trigger parcial y lineage ambigua. Ninguno escribe source, repo o GitHub fake.
- Dos recoveries compatibles reutilizan sucesor; reintentos sin clave no duplican efectos; con clave se mantiene la semántica normal de publicación/revisión siguiente.
- `--help` enumera los cuatro checkpoints; outputs/eventos no filtran prompts, patches ni salida de `ssh-add`.

## Validation

- Ejecutar focused pytest para Phase 15.7–15.12, continuidad PR, recovery planner y publish runner, todos con `TMPDIR=/tmp TEMP=/tmp TMP=/tmp`.
- Ejecutar el suite completo: `TMPDIR=/tmp TEMP=/tmp TMP=/tmp uv run pytest`.
- Ejecutar `uv run ruff format --check src tests`, `uv run ruff check src tests`, `uv run mypy src`, `uv run mkdocs build --strict` y `git diff --check`.
- No ejecutar comandos reales contra la PR #45 durante implementación o validación.

## Risks Or Recovery Notes

- Esta fase no autoriza retomar la PR hasta revisión staged, aceptación, instalación local y publicación de esta versión.
- Después de aceptar e instalar: cargar la clave en el socket persistente, usar primero `ai_dev_loop pr-review recover --dry-run crypto-sentinel-20260718T212910Z-9488fe`, luego ejecutar el recovery sin dry-run y el `resume_command` resultante. A es controlador y B queda inactiva.
- Si cambian PR, SHA, rama, patch staged, threads o estado de publicación, no forzar el recovery ni editar `state.json`; detenerse y preparar otra fase.

## OpenQuestions

None.
