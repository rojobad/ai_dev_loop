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

# Opcional. Omitir la sección conserva el implementer Cursor.
# implementer:
#   provider: cursor

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
  `--sandbox workspace-write`; los reviews posteriores reanudan esa misma sesion con
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

## `implementer`

Sección opcional. Si falta, o si `provider` se omite, el implementer es Cursor y
se usan `cursor` más `workflow.cursor_timeout_minutes`. Un mapping explícito
`null`, un provider desconocido, strings vacíos o campos Codex sobre `cursor` se
rechazan.

| Campo | Default | Descripcion |
| --- | --- | --- |
| `provider` | `cursor` | `cursor` o `codex`. |
| `command` | `codex` | Solo `provider: codex`. Rechazado para Cursor. |
| `model` | obligatorio si es Codex | Modelo exacto. No sustituye `--codex-review-model`. |
| `reasoning_effort` | obligatorio si es Codex | Vocabulario Codex existente, incluido `high` y `xhigh`. |
| `timeout_minutes` | `90` | Entero positivo. `true` no se acepta como `1`. |

El sandbox del implementer Codex queda congelado en `workspace-write`. La política
de aprobación no interactiva queda congelada en `never`. No se acepta un bypass.
En runs standalone y hojas de secuencia v2 el scheduler lanza la implementación inicial y reanuda la
misma sesión autenticada para correcciones. Modelo, effort, timeout e identidad
son independientes del reviewer. Los manifests de secuencia v1 siguen siendo
Cursor-only; v2 permite elegir por fase. No existe cambio de provider durante el run.

Los contextos antiguos sin metadata de implementer se leen como Cursor. Los runs
nuevos persisten el provider de forma explícita.

## `workflow`

| Campo | Default | Descripcion |
| --- | --- | --- |
| `max_review_iterations` | `3` | Numero maximo de reviews Codex, incluyendo el primero. |
| `require_clean_worktree` | `true` | Se congela en submit; la admision one-shot ocurre en el primer tick. |
| `stage_mode` | `all` | Solo `all` esta soportado (`git add -A` tras un turno de implementación exitoso). |
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

El scheduler no implementa `recover`, `resume` ni `extend` legacy de runs XDG. Los
limites congelados en el contexto enviado no cambian; un techo mayor solo puede
otorgarse con `scheduler extend` mientras el run esta en `max_iterations_reached`.
Los limites
operativos documentados son:

- `max_iterations_reached`: el run conserva cambios staged y el último fix
  prompt. `scheduler extend <run-id> --max-review-iterations <higher-total>`
  autoriza explícitamente un techo mayor y encola la corrección pendiente.
- `scheduler review retry`: autoriza reintentos elegibles del reviewer o crea
  un sucesor same-reviewer de un origen bloqueado elegible; no cambia los
  valores congelados de modelo y razonamiento.
- `scheduler cursor-retry`: adelanta o autoriza otro intento de un turno Cursor
  con timeout confirmado, conservando chat, modelo, prompt y trabajo parcial.
  `--force` publica un sucesor del mismo chat para un fallo inicial o una
  corrección, también cuando el run es la hoja bloqueada actual de una secuencia.
  La secuencia conserva el ordinal y la entrada congelada. Una hoja obsoleta,
  un aborto o una reserva ajena se rechazan.
- `scheduler implementer-retry`: entrada compartida para el timeout confirmado del
  provider congelado. En Codex conserva sesión, modelo, effort, timeout y prompt;
  admite tres reintentos automáticos por turno con espera de 30 minutos. No toma
  valores nuevos de YAML ni de la configuración local de Codex.
  `--force` publica o repite un sucesor para un fallo genérico Codex terminado y
  autenticado. Conserva los settings enviados y las identidades exactas; no permite
  cambiar provider/modelo ni reintentar una identidad incierta. Una hoja vigente de
  secuencia se adopta en el mismo ordinal antes del dispatch; una hoja obsoleta sin
  relación se rechaza.
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

## Manifest de secuencia v2

`schema_version: 2` permite `implementer` por fase, con los campos anteriores.
Omitir la sección o su provider selecciona Cursor, incluso si YAML seleccionaba
Codex. Para `provider: codex`, los campos de fase prevalecen sobre defaults YAML
del mismo provider; el modelo y effort deben quedar explícitamente resueltos.
Cambiar de provider elimina los defaults incompatibles. Los campos Cursor
explícitos (`cursor` o `workflow.cursor_timeout_minutes`, incluidos overrides
CLI globales) entran en conflicto con una fase Codex y se rechazan.
Los campos reviewer `codex.review_model` y `codex.review_reasoning_effort` siguen
siendo obligatorios por fase y no se heredan del implementer.

Prepare congela ejecutables, configuración, prompts y hashes independientes por
ordinal sin ejecutar Git/agentes. Los cambios posteriores en YAML o manifest no
alteran esa definición preparada. Un prepare con inputs distintos crea una nueva
definición; no reemplaza la anterior. Manifest v1 y sus entradas congeladas
históricas conservan Cursor y hashes, sin migración a Codex. Un prepare v1 nuevo
rechaza defaults YAML que seleccionen Codex; usa manifest v2 para esa selección.
