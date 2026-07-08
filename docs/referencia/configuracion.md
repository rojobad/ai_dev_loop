# Referencia de configuracion

Archivo:

```text
ai_dev_loop.yaml
```

Version soportada:

```yaml
version: 1
```

## Schema

```yaml
version: 1

project:
  name: my-project

cursor:
  command: agent
  model: composer-2.5-fast
  output_format: stream-json
  force: true
  trust_workspace: true
  sandbox: disabled

codex:
  command: codex
  review_model: gpt-5.5
  review_skill: review-staged-cursor-execution
  sandbox: workspace-write

workflow:
  max_review_iterations: 3
  require_clean_worktree: true
  stage_mode: all
  cursor_timeout_minutes: 90
  codex_timeout_minutes: 90

prompt:
  directory: docs/plans
  filename_template: prompt_{plan_stem}.txt
```

## `project`

| Campo | Requerido | Descripcion |
| --- | --- | --- |
| `name` | Si | Slug del proyecto. Debe ser minuscula con guiones opcionales. |

Ejemplos validos:

```text
parish360-platform
my-project
```

## `cursor`

| Campo | Default | Descripcion |
| --- | --- | --- |
| `command` | `agent` | Ejecutable Cursor CLI. |
| `model` | `composer-2.5-fast` | Modelo Cursor exacto. |
| `output_format` | `stream-json` | Formato de salida. |
| `force` | `true` | Pasa `--force` cuando aplica. |
| `trust_workspace` | `true` | Pasa `--trust` cuando aplica. |
| `sandbox` | `disabled` | Sandbox Cursor. |

Valores soportados:

```text
output_format: stream-json, json, text
sandbox: enabled, disabled
```

La forma de ejecucion esperada para Cursor es:

```text
agent -p --force --trust --workspace <repo> --resume <chat-id> --model <model> --output-format stream-json --sandbox disabled <prompt>
```

El prompt se pasa como argumento posicional, no por stdin.

## `codex`

| Campo | Default | Descripcion |
| --- | --- | --- |
| `command` | `codex` | Ejecutable Codex CLI en WSL. |
| `review_model` | Requerido | Modelo de review disponible para la cuenta. |
| `review_skill` | `review-staged-cursor-execution` | Skill que Codex debe invocar para revisar staged changes. |
| `sandbox` | `workspace-write` | Sandbox para `codex exec`. |

Valores soportados de sandbox:

```text
read-only
workspace-write
danger-full-access
```

La forma de review esperada es:

```text
codex exec --cd <repo> --sandbox <sandbox> resume --model <model> --json --output-schema <schema> --output-last-message <result.json> <session-id> -
```

`--cd` y `--sandbox` van antes de `resume` para la version de Codex validada.

## `workflow`

| Campo | Default | Descripcion |
| --- | --- | --- |
| `max_review_iterations` | `3` | Numero maximo de reviews Codex, incluyendo el primero. |
| `require_clean_worktree` | `true` | Rechaza trabajo no relacionado antes de preparar. |
| `stage_mode` | `all` | Modo de staging. Solo `all` esta soportado. |
| `cursor_timeout_minutes` | `90` | Timeout por turno Cursor. |
| `codex_timeout_minutes` | `90` | Timeout por review Codex. |

Con `max_review_iterations: 3`:

```text
Cursor inicial
Review 1
Cursor fix 1
Review 2
Cursor fix 2
Review 3
Stop
```

Si Review 3 aun tiene findings, el estado final es `max_iterations_reached`.

## `prompt`

| Campo | Default | Descripcion |
| --- | --- | --- |
| `directory` | `docs/plans` | Directorio esperado para plan y prompt. |
| `filename_template` | `prompt_{plan_stem}.txt` | Template de prompt. Debe incluir `{plan_stem}`. |

## Validacion

```bash
ai_dev_loop config validate --repo /path/al/repo
```

Salida JSON:

```bash
ai_dev_loop config validate --repo /path/al/repo --output json
```
