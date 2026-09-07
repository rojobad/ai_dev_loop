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
    └── active-process.json
```

No todos los archivos existen en todos los estados. Por ejemplo, `prompts/fixes/NN.txt` existe solo si Codex reporto findings en review `NN`.

## PR-review v2 (SQLite + artefactos XDG)

Tras Phase 16.9, el ciclo `ai_dev_loop pr-review` usa un motor durable separado del
`state.json` del run A/B local. Los runs v1 con `RunState.github_pr_review` y el lock
`locks/pr-review-worker.json` ya no aplican.

Layout bajo `$XDG_STATE_HOME/ai_dev_loop/pr-review-v2/`:

```text
engine.sqlite3                 # autoridad: PreparedState, eventos, claims, leases
artifacts/
  runs/<sha256(run_id)>/       # contenido protegido content-addressed
    writes/<kind>/<sha256>.json
    ...                        # prompts, patches, resultados Codex, metadata
```

- `status` / `history` leen SQLite y rutas relativas redactadas; no exponen cuerpos
  completos, tokens, session IDs ni argv.
- El supervisor detached registra ownership fuera del repositorio objetivo.
- `abort` persiste solicitud durable antes de senalar procesos owned.

Resiliencia publica (`pr-review recover`, migracion v1): no implementada; ver
`PHASE_16_8_DEFERRED_ISSUES.md`.

## Scheduler central (Phase 17.1+)

El scheduler A/B local usa una autoridad SQLite generica separada del run legacy
`state.json` y del motor `pr-review-v2`:

```text
$XDG_STATE_HOME/ai_dev_loop/
├── engine.sqlite3              # autoridad: runs queued/authorized, eventos, reservas
└── artifacts/
    runs/<sha256(run_id)>/      # snapshots inmutables verificados por hash
        plan/plan.md
        prompts/cursor-initial.txt
        effective-config.yaml
        source-config.yaml
        codex/fresh-reviewer-input.json     # v2: modelo/reasoning congelados en prepare/submit
        codex/fresh-reviewer-binding.json   # evidencia de bootstrap read-only (post-review)
        codex/fresh-reviewer-bootstrap-uncertainty.json  # bloqueo durable si la identidad es ambigua
        codex/session-runtime.json          # v1 legacy session-bound solamente
        git/baseline-status.txt
```

- `ai_dev_loop scheduler submit` congela un run `queued` sin lanzar agentes, probes,
  Git ni systemd. Requiere `--controller-session-id`, `--codex-review-model` y
  `--codex-review-reasoning-effort`; no acepta `--codex-session-id`. El contexto
  v2 persiste `codex/fresh-reviewer-input.json` en el manifiesto; la evidencia de
  bootstrap (`codex/fresh-reviewer-binding.json`) se escribe solo en el primer review.
- Los submits v1 session-bound historicos permanecen read-only en el ledger; la
  accion segura es un submit v2 fresco, no una conversion automatica.
- `scheduler status` y `scheduler list` son proyecciones read-only con IDs redactados.
- `scheduler start`, `scheduler tick` y la ejecucion de agentes llegan en fases
  posteriores; Phase 17.1 se detiene en el limite `queued`.

## Lineage de recovery

Un sucesor creado por `ai_dev_loop recover` incluye en `state.json` una seccion opcional `recovery`:

```text
source_run_id
source_status                 # failed
source_iteration
recovered_checkpoint          # staging | reviewing | process_review | cursor
source_staged_patch_sha256    # null para cursor e initial_staging_failed
cursor_output_fingerprint_sha256   # requerido para staging
previous_staged_patch_sha256       # requerido para correction_staging_failed; null para initial
legacy_cursor_output_adopted       # opcional; solo correction staging historico
legacy_cursor_usage_limit_adopted  # opcional; adopcion historica de limite de uso Cursor
source_cursor_model                # checkpoint cursor
cursor_model_fallback              # checkpoint cursor; congelado por --cursor-model
source_prompt_path                   # checkpoint cursor
source_prompt_sha256               # checkpoint cursor
usage_limit_fingerprint_path       # checkpoint cursor
usage_limit_fingerprint_sha256     # checkpoint cursor
continuation_envelope_path         # checkpoint cursor
continuation_envelope_sha256       # checkpoint cursor
created_at
runtime_migration             # none | phase9_session_capture
reason_code                   # incluye initial_staging_failed, correction_staging_failed,
                              # cursor_usage_limit, codex_review_result_artifact_missing
```

`ai_dev_loop recover` usa checkpoints `staging`, `reviewing`, `process_review` y
`cursor` del loop local A/B.

El sucesor del loop local no republica triggers GitHub. El run origen permanece
terminal e inmutable. El sucesor copia snapshots/artefactos necesarios para
continuar (plan, prompt, chat, Cursor/git hasta la iteracion recuperada, reviews
previos, y el review valido solo si el checkpoint es `process_review`). Los
intentos Codex fallidos quedan en el origen.

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
