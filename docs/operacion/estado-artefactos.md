# Estado y artefactos

`ai_dev_loop` guarda estado fuera del repositorio objetivo. Esto evita mezclar auditoria del orquestador con el codigo que Cursor modifica.

## Rutas XDG

Si las variables XDG existen:

```text
$XDG_CONFIG_HOME/ai_dev_loop
$XDG_STATE_HOME/ai_dev_loop
$XDG_CACHE_HOME/ai_dev_loop
```

Fallbacks:

```text
~/.config/ai_dev_loop
~/.local/state/ai_dev_loop
~/.cache/ai_dev_loop
```

Permisos esperados cuando el filesystem los soporta:

- directorios: `0700`;
- prompts, session IDs, output de agentes, patches y reviews: `0600`.

## Directorio de run

```text
$XDG_STATE_HOME/ai_dev_loop/runs/<project>/<run-id>/
├── state.json
├── effective-config.yaml
├── source-config.yaml
├── manifest.json
├── plan/
│   ├── plan.md
│   └── metadata.json
├── prompts/
│   ├── cursor-initial.txt
│   └── fixes/
├── cursor/
│   ├── chat.json
│   └── iterations/
├── codex/
│   ├── session-runtime.json
│   ├── events/
│   └── reviews/
├── git/
│   ├── baseline-status.txt
│   ├── status/
│   └── diffs/
├── logs/
│   ├── ai_dev_loop.log
│   └── events.jsonl
└── locks/
    ├── run.lock
    ├── abort-request.json
    └── active-process.json
```

No todos los archivos existen en todos los estados. Por ejemplo, `prompts/fixes/NN.txt` existe solo si Codex reporto findings en review `NN`.

`codex/session-runtime.json` registra la captura segura de Fase 10: prefijo del session ID, modelo/reasoning de sesion, origen y tipo de evento permitido. No contiene transcript ni la ruta absoluta del rollout.

En `state.json`, `codex` incluye:

```text
session_model
session_reasoning_effort
review_model
review_reasoning_effort
review_model_source       # session | explicit
review_reasoning_source   # session | explicit
```

En runs nuevos, los valores efectivos de review y sus fuentes quedan congelados en `prepare`. Runs historicos de Fase 9 con valores nulos y sin procedencia siguen siendo legibles por el camino legacy, pero no se reinterpretan como session-derived.

## Iteraciones

La numeracion es estable y empieza en `01`.

| Iteracion | Tipo | Prompt Cursor |
| --- | --- | --- |
| `01` | implementacion inicial | `prompts/cursor-initial.txt` |
| `02` | primera correccion | `prompts/fixes/01.txt` |
| `NN` | correccion | `prompts/fixes/{NN-1}.txt` |

Cada iteracion puede tener:

```text
cursor/iterations/NN/events.jsonl
cursor/iterations/NN/stderr.txt
cursor/iterations/NN/final.txt
cursor/iterations/NN/metadata.json

git/status/NN-before-cursor.txt
git/status/NN-after-cursor.txt
git/status/NN-before-staging.txt
git/status/NN-after-staging.txt
git/diffs/NN.stat
git/diffs/NN.name-only.txt
git/diffs/NN.patch

codex/events/NN.jsonl
codex/events/NN.stderr.txt
codex/reviews/NN.json
codex/reviews/NN.md
codex/reviews/NN.metadata.json
```

Los patches staged son snapshots acumulativos del index en esa iteracion, no necesariamente diffs incrementales.

## Locks

`ai_dev_loop` usa:

- un lock por run bajo el directorio del run;
- un lock por worktree bajo `$XDG_STATE_HOME/ai_dev_loop/repository-locks/`.

Esto previene dos loops mutando el mismo worktree simultaneamente.

## SessionStart records

El hook global guarda metadata minima de sesiones Codex bajo:

```text
$XDG_STATE_HOME/ai_dev_loop/codex-sessions/<session-id>.json
```

No guarda transcript content.
