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

Los workers desacoplados de `ai_dev_loop` eliminan `CODEX_HOME` y
`CODEX_SQLITE_HOME` heredados solo cuando apuntan a DrvFS (`/mnt/...`). Por eso
un fresh reviewer B usa la autenticacion, configuracion, sesiones y SQLite
nativos de WSL (`~/.codex` por defecto), aunque la sesion controladora A viva
en Codex Desktop. Los overrides que ya apunten a una ruta nativa de WSL se
conservan.

## Puente soportado

El puente permitido es un symlink anidado:

```text
~/.codex/sessions/from-desktop -> /mnt/c/Users/<usuario>/.codex/sessions
```

Esto expone rollouts `.jsonl` por session ID. No expone SQLite ni auth.

Durante `prepare`, `ai_dev_loop` puede buscar el UUID exacto tanto en sesiones nativas WSL como a traves de este puente validado. Lee JSONL en streaming y procesa solo `session_meta`, `thread_settings_applied` y `turn_context`, incluidos wrappers `event_msg`, para obtener modelo y reasoning. No conserva contenido del transcript ni persiste la ruta absoluta del rollout.

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

En runs nuevos, el comando incluye siempre `--model <modelo-efectivo>` y `-c model_reasoning_effort="<effort-efectivo>"`. La omision en YAML significa usar la captura de sesion realizada en `prepare`, no el default del Codex CLI WSL.

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
