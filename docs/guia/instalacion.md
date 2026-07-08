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

Desde un wheel:

```bash
uv run python -m build
uv tool install --force dist/ai_dev_loop-0.1.0-py3-none-any.whl
```

`pipx install .` es una alternativa valida si ya usas `pipx`, pero en este proyecto `uv` es la ruta recomendada porque puede administrar Python 3.11+ sin tocar `/usr/bin/python3`.

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
```

No guardes ni publiques respuestas completas de autenticacion. `doctor` resume lo necesario sin mutar archivos:

```bash
ai_dev_loop doctor
```
