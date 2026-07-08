# Estado, logs e inspeccion

Los comandos read-only permiten ver el estado de un run sin mutar el repositorio objetivo.

## Status

```bash
ai_dev_loop status <run-id>
ai_dev_loop status <run-id> --output json
```

Muestra:

- estado actual;
- repositorio;
- branch;
- HEAD inicial;
- iteracion actual y maximo;
- Cursor chat ID;
- Codex session ID acortado;
- proceso activo si existe;
- ultimo error;
- siguiente accion segura.

`status` debe seguir funcionando aunque el run lock este tomado por un `start` o `resume` activo.

## Inspect

```bash
ai_dev_loop inspect <run-id>
ai_dev_loop inspect <run-id> --output json
```

Muestra rutas de artefactos, resumen de iteraciones, paths de reportes y diagnosticos de abort.

Por defecto no imprime prompts completos.

Para imprimir prompts:

```bash
ai_dev_loop inspect <run-id> --show-prompts
```

Usa esa opcion solo en entornos donde el contenido del prompt pueda mostrarse.

## Logs

```bash
ai_dev_loop logs <run-id>
ai_dev_loop logs <run-id> --component ai_dev_loop
ai_dev_loop logs <run-id> --component cursor
ai_dev_loop logs <run-id> --component codex
```

Comportamiento:

- `ai_dev_loop`: log humano y eventos estructurados resumidos.
- `cursor`: artefactos y metadata de Cursor sin mostrar prompts completos.
- `codex`: resumen de reviews, paths y campos redacted.

El comando no imprime por defecto:

- prompt inicial completo;
- prompts de fix;
- staged patches;
- Markdown completo de review;
- JSONL crudo;
- auth payloads;
- session IDs completos en salida humana.

## List

```bash
ai_dev_loop list
ai_dev_loop list --project my-project
ai_dev_loop list --status completed
ai_dev_loop list --output json
```

Lista runs recientes encontrados en XDG state. Es util cuando no recuerdas el `run-id`.

## Doctor

```bash
ai_dev_loop doctor
ai_dev_loop doctor --repo /path/al/repo
ai_dev_loop doctor --output json
```

`doctor` es read-only. Verifica runtime, permisos, CLIs externas, schemas, configuracion del repo y estado de integraciones. No instala ni repara por si solo.
