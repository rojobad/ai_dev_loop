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

## `prepare`

```bash
ai_dev_loop prepare [OPTIONS]
```

Lee el prompt exacto desde stdin y crea un run preparado.

Opciones principales:

```text
--config-path PATH
--project-name TEXT
--repo-path PATH
--plan-path PATH
--prompt-source-path PATH
--codex-session-id TEXT
--controller-session-id TEXT
--cursor-command TEXT
--cursor-model TEXT
--cursor-output-format TEXT
--codex-command TEXT
--codex-review-model TEXT
--codex-review-reasoning-effort TEXT
--review-skill TEXT
--max-review-iterations INTEGER
--cursor-timeout-minutes INTEGER
--codex-timeout-minutes INTEGER
--output [text|json]
```

`--codex-review-model` y `--codex-review-reasoning-effort` son overrides opcionales e independientes. Si no se pasan y YAML omite/usa `null`, `prepare` captura el campo correspondiente de la sesion exacta. No hay flag para limpiar un override del YAML; configura herencia antes de `prepare`.

`--controller-session-id` es opcional. En el flujo A/B (skill handoff) es el session ID exacto del controller A y debe diferir de `--codex-session-id` (reviewer B). Con controller: `requires_codex_exit` es `false`, `reviewer_must_remain_inactive` es `true` y `launch_command` queda disponible. Sin controller: comportamiento legacy con `requires_codex_exit: true` y `start`.

Ejemplo A/B:

```bash
ai_dev_loop prepare \
  --repo-path /path/al/repo \
  --plan-path docs/plans/mi-plan.md \
  --prompt-source-path docs/plans/prompt_mi-plan.txt \
  --codex-session-id "<exact-reviewer-session-id>" \
  --controller-session-id "<exact-controller-session-id>" \
  --output json < docs/plans/prompt_mi-plan.txt
```

Ejemplo legacy:

```bash
ai_dev_loop prepare \
  --repo-path /path/al/repo \
  --plan-path docs/plans/mi-plan.md \
  --prompt-source-path docs/plans/prompt_mi-plan.txt \
  --codex-session-id "<exact-codex-session-id>" \
  --output json < docs/plans/prompt_mi-plan.txt
```

## `scheduler submit`

```bash
ai_dev_loop scheduler submit [OPTIONS]
```

Lee el prompt exacto desde stdin y congela un run A/B `queued` en el ledger central
(`engine.sqlite3`) mas artefactos protegidos bajo `$XDG_STATE_HOME/ai_dev_loop/artifacts/`.
No crea `state.json`, no lanza agentes, no ejecuta preflight ni muta el repositorio
objetivo.

Opciones principales: las mismas rutas/sesiones/overrides que `prepare`, con
`--controller-session-id` obligatorio y distinto de `--codex-session-id`.

Salida redactada. La siguiente accion segura documentada es
`ai_dev_loop scheduler start <run-id>` (implementada en Phase 17.2).

Tambien existen `ai_dev_loop scheduler status <run-id>` y `ai_dev_loop scheduler list`
como proyecciones read-only del ledger central.

## `launch`

```bash
ai_dev_loop launch <run-id> --controller-session-id TEXT [--repo-path PATH] \
  [--update-tools|--skip-tool-update] [--allow-incompatible-tools] [--output text|json]
```

Lanza un run A/B preparado en un worker local detachado. Tambien reanuda el checkpoint `waiting_for_cursor_fix` despues de un `extend`. Requiere el controller session ID exacto del prepare. Es idempotente si el worker ya esta vivo. No sustituye `start` para runs legacy sin controller.

Los flags de compatibilidad de herramientas son los mismos que en `start`/`resume`, pero el worker es siempre non-interactive: nunca pregunta. `--update-tools` autoriza updaters; `--skip-tool-update` (o la omision) no actualiza; `--allow-incompatible-tools` permite continuar ante incompatibilidad confirmada. `--update-tools` y `--skip-tool-update` son mutuamente excluyentes.

## `controller status`

```bash
ai_dev_loop controller status \
  --controller-session-id TEXT \
  --repo-path PATH \
  [--run-id TEXT] \
  [--include-terminal] \
  [--output text|json]
```

Lookup read-only por controller session ID y repositorio. Ante ambiguedad (0 o N matches) no elige por timestamp; usa `--run-id` para desambiguar. Incluye liveness del worker en la respuesta.

## `github doctor`

```bash
ai_dev_loop github doctor [--repo-path PATH] [--output text|json]
```

Verifica disponibilidad de `gh`, autenticacion (cuenta redactada), schemas GitHub y, con `--repo-path`, si `github.enabled` y el remote SSH estan listos. No imprime tokens.

## `pr-review`

Ciclo opt-in post-PR con motor durable SQLite (requiere `pr_review_v2.enabled: true` en
`ai_dev_loop.yaml`). Los runs v1 legacy (`RunState.github_pr_review`) y subcomandos
retirados (`continue`, `recover`, `set-cursor-model`) ya no estan soportados.

```bash
ai_dev_loop pr-review create <source-run-id> [--config-path PATH]
ai_dev_loop pr-review prepare --repo OWNER/REPO --pr N \
  --codex-session-id UUID --plan PATH --prompt PATH \
  [--cursor-chat-id ID] [--review-model MODEL] [--repo-path PATH] [--config-path PATH]
ai_dev_loop pr-review start <run-id>
ai_dev_loop pr-review status <run-id> [--output text|json]
ai_dev_loop pr-review history <run-id> [--limit N] [--newest] [--output text|json]
ai_dev_loop pr-review resume <run-id> [--confirm-user-continuation] [--recover-mixed-adjudication]
ai_dev_loop pr-review abort <run-id>
```

Origenes:

- **`create` (source_run):** congela un `PreparedState` desde un run A/B
  `completed` / `completed_with_residual_risk` con plan/prompt/patch verificados.
- **`prepare` (existing_pr):** adopta un PR ya abierto con discovery read-only.
  Exige checkout local alineado con head branch/SHA, plan/prompt confinados al repo,
  y sesion Codex exacta.

Contrato de seguridad:

- `create` / `prepare` solo validan, congelan inputs y crean/reusan un
  `PreparedState` durable. No arrancan workers/agentes, no escriben GitHub, no
  hacen commit/push ni llamadas de modelo.
- `start <run-id>` es la unica puerta a efectos externos: aplica el evento durable
  y luego lanza/reusa el supervisor detached (`python -m
  ai_dev_loop.pr_review_v2_supervisor_worker`) con metadata de ownership. Si el
  spawn falla, reporta `spawn_failed` y no inventa un proceso vivo.
- `resume` en `waiting_for_user` tiene tres formas distintas segun el checkpoint:
  - **`resume` ordinario** cuando hay un `post_thread_reply` diferido ya persistido,
    validado y pendiente (cola no vacia): repara/lanza el supervisor para despachar
    ese efecto ya autorizado por `start`; no emite `UserContinuationRequested` ni
    duplica filas de efecto.
  - **`resume --confirm-user-continuation`** solo despues de que todos los replies
    diferidos hayan completado (cola vacia, sin efecto activo); persiste evidencia
    de operador protegida y programa la siguiente observacion; no dispara timers
    futuros antes de tiempo.
  - **`resume --recover-mixed-adjudication`** solo para lotes mixtos legacy sin
    `fix_prompt_ref` (actionable + reply): re-adjudica sobre el snapshot congelado
    original sin escribir en GitHub ni reescribir SQLite historico.
  Si un reply esta claimed, en retry, malformado, stale, o hay supervisor/lease vivo,
  `status` debe decir `wait-until`; no repare SQLite manualmente.
  Repara solo el supervisor cuando el estado ya es activo fuera de esos checkpoints.
- `status` marca acciones de resume solo cuando el run es no terminal, el
  supervisor no esta vivo y el lease anterior ya expiro. Un claim mutante
  expirado se reconcilia antes de cualquier reintento de escritura.
- `abort` persiste el abort durable antes de senalar procesos Cursor/Codex hijos
  con ownership exacta y, despues, el supervisor owned.
- `status` / `history` son acotados y redactados (sin prompts, patches, bodies,
  tokens, session IDs completos, argv, PID/PGID ni environments).

Resiliencia diferida (Phase 16.8): ver `PHASE_16_8_DEFERRED_ISSUES.md`. No hay
`pr-review recover` publico ni migracion de runs v1.

## `start`

```bash
ai_dev_loop start <run-id> [--update-tools|--skip-tool-update] [--allow-incompatible-tools]
```

Ejecuta el loop automatizado completo para un run preparado.

- `--update-tools`: autoriza ejecutar el updater oficial de cada CLI WSL incompatible, sin prompt.
- `--skip-tool-update`: nunca ejecuta updaters.
- `--allow-incompatible-tools`: permite continuar pese a incompatibilidad confirmada.

`--update-tools` y `--skip-tool-update` son mutuamente excluyentes. Sin flags, un TTY puede preguntar por cada herramienta incompatible y usa `no` por defecto. Non-TTY nunca pregunta y falla ante incompatibilidad salvo autorizacion explicita para actualizar o continuar.

## `resume`

```bash
ai_dev_loop resume <run-id> [--update-tools|--skip-tool-update] [--allow-incompatible-tools]
```

Continua un run checkpointed o interrumpido si el siguiente paso seguro puede derivarse de estado y artefactos.

Aplica la misma politica de compatibilidad y updates que `start`. Tras un update se vuelven a consultar version y catalogos (`agent models`, `codex debug models`). Un abort pendiente tiene prioridad.

## `extend`

```bash
ai_dev_loop extend <run-id> --additional-review-iterations INTEGER [--output text|json]
```

Solo aplica a un run detenido en `max_iterations_reached`. Requiere un entero positivo, aumenta ese presupuesto sin crear un run nuevo y restaura el checkpoint `waiting_for_cursor_fix` con el fix prompt exacto de la ultima review. Conserva el chat de Cursor, la sesion revisora Codex, los cambios staged y todos los artefactos.

Despues, en un run legacy usa `ai_dev_loop resume <run-id>`. En un run A/B deja B inactiva y usa `ai_dev_loop launch <run-id> --controller-session-id <exact-controller-session-id>`: el worker detecta el checkpoint y ejecuta `resume` de forma detachada.

## `recover`

```bash
ai_dev_loop recover <run-id> [--dry-run] [--adopt-current-cursor-output] [--cursor-model TEXT] [--output text|json]
```

Analiza un run `failed` y, si es elegible, crea un run sucesor `interrupted` sin mutar el origen ni el repositorio.

Checkpoints: `reviewing`, `process_review`, `staging` (Cursor completo / staging incompleto; `initial_staging_failed` o `correction_staging_failed`), y `cursor` (limite de uso de Cursor con turno incompleto).

- `--dry-run`: solo reporta elegibilidad, checkpoint, blockers y migracion de runtime.
- `--adopt-current-cursor-output`: atestacion explicita para fallos de staging historicos de **correccion** sin fingerprint post-Cursor, o para fallos historicos de limite de uso de Cursor sin fingerprint contemporaneo, cuando el status actual coincide con `NN-after-cursor.txt`. No aplica a `initial_staging_failed`. Para checkpoint `cursor` tambien requiere `--cursor-model`.
- `--cursor-model`: obligatorio para checkpoint `cursor`. Congela el modelo fallback solicitado en el sucesor (por ejemplo `auto`). Invalido para checkpoints `staging`/`reviewing`/`process_review`. No es un default implicito ni se lee desde YAML.
- Sin `--dry-run`: crea o reutiliza el sucesor y imprime `resume_command`.
- No lanza agentes ni updaters; pasa `--update-tools` a `resume` si hace falta.
- JSON incluye `recovery_run_id`, `checkpoint`, `runtime_migration`, `reused_existing_successor` y campos de fingerprint/adopcion cuando aplican.

TTY vs non-TTY:

- tras un fallo `cursor_usage_limit` en `start`/`resume`, un TTY puede ofrecer `recover --cursor-model auto` y continuar; la respuesta por defecto es no;
- non-TTY imprime el comando explicito y no cambia de modelo ni crea sucesor automaticamente.

Ejemplo de limite de uso:

```bash
ai_dev_loop recover --dry-run <failed-run-id> --cursor-model auto
ai_dev_loop recover <failed-run-id> --cursor-model auto
ai_dev_loop recover --dry-run <failed-run-id> --adopt-current-cursor-output --cursor-model auto
ai_dev_loop recover <failed-run-id> --adopt-current-cursor-output --cursor-model auto
ai_dev_loop resume <recovery-run-id>
```

## `abort`

```bash
ai_dev_loop abort <run-id>
```

Solicita cancelacion no destructiva de un run no terminal.

## `status`

```bash
ai_dev_loop status <run-id> [--output text|json]
```

Muestra estado resumido y siguiente accion segura.

## `list`

```bash
ai_dev_loop list [--project PROJECT] [--status STATUS] [--output text|json]
```

Lista runs encontrados bajo XDG state.

## `logs`

```bash
ai_dev_loop logs <run-id> [--component COMPONENT]
```

Componentes soportados:

```text
ai_dev_loop
cursor
codex
```

## `inspect`

```bash
ai_dev_loop inspect <run-id> [--output text|json] [--show-prompts]
```

Lista artefactos y resumen de iteraciones. `--show-prompts` imprime contenido sensible.

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
