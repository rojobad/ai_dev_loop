# Elegir target de integracion

`ai_dev_loop` soporta dos targets de integracion con Codex:

```text
wsl-cli
codex-desktop-wsl
```

La eleccion depende de donde vive la sesion interactiva original de Codex.

## Comparacion

| Target | Usa este target cuando | Skill visible para | Hook registrado en | Hook ejecuta |
| --- | --- | --- | --- | --- |
| `wsl-cli` | Codex interactivo corre en WSL | Codex CLI en WSL | `~/.codex/hooks.json` de WSL | `python3 ~/.codex/hooks/ai_dev_loop_session_start.py` |
| `codex-desktop-wsl` | Codex Desktop corre en Windows y los agentes corren en WSL | Codex Desktop | `/mnt/c/Users/<usuario>/.codex/hooks.json` | `wsl.exe -d <distro> --exec python3 ...` |

El target por defecto de CLI es `wsl-cli` por compatibilidad. En documentacion y uso diario conviene pasar `--target` explicitamente.

## Decision rapida

Si abres Codex desde una terminal WSL:

```bash
ai_dev_loop integrations install --target wsl-cli
```

Si abres Codex Desktop como aplicacion Windows:

```bash
ai_dev_loop integrations install --target codex-desktop-wsl --wsl-distro Ubuntu-22.04
```

## Por que importa

Codex Desktop y Codex CLI en WSL no comparten la misma home:

```text
Codex Desktop:
/mnt/c/Users/<usuario>/.codex

Codex CLI en WSL:
/home/<usuario>/.codex
```

Instalar hooks solo en la home WSL no alcanza para Codex Desktop. Desktop no vera ese `hooks.json`.

Tampoco es seguro apuntar `CODEX_HOME` de WSL a `/mnt/c/.../.codex`. Esa ruta contiene bases SQLite y estado propio de Desktop. Compartir toda la home puede causar errores de esquema, locks o corrupcion.

## Ver estado

```bash
ai_dev_loop integrations status --target wsl-cli
ai_dev_loop integrations status --target codex-desktop-wsl --wsl-distro Ubuntu-22.04
ai_dev_loop integrations sessions status
```

`hook_trust` se reporta como `unknown` porque no se detecta de forma segura desde archivos locales. La verificacion correcta es abrir `/hooks` en la superficie de Codex correspondiente.
