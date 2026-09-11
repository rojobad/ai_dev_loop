# Estado y artefactos

`ai_dev_loop` guarda estado fuera del repositorio objetivo. Esto evita mezclar auditoria del orquestador con el codigo que Cursor modifica.

## Autoridad del scheduler (Phase 17.7)

El flujo soportado persiste estado durable en:

```text
$XDG_STATE_HOME/ai_dev_loop/engine.sqlite3
$XDG_STATE_HOME/ai_dev_loop/artifacts/
```

Los comandos `scheduler status`, `scheduler list` y `scheduler history` leen solo
ese ledger central.

## Layout del scheduler

```text
$XDG_STATE_HOME/ai_dev_loop/
├── engine.sqlite3              # autoridad: runs, eventos, leases, attempts
├── artifacts/
│   └── runs/<sha256(run_id)>/  # snapshots inmutables verificados por hash
│       plan/plan.md
│       prompts/cursor-initial.txt
│       effective-config.yaml
│       source-config.yaml
│       codex/fresh-reviewer-input.json
│       codex/fresh-reviewer-binding.json
│       git/admission-status.txt
├── repository-locks/
├── codex-sessions/             # metadata minima del hook SessionStart
└── locks/                      # locks de cutover y otros control-plane
```

- `scheduler submit` congela un run `queued` sin lanzar agentes ni mutar el repo.
- `scheduler start` autoriza el run desde la sesion controller.
- `scheduler tick` ejecuta preflight, Cursor, staging, review y correcciones segun el checkpoint.
- `require_clean_worktree` se congela en submit y se aplica en la admision one-shot del primer tick.

## Estado legacy retirado

Los arboles historicos siguientes ya no tienen comandos de ejecucion:

```text
$XDG_STATE_HOME/ai_dev_loop/runs/<project>/<run-id>/   # state.json legacy
$XDG_STATE_HOME/ai_dev_loop/pr-review-v2/              # motor PR-review v2
```

Eliminalos solo con `scheduler cutover cleanup --confirm delete-legacy-state` tras
aceptacion humana independiente (ver [Desinstalacion y limpieza](desinstalacion-limpieza.md)).

El layout legacy `runs/` incluia `state.json`, `cursor/`, `codex/`, `git/`, etc.
Esa informacion ya no es la autoridad operativa; conservala solo para auditoria
manual hasta el cutover.

## Recuperacion

No existe `recover` publico en el scheduler central. Ante fallos:

- usa `scheduler status` y `scheduler history` para el checkpoint durable;
- `scheduler abort` cancela sin borrar artefactos ni cambios staged;
- para trabajo nuevo tras un run terminal, `scheduler submit` con
  `--resubmission-id <uuid>` e identidad controller y modelo de review explicitos;
  repetir submit sin esa opcion reutiliza el run terminal existente.

Los contratos de recovery legacy (`recover`, sucesores `interrupted`, checkpoints
`cursor`/`staging`) aplicaban solo al motor `runs/` retirado.

## Locks

`ai_dev_loop` usa:

- locks del scheduler en SQLite (tick lease, capacity, attempts);
- un lock por worktree bajo `$XDG_STATE_HOME/ai_dev_loop/repository-locks/`;
- `locks/cutover.cleanup.lock` durante cleanup destructivo legacy.

## SessionStart records

El hook global guarda metadata minima de sesiones Codex bajo:

```text
$XDG_STATE_HOME/ai_dev_loop/codex-sessions/<session-id>.json
```

No guarda transcript content.
