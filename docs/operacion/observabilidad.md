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
- proceso hijo activo si existe;
- ultimo error;
- siguiente accion segura;
- si es sucesor de recovery: run origen, checkpoint recuperado (`staging` | `reviewing` | `process_review` | `cursor`), y si aplica fingerprint verificado, adopcion historica, o modelo fallback congelado;
- para runs `failed` por limite de uso: siguiente accion con `recover --cursor-model auto` cuando el analisis lo marca elegible.

`status` debe seguir funcionando aunque el run lock este tomado por un `start`, `resume` o worker de `launch` activo.

## Controller status

```bash
ai_dev_loop controller status \
  --controller-session-id "<exact-controller-session-id>" \
  --repo-path /path/al/repo \
  [--run-id <run-id>] \
  [--output text|json]
```

Lookup read-only para runs A/B: no muta estado, no adquiere locks de mutacion y no llama Cursor/Codex.

Ademas del resumen seguro del run, reporta liveness del worker detachado (`launcher_live` / stale) y la siguiente accion segura. Ante 0 o N coincidencias, no elige un run por timestamp; usa `--run-id` para desambiguar.

Session IDs de controller y reviewer salen acortados en texto y como prefijos en JSON.

## Inspect

```bash
ai_dev_loop inspect <run-id>
ai_dev_loop inspect <run-id> --output json
```

Muestra rutas de artefactos, resumen de iteraciones, paths de reportes, diagnosticos de abort y lineage de recovery cuando existe.

Para checkpoint `cursor` (limite de uso), `inspect` puede listar:

- `git/cursor-output/NN.usage-limit-failure.json` (fingerprint de trabajo parcial en fallo);
- `git/cursor-output/NN.usage-limit-adopted.json` (metadata segura de adopcion historica explicita);
- `prompts/cursor-recovery/NN.usage-limit-continuation.txt` (envelope de continuacion);
- en el sucesor, `recovery.cursor_model_fallback`, `recovery.source_cursor_model` y hashes de fingerprint/envelope (sin contenido sensible en salida por defecto).

Eventos estructurados relevantes incluyen `cursor_usage_limit_detected` en el origen y `cursor_usage_limit_recovery_successor_created` en el sucesor.

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

Lista runs recientes encontrados en XDG state. Marca sucesores de recovery sin exponer session IDs. Es util cuando no recuerdas el `run-id`.

## Doctor

```bash
ai_dev_loop doctor
ai_dev_loop doctor --repo /path/al/repo
ai_dev_loop doctor --output json
```

`doctor` es read-only. Verifica runtime, permisos, CLIs externas, schemas, configuracion del repo y estado de integraciones. No instala ni repara por si solo.
