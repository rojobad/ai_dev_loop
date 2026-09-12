# Referencia CLI

Comando raiz:

```bash
ai_dev_loop [OPTIONS] COMMAND [ARGS]...
```

Opciones globales:

```text
--version
--help
```

## `scheduler submit`

```bash
ai_dev_loop scheduler submit [OPTIONS]
```

Lee el prompt exacto desde stdin y congela un run `queued` en el ledger central
(`engine.sqlite3`) mas artefactos protegidos bajo `$XDG_STATE_HOME/ai_dev_loop/artifacts/`.
No crea `state.json` legacy, no lanza agentes, no ejecuta preflight ni muta el repositorio
objetivo.

Opciones principales:

```text
--config-path PATH
--project-name TEXT
--repo-path PATH
--plan-path PATH
--prompt-source-path TEXT
--controller-session-id TEXT   (opcional; proveniencia A)
--codex-review-model TEXT      (obligatorio)
--codex-review-reasoning-effort TEXT (obligatorio)
--cursor-command TEXT
--cursor-model TEXT
--review-skill TEXT
--max-review-iterations INTEGER
--cursor-timeout-minutes INTEGER
--codex-timeout-minutes INTEGER
--resubmission-id UUID   (opcional; envio fresco idempotente tras un run terminal)
--output [text|json]
```

No admite `--codex-session-id`. Repetir submit sin `--resubmission-id` reutiliza
el run existente y reporta su `state_kind` real (por ejemplo `aborted` sin accion
de `start`). Tras un run terminal, usa `--resubmission-id` con un UUID elegido
por el operador para crear un run `queued` distinto; reutiliza el mismo UUID
solo para replay idempotente de ese envio. El primer review del scheduler crea exactamente un
reviewer B con `codex exec` en `--sandbox read-only`; los reviews posteriores reanudan
esa misma sesion con `codex exec resume` (nunca `--last` ni un segundo B).

Ejemplo:

```bash
ai_dev_loop scheduler submit \
  --repo-path /path/al/repo \
  --plan-path docs/plans/mi-plan.md \
  --prompt-source-path docs/plans/prompt_mi-plan.txt \
  --codex-review-model "<review-model>" \
  --codex-review-reasoning-effort high \
  --output json < docs/plans/prompt_mi-plan.txt
```

## `scheduler sequence prepare` / `sequence status`

```bash
ai_dev_loop scheduler sequence prepare --manifest /path/to/sequence.yaml [OPTIONS]
ai_dev_loop scheduler sequence status <sequence-id> [--output text|json]
```

Phase 20.1 congela una definicion lineal inmutable de 2 a 32 fases sin reservar el
repositorio, sin invocar Git y sin crear filas en `scheduler_runs`. Cada fase congela
plan, prompt, configuracion efectiva, rutas ejecutables, limites de workflow, modelo y
reasoning de review, y (para fases no finales) el mensaje de commit intermedio. Los
artefactos viven bajo `$XDG_STATE_HOME/ai_dev_loop/artifacts/sequences/`.

`scheduler sequence start` existe solo como placeholder discoverable y termina con
error hasta Phase 20.2. La accion segura tras `prepare` es
`scheduler sequence status <sequence-id>`.

Opciones de repositorio, `--config-path`, `--controller-session-id`, `--resubmission-id`
y overrides globales siguen el contrato de `scheduler submit`. Cada fase del manifest
debe declarar `codex.review_model` y `codex.review_reasoning_effort`; la fase final no
puede incluir `commit_message`.

## `scheduler start` / `tick` / `status` / `list` / `abort` / `history` / `timeline`

```bash
ai_dev_loop scheduler start <run-id>
ai_dev_loop scheduler tick
ai_dev_loop scheduler status <run-id> [--output text|json]
ai_dev_loop scheduler list [--output text|json]
ai_dev_loop scheduler abort <run-id> [--output text|json]
ai_dev_loop scheduler history <run-id> [--limit N] [--order oldest|newest] [--output text|json]
ai_dev_loop scheduler timeline <run-id> [--limit N] [--order oldest|newest] [--output text|json]
```

`scheduler abort` persiste primero la cancelacion durable, invalida effects/timers/claims
pendientes y no borra artefactos ni cambios staged del repositorio objetivo.

`scheduler history` devuelve eventos acotados y redactados.

`scheduler status` y `scheduler list` exponen `review_iterations_completed` y
`max_review_iterations` segun el presupuesto de reviews del run.

`scheduler timeline` devuelve una tabla acotada de intentos Cursor/Codex por
iteracion: fase, ordinal de reintento, estado seguro, marcas de tiempo durables
y `observed_duration_seconds` solo cuando existen `launch_requested_at` y
`completed_at`. No incluye cola, preflight, IDs internos, artefactos ni salidas
raw. Limite por defecto 50; maximo duro 200. Valores mayores se truncan y
`truncated` indica overflow.

## `scheduler cutover cleanup`

```bash
ai_dev_loop scheduler cutover cleanup --confirm delete-legacy-state [--dry-run] [--output text|json]
```

Elimina solo `$XDG_STATE_HOME/ai_dev_loop/runs/` y `pr-review-v2/` tras validar el state
root, rechazar symlinks, comprobar que no hay trabajo scheduler activo y adquirir
coordinacion contra ticks concurrentes.

En modo JSON, el anuncio de rutas exactas va a stderr; stdout contiene un unico documento JSON.

## `scheduler timer`

```bash
ai_dev_loop scheduler timer validate [--output text|json]
ai_dev_loop scheduler timer install [--enable] [--output text|json]
ai_dev_loop scheduler timer status [--output text|json]
ai_dev_loop scheduler timer disable [--output text|json]
```

`install` escribe unidades empaquetadas bajo `~/.config/systemd/user/` y recarga
`systemctl --user`. `--enable` es explicito; `submit` y `tick` no habilitan timers.

La unidad de servicio empaquetada invoca `ai_dev_loop scheduler tick` mediante
`/usr/bin/env` con un `PATH` acotado que incluye `%h/.local/bin` (instalacion
habitual con `uv tool`) mas los directorios binarios del sistema. No ejecuta un
shell ni lee archivos de perfil interactivos.

La configuración real en WSL (systemd, `linger`, habilitación, actualización,
verificación y deshabilitación) se documenta en
[Timer del scheduler en WSL](../operacion/timer-systemd-wsl.md).

## `controller status`

```bash
ai_dev_loop controller status \
  --repo-path PATH \
  (--run-id TEXT | --controller-session-id TEXT) \
  [--include-terminal] \
  [--output text|json]
```

Lookup read-only. Con `--run-id` y `--repo-path` basta; no requiere A. Con
`--controller-session-id` se conserva el descubrimiento legacy por proveniencia A.
Sin ninguno de los dos selectores, falla con un error de validacion. Ante
ambiguedad (0 o N matches legacy) no elige por timestamp; usa `--run-id`.

## `doctor`

```bash
ai_dev_loop doctor [--repo PATH] [--output text|json]
```

Verifica entorno local y, opcionalmente, configuracion de un repositorio objetivo. Es read-only.

## `config validate`

```bash
ai_dev_loop config validate [--repo PATH] [--config-path PATH] [--output text|json]
```

Valida `ai_dev_loop.yaml`.

## `integrations install`

```bash
ai_dev_loop integrations install [OPTIONS]
```

Opciones:

```text
--output [text|json]
--target [wsl-cli|codex-desktop-wsl]
--windows-codex-home PATH
--wsl-distro TEXT
--wsl-hook-python TEXT
--wsl-hook-script-path PATH
--install-session-bridge
```

## `integrations uninstall`

```bash
ai_dev_loop integrations uninstall [OPTIONS]
```

Opciones:

```text
--output [text|json]
--target [wsl-cli|codex-desktop-wsl]
--windows-codex-home PATH
--wsl-distro TEXT
--wsl-hook-python TEXT
--wsl-hook-script-path PATH
```

## `integrations status`

```bash
ai_dev_loop integrations status [OPTIONS]
```

Opciones:

```text
--output [text|json]
--target [wsl-cli|codex-desktop-wsl]
--windows-codex-home PATH
--wsl-distro TEXT
--wsl-hook-python TEXT
--wsl-hook-script-path PATH
```

## `integrations sessions`

```bash
ai_dev_loop integrations sessions install [--output text|json] [--windows-codex-home PATH] [--wsl-codex-home PATH]
ai_dev_loop integrations sessions status  [--output text|json] [--windows-codex-home PATH] [--wsl-codex-home PATH]
ai_dev_loop integrations sessions list    [--output text|json] [--windows-codex-home PATH] [--wsl-codex-home PATH] [--desktop-sessions-dir PATH] [--limit INTEGER]
ai_dev_loop integrations sessions remove  [--output text|json] [--wsl-codex-home PATH]
```

Gestiona el symlink seguro `sessions/from-desktop`.

## Comandos retirados (Phase 17.7)

Los siguientes comandos ya no existen en la CLI publica:

- `prepare`, `start`, `resume`, `recover`, `extend`, `launch`
- `abort`, `status`, `list`, `logs`, `inspect` de nivel superior
- `pr-review` y `github doctor`

La accion segura para trabajo nuevo es `scheduler submit` + `scheduler start` + `scheduler tick`
(o timer habilitado explicitamente). Para retirar estado legacy, usa
`scheduler cutover cleanup` solo tras aceptacion humana independiente.
