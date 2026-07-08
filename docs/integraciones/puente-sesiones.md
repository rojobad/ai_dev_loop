# Puente de sesiones Desktop/WSL

El puente permite que `codex exec resume <session-id>` en WSL encuentre rollouts de sesiones originadas en Codex Desktop.

No comparte la home completa de Codex. Solo expone archivos rollout bajo `sessions/`.

## Topologia segura

```text
WSL Codex home:
/home/<wsl-user>/.codex

Windows Codex Desktop home:
/mnt/c/Users/<windows-user>/.codex

Symlink permitido:
/home/<wsl-user>/.codex/sessions/from-desktop
  -> /mnt/c/Users/<windows-user>/.codex/sessions
```

## Instalar puente

```bash
ai_dev_loop integrations sessions install
```

Opciones utiles:

```bash
ai_dev_loop integrations sessions install \
  --windows-codex-home "/mnt/c/Users/<usuario>/.codex"

ai_dev_loop integrations sessions install \
  --wsl-codex-home "/home/<usuario>/.codex"
```

## Estado

```bash
ai_dev_loop integrations sessions status
```

El status reporta:

- WSL Codex home;
- directorio WSL `sessions`;
- ruta del symlink `from-desktop`;
- destino del symlink;
- si el destino coincide con la home desktop resuelta;
- cantidad de rollouts alcanzables;
- warnings.

Un symlink que apunta al lugar equivocado se considera no saludable.

## Listar IDs de sesiones desktop

```bash
ai_dev_loop integrations sessions list
```

El comando lee nombres de archivo `rollout-*.jsonl` y extrae IDs de sesion desde esos nombres. No lee contenido de transcript ni rollout.

Limitar salida:

```bash
ai_dev_loop integrations sessions list --limit 10
```

Si quieres listar una carpeta concreta sin depender del puente:

```bash
ai_dev_loop integrations sessions list \
  --desktop-sessions-dir "/mnt/c/Users/<usuario>/.codex/sessions"
```

## Remover puente

```bash
ai_dev_loop integrations sessions remove
```

Solo elimina el symlink `from-desktop`. Rechaza borrar archivos o directorios que no sean symlink. No elimina sesiones de Windows.

## Estados prohibidos

No hagas esto:

```bash
export CODEX_HOME=/mnt/c/Users/<usuario>/.codex
```

No symlinkees toda la home:

```text
~/.codex -> /mnt/c/Users/<usuario>/.codex
```

No symlinkees todo el directorio WSL `sessions/` hacia Windows. El puente soportado es solo el symlink anidado `sessions/from-desktop`.

Motivos:

- Desktop y CLI pueden tener esquemas SQLite distintos.
- SQLite sobre DrvFS puede tener problemas de locking/corrupcion.
- Compartir auth/config/indices entre superficies aumenta riesgo operativo.
