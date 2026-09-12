# Referencia de configuracion

Archivo:

```text
ai_dev_loop.yaml
```

Version soportada:

```yaml
version: 1
```

El flujo soportado usa `scheduler submit` con `--codex-review-model` y
`--codex-review-reasoning-effort` obligatorios en CLI. `--controller-session-id`
es opcional para proveniencia y lookup controller A.
Esos valores se congelan en el ledger central; no se capturan de la sesion Codex
durante submit ni se infieren de `config.toml` o defaults de Codex CLI.

`ProjectConfig` usa `extra: forbid`. La clave `pr_review_v2` se rechaza incluso con
`enabled: false`; eliminala de cualquier `ai_dev_loop.yaml` usado para nuevos
`scheduler submit`. La seccion opcional `github` sigue siendo valida en el schema
pero ya no tiene comandos CLI asociados tras Phase 17.7.

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

## `cursor`

| Campo | Default | Descripcion |
| --- | --- | --- |
| `command` | `agent` | Ejecutable Cursor CLI. |
| `model` | `composer-2.5-fast` | Modelo Cursor exacto para el run. |
| `output_format` | `stream-json` | Formato de salida. |
| `force` | `true` | Pasa `--force` cuando aplica. |
| `trust_workspace` | `true` | Pasa `--trust` cuando aplica. |
| `sandbox` | `disabled` | Sandbox Cursor. |

El prompt se pasa como argumento posicional, no por stdin.

Al hacer `scheduler submit`, `cursor.command` se resuelve desde el entorno
interactivo de A y se congela como una ruta absoluta canónica sólo en el contexto
y la configuración efectiva protegidos del run. El YAML fuente no se modifica.
Así, un worker invocado más tarde por systemd no depende de que su `PATH` coincida
con el de la terminal que creó el run. El valor debe ser un único ejecutable (un
nombre resoluble o una ruta), sin argumentos.

## `codex`

| Campo | Default | Descripcion |
| --- | --- | --- |
| `command` | `codex` | Ejecutable Codex CLI en WSL. |
| `review_skill` | `review-staged-cursor-execution` | Skill invocado en cada review. |
| `sandbox` | `workspace-write` | Sandbox para reviews legacy fuera del scheduler. |

En el flujo scheduler actual:

- `--codex-review-model` y `--codex-review-reasoning-effort` se pasan en
  `scheduler submit` y quedan congelados en `codex/fresh-reviewer-input.json`.
- El reviewer B se crea una sola vez en el primer review con `codex exec` en
  `--sandbox read-only`; los reviews posteriores reanudan esa misma sesion con
  `codex exec resume` (nunca `--last` ni un segundo B).
- Los campos YAML `review_model` y `review_reasoning_effort` no sustituyen los
  flags de submit en runs nuevos del scheduler.

Al igual que Cursor, `codex.command` se resuelve y congela durante `scheduler
submit` para que el reviewer B pueda ejecutarse desde el entorno aislado del
worker. Conserva `codex` en el YAML cuando quieras una configuración portable;
no añadas rutas locales al archivo del repositorio.

Valores soportados de `review_reasoning_effort` cuando se usan como override
explicito en submit:

```text
minimal
low
medium
high
xhigh
max
ultra
```

## `workflow`

| Campo | Default | Descripcion |
| --- | --- | --- |
| `max_review_iterations` | `3` | Numero maximo de reviews Codex, incluyendo el primero. |
| `require_clean_worktree` | `true` | Se congela en submit; la admision one-shot ocurre en el primer tick. |
| `stage_mode` | `all` | Solo `all` esta soportado (`git add -A` tras Cursor). |
| `cursor_timeout_minutes` | `90` | Timeout por turno Cursor. |
| `codex_timeout_minutes` | `90` | Timeout por review Codex. |

Los valores congelados de `cursor_timeout_minutes` y `codex_timeout_minutes` en el
contexto submitido tambien forman el presupuesto de ejecucion de cada unidad
transitoria del scheduler (`RuntimeMaxSec` / `TimeoutStartSec`), mas una gracia
interna acotada de finalizacion. No los sustituye un default generico de una hora
del backend systemd.

## `prompt`

| Campo | Default | Descripcion |
| --- | --- | --- |
| `directory` | `docs/plans` | Directorio esperado para plan y prompt. |
| `filename_template` | `prompt_{plan_stem}.txt` | Template de prompt. Debe incluir `{plan_stem}`. |

## Recuperacion y limites (scheduler)

El scheduler no implementa `recover`, `resume` ni `extend` legacy. Los limites
operativos documentados son:

- `max_iterations_reached`: el run termina con cambios staged y el ultimo fix
  prompt preservado; no hay comando publico para ampliar el presupuesto.
- `scheduler abort`: cancelacion no destructiva; preserva artefactos y cambios staged.
- Runs interrumpidos o bloqueados: inspecciona `scheduler status` y
  `scheduler history`; la accion segura depende del checkpoint durable en el ledger.
- No existe conversion automatica de runs legacy `runs/` ni `pr-review-v2` al ledger
  central; la accion segura es un `scheduler submit` fresco.

## Validacion

```bash
ai_dev_loop config validate --repo /path/al/repo
ai_dev_loop config validate --repo /path/al/repo --output json
```
