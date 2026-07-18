# Phase 15.8 — Artefactos de revisión Codex y recuperación post-Cursor

## Goal

Corregir el fallo observado al recuperar la revisión de PR #45. El sucesor
`crypto-sentinel-20260718T024655Z-ff42cb` adjudicó el feedback externo,
ejecutó Cursor y dejó su corrección staged, pero el proceso Codex no pudo
escribir `codex/reviews/01.json` porque el directorio padre no existía antes de
invocar `codex exec --output-last-message`.

La fase debe:

1. Crear de forma segura todos los directorios de artefactos requeridos antes
   de iniciar el proceso Codex.
2. Añadir una recuperación inmutable para el checkpoint de revisión local ya
   completado por Cursor pero sin resultado persistido, de modo que reintente
   solamente Codex B y conserve el trabajo staged, el chat Cursor y el ciclo
   GitHub existente.

## Non-Goals

- No reconstruir ni aceptar un resultado Codex analizando JSONL, Markdown,
  stderr o mensajes de agentes. El resultado estructurado escrito por Codex es
  la única fuente de decisión.
- No reejecutar Cursor para el run afectado ni crear un chat Cursor nuevo.
- No responder, resolver, publicar, hacer push, commit, crear PR ni volver a
  publicar `@codex review` durante recovery o resume.
- No modificar el source terminal ni editar manualmente `state.json` ni sus
  artefactos.
- No ampliar la recuperación a fallos generales de Codex, fallos de GitHub,
  errores de SSH o artefactos inválidos/dudosos.

## Scope

- Inicialización de directorios de artefactos en
  `src/ai_dev_loop/runners/codex.py`.
- Clasificación artefacto-dirigida del checkpoint local: revisión Codex no
  persistida después de una corrección Cursor y staging completos.
- Extensión acotada de `ai_dev_loop pr-review recover` y de la reanudación de
  PR-review para crear un sucesor de checkpoint `reviewing` y reintentar sólo
  el review local.
- Lineage, status, CLI/help, documentación y pruebas necesarias para que el
  comportamiento sea auditable, idempotente y compatible con estados previos.

## Out of Scope

- No cambiar los schemas de output de Codex ni la recuperación de adjudicación
  externa de Phase 15.7.
- No alterar `run-state-v1.json` o modelos persistidos salvo que un campo
  tipado adicional sea indispensable para describir el nuevo checkpoint; no
  duplicar datos que ya se prueban mediante artefactos deterministas.
- No tocar la configuración de `ai_dev_loop.yaml`, el socket SSH, hooks de
  Codex Desktop, políticas de merge, ni el proyecto CryptoSentinel.
- No recuperar el resultado válido visto sólo en los eventos del run afectado:
  se debe reanudar la misma sesión B para producir un nuevo resultado
  estructurado y persistido.

## Required Context

- Source de Phase 15.7: `pr_review_recover.py` recupera exclusivamente el
  checkpoint `external_adjudication`. El sucesor actual ya contiene
  `github/cycles/01/result.json`, `github/cycles/01/report.md`, un chat Cursor,
  `cursor/iterations/01/*` y `git/diffs/01.patch`.
- Fallo comprobado: `run_codex_review` calcula
  `codex/reviews/01.json`, entrega esa ruta a `--output-last-message`, llama al
  proceso y sólo después escribe `01.metadata.json`. Por ello el metadata crea
  el directorio demasiado tarde.
- El resultado que faltó es local (`codex/reviews/01.json`), no el resultado
  externo recuperado. El sucesor actual dejó cambios staged en CryptoSentinel;
  no deben tocarse durante la implementación.
- `commands/recover.py` ya implementa semántica de recovery `reviewing` para
  los loops locales. Reutilizar sus invariantes y helpers cuando sean
  compatibles; no duplicar una segunda interpretación de estado.
- `commands/pr_review.py` conserva el estado de publicación/adjudicación y
  delega el loop local a `workflow_engine.resume_run`. La vía PR-review debe
  mantener el mismo PR, SHA, thread set, request marker, B y A.

## Cursor Rules And Skills

Lee y aplica todas las reglas de `.cursor/rules/`, particularmente:

- `ai-dev-loop-governance.mdc`;
- `ai-dev-loop-orchestrator-contracts.mdc`;
- `ai-dev-loop-state-and-schema-contracts.mdc`;
- `ai-dev-loop-loop-and-resume-contracts.mdc`;
- `ai-dev-loop-codex-review-contracts.mdc`;
- `ai-dev-loop-global-integrations-contracts.mdc`;
- `ai-dev-loop-abort-contracts.mdc`; y
- `ai-dev-loop-docs-acceptance-contracts.mdc`.

Para documentación, usa la skill `ai-dev-loop-docs-acceptance-governance`.
No uses la skill de staged review mientras implementas; la revisión posterior
la hará Codex. Los tests deben usar ejecutables falsos de Cursor y Codex, nunca
modelos reales, GitHub real ni credenciales reales.

## Architecture Guardrails

- Antes de construir/invocar el proceso Codex, crear explícitamente los padres
  de `events_path`, `stderr_path`, `result_path`, `report_path` y
  `metadata_path` bajo el run directory, con los permisos de usuario
  correspondientes. La creación debe ser idempotente y no borrar artefactos
  parciales existentes.
- Mantener `shell=False`, argumentos como arrays, stdin bajo timeout, captura
  separada stdout/stderr y registro activo de proceso. No ocultar un exit no
  cero del CLI como éxito.
- El recovery post-Cursor debe basarse en evidencia duradera: run `failed`,
  iteración Cursor y staging completos, patch staged registrado, chat Cursor
  persistido, identidad de Codex válida, resultado `codex/reviews/NN.json`
  ausente, y sin resultado/review procesado. Nunca basarse en `last_error` ni
  en texto de eventos para determinar elegibilidad.
- Exigir que el worktree actual preserve exactamente branch, HEAD, raíz/Git
  dirs, patch staged y ausencia de cambios tracked unstaged o untracked
  non-ignored, igual que un checkpoint `reviewing`. Ante drift, parar sin
  crear sucesor ni escribir en GitHub/repo.
- El sucesor es inmutablemente derivado del run fallido y conserva de forma
  exacta `codex.session_id`, runtime congelado, `cursor.chat_id`, PR, SHA,
  marcador, thread IDs esperados, plan/prompt snapshots y artefactos Cursor/Git
  requeridos. No copiar ni exponer transcript, prompts completos, stderr o
  sesiones completas en eventos públicos.
- El sucesor debe usar lineage tipado
  `recovered_checkpoint: reviewing` y un reason code específico y seguro, por
  ejemplo `codex_review_result_artifact_missing`. No modificar el source ni
  añadir una transición `failed -> interrupted`.
- `pr-review recover --dry-run` es totalmente read-only. `recover` no lanza
  procesos ni GitHub; sólo el `pr-review resume` explícito desde A puede lanzar
  la revisión local.
- La reanudación de este checkpoint debe saltar Cursor, no re-adjudicar los
  hilos y no entrar en polling. Debe invocar únicamente el workflow local desde
  `reviewing`, con la sesión B exacta, y continuar el flujo normal sólo tras
  persistir y validar un nuevo resultado estructurado.
- No inferir ni sustituir los IDs A/B; exigir el controlador A ya ligado al
  sucesor. No usar `--last`.

## Implementation Plan

1. En `runners/codex.py`, extraer una preparación explícita de artefactos que
   cree los directorios padres antes de `run_process_streaming`. Aplicarla a
   todos los artefactos de revisión, conservar paths relativos existentes y
   mantener permisos sensibles para archivos cuando se escriban. Asegurar que
   un run recién creado o un sucesor sin `codex/reviews/` funciona.
2. Añadir pruebas unitarias del runner con un `codex` falso que falla si el
   padre de `--output-last-message` no existe. Probar que el archivo de
   resultado y metadata se persisten, que un padre existente no se borra y que
   los errores de subprocess continúan preservando eventos/metadata.
3. En el análisis de `pr-review recover`, separar los checkpoints:
   - conservar sin cambios la ruta de `external_adjudication` de Phase 15.7;
   - reconocer el checkpoint `reviewing` sólo mediante modelos y artefactos
     requeridos, incluidos el resultado local ausente y el staging auditable;
   - rechazar resultados presentes pero inválidos, Cursor incompleto, patch o
     worktree drift, procesos activos, PR/SHA/thread set drift, publicación
     iniciada, hilos ya procesados/respondidos/resueltos y otras causas de
     fallo.
4. Crear un sucesor transaccional para `reviewing` que deje el source terminal
   intacto. Copiar por allowlist los snapshots de identidad y los artefactos
   necesarios para reanudar la corrección ya terminada: chat Cursor, iteración,
   fingerprints/status/diff Git y el contexto externo validado. No copiar el
   resultado Codex ausente, stderr crudo, prompts no necesarios ni artefactos
   de otra iteración.
5. Hacer idempotente el matching de sucesores por source, checkpoint,
   iteración, patch hash, PR/SHA y thread set. Reutilizar sólo un sucesor
   no-terminal equivalente; si ya existe uno failed, permitir recuperarlo como
   fuente de la siguiente generación sólo cuando satisfaga de nuevo el
   checkpoint `reviewing`, sin saltar generaciones ni crear ramas paralelas.
6. Extender `pr-review resume` para el sucesor `reviewing`: validar el
   controlador A exacto, tomar los locks normales, restablecer el estado al
   checkpoint de review y delegar a `workflow_engine.resume_run` sin invocar
   Cursor, polling, adjudicación ni publicación. Si faltan artefactos o hay
   ambigüedad, persistir un fallo seguro y no lanzar un agente.
7. Confirmar que el loop local, tras un resultado Codex persistido, conserva la
   semántica existente: sin hallazgos completa con cambios staged; hallazgos
   crean un fix prompt Codex y continúan sólo si el límite lo permite. No
   implementar lógica especial que decida desde eventos o Markdown.
8. Actualizar CLI help, `docs/referencia/cli.md`, el flujo completo y
   troubleshooting con el procedimiento: recovery externo primero, recovery
   local `reviewing` cuando el resultado de Codex faltó, reanudación sólo desde
   A, y garantía de no repetir `@codex review`. Mantener las referencias al run
   histórico como ejemplo, no como dependencia de runtime.

## Testing Criteria

- Unitario de `runners/codex.py` con fake Codex:
  - el directorio de `--output-last-message` existe antes de ejecutar el fake;
  - el resultado estructurado, reporte y metadata se escriben correctamente;
  - el comportamiento sigue siendo seguro si el fake falla o expira; no hay
    modelo real ni parsing de Markdown para decidir.
- Integración PR-review con fake GitHub/Cursor/Codex:
  - un run recuperado de adjudicación con `codex/reviews/` ausente completa una
    revisión local sin error de directorio;
  - un source failed después de Cursor/staging pero sin resultado local produce
    un sucesor `reviewing`; `--dry-run` no muta nada;
  - `resume` reutiliza el chat Cursor y la sesión B exactos, ejecuta una sola
    revisión Codex y no ejecuta Cursor, polling, `post_issue_comment`, replies,
    resolve ni trigger;
  - el resultado con y sin hallazgos recorre el loop local normal;
  - check de igualdad de patch/worktree, PR cerrado, SHA/thread drift, chat
    faltante, resultado ya presente, source sin staging completo, proceso vivo
    y fallos no relacionados rechazan recovery sin side effects;
  - recovery repetido reusa sólo el sucesor non-terminal equivalente y un
    sucesor fallido válido puede ser fuente para una nueva generación;
  - no se emiten IDs de sesión completos, review Markdown, JSONL, patches o
    prompts en status/errores/eventos públicos.
- Compatibilidad: estados históricos sin el nuevo reason code/campos siguen
  cargando. Actualizar schema/model tests si el lineage cambia.

## Validation

```bash
TMPDIR=/tmp TEMP=/tmp TMP=/tmp uv run pytest -s \
  tests/unit/test_codex_review*.py \
  tests/integration/test_phase15_7_adjudication_recovery.py
TMPDIR=/tmp TEMP=/tmp TMP=/tmp uv run pytest
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src
uv run mkdocs build --strict
git diff --check
```

No ejecutar `pr-review recover`, `resume`, `start`, Cursor real, Codex real ni
GitHub real durante la implementación o los tests. La recuperación del run de
CryptoSentinel se hará sólo después de una revisión de cambios staged,
instalación local explícita y autorización del usuario.

## Risks Or Recovery Notes

- El resultado visto en el JSONL del run fallido no es un artefacto de decisión
  durable; tratarlo como resultado sería una interpretación de salida de agente
  no validada. Repetir sólo la revisión local con la misma sesión B es el camino
  seguro.
- El run afectado conserva su corrección staged. Hasta que exista la versión
  corregida, no se debe resetear, unstaging, commitear, hacer push ni lanzar
  otra recuperación externa.
- Tras instalar una implementación aceptada, el procedimiento esperado será:

  ```bash
  cd ~/Projects/crypto-sentinel
  ai_dev_loop pr-review recover crypto-sentinel-20260718T024655Z-ff42cb --dry-run
  ai_dev_loop pr-review recover crypto-sentinel-20260718T024655Z-ff42cb --output json
  # Desde A, usando el comando resume que devuelve recover.
  ```

  El comando de `resume` debe conservar el controlador vinculado y no publicar
  otro marcador en GitHub.

## OpenQuestions

None.
