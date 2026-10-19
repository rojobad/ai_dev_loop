# Referencia de configuracion

Archivo:

```text
ai_dev_loop.yaml
```

Versiones soportadas:

```yaml
version: 1  # lector histórico; ejecución nueva solo con provider Codex efectivo
# o
version: 2  # seleccion Cursor SDK local, completamente explicita
```

El flujo soportado usa `scheduler submit` con `--short-name`,
`--codex-review-model` y `--codex-review-reasoning-effort` obligatorios en CLI.
`--controller-session-id` es opcional para proveniencia y lookup controller A.
Esos valores se congelan en el ledger central; no se capturan de la sesion Codex
durante submit ni se infieren de `config.toml` o defaults de Codex CLI.

`ProjectConfig` usa `extra: forbid`. La clave `pr_review_v2` se rechaza incluso con
`enabled: false`; eliminala de cualquier `ai_dev_loop.yaml` usado para nuevos
`scheduler submit`. La seccion opcional `github` sigue siendo valida en el schema
pero ya no tiene comandos CLI asociados tras Phase 17.7.

Los defaults dependen de la version. `version: 1` conserva el contrato de lectura
histórico y los campos inertes que necesita un implementer Codex efectivo. Una
selección efectiva Cursor v1 se rechaza antes de publicar o ejecutar trabajo
nuevo. `version: 2` no hereda `cursor.command`, modelo, output format ni
ningun otro default Cursor de v1: exige todos los campos SDK que aparecen abajo.
La configuracion global y la del repositorio deben usar la misma version; no se
mezclan schemas.

## Schema v1 histórico y ejecución Codex

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

## `cursor` v1

| Campo | Default | Descripcion |
| --- | --- | --- |
| `command` | `agent` | Ejecutable Cursor CLI. |
| `model` | `composer-2.5-fast` | Modelo Cursor exacto para el run. |
| `output_format` | `stream-json` | Formato de salida. |
| `force` | `true` | Pasa `--force` cuando aplica. |
| `trust_workspace` | `true` | Pasa `--trust` cuando aplica. |
| `sandbox` | `disabled` | Sandbox Cursor. |

Estos campos permiten parsear registros auténticos sin reescribir bytes ni
hashes. Ya no hay una ruta de ejecución Cursor CLI: un submit o definición nueva
con provider efectivo Cursor debe migrar explícitamente a config v2/manifest v4.
Si el provider efectivo es Codex, `cursor.command` permanece inerte y no se
resuelve ni exige que exista `agent`; se congelan solo el comando y settings
Codex seleccionados.

## Schema v2 (Cursor SDK local)

```yaml
version: 2

project:
  name: my-project

cursor:
  runtime: sdk-local
  sdk_version: 1.0.37
  model: grok-4.7
  parameters:
    context: 256k
    fast: false
    reasoning_effort: high
  credential_ref: cursor-default
  setting_sources: [project]
  sandbox: disabled

codex:
  command: codex
  review_skill: review-staged-ai-dev-loop-execution
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

Todos los campos de `cursor` son obligatorios y `extra: forbid` tambien aplica a
esta seccion. El runtime es exactamente `sdk-local` y la version esta fijada a
`cursor-sdk==1.0.37`; otra version falla en validacion o al abrir el adapter. El
modelo es un string no vacio. `parameters` es un mapping no vacio, ordenado de
forma canonica: `fast` es un boolean estricto, `context` y `reasoning_effort` son
strings no vacios, y los demas parametros solo pueden ser strings no vacios o
booleanos estrictos. No hay coercion entre `false` y `"false"`. Las fuentes
admitidas son `project`, `user`, `team`, `mdm`, `plugins` y `all`, sin duplicados.
El sandbox debe ser `enabled` o `disabled`.

`credential_ref` es solo un nombre seguro, no un secreto ni una ruta. Resuelve a
`$XDG_CONFIG_HOME/ai_dev_loop/credentials/<credential_ref>`; los directorios
deben pertenecer al usuario y tener modo `0700`, y el archivo debe ser regular,
no symlink, pertenecer al usuario y tener modo `0600`. El valor debe ser UTF-8 de
una sola linea, como maximo 16 KiB. El YAML, ledger, artefactos, argv, salida y
errores nunca contienen el valor.

Cada conversacion SDK usa workspace explicito dentro del repositorio y dos roots
privados separados bajo
`$XDG_STATE_HOME/ai_dev_loop/cursor-sdk/<sha256-owner>/`: `bridge/` para
`state_root` y `store/` para el store JSONL nativo. Ninguno puede solaparse con
el repositorio. La identidad del owner se convierte en un digest SHA-256; no se
usa un nombre inferido, path libre ni estado dentro del checkout.

El flujo vigente permite que `scheduler submit` con implementer Cursor congele config v2
en un contexto v7. `submit` no importa el SDK, abre el bridge, lee la credencial ni
consulta el catalogo. El intento aislado hace ese preflight y persiste, antes de
inferir, el intent de creación, el `agent_id` exacto y luego el intent de envío y
el run nativo. Una selección Codex sigue siendo independiente y no requiere
binario, package, credencial ni environment de Cursor.

Las secuencias SDK nuevas usan manifest v4: conservan los `short_name` de v3 y
exigen `runtime`, `model` y `parameters` completos en cada fase. La definición y
entrada congeladas son v3 y cada hoja materializa contexto v8. Manifests v1-v3 ya
aceptados conservan sus hashes y semántica de lectura; no se convierten a SDK.
Una definición histórica Codex-only puede seguir ejecutando y recuperando. Si
contiene una hoja Cursor CLI efectiva, start, materialización, handoff y recovery
rechazan la secuencia completa sin modificar el registro.
La recuperación SDK exige primero terminación comprobada del árbol de procesos y
reconciliación pública del run nativo para el owner/key/agent exacto. Un run
conocido terminal se reproduce sin otro prompt; uno conocido no terminal se
cancela y confirma antes de aplicar la política normal de timeout/capacidad. Para
un envío cuya respuesta se perdió, solo se adopta el único run nuevo respecto al
inventario pre-envío. Cero o varios candidatos, o una creación cuya identidad no
puede probarse, retienen la reserva para inspección o `scheduler abort` sin crear
otro agente ni reenviar el prompt.

El wheel publica el extra opcional `cursor-sdk` con pin exacto a 1.0.37. La
instalacion base no importa ni instala ese package y conserva lectores históricos
y ejecución Codex-only. El desarrollo no autoriza hot-install de este checkout en un
scheduler estable; la instalación o actualización sigue siendo una acción
separada del operador.

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
En runs standalone y hojas de secuencia históricas Codex-only el scheduler lanza la implementación inicial y reanuda la
misma sesión autenticada para correcciones. Modelo, effort, timeout e identidad
son independientes del reviewer. Un v3 nuevo debe ser Codex-only; v4 permite
elegir Cursor SDK o Codex por fase. No existe cambio de provider durante el run.

Los contextos antiguos sin metadata de implementer se leen como Cursor para
consultas, pero su runtime CLI no se vuelve ejecutable. Los runs nuevos persisten
el provider de forma explícita.

La precedencia de configuracion es: defaults de la version, campos explicitos de
`$XDG_CONFIG_HOME/ai_dev_loop/config.yaml`, campos explicitos del repositorio y,
por ultimo, overrides CLI soportados. Un mapping `implementer` explicito sin
`provider` selecciona Cursor. Cuando una capa cambia el provider, reemplaza la
seccion completa en vez de conservar campos incompatibles del provider anterior.
En secuencias v3/v4, la seleccion explicita de cada fase prevalece para ese ordinal;
no cambia durante el run y no altera el reviewer independiente.

Phase 24 continúa sin ejecutarse y no forma parte de este contrato. La telemetría
numérica no cambia decisiones de review, autoridad Git, límites ni recovery.

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

## Manifests de secuencia históricos y v3/v4

`schema_version: 2` permitía `implementer` por fase y permanece aceptado para
replay de secuencias ya preparadas. Una preparación nueva Codex-only usa
`schema_version: 3`, conserva esos campos de implementer y requiere `short_name`
tanto en la secuencia como en cada fase. Una preparación con Cursor SDK usa v4.
Omitir la sección o su provider selecciona Cursor y por ello requiere v4/config
SDK, incluso si YAML seleccionaba Codex. Para `provider: codex`, los campos de fase prevalecen sobre defaults YAML
del mismo provider; el modelo y effort deben quedar explícitamente resueltos.
Cambiar de provider elimina los defaults incompatibles. Los campos Cursor CLI
ya no tienen overrides globales nuevos. En v4, la selección SDK completa queda
congelada por ordinal y no entra en conflicto con otra fase Codex independiente.
Los campos reviewer `codex.review_model` y `codex.review_reasoning_effort` siguen
siendo obligatorios por fase y no se heredan del implementer.

Prepare congela ejecutables, configuración, prompts y hashes independientes por
ordinal sin ejecutar Git/agentes. Los cambios posteriores en YAML o manifest no
alteran esa definición preparada. Un prepare con inputs distintos crea una nueva
definición; no reemplaza la anterior. Manifest v1 y sus entradas congeladas
históricas conservan Cursor y hashes, sin migración a Codex; si el runtime
efectivo es CLI no se ejecutan. Preparaciones nuevas con manifest v1/v2 se
rechazan con una indicación de actualización a v3 Codex-only o v4 SDK.
