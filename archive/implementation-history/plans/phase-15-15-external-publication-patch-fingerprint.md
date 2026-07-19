# Phase 15.15 — Fingerprint del patch tras corrección externa

## Goal

Corregir el contrato del hash staged entre una corrección externa de Cursor y
la publicación del PR, y desbloquear de forma segura el run de CryptoSentinel
`crypto-sentinel-20260718T212910Z-9488fe` (PR #45).

El patch actual no fue sustituido ni contiene drift real: coincide con
`git/diffs/02.patch` según la normalización existente; sólo cambia el salto de
línea final. El bloqueo `staged_patch_drift` ocurre porque
`github_pr_review.staged_patch_sha256` conserva `b15e…`, un fingerprint
heredado del estado anterior a la corrección externa. La iteración 02 dejó el
patch aceptado por Cursor/Codex, pero no actualizó ese checkpoint antes de
publicación/recovery.

Esta fase debe persistir el fingerprint actual al completar una corrección
externa aceptada y permitir que el recovery histórico `publication_pre_commit`
adopte el fingerprint live sólo con prueba estricta de equivalencia contra el
artefacto de staging de la última iteración. El source continúa inmutable.

## Non-Goals

- No aceptar patches distintos del artefacto por heurística, texto de errores,
  timestamps, nombre de archivos, respuesta de agentes ni intervención manual.
- No reejecutar Cursor/Codex, re-adjudicar GitHub, responder/resolver threads,
  repostear `@codex review`, ni modificar el source terminal.
- No relajar PR/SHA/rama/baseline/threads/locks/A-B ni las garantías de commit
  y push no-force.
- No modificar SSH, `IdentityAgent`, Phase 15.14, `gh`, CryptoSentinel, YAML,
  modelos o configuración de agentes.

## Scope

- `src/ai_dev_loop/commands/pr_review.py`: checkpoint de patch antes de
  publicación externa tras workflow local aceptado.
- `src/ai_dev_loop/commands/pr_review_recover.py`: análisis y sucesor del
  checkpoint `publication_pre_commit` para fingerprint histórico obsoleto.
- Helpers de Git/fingerprint estrictamente necesarios y tests de integración/
  regresión Phase 15.12–15.13.
- Documentación de estado, troubleshooting, CLI/flujo y trazabilidad.

## Out of Scope

- Ejecutar `recover`, `resume`, `launch`, GitHub, SSH, Cursor, Codex, commit o
  push reales durante implementación o validación.
- Crear otro tipo de recovery o cambiar estados/schemas sin necesidad de una
  evidencia durable adicional.
- Reescribir artefactos `git/diffs/NN.patch` históricos o editar `state.json`
  manualmente.

## Required Context

- Source: `crypto-sentinel-20260718T212910Z-9488fe`, failed,
  `lifecycle=failed`, `publication_phase=pre_commit`, PR #45, iteración 02,
  revisión Codex sin hallazgos y patch staged no vacío.
- `gpr.staged_patch_sha256` es `b15e…`, heredado del ciclo fuente. El artefacto
  `git/diffs/02.patch` y el patch staged live tienen mismo contenido tras
  `normalize_patch_text`; la única diferencia es el newline final. El hash raw
  live es `5e2b…`; el hash de archivo sin normalizar es distinto y no debe
  confundirse con el fingerprint usado por publicación.
- `validate_staged_patch_matches_artifact()` ya compara contenido normalizado,
  mientras `publish_accepted_staged_patch()` y el estado de publicación usan
  `sha256_text()` del patch live. La corrección debe establecer una única
  semántica explícita en cada frontera, sin mezclar hash de bytes de artefacto
  con hash raw del patch live.
- Phase 15.12 programa una nueva iteración externa y Phase 15.13 introdujo
  `publication_pre_commit`; ninguna actualiza el campo GPR con el patch final
  de esa iteración antes de publicar.

## Cursor Rules And Skills

Leer y aplicar todas las reglas de `.cursor/rules/`: `ai-dev-loop-governance.mdc`,
`ai-dev-loop-orchestrator-contracts.mdc`, `ai-dev-loop-state-and-schema-contracts.mdc`,
`ai-dev-loop-loop-and-resume-contracts.mdc`, `ai-dev-loop-codex-review-contracts.mdc`,
`ai-dev-loop-abort-contracts.mdc`, `ai-dev-loop-global-integrations-contracts.mdc` y
`ai-dev-loop-docs-acceptance-contracts.mdc`.

Para documentación usar `ai-dev-loop-docs-acceptance-governance`. No usar la
skill staged-review durante implementación. Usar fakes de `agent`, `codex`,
Git, `gh` y SSH; nunca servicios, claves, sockets o agentes reales.

## Architecture Guardrails

- El patch staged live sólo es publicable si pasa `validate_clean_except_staged`
  y coincide con el artefacto de la última iteración mediante
  `validate_staged_patch_matches_artifact` / `normalize_patch_text`. La
  normalización sólo puede tratar CRLF y newlines terminales como equivalentes;
  cualquier otro byte/contenido es drift y bloquea.
- El hash que el sucesor entrega a la publicación debe ser el fingerprint raw
  de `git diff --cached --binary` live, calculado después de la equivalencia
  anterior. Así coincide con el hash que `publish_accepted_staged_patch()`
  verificará. Nunca usar `sha256_file` de un artefacto para sustituirlo.
- Para runs nuevos, actualizar `gpr.staged_patch_sha256` exactamente después de
  staging y revisión local sin hallazgos, bajo locks y antes de la transición a
  `publishing_external_fix`. Persistirlo atómicamente; si el patch/artifact no
  valida, fallar antes de publicación.
- Para el recovery histórico, un GPR hash distinto no es por sí solo prueba de
  drift. Puede adoptarse sólo en el sucesor si la fase es `pre_commit`, la
  revisión local final es válida/no accionable, el artefacto de esa iteración
  existe y coincide normalizado con el index live, y mantienen PR/SHA/rama,
  threads, baseline, texto de publicación y ausencia de efectos parciales.
- El source permanece byte-a-byte inmutable. El sucesor registra el hash live
  probado en `RecoveryState.source_staged_patch_sha256` y GPR; no reescribe el
  hash obsoleto del source ni los artefactos históricos.
- Si falta el artefacto, el patch difiere más allá de la normalización, hay
  worktree sucio/worker vivo, hash/PR/SHA/thread drift, revisión accionable o
  publicación parcial, detenerse sin sucesor, commit, push ni GitHub writes.
- No usar `last_error`, logs, artefactos de prompts ni Markdown de revisión
  como evidencia. Mantener rutas relativas, permisos sensibles, outputs
  redactados, argv estructurados, locks y A/B exacto.

## Implementation Plan

1. Localizar la transición workflow local aceptado → publicación externa y
   añadir un helper que obtenga la última iteración durable, valide index y
   artefacto, calcule el hash raw live compatible con publish y actualice
   `github_pr_review.staged_patch_sha256` bajo lock antes de `_publish_external_fix`.
2. Garantizar que la ruta se limita a correcciones externas; no modificar el
   contrato de patches iniciales/locales que ya tienen estado válido. Si existe
   una abstracción común segura, documentar y probar las fronteras afectadas.
3. Cambiar el análisis `publication_pre_commit`: comprobar siempre primero la
   equivalencia normalizada artefacto/index. Si el hash GPR histórico coincide
   con el hash live, seguir normal. Si no coincide pero toda la evidencia
   estricta es válida, marcar la adopción histórica tipada y usar el hash raw
   live sólo para el sucesor. No permitir adopción cuando el artefacto falta o
   difiere realmente.
4. Ajustar creación/reuso del sucesor y su lineage para que la idempotencia
   incluya el fingerprint live probado. Reusar únicamente un sucesor cuya
   lineage, patch, PR/SHA, iteration y freeze coincidan; no crear sucesores
   paralelos ni modificar source.
5. Confirmar que `pr-review resume` no necesita un nuevo camino: debe usar el
   GPR hash adoptado/actualizado y ejecutar sólo la publicación actual. Mantener
   las verificaciones de patch de `publish_accepted_staged_patch`.
6. Actualizar docs para distinguir hash de bytes de artefacto, equivalencia
   normalizada y fingerprint de publicación; documentar que una diferencia de
   newline terminal no autoriza cambios de contenido ni edición de state.

## Testing Criteria

- Regresión Phase 15.12: una corrección externa que produce iteración nueva y
  review local no accionable actualiza `gpr.staged_patch_sha256` al hash raw del
  patch staged antes de publicación; publication no acusa falsamente cambio.
- Recovery histórico: fixture igual al incidente, con GPR hash heredado obsoleto
  y artefacto/live iguales salvo newline terminal, es elegible. `--dry-run` no
  escribe; el sucesor conserva source inmutable y usa el hash raw live; resume
  sólo publica con fakes.
- Rechazos: un cambio de contenido, CRLF no equivalente fuera de la normalización,
  artefacto faltante, review accionable/faltante, index vacío/sucio, PR/SHA/rama
  distinto, thread side effect, worker vivo o fase posterior rechaza recovery y
  no realiza writes.
- Idempotencia: segunda recuperación con mismo patch probado reutiliza sucesor;
  cambio posterior real en el index lo invalida y no adopta nuevo hash.
- Compatibilidad: tests Phase 15.7–15.14, hashes de publicación normal e
  initial/source-run no cambian involuntariamente. Validar schema/model si se
  persiste una marca adicional; no añadir marca si lineage actual basta.

## Validation

- Ejecutar tests nuevos/focalizados de Phase 15.12, 15.13, publicación y
  recovery con `TMPDIR=/tmp TEMP=/tmp TMP=/tmp`.
- Ejecutar suite completo: `TMPDIR=/tmp TEMP=/tmp TMP=/tmp uv run pytest`.
- Ejecutar `uv run ruff format --check src tests`, `uv run ruff check src tests`,
  `uv run mypy src`, `uv run mkdocs build --strict` y `git diff --check`.
- No ejecutar operaciones reales sobre PR #45 ni usar el estado local de
  CryptoSentinel como test mutante.

## Risks Or Recovery Notes

- No ejecutar aún el recovery del run afectado; debe permanecer sin sucesor
  hasta que esta fase sea revisada, aceptada, instalada y publicada.
- Después de aceptación: cargar/verificar la clave, ejecutar
  `pr-review recover --dry-run` sobre `...212910Z-9488fe`, crear el sucesor y
  ejecutar el `resume_command` desde A. B permanece inactiva. El resume debe
  publicar sin nuevos agentes ni trigger duplicado.
- Una discrepancia de contenido real no se puede arreglar con este recovery:
  requiere intervención/plan nuevo, no edición manual del estado.

## OpenQuestions

None.
