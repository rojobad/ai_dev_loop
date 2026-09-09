# Ejecutar runs con el scheduler

Phase 17.7 retiro los comandos legacy (`prepare`, `start`, `resume`, `recover`, `launch`, `extend`, `abort` de nivel superior, `pr-review`, etc.). El flujo soportado es exclusivamente el scheduler central.

## Comandos principales

```bash
ai_dev_loop scheduler submit
ai_dev_loop scheduler start <run-id> --controller-session-id <exact-controller-session-id>
ai_dev_loop scheduler tick
ai_dev_loop scheduler status <run-id>
ai_dev_loop scheduler list
ai_dev_loop scheduler history <run-id>
ai_dev_loop scheduler abort <run-id>
ai_dev_loop controller status --controller-session-id <exact-controller-session-id> --repo-path /path/al/repo
```

## `scheduler submit`

Congela un run `queued` en el ledger central (`engine.sqlite3`) y artefactos protegidos bajo `$XDG_STATE_HOME/ai_dev_loop/artifacts/`. No crea `state.json` legacy, no lanza agentes y no muta el repositorio objetivo.

```bash
ai_dev_loop scheduler submit \
  --repo-path /path/al/repo \
  --plan-path docs/plans/mi-plan.md \
  --prompt-source-path docs/plans/prompt_mi-plan.txt \
  --controller-session-id "<exact-controller-session-id>" \
  --codex-review-model "<review-model>" \
  --codex-review-reasoning-effort high \
  --output json < docs/plans/prompt_mi-plan.txt
```

Requisitos:

- lee el prompt exacto desde stdin;
- `--controller-session-id`, `--codex-review-model` y `--codex-review-reasoning-effort` son obligatorios;
- no admite `--codex-session-id` (reviewer B se crea en el primer review);
- congela inputs inmutables; la admision del worktree ocurre en el primer tick.

## `scheduler start`

Autoriza un run `queued` desde la misma sesion controller A usada en submit:

```bash
ai_dev_loop scheduler start <run-id> \
  --controller-session-id "<exact-controller-session-id>"
```

## `scheduler tick`

Ejecuta un paso acotado del scheduler: preflight, Cursor, staging, review o correccion segun el checkpoint actual. Durante desarrollo puedes invocarlo manualmente; en produccion puede hacerlo un timer systemd empaquetado (habilitacion explicita y separada).

```bash
ai_dev_loop scheduler tick
```

## Observabilidad y control

```bash
ai_dev_loop scheduler status <run-id>
ai_dev_loop scheduler list
ai_dev_loop scheduler history <run-id>
ai_dev_loop controller status \
  --controller-session-id "<exact-controller-session-id>" \
  --repo-path /path/al/repo
```

## `scheduler abort`

Cancela un run activo de forma no destructiva: preserva artefactos, prompts, patches y cambios staged del repositorio.

```bash
ai_dev_loop scheduler abort <run-id>
```

## Timer systemd (habilitacion explicita)

Instalar, validar o deshabilitar el timer no ocurre automaticamente con `submit` o `tick`:

```bash
ai_dev_loop scheduler timer validate
ai_dev_loop scheduler timer install          # escribe unidades; no habilita
ai_dev_loop scheduler timer install --enable # habilitacion explicita
ai_dev_loop scheduler timer status
ai_dev_loop scheduler timer disable
```

Solo habilita el timer en produccion tras aceptacion manual independiente.

## Cutover de estado legacy (destructivo)

Elimina unicamente `$XDG_STATE_HOME/ai_dev_loop/runs/` y `pr-review-v2/` tras confirmacion explicita. No toca `engine.sqlite3`, `artifacts/` ni otros datos del scheduler.

```bash
ai_dev_loop scheduler cutover cleanup --dry-run --confirm delete-legacy-state
ai_dev_loop scheduler cutover cleanup --confirm delete-legacy-state
```

Requiere aceptacion humana independiente antes de ejecutarlo contra un state root real.

## Runs historicos

Los runs legacy bajo `runs/` y los motores `pr-review-v2` ya no tienen comandos de ejecucion. La accion segura es un `scheduler submit` fresco con identidad controller y modelo de review explicitos. Consulta [Desinstalacion y limpieza](desinstalacion-limpieza.md) para retirar estado legacy de forma controlada.
