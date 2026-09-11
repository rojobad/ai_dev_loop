# Desinstalacion y limpieza

## Desinstalar integraciones

Para WSL CLI:

```bash
ai_dev_loop integrations uninstall --target wsl-cli
```

Para Codex Desktop + WSL:

```bash
ai_dev_loop integrations uninstall --target codex-desktop-wsl --wsl-distro Ubuntu-22.04
```

La desinstalacion preserva por defecto:

- hooks no relacionados;
- skills no relacionados;
- run history del scheduler (`engine.sqlite3` y `artifacts/`);
- config, cache, sesiones Codex, hooks y skills instalados;
- puente `from-desktop` del target desktop.

## Remover puente de sesiones

```bash
ai_dev_loop integrations sessions remove
```

Esto elimina solo:

```text
~/.codex/sessions/from-desktop
```

No elimina sesiones de Windows ni archivos rollout.

## Desinstalar el comando

Antes de retirar el ejecutable, deshabilita y detén el timer si estaba activo:

```bash
ai_dev_loop scheduler timer disable --output json
ai_dev_loop scheduler timer status --output json
```

Esto evita que una unidad systemd habilitada intente ejecutar un comando ya
desinstalado. `disable` conserva los archivos de unidad owned; consulta
[Timer del scheduler en WSL](timer-systemd-wsl.md) para el procedimiento
completo y la decisión independiente sobre `linger`.

Si instalaste con `uv tool`:

```bash
uv tool uninstall ai_dev_loop
```

Si instalaste con `pipx`:

```bash
pipx uninstall ai_dev_loop
```

## Cutover: eliminar estado legacy

Tras validar el scheduler central, puedes eliminar **solo** los dos roots legacy
con confirmacion explicita:

```bash
ai_dev_loop scheduler cutover cleanup --confirm delete-legacy-state
```

El comando muestra las rutas exactas antes de borrar y rechaza:

- targets symlink o fuera del state root resuelto;
- trabajo activo del scheduler (lease, attempt o capacity holder);
- tokens de confirmacion incorrectos.

Usa `--dry-run` para validar sin borrar.

Rutas afectadas exclusivamente:

```text
$XDG_STATE_HOME/ai_dev_loop/runs/
$XDG_STATE_HOME/ai_dev_loop/pr-review-v2/
```

Se preservan `engine.sqlite3`, `artifacts/`, config, cache, hooks, skills y
demas paths bajo `$XDG_STATE_HOME/ai_dev_loop`.

## Limpiar estado XDG manualmente

Para config, cache o el ledger del scheduler, sigue siendo una decision manual.
Revisa antes de borrar:

```text
$XDG_STATE_HOME/ai_dev_loop/engine.sqlite3
$XDG_STATE_HOME/ai_dev_loop/artifacts/
$XDG_CONFIG_HOME/ai_dev_loop
$XDG_CACHE_HOME/ai_dev_loop
```

No borres estado si necesitas auditar o continuar runs del scheduler activos.
