# Configuracion del repositorio objetivo

Cada repositorio que sera controlado por `ai_dev_loop` debe tener un archivo `ai_dev_loop.yaml` en su raiz.

## Ejemplo recomendado (herencia de sesion)

Por defecto, omite `review_model` y `review_reasoning_effort` para que el review reanudado use el modelo y reasoning de la sesion Codex exacta:

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

## Overrides explicitos

Puedes fijar modelo y/o reasoning de forma independiente:

```yaml
codex:
  command: codex
  review_model: gpt-5.6-terra
  review_reasoning_effort: high
  review_skill: review-staged-cursor-execution
  sandbox: workspace-write
```

Tambien puedes fijar solo uno de los dos campos. `null` o omitir el campo significa herencia. No uses una cadena magica como `inherit`.

## Reglas de validacion

- `version` debe ser `1`.
- `project.name` debe ser un slug en minusculas con guiones opcionales.
- `cursor.output_format` soporta `stream-json`, `json` o `text`.
- `cursor.sandbox` soporta `enabled` o `disabled`.
- `codex.sandbox` soporta `read-only`, `workspace-write` o `danger-full-access`.
- `codex.review_model` es opcional; si esta presente, no puede ser vacio.
- `codex.review_reasoning_effort` es opcional; valores validos: `minimal`, `low`, `medium`, `high`, `xhigh`.
- `workflow.stage_mode` soporta actualmente `all`.
- Timeouts y `max_review_iterations` deben ser positivos.
- `prompt.filename_template` debe incluir `{plan_stem}`.

La configuracion se parsea como YAML estructurado. No se interpreta con expresiones regulares.

## Precedencia

La configuracion efectiva se resuelve en este orden:

1. Flags explicitos de CLI.
2. `ai_dev_loop.yaml` del repositorio.
3. Configuracion global opcional bajo XDG config.
4. Defaults seguros del paquete (herencia para modelo/reasoning de Codex).

Durante `prepare`, `ai_dev_loop` persiste tanto la configuracion fuente como la configuracion efectiva. Esos valores quedan congelados para el run.

## Modelo y reasoning de review

El default recomendado es heredar ambos valores de la sesion Codex reanudada. Eso evita forzar un modelo como el antiguo default `o4-mini`, que puede no estar disponible para todas las cuentas.

Si necesitas un override:

1. Fija `codex.review_model` y/o `codex.review_reasoning_effort` en YAML, o
2. Pasa `--codex-review-model` y/o `--codex-review-reasoning-effort` en `prepare`.

Verifica modelos disponibles con:

```bash
codex debug models
```

Ese comando puede emitir mucha informacion; no la pegues completa en issues o logs.

No mezcles IDs de Cursor Agent con IDs de Codex. En Codex, el modelo y el reasoning effort son campos distintos.

## Validar configuracion

```bash
ai_dev_loop config validate --repo /path/al/repo
```

La salida indica si el modelo/reasoning son explicitos o `inherited from session`.

Para fixtures o directorios anidados que no son un repositorio Git independiente, usa `--config-path`:

```bash
ai_dev_loop config validate \
  --repo /path/al/repo \
  --config-path /path/al/repo/ai_dev_loop.yaml
```

## Convencion de planes y prompts

La convencion esperada es:

```text
docs/plans/<plan>.md
docs/plans/prompt_<plan>.txt
```

El plan aprobado y el prompt exacto se tratan como contrato. Si cambian despues de `prepare`, `start` debe fallar y pedir preparar un run nuevo.
