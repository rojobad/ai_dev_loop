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
- run history;
- prompts;
- reviews;
- staged patches;
- logs;
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

Si instalaste con `uv tool`:

```bash
uv tool uninstall ai_dev_loop
```

Si instalaste con `pipx`:

```bash
pipx uninstall ai_dev_loop
```

## Limpiar estado XDG manualmente

No existe un comando destructivo de cleanup en `ai_dev_loop`.

Si decides eliminar historial local, revisa y borra manualmente:

```text
~/.local/state/ai_dev_loop
~/.config/ai_dev_loop
~/.cache/ai_dev_loop
```

O las rutas equivalentes si usas variables XDG:

```text
$XDG_STATE_HOME/ai_dev_loop
$XDG_CONFIG_HOME/ai_dev_loop
$XDG_CACHE_HOME/ai_dev_loop
```

Antes de borrar, considera que ahi viven:

- prompts iniciales;
- fix prompts;
- patches staged;
- reportes de review;
- logs;
- metadata de sesiones;
- evidencia para recovery.

No borres estado si necesitas auditar o reanudar runs.
