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

Ejemplo:

```bash
ai_dev_loop prepare \
  --repo-path /path/al/repo \
  --plan-path docs/plans/mi-plan.md \
  --prompt-source-path docs/plans/prompt_mi-plan.txt \
  --codex-session-id "<session-id-exacto>" \
  --output json < docs/plans/prompt_mi-plan.txt
```

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
