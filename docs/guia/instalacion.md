# Instalacion

## Requisitos

- WSL sobre Windows como entorno principal soportado.
- Repositorios objetivo almacenados y ejecutados dentro de WSL.
- Python 3.11 o superior para `ai_dev_loop`.
- `git` instalado en WSL.
- Codex CLI instalado en WSL como `codex`.
- Para ejecutar implementaciones Cursor: extra empaquetado
  `cursor-sdk==1.0.37` y una credencial privada provisionada por separado. El
  package base basta para consultas históricas y flujos Codex-only.
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
uv tool install '.[cursor-sdk]'
ai_dev_loop --help
```

Para reinstalar un build local actualizado en WSL despues de cambios:

```bash
uv tool install --force '.[cursor-sdk]'
which ai_dev_loop
ai_dev_loop --version
ai_dev_loop scheduler --help
```

Desde un wheel:

```bash
uv run python -m build
uv tool install --force \
  'ai-dev-loop[cursor-sdk] @ file:///ruta/absoluta/dist/ai_dev_loop-0.1.0-py3-none-any.whl'
```

El wheel incluye el dashboard local y sus assets same-origin. Una instalación
ordinaria lo sirve sin Node ni checkout:

```bash
ai_dev_loop dashboard serve --port 8765
```

Abre `http://127.0.0.1:8765/`. El proceso solo enlaza loopback y corre en primer
plano; no lo publiques mediante proxy o LAN. Consulta [Dashboard
local](../operacion/dashboard-local.md). La construcción desde fuentes valida
que los assets correspondan a las fuentes y al lock del frontend; assets
ausentes o stale hacen fallar `python -m build`.

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

No instales un checkout que todavía está cambiando sobre el driver estable que
ejecuta su propia aceptación. El cutover exige paquete completamente aceptado,
quiescencia y autorización de instalación separada; sigue el
[procedimiento de cutover Cursor SDK](../operacion/cutover-cursor-sdk.md).

## Validacion de runtimes externos

```bash
codex --help
codex login status
codex exec resume --help
codex debug models
```

No guardes ni publiques respuestas completas de autenticacion. `doctor` resume lo necesario sin mutar archivos:

```bash
ai_dev_loop doctor
```

El scheduler ejecuta Cursor mediante el SDK local y Codex mediante CLI durante
`scheduler tick`. `doctor` ayuda a detectar problemas Codex de entorno antes de
submit; el preflight Cursor SDK ocurre dentro del intento owned.

Tambien puedes actualizar manualmente:

```bash
codex update
```

Una actualizacion puede agregar soporte, pero no se presume que exista una
version nueva. Despues se repiten los probes. Este flujo actualiza solo Codex CLI
dentro de WSL, no Cursor Desktop ni Codex Desktop en Windows.

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
- `ai-dev-loop-fallback-recovery/SKILL.md`, su referencia
  `references/workflow.md` y metadata `agents/openai.yaml`
- el script `SessionStart` `ai_dev_loop_session_start.py`
- el registro del hook en el `hooks.json` visible para ese target

No hace, por defecto:

- confiar el hook en Codex (sigue siendo paso manual en `/hooks`);
- crear el puente `sessions/from-desktop` (salvo `--install-session-bridge` o `integrations sessions install`);
- abrir una sesion Codex nueva;
- enviar notificaciones automaticas.

Ver [Confianza de hooks](../integraciones/confianza-hooks.md) y [Codex Desktop + WSL](../integraciones/codex-desktop-wsl.md).
