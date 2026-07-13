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

## `launch`

```bash
ai_dev_loop launch <run-id> --controller-session-id TEXT [--repo-path PATH] \
  [--update-tools|--skip-tool-update] [--allow-incompatible-tools] [--output text|json]
```

Lanza un run A/B preparado en un worker local detachado. Requiere el controller session ID exacto del prepare. Es idempotente si el worker ya esta vivo. No sustituye `start` para runs legacy sin controller.

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

## `recover`

```bash
ai_dev_loop recover <run-id> [--dry-run] [--adopt-current-cursor-output] [--cursor-model TEXT] [--output text|json]
```

Analiza un run `failed` y, si es elegible, crea un run sucesor `interrupted` sin mutar el origen ni el repositorio.

Checkpoints: `reviewing`, `process_review`, `staging` (Cursor completo / staging incompleto), y `cursor` (limite de uso de Cursor con turno incompleto).

- `--dry-run`: solo reporta elegibilidad, checkpoint, blockers y migracion de runtime.
- `--adopt-current-cursor-output`: atestacion explicita para fallos de staging historicos sin fingerprint post-Cursor, o para fallos historicos de limite de uso de Cursor sin fingerprint contemporaneo, cuando el status actual coincide con `NN-after-cursor.txt`. Para checkpoint `cursor` tambien requiere `--cursor-model`.
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
