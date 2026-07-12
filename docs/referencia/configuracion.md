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

Herencia recomendada (sin overrides de modelo ni reasoning):

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

Overrides explicitos e independientes:

```yaml
codex:
  command: codex
  review_model: gpt-5.6-terra
  review_reasoning_effort: high
  review_skill: review-staged-cursor-execution
  sandbox: workspace-write
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

`cursor.model` en YAML fija el modelo del run preparado. No configura el fallback de recovery por limite de uso: ese valor se congela solo en el sucesor via `recover --cursor-model <modelo>`. `auto` es una solicitud de enrutamiento de Cursor, no un modelo de proveedor fijado en YAML.

No uses IDs de modelo de Cursor Agent (por ejemplo `gpt-5.6-terra-high`) como `codex.review_model`. En Codex, modelo y reasoning effort son campos separados.

## `codex`

| Campo | Default | Descripcion |
| --- | --- | --- |
| `command` | `codex` | Ejecutable Codex CLI en WSL. |
| `review_model` | `null` (captura) | Override opcional. En un run nuevo, omitido o `null` usa el modelo capturado de la sesion durante `prepare`. |
| `review_reasoning_effort` | `null` (captura) | Override opcional. En un run nuevo, omitido o `null` usa el reasoning capturado de la sesion durante `prepare`. |
| `review_skill` | `review-staged-cursor-execution` | Skill que Codex debe invocar para revisar staged changes. |
| `sandbox` | `workspace-write` | Sandbox para `codex exec`. |

Valores soportados de sandbox:

```text
read-only
workspace-write
danger-full-access
```

Valores soportados de `review_reasoning_effort`:

```text
minimal
low
medium
high
xhigh
max
ultra
```

Los overrides son independientes: puedes fijar solo el modelo, solo el reasoning, ambos, o ninguno. La captura de sesion completa el campo omitido; no se consulta WSL `config.toml` ni el default de Codex CLI.

`prepare` congela valores de sesion, valores efectivos y procedencia (`session` o `explicit`). Cambiar `ai_dev_loop.yaml` o el rollout despues no altera un run ya preparado; prepara un run nuevo.

Forma de review para runs nuevos (argv separados, sin shell):

```text
codex exec --cd <repo> --sandbox <sandbox> resume --model <model> -c model_reasoning_effort="high" --json --output-schema <schema> --output-last-message <result.json> <session-id> -
```

`--cd` y `--sandbox` van antes de `resume`; `--model` y `-c` van despues. Runs historicos de Fase 9 con ambos valores `null` y sin procedencia omiten ambos por compatibilidad y advierten que se vuelva a preparar. Esos `null` no significan captura de sesion.

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

Inspecciona la configuracion efectiva antes de `prepare`:

```bash
ai_dev_loop config validate --repo /path/al/repo
```

Salida JSON:

```bash
ai_dev_loop config validate --repo /path/al/repo --output json
```

La salida muestra si `review_model` y `review_reasoning_effort` son explicitos o `inherited from session`.
