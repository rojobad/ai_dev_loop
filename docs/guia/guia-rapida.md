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

`--codex-review-model` y `--codex-review-reasoning-effort` se pasan en `scheduler submit`
y quedan congelados en el ledger. No se infieren de `config.toml` ni del default local
de Codex CLI.

Valida:

```bash
ai_dev_loop config validate --repo /path/al/repo
```

## 4. Submit desde Codex (controller A)

Desde la sesion interactiva original de Codex, despues de aprobar el plan y el prompt:

```bash
ai_dev_loop scheduler submit \
  --repo-path /path/al/repo \
  --plan-path docs/plans/mi-plan.md \
  --prompt-source-path docs/plans/prompt_mi-plan.txt \
  --controller-session-id "<session-id-exacto>" \
  --codex-review-model "<review-model>" \
  --codex-review-reasoning-effort high \
  --output json < docs/plans/prompt_mi-plan.txt
```

## 5. Autoriza y ejecuta el loop

```bash
ai_dev_loop scheduler start <run-id> --controller-session-id "<session-id-exacto>"
ai_dev_loop scheduler tick
```

Durante desarrollo invoca `scheduler tick` manualmente. Para progreso eventual con WSL
activo, instala y habilita el timer solo tras aceptacion manual independiente:

```bash
ai_dev_loop scheduler timer validate
ai_dev_loop scheduler timer install
ai_dev_loop scheduler timer install --enable   # habilitacion explicita
ai_dev_loop scheduler timer status
```

Si necesitas cancelar:

```bash
ai_dev_loop scheduler abort <run-id>
```

## 6. Revisa resultados

```bash
ai_dev_loop scheduler status <run-id>
ai_dev_loop scheduler list
ai_dev_loop scheduler history <run-id>
ai_dev_loop controller status \
  --controller-session-id "<session-id-exacto>" \
  --repo-path /path/al/repo
```

Los cambios finales quedan staged en el repositorio objetivo. `ai_dev_loop` no hace commit.
