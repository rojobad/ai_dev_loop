# Codex Desktop en Windows con WSL

Usa `codex-desktop-wsl` cuando:

- Codex interactivo corre como aplicacion de escritorio en Windows.
- Cursor CLI y Codex CLI no interactivo corren dentro de WSL.
- El repositorio objetivo vive en WSL.

## Instalar

```bash
ai_dev_loop integrations install --target codex-desktop-wsl --wsl-distro Ubuntu-22.04
```

Si autodeteccion de la home Windows falla:

```bash
ai_dev_loop integrations install \
  --target codex-desktop-wsl \
  --wsl-distro Ubuntu-22.04 \
  --windows-codex-home "/mnt/c/Users/<usuario>/.codex"
```

El target desktop instala:

```text
/mnt/c/Users/<usuario>/.agents/skills/ai-dev-loop-handoff/SKILL.md
/mnt/c/Users/<usuario>/.agents/skills/ai-dev-loop-controller/SKILL.md
/home/<usuario>/.codex/hooks/ai_dev_loop_session_start.py
/mnt/c/Users/<usuario>/.codex/hooks.json
```

La definicion en `hooks.json` de Windows ejecuta el hook a traves de WSL:

```text
wsl.exe -d Ubuntu-22.04 --exec python3 /home/<usuario>/.codex/hooks/ai_dev_loop_session_start.py
```

El hook escribe metadata minima en el XDG state de WSL, no en estado de Windows.

El instalador no confia el hook automaticamente, no crea el puente de sesiones, no abre una sesion Codex nueva y no envia notificaciones.

## Control remoto / movil (A/B)

Con Desktop (incluido ChatGPT movil sobre la misma conversacion de planificacion):

1. En la sesion controller A, usa `ai-dev-loop-handoff` tras aprobar plan y prompt.
2. A crea un fork same-directory a B; B prepara el run y devuelve la identidad a A.
3. B queda inactiva.
4. Desde A, `ai-dev-loop-controller` lanza, consulta estado o aborta.

El estado es bajo demanda (`controller status`). Phase 14 no envia notificaciones push automaticas al movil ni a ChatGPT.

El puente de sesiones y el hook `SessionStart` no cambian de topologia respecto a fases anteriores.

## Puente de sesiones

`integrations install --target codex-desktop-wsl` no crea el puente de sesiones por defecto.

Verifica primero:

```bash
ai_dev_loop integrations sessions status
```

Si falta:

```bash
ai_dev_loop integrations sessions install
```

Tambien puedes pedirlo durante install, de forma explicita:

```bash
ai_dev_loop integrations install \
  --target codex-desktop-wsl \
  --wsl-distro Ubuntu-22.04 \
  --install-session-bridge
```

## Confiar el hook en Desktop

La confianza se gestiona en Codex Desktop, no en WSL:

1. Abre Codex Desktop.
2. Abre `/hooks`.
3. Confia el hook `ai_dev_loop`.
4. Reinicia o reanuda la sesion.

`ai_dev_loop` no intenta detectar ni modificar el estado de confianza. El status lo reporta como `unknown`.

## Verificaciones utiles

```bash
ai_dev_loop integrations status --target codex-desktop-wsl --wsl-distro Ubuntu-22.04
ai_dev_loop integrations sessions status
ai_dev_loop integrations sessions list
```

Para verificar resolucion de un ID desktop desde WSL sin enviar prompt:

```bash
codex exec resume <exact-desktop-session-id> --help
```

No uses `--last`. Usa solo el ID exacto de la sesion reviewer que preparo el run.

## Desinstalar

```bash
ai_dev_loop integrations uninstall --target codex-desktop-wsl --wsl-distro Ubuntu-22.04
```

La desinstalacion desktop:

- elimina ambos `SKILL.md` owned (`ai-dev-loop-handoff`, `ai-dev-loop-controller`);
- elimina la entrada de hook desktop en `hooks.json` de Windows;
- preserva el hook script WSL cuando puede ser usado por otro target;
- preserva el puente `from-desktop`;
- preserva estado XDG y run history;
- preserva archivos añadidos por el usuario dentro de los directorios de skill owned.
