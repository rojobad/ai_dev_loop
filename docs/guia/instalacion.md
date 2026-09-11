# Instalacion

## Requisitos

- WSL sobre Windows como entorno principal soportado.
- Repositorios objetivo almacenados y ejecutados dentro de WSL.
- Python 3.11 o superior para `ai_dev_loop`.
- `git` instalado en WSL.
- Cursor CLI instalado en WSL como `agent`.
- Codex CLI instalado en WSL como `codex`.
- Para Codex Desktop en Windows: acceso a la home de Codex Desktop desde WSL, normalmente bajo `/mnt/c/Users/<usuario>/.codex`.

`ai_dev_loop` no modifica Python del sistema. El flujo recomendado usa `uv`.

## Instalacion de desarrollo

```bash
uv python install 3.11
uv venv --python 3.11 .venv
source .venv/bin/activate
uv sync --all-extras
uv run ai_dev_loop --help
```

Tambien existe un helper:

```bash
bash scripts/development-install.sh
```

## Instalacion como herramienta de usuario

Desde el repo:

```bash
uv tool install .
ai_dev_loop --help
```

Para reinstalar un build local actualizado en WSL despues de cambios:

```bash
uv tool install --force .
which ai_dev_loop
ai_dev_loop --version
ai_dev_loop scheduler --help
```

Desde un wheel:

```bash
uv run python -m build
uv tool install --force dist/ai_dev_loop-0.1.0-py3-none-any.whl
```

`pipx install .` es una alternativa valida si ya usas `pipx`, pero en este proyecto `uv` es la ruta recomendada porque puede administrar Python 3.11+ sin tocar `/usr/bin/python3`.

## Timer persistente del scheduler en WSL

La instalación del comando no habilita ejecuciones automáticas. Para configurar
el timer de usuario systemd, incluyendo requisitos de WSL, `linger`, verificación
después de un reinicio, actualización y deshabilitación segura, sigue
[Timer del scheduler en WSL](../operacion/timer-systemd-wsl.md). Es una acción
operativa explícita y separada de `submit` o `scheduler tick`.

## Validacion local

```bash
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run python -m build
uv run mkdocs build --strict
```

En WSL, evita usar temporales de DrvFS para la suite completa. Si `TMP`, `TEMP` o `TMPDIR` apuntan a `/mnt/c`, pytest puede fallar antes de recolectar tests por problemas de captura temporal. Usa temporales nativos:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
```

## Validacion de CLIs externas

```bash
agent --help
agent status --format json
agent models

codex --help
codex login status
codex exec resume --help
codex debug models
```

No guardes ni publiques respuestas completas de autenticacion. `doctor` resume lo necesario sin mutar archivos:

```bash
ai_dev_loop doctor
```

El scheduler ejecuta Cursor y Codex durante `scheduler tick`. Manten las CLIs
actualizadas; `doctor` ayuda a detectar problemas de entorno antes de submit.

Tambien puedes actualizar manualmente:

```bash
agent update
codex update
```

Una actualizacion puede agregar soporte, pero no se presume que exista una version nueva. Despues se repiten los probes. Este flujo actualiza solo binarios CLI dentro de WSL, no Cursor Desktop ni Codex Desktop en Windows.

## Integraciones Codex (skills y hook)

Tras instalar el paquete, instala la integracion del target que uses:

```bash
ai_dev_loop integrations install --target wsl-cli
# o
ai_dev_loop integrations install --target codex-desktop-wsl --wsl-distro Ubuntu-22.04
```

El instalador coloca:

- `ai-dev-loop-handoff/SKILL.md`
- `ai-dev-loop-controller/SKILL.md`
- el script `SessionStart` `ai_dev_loop_session_start.py`
- el registro del hook en el `hooks.json` visible para ese target

No hace, por defecto:

- confiar el hook en Codex (sigue siendo paso manual en `/hooks`);
- crear el puente `sessions/from-desktop` (salvo `--install-session-bridge` o `integrations sessions install`);
- abrir una sesion Codex nueva;
- enviar notificaciones automaticas.

Ver [Confianza de hooks](../integraciones/confianza-hooks.md) y [Codex Desktop + WSL](../integraciones/codex-desktop-wsl.md).
