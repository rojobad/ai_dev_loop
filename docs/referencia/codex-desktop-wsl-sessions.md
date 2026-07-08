# Sesiones Codex Desktop y WSL

Esta pagina documenta el problema de sesiones entre Codex Desktop en Windows y Codex CLI dentro de WSL, y el puente soportado por `ai_dev_loop`.

## Problema

Codex Desktop y Codex CLI en WSL usan homes diferentes:

| Herramienta | Home | Filesystem |
| --- | --- | --- |
| Codex CLI en WSL | `/home/<usuario>/.codex` | ext4 nativo WSL |
| Codex Desktop en Windows | `C:\Users\<usuario>\.codex` o `/mnt/c/Users/<usuario>/.codex` | DrvFS |

Aunque Codex Desktop ejecute agentes en WSL, la aplicacion de escritorio sigue leyendo su home Windows.

Consecuencia: un `codex resume` interactivo en WSL no lista automaticamente chats de Desktop.

## Por que no compartir toda la home

No configures:

```bash
export CODEX_HOME=/mnt/c/Users/<usuario>/.codex
```

Riesgos:

1. Desktop y CLI pueden mantener bases `state_*.sqlite` con esquemas diferentes.
2. SQLite sobre la frontera WSL/Windows puede fallar por locking o corrupcion.
3. Compartir auth/config/indices entre superficies aumenta el radio de dano.

Tampoco symlinkees:

```text
~/.codex -> /mnt/c/Users/<usuario>/.codex
~/.codex/sessions -> /mnt/c/Users/<usuario>/.codex/sessions
```

## Puente soportado

El puente permitido es un symlink anidado:

```text
~/.codex/sessions/from-desktop -> /mnt/c/Users/<usuario>/.codex/sessions
```

Esto expone rollouts `.jsonl` por session ID. No expone SQLite ni auth.

Comandos:

```bash
ai_dev_loop integrations sessions install
ai_dev_loop integrations sessions status
ai_dev_loop integrations sessions list
ai_dev_loop integrations sessions remove
```

## Reanudar desde WSL

Con el puente saludable, WSL puede resolver una sesion Desktop por ID exacto:

```bash
codex exec resume <SESSION_ID> --help
```

Para un review real, `ai_dev_loop` ejecuta:

```text
codex exec ... resume ... <SESSION_ID> -
```

No uses `--last`. El ID debe venir del contexto `SessionStart` o de una recuperacion manual confiable.

## Limitaciones

- El picker interactivo `codex resume` sin ID puede no listar chats Desktop.
- No edites la misma sesion simultaneamente desde Desktop y WSL.
- Leer archivos bajo `/mnt/c` es mas lento que ext4 nativo.
- Reanudar una sesion Desktop desde WSL y enviar prompt agrega turns al rollout de esa misma sesion.

## Diagnostico

```bash
ai_dev_loop integrations status --target codex-desktop-wsl --wsl-distro Ubuntu-22.04
ai_dev_loop integrations sessions status
ai_dev_loop integrations sessions list
```

Si el puente apunta mal, `status` debe reportarlo y `sessions list` debe rechazarlo.
