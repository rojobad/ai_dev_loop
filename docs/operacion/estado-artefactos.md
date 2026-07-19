# Estado y artefactos

`ai_dev_loop` guarda estado fuera del repositorio objetivo. Esto evita mezclar auditoria del orquestador con el codigo que Cursor modifica.

## Rutas XDG

Si las variables XDG existen:

```text
$XDG_CONFIG_HOME/ai_dev_loop
$XDG_STATE_HOME/ai_dev_loop
$XDG_CACHE_HOME/ai_dev_loop
```

Fallbacks:

```text
~/.config/ai_dev_loop
~/.local/state/ai_dev_loop
~/.cache/ai_dev_loop
```

Permisos esperados cuando el filesystem los soporta:

- directorios: `0700`;
- prompts, session IDs, output de agentes, patches y reviews: `0600`.

## Directorio de run

```text
$XDG_STATE_HOME/ai_dev_loop/runs/<project>/<run-id>/
├── state.json
├── effective-config.yaml
├── source-config.yaml
├── manifest.json
├── plan/
│   ├── plan.md
│   └── metadata.json
├── prompts/
│   ├── cursor-initial.txt
│   ├── cursor-recovery/
│   └── fixes/
├── cursor/
│   ├── chat.json
│   └── iterations/
├── codex/
│   ├── session-runtime.json
│   ├── events/
│   └── reviews/
├── git/
│   ├── baseline-status.txt
│   ├── status/
│   └── diffs/
├── logs/
│   ├── ai_dev_loop.log
│   └── events.jsonl
└── locks/
    ├── run.lock
    ├── abort-request.json
    ├── active-process.json
    └── pr-review-worker.json   # launcher del poller PR-review (PID/token; no en status humano)
```

No todos los archivos existen en todos los estados. Por ejemplo, `prompts/fixes/NN.txt` existe solo si Codex reporto findings en review `NN`.

En ciclos GitHub PR-review, `github_pr_review.expected_eligible_thread_ids` es el
snapshot exact-set de **una** ventana de review. Se limpia a `null` al publicar el
trigger del siguiente ciclo externo; `processed_thread_ids` /
`resolved_thread_ids` siguen siendo acumulativos. La lineage `recovery.expected_*`
de `external_adjudication` no sustituye ese snapshot tras avanzar `cycle_number`.
Si un run histórico quedó congelado con el snapshot del ciclo recuperado tras
avanzar de ciclo, `pr-review continue` (con comentario exacto nuevo) puede
limpiar solo ese freeze obsoleto cuando la lineage lo prueba — recovery
`external_adjudication` directa, o un descendiente `reviewing` con un único
ancestro terminal verificado —; el evento
`pr_review_legacy_cycle_freeze_cleared` registra conteos y números de ciclo, no
IDs de hilo ni rutas de source. No edites `state.json` a mano.
`pr-review status` reporta liveness del worker (`live`/`stale`/`absent`) sin
exponer PID, token ni argv.

`codex/session-runtime.json` registra la captura segura de Fase 10: prefijo del session ID, modelo/reasoning de sesion, origen y tipo de evento permitido. No contiene transcript ni la ruta absoluta del rollout.

En `state.json`, `codex` incluye:

```text
session_model
session_reasoning_effort
review_model
review_reasoning_effort
review_model_source       # session | explicit
review_reasoning_source   # session | explicit
```

En runs nuevos, los valores efectivos de review y sus fuentes quedan congelados en `prepare`. Runs historicos de Fase 9 con valores nulos y sin procedencia siguen siendo legibles por el camino legacy, pero no se reinterpretan como session-derived.

## Iteraciones

La numeracion es estable y empieza en `01`.

| Iteracion | Tipo | Prompt Cursor |
| --- | --- | --- |
| `01` | implementacion inicial | `prompts/cursor-initial.txt` |
| `02` | primera correccion | `prompts/fixes/01.txt` |
| `NN` | correccion | `prompts/fixes/{NN-1}.txt` |

Cada iteracion puede tener:

```text
cursor/iterations/NN/events.jsonl
cursor/iterations/NN/stderr.txt
cursor/iterations/NN/final.txt
cursor/iterations/NN/metadata.json

git/status/NN-before-cursor.txt
git/status/NN-after-cursor.txt
git/cursor-output/NN.json
git/cursor-output/NN.post-normalization.json
git/status/NN-before-staging.txt
git/status/NN-after-staging.txt
git/diffs/NN.stat
git/diffs/NN.name-only.txt
git/diffs/NN.patch

codex/events/NN.jsonl
codex/events/NN.stderr.txt
codex/reviews/NN.json
codex/reviews/NN.md
codex/reviews/NN.metadata.json
```

`git/cursor-output/NN.json` es un fingerprint sensible post-Cursor (hashes y rutas; sin contenidos de archivo). Se captura despues de Cursor y antes de `git add -A`.

`git/cursor-output/NN.post-normalization.json` se captura inmediatamente despues de un `git add -A` exitoso, antes de persistir el patch staged. Permite recuperar fallos parciales de staging cuando el index ya fue normalizado.

`git/cursor-output/NN.usage-limit-failure.json` captura un fingerprint de trabajo parcial tras un fallo clasificado como limite de uso de Cursor (Phase 13). Se usa para validar que el repositorio no cambio antes de `recover`.

`git/cursor-output/NN.usage-limit-adopted.json` registra metadata segura de una adopcion explicita historica cuando el origen carecia de fingerprint contemporaneo. Contiene hashes del stderr protegido, status after-cursor, prompt y fingerprint de contenido capturado en el momento del `recover`, no en el fallo original.

`prompts/cursor-recovery/NN.usage-limit-continuation.txt` es el envelope de continuacion que embebe el prompt exacto previo para retomar el mismo chat en el sucesor.

Los patches staged son snapshots acumulativos del index en esa iteracion, no necesariamente diffs incrementales.

Las correcciones envian a Cursor un envelope operacional que embebe byte a byte el `prompts/fixes/NN.txt` exacto; el envelope auditado vive en `prompts/fixes/NN.execution-envelope.txt`.

## Lineage de recovery

Un sucesor creado por `ai_dev_loop recover` incluye en `state.json` una seccion opcional `recovery`:

```text
source_run_id
source_status                 # failed
source_iteration
recovered_checkpoint          # staging | reviewing | process_review | cursor |
                              # external_adjudication | external_feedback_cursor |
                              # publication_pre_commit
source_staged_patch_sha256    # null para cursor, initial_staging_failed,
                              # external_adjudication y external_feedback_cursor;
                              # requerido para publication_pre_commit
cursor_output_fingerprint_sha256   # requerido para staging
previous_staged_patch_sha256       # requerido para correction_staging_failed; null para initial
legacy_cursor_output_adopted       # opcional; solo correction staging historico
legacy_cursor_usage_limit_adopted  # opcional; adopcion historica de limite de uso Cursor
source_cursor_model                # checkpoint cursor
cursor_model_fallback              # checkpoint cursor; congelado por --cursor-model
source_prompt_path                   # cursor y external_feedback_cursor
source_prompt_sha256               # cursor y external_feedback_cursor
usage_limit_fingerprint_path       # checkpoint cursor
usage_limit_fingerprint_sha256     # checkpoint cursor
continuation_envelope_path         # checkpoint cursor
continuation_envelope_sha256       # checkpoint cursor
expected_eligible_thread_ids       # external_adjudication, external_feedback_cursor y
                                   # publication_pre_commit; IDs, no cuerpos
created_at
runtime_migration             # none | phase9_session_capture
reason_code                   # incluye initial_staging_failed, correction_staging_failed,
                              # cursor_usage_limit, github_adjudication_schema_incompatible,
                              # codex_review_result_artifact_missing,
                              # external_feedback_cursor_not_started,
                              # publication_pre_commit_interrupted
```

`pr-review recover` usa:

- `external_adjudication` cuando Codex rechazo el schema de adjudicacion antes de
  side effects;
- `reviewing` + `codex_review_result_artifact_missing` cuando Cursor/staging
  terminaron pero falta el resultado local `codex/reviews/NN.json`;
- `external_feedback_cursor` + `external_feedback_cursor_not_started` cuando la
  adjudicacion externa ya dejo resultado/prompt accionables pero la iteracion
  fresca de Cursor no llego a empezar. El estado tambien puede registrar
  `github_pr_review.external_cursor_iteration` para esa iteracion monotona;
- `publication_pre_commit` + `publication_pre_commit_interrupted` cuando la
  revision local acepto el staged patch y la publicacion quedo en
  `pre_commit` sin commit (p. ej. `ssh-agent` sin identidad). La elegibilidad
  no usa `last_error`. El fingerprint de publicacion es el sha256 raw del
  patch live (`git diff --cached --binary`) tras equivalencia normalizada
  (CRLF / newline terminal) con `git/diffs/NN.patch` de la ultima iteracion;
  no confundir con `sha256_file` del artefacto. Tras una correccion externa
  aceptada, runs nuevos actualizan `github_pr_review.staged_patch_sha256`
  antes de `publishing_external_fix`. En recovery historico, un hash GPR
  obsoleto puede adoptarse solo en el sucesor cuando esa equivalencia
  normalizada se cumple; el origen permanece inmutable.

El sucesor no republica el trigger `@codex review`. En `reviewing` no reejecuta
Cursor; en `external_feedback_cursor` el `resume` abre Cursor antes de staging;
en `publication_pre_commit` el `resume` publica solamente.

El run origen permanece terminal e inmutable. El sucesor copia snapshots/artefactos necesarios para continuar (plan, prompt, chat, Cursor/git hasta la iteracion recuperada, reviews previos, y el review valido solo si el checkpoint es `process_review`). Los intentos Codex fallidos quedan en el origen.

## Locks

`ai_dev_loop` usa:

- un lock por run bajo el directorio del run;
- un lock por worktree bajo `$XDG_STATE_HOME/ai_dev_loop/repository-locks/`.

Esto previene dos loops mutando el mismo worktree simultaneamente.

## SessionStart records

El hook global guarda metadata minima de sesiones Codex bajo:

```text
$XDG_STATE_HOME/ai_dev_loop/codex-sessions/<session-id>.json
```

No guarda transcript content.
