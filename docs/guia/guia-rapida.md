# Guia rapida

Esta guia asume que estas en WSL, que el repositorio objetivo vive dentro de WSL y que `agent`, `codex` y `git` estan disponibles en `PATH`.

## 1. Instala la herramienta

Para desarrollo local:

```bash
uv sync --all-extras
uv run ai_dev_loop --help
```

Para instalar el comando como herramienta de usuario:

```bash
uv tool install .
ai_dev_loop --help
```

Tambien puedes instalar desde un wheel ya construido:

```bash
uv run python -m build
uv tool install --force dist/ai_dev_loop-0.1.0-py3-none-any.whl
```

## 2. Instala la integracion correcta

Si Codex interactivo corre dentro de WSL:

```bash
ai_dev_loop integrations install --target wsl-cli
```

Si usas Codex Desktop en Windows con agentes en WSL:

```bash
ai_dev_loop integrations install --target codex-desktop-wsl --wsl-distro Ubuntu-22.04
ai_dev_loop integrations sessions status
```

Si falta el puente de sesiones desktop:

```bash
ai_dev_loop integrations sessions install
```

Despues abre `/hooks` en Codex o Codex Desktop y confia el hook `ai_dev_loop` si aparece pendiente.

## 3. Configura el repositorio objetivo

En la raiz del repositorio que sera modificado por Cursor, crea `ai_dev_loop.yaml`:

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

Por defecto, `review_model` y `review_reasoning_effort` se omiten para heredar el modelo y reasoning de la sesion Codex reanudada. Para overrides explicitos, ver [Configuracion del repositorio](configuracion-repositorio.md).

Valida:

```bash
ai_dev_loop config validate --repo /path/al/repo
```

## 4. Prepara desde Codex

Desde la sesion interactiva original de Codex, despues de aprobar el plan y el prompt:

```bash
ai_dev_loop prepare \
  --repo-path /path/al/repo \
  --plan-path docs/plans/mi-plan.md \
  --prompt-source-path docs/plans/prompt_mi-plan.txt \
  --codex-session-id "<session-id-exacto>" \
  --output json < docs/plans/prompt_mi-plan.txt
```

`prepare` devuelve un `start_command`.

## 5. Sal de Codex y ejecuta el loop

No ejecutes `start` desde la UI interactiva que posee esa misma sesion.

```bash
ai_dev_loop start <run-id>
```

Si el run se interrumpe:

```bash
ai_dev_loop resume <run-id>
```

Si necesitas cancelar:

```bash
ai_dev_loop abort <run-id>
```

## 6. Revisa resultados

```bash
ai_dev_loop status <run-id>
ai_dev_loop inspect <run-id>
ai_dev_loop logs <run-id>
ai_dev_loop logs <run-id> --component codex
```

Los cambios finales quedan staged en el repositorio objetivo. `ai_dev_loop` no hace commit.
