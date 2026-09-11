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
- resuelve `cursor.command` y `codex.command` en la terminal que hace submit y
  congela sus rutas absolutas sólo en los artefactos protegidos del run; el YAML
  del repositorio permanece portable y los workers no dependen del `PATH` de systemd.

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

El run abortado queda terminal e inmutable. Repetir el mismo `scheduler submit`
sin `--resubmission-id` reutiliza ese run y devuelve su estado real (`aborted`)
sin accion de `start`. No crea un run nuevo ni recupera la reserva liberada.

## Reenvio fresco tras un run terminal

Para iniciar trabajo nuevo con los mismos inputs congelados tras un abort (u otro
estado terminal), el operador elige un UUID explicito y lo conserva solo el
tiempo necesario para repetir un submit incierto sin duplicar filas:

```bash
RESUBMISSION_ID="$(uuidgen)"

ai_dev_loop scheduler submit \
  --repo-path /path/al/repo \
  --plan-path docs/plans/mi-plan.md \
  --prompt-source-path docs/plans/prompt_mi-plan.txt \
  --controller-session-id "<exact-controller-session-id>" \
  --codex-review-model "<review-model>" \
  --codex-review-reasoning-effort high \
  --resubmission-id "$RESUBMISSION_ID" \
  --output json < docs/plans/prompt_mi-plan.txt
```

Reglas:

- `--resubmission-id` es opcional y solo para un envio intencionalmente distinto;
- reutiliza el mismo UUID para replay idempotente de ese envio fresco;
- el identificador no se persiste ni aparece en status, history, logs o JSON;
- mientras otro run no terminal retenga la reserva del worktree, un segundo
  `--resubmission-id` distinto falla con conflicto activo.

Tras un envio fresco `queued`, autoriza con `scheduler start` como de costumbre.

## Timer systemd (habilitacion explicita)

Instalar, validar o deshabilitar el timer no ocurre automaticamente con `submit` o `tick`:

```bash
ai_dev_loop scheduler timer validate
ai_dev_loop scheduler timer install          # escribe unidades; no habilita
ai_dev_loop scheduler timer install --enable # habilitacion explicita
ai_dev_loop scheduler timer status
ai_dev_loop scheduler timer disable
```

Solo habilita el timer en produccion tras aceptacion manual independiente. Para
los requisitos de WSL/systemd, `linger`, persistencia tras reiniciar la
distribucion, observabilidad, actualizaciones y desinstalacion, sigue
[Timer del scheduler en WSL](timer-systemd-wsl.md).

La unidad de servicio fija un `PATH` acotado con `%h/.local/bin` para localizar el
CLI instalado con `uv tool` desde un entorno systemd minimo, sin shell ni perfiles
interactivos. `scheduler status` y `scheduler list` muestran un prefijo redactado
del reviewer B una vez autenticado y enlazado; antes del binding el prefijo es
`null` y eso es esperado en runs fresh-B.

## Cutover de estado legacy (destructivo)

Elimina unicamente `$XDG_STATE_HOME/ai_dev_loop/runs/` y `pr-review-v2/` tras confirmacion explicita. No toca `engine.sqlite3`, `artifacts/` ni otros datos del scheduler.

```bash
ai_dev_loop scheduler cutover cleanup --dry-run --confirm delete-legacy-state
ai_dev_loop scheduler cutover cleanup --confirm delete-legacy-state
```

Requiere aceptacion humana independiente antes de ejecutarlo contra un state root real.

## Runs historicos

Los runs legacy bajo `runs/` y los motores `pr-review-v2` ya no tienen comandos de ejecucion. La accion segura es un `scheduler submit` fresco con identidad controller y modelo de review explicitos. Consulta [Desinstalacion y limpieza](desinstalacion-limpieza.md) para retirar estado legacy de forma controlada.
