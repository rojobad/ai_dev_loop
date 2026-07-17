# Phase 15.6 — Acuse y finalización verificable de revisiones GitHub

## Goal

Corregir el worker de revisión de PR para que pueda finalizar un ciclo cuando el
revisor configurado publica una confirmación verificable de que revisó el
`bound_head_sha` y no dejó hilos accionables. El comportamiento debe cubrir los
ciclos Phase 15 de un run fuente y Phase 15.5 de un PR independiente, sin crear
un chat Cursor ni publicar cambios cuando no hay feedback que corregir.

Además, exponer una señal operativa de salud mientras se espera al bot: la
reacción `eyes` del reviewer permitido sobre el comentario que contiene
`@codex review`. Esta señal confirma que el trigger fue observado, pero **no**
completa ni falla el ciclo por sí sola.

El incidente que motiva el trabajo fue el run
`crypto-sentinel-20260717T161324Z-1caaa3`: el bot publicó una respuesta de
revisión positiva para `83b5e2a652...`, pero el worker sólo consultaba hilos
inline y permaneció en `awaiting_bot_review`.

## Non-Goals

- No modificar, resolver, contestar ni volver a pedir una revisión GitHub.
- No cambiar la política de SSH, commits, push, PR creation o merge.
- No usar una ausencia de hilos como señal de éxito.
- No ejecutar Cursor, Codex, una revisión real de GitHub ni migrar runs ya
  terminados como parte de esta implementación.
- No incluir cuerpos de comentarios de GitHub en el estado ni en logs normales.

## Scope

- Cliente `gh` tipado para leer comentarios generales del PR y un matcher puro
  para una señal de finalización sin hallazgos.
- Cliente `gh` tipado para leer las reacciones del comentario trigger y una
  observación de acuse `eyes` del bot configurado.
- Contrato de configuración/estado/auditoría para ambas señales.
- Transición segura desde `awaiting_bot_review` a `completed`.
- Cobertura unitaria, de integración, CLI/status y documentación operativa.

## Out of Scope

- Cambiar el formato emitido por `chatgpt-codex-connector`.
- Inferir aprobación mediante NLP, traducciones o frases similares no
  configuradas.
- Tratar comentarios manuales, reviews antiguas, comentarios de otros bots o
  comentarios relativos a otro SHA como completitud válida.
- Reanudar automáticamente un run abortado; seguirá requiriendo el comando de
  inicio/reanudación existente cuando corresponda.
- Activar o gestionar desde ai_dev_loop la opción remota de **Automatic
  reviews** de Codex. El ciclo ya publica `@codex review`; habilitar ambas vías
  puede producir revisiones duplicadas.

## Required Context

- Worker actual: `src/ai_dev_loop/commands/pr_review.py`, especialmente
  `_run_pr_review_worker_loop_inner`.
- Adaptador GitHub: `src/ai_dev_loop/runners/github.py`; ya existe el tipo
  `GithubIssueComment`, pero se debe verificar y completar su lectura/uso.
- Estado: `src/ai_dev_loop/state.py` y
  `src/ai_dev_loop/schemas/run-state-v1.json`.
- Configuración: `src/ai_dev_loop/config.py`.
- Tests base: `tests/unit/test_github_client.py`,
  `tests/integration/test_phase15_pr_review.py` y
  `tests/integration/test_phase15_5_independent_pr_review.py`.
- El repositorio de destino CryptoSentinel configura el revisor aceptado como
  `chatgpt-codex-connector`; la solución debe permanecer genérica y obedecer
  `github.reviewer_logins`.
- La documentación pública de Codex describe la reacción como acuse y después
  la publicación de una revisión, pero no ofrece configuración para cambiar el
  emoji, controlar su retirada, imponer un cuerpo de aceptación ni recibir un
  webhook de finalización. El plan por tanto trata la reacción sólo como
  telemetría y el comentario final como contrato local configurable.
- Evidencia observada en PR #42: un comentario general de
  `chatgpt-codex-connector`, posterior al trigger, con el prefijo
  `Codex Review: Didn't find any major issues.` y
  `**Reviewed commit:** \`83b5e2a652\``. El texto se convierte en configuración
  explícita, no en una heurística implícita.

## Cursor Rules And Skills

Lee y aplica antes de editar las reglas locales bajo `.cursor/rules/` que
correspondan a ai_dev_loop, en particular las de arquitectura, seguridad,
privacidad, testing, documentación, CLI/estado y calidad. Si existe una regla
más específica para GitHub o Phase 15, prevalece sobre esta lista.

Para documentación y aceptación usa la skill
`ai-dev-loop-docs-acceptance-governance`. No uses la skill de revisión staged
durante la implementación: esa revisión la realizará Codex después.

## Architecture Guardrails

- La finalización sólo puede derivarse de una señal explícita, determinista y
  configurada; nunca de `eligible == []`, de la presencia de `eyes`, ni de la
  desaparición de esa reacción.
- Validar simultáneamente: PR actual abierto, `bound_head_sha` exacto, autor en
  `reviewer_logins`, comentario posterior a `request_created_at`, y una prueba
  textual/estructural aprobada que vincule la señal al SHA solicitado.
- Si la respuesta del bot sólo contiene lenguaje natural ambiguo, falta fecha,
  falta el SHA, es anterior a la solicitud, o compite con un hilo elegible,
  continuar el polling existente; no completar el run.
- Antes de persistir la finalización, volver a leer los hilos bajo el lock o
  tras una barrera de estabilización definida. Un hilo elegible prevalece sobre
  la señal de “sin hallazgos”, evitando carreras de consistencia eventual.
- Persistir únicamente metadatos auditables y no sensibles (id, fecha, regla y
  hash del cuerpo), nunca el cuerpo del comentario. Mantener compatibilidad de
  lectura para estados Phase 15/15.5 anteriores.
- La transición debe ser idempotente y segura frente a reinicios: un run ya
  completado no vuelve a consultar, crear chat Cursor, hacer commit/push,
  resolver hilos ni publicar un nuevo `@codex review`.
- Mantener el timeout actual si no existe una señal verificable.
- La ausencia del acuse `eyes` al vencer su plazo es diagnóstica, no terminal:
  no reintenta el trigger, no crea un segundo comentario y no aborta ni
  completa el worker. Una reacción puede no ser observada entre dos polls.
- La desaparición de un `eyes` previamente observado debe conservarse como
  telemetría opcional (`acknowledgement_cleared_at`), pero el worker continúa
  esperando el resultado final verificable.
- No ampliar permisos de `gh` ni introducir tokens, secretos o llamadas de
  shell no tipadas.

## Implementation Plan

1. Definir en `GithubSection` una política segura y explícita, desactivada por
   defecto para mantener compatibilidad con repositorios existentes. La
   configuración de un repositorio que quiera la función debe declarar:

   ```yaml
   github:
     acknowledgement:
       enabled: true
       reaction: eyes
       timeout_seconds: 300
       on_timeout: diagnostic_only
     no_findings_completion:
       enabled: true
       accepted_comment_prefixes:
         - "Codex Review: Didn't find any major issues."
       reviewed_commit_prefix_length: 12
   ```

   Los nombres finales deben seguir las convenciones del modelo de configuración
   existente, pero la semántica anterior es obligatoria. Validar prefijos,
   timeout, longitud SHA y `on_timeout`; no aceptar listas vacías cuando la
   función esté habilitada. Documentar que **Automatic reviews** de Codex debe
   permanecer apagado mientras ai_dev_loop publica el trigger explícito.
2. En `runners/github.py`, implementar o completar consultas paginadas y
   tipadas para: (a) comentarios generales del PR y (b) reacciones del
   comentario trigger. Crear matchers puros separados: uno para el acuse
   `eyes` de un reviewer permitido y otro para finalización sin hallazgos.
   Sanitizar errores y no trasladar cuerpos de comentarios fuera del artefacto
   de lectura necesario.
3. El matcher de finalización debe exigir exactamente: comentario general de
   un login en `reviewer_logins`, posterior a `request_created_at`, prefijo en
   `accepted_comment_prefixes`, y una línea estructural `Reviewed commit:` cuyo
   SHA coincida con los primeros `reviewed_commit_prefix_length` caracteres de
   `bound_head_sha`. La comparación debe ser sensible al contrato configurado,
   no a similitud de frases.
4. Extender `GithubPrReviewState` y su JSON Schema con estructuras opcionales
   de evidencia de acuse y finalización. El acuse conserva id del comentario
   trigger, reacción, primer instante observado, y opcionalmente el instante
   en que dejó de observarse/expiró el plazo. La finalización conserva id de
   comentario, timestamp, identificador de regla y SHA-256 del cuerpo. Mantener
   compatibilidad de lectura para estados Phase 15/15.5 anteriores y no exponer
   cuerpos en `status` ni logs.
5. Reordenar el polling de `pr_review.py` sin introducir un estado terminal
   adicional:
   - mientras esté en `awaiting_bot_review`, observar y persistir el acuse
     cuando exista;
   - al vencer `acknowledgement.timeout_seconds` sin acuse, persistir una
     advertencia diagnóstica y continuar el polling normal;
   - identificar hilos elegibles primero; sólo si no los hay, buscar la señal
     final configurable;
   - antes de completar, releer PR/hilos bajo lock o mediante la barrera de
     estabilización definida; un hilo elegible nuevo gana sobre el comentario
     positivo;
   - con la evidencia válida, persistir `lifecycle=completed`, estado terminal
     y mensaje sanitizado. La retirada del emoji no altera esta decisión.
6. Revisar los comandos `status`, `resume`, `continue`, `abort` y los flujos
   Phase 15.5 para que una finalización sin hallazgos tenga semántica coherente
   y no pueda reanimar accidentalmente un run completado.
7. Actualizar README, referencias de configuración/CLI/trazabilidad y
   troubleshooting. Explicar qué se reconoce, qué no, cómo diagnosticar un
   worker que no recibió el acuse, que la retirada de `eyes` no es finalización,
   que Automatic reviews se mantiene apagado para este flujo, y cómo reiniciar
   manualmente sin editar el estado.

## Testing Criteria

- Unitarios del cliente/matcher:
  - acepta sólo un comentario posterior de un reviewer permitido que satisface
    exactamente el protocolo aprobado y está ligado al SHA;
  - rechaza autor distinto, comentario antiguo, SHA distinto/ausente,
    timestamp ausente, formato incompleto, frase libre no aprobada y un
    comentario previo de una revisión manual;
  - cubre paginación, errores `gh` sanitizados y no exposición de cuerpos.
  - acepta `eyes` sólo del reviewer permitido sobre el comentario trigger;
    rechaza otro emoji, otro autor, reacción sobre otro comentario y datos
    incompletos;
  - una reacción no observada o retirada entre polls nunca se convierte en una
    señal negativa ni positiva de finalización.
- Integración Phase 15 y Phase 15.5:
  - sin hilos + señal válida finaliza el run;
  - no crea chat Cursor, no invoca adjudicación Codex, no hace commit/push, no
    resuelve hilos ni publica un nuevo trigger;
  - un hilo elegible observado antes o durante la revalidación gana sobre la
    señal positiva;
  - sin señal válida mantiene polling/timeout actual;
  - la señal `eyes` observada se muestra como acuse y el vencimiento de su plazo
    queda como diagnóstico, pero ambos caminos continúan esperando el resultado
    final sin publicar otro trigger;
  - el comentario final puede completar aun si jamás se observó `eyes`, porque
    la reacción es deliberadamente una señal best-effort;
  - el estado reabierto después de completar es idempotente y conserva la
    evidencia sin cuerpos; los metadatos de acuse no cambian la terminalidad.
- Regresión de esquema/config: estados históricos y YAML sin la nueva política
  validan correctamente.

## Validation

Ejecutar, ajustando sólo si las reglas del repositorio especifican equivalentes:

```bash
uv run pytest tests/unit/test_github_client.py tests/integration/test_phase15_pr_review.py tests/integration/test_phase15_5_independent_pr_review.py
uv run pytest
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src
uv run mkdocs build --strict
git diff --check
```

No realizar aceptación contra un PR real sin autorización explícita del usuario.
En la entrega, reportar los comandos ejecutados, resultados, archivos tocados,
el protocolo elegido y cualquier limitación restante.

## Risks Or Recovery Notes

- El texto del bot puede cambiar. Por eso el reconocimiento depende de un
  prefijo configurado y de `Reviewed commit:`, no de similitud lingüística. Un
  cambio del bot se recupera ajustando la configuración y relanzando, nunca
  relajando el matcher en caliente.
- Las reacciones no tienen historial fiable una vez retiradas. Son telemetría
  best-effort para detectar triggers que parecen no iniciar, no evidencia de
  resultado ni mecanismo de reintento.
- GitHub puede entregar comentarios e hilos en órdenes diferentes; la segunda
  lectura antes de completar evita cerrar una ronda que ya tiene observaciones.
- Si el contrato del bot no puede ser estable, mantener el timeout/polling es
  más seguro que declarar una revisión aprobada por inferencia.
- La recuperación normal es abortar el worker y relanzar/reanudar con los
  comandos existentes; nunca editar manualmente el estado del run.

## OpenQuestions

None.
