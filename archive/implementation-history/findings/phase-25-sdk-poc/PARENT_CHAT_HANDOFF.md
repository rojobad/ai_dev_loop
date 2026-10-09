# Handoff para planes de Fase 25

Leer el informe completo:

[SDK_ADOPTION_REPORT.md](/tmp/cursor-sdk-poc-o80OgDfU/SDK_ADOPTION_REPORT.md)

POC aislada de Cursor SDK Python 1.0.37 completada sobre base ai_dev_loop HEAD 58944326a06cf319df521d595050fec091d870da. Fase 24 no se ejecutó. Ninguna modificación a repo principal, ledger/timer/instalación o runs CLI. 27 verificaciones pasan; producción aún necesita integración.

Confirmado: create con identidad inmediata; turnos y resume exacto entre procesos; edición; reglas, skill, AGENTS; MCP project y custom callback; tokens completos en turnos finalizados; consulta durable de resultados/usage y replay con offsets sin inferencia. Fixtures históricos propios se leen sin CLI y sin cambiar bytes.

Hallazgos que deben entrar en planes:

- Cursor SDK agent_id puede ser agent-UUID o UUID con custom callbacks. No sustituir/normalizar identidad ni relajar el tipo UUID compartido del reviewer Codex.
- YAML CLI grok-4.7-high no equivale a copiar SDK ID/defaults: congelar ID base + parámetros explícitos.
- Reaplicar modelo, settings, herramientas y callbacks al resume; conservar engine.sqlite3/artifacts como autoridad.
- Cancel normal detuvo el comando. SIGKILL del runtime dejó herramienta viva y run persistido running. Supervisar todo el árbol; launcher shell PID != Node PID.
- Recuperación **probada**: comprobar procesos muertos, bridge nuevo con mismo store, get_run exacto con runtime local/agentId/apiKey, run.cancel(), confirmar cancelled, resume mismo agente. Conservó memoria. Detached cancel sin registrar el run falló.
- No copiar cursor.force del CLI a SendOptions.local.force: ese campo SDK expira turnos activos; cambia la semántica.
- Manejar create/send ambiguos sin reenviar automáticamente; probar ventana antes de persistir SDK run_id.
- Usage puede ser null tras cancel/crash con consumo real: cobertura parcial/desconocida, nunca cero por ausencia. Deduplicar evento/resultado/replay. reasoning no se suma otra vez.
- Billing get_usage está feature_unavailable en esta cuenta; tokens por turno sí funcionan. No prometer USD.
- SDK 1.0.37 tiene guard de key que bloquea replay local con allow_api_key_env_fallback=False y supports('observe') false pese a observe funcional. Errores busy/feature_unavailable llegaron como InternalServerError.
- Custom callbacks necesitaron mcp/getMcpTools; no son obligatorios para migrar.
- Conservar lectores históricos versionados. status/history validan snapshots, así que conservar únicamente archivos de artifacts no basta.
- Codex no fue probado en vivo; su captura de usage es un contrato separado. Mantener Codex como reviewer final y su binding/modelo/effort congelados.

Oportunidades documentadas: métricas por turno/etapa, replay por cursor, recuperación de resultados sin inferencia, catálogo parametrizado, allowlists y callbacks propios; distinguir mejoras de la integración actual de capacidades que CLI ya tenía.

Para los planes futuros: exigir los contratos y pruebas pendientes del informe (worker/systemd simulado, flujo scheduler/sequence/recovery, captura/integridad/redacción, ventana de incertidumbre, métricas, históricos, gate pipeline). No presentar 27 checks de POC como aceptación de producción.

Agrupación posible de cinco subfases: fundamento SDK/configuración; identidad/worker/evidencia; continuidad/recovery; métricas/consulta histórica; integración/documentación/corte. Crear los planes cuando el usuario lo pida, desde la base actual y sin depender de implementación Fase 24.

La credencial suministrada no quedó en archivos; los procesos propios terminaron y el usuario fue informado de que puede revocarla. No incluir la clave en planes, prompts ni artifacts.

Este handoff está preparado en el filesystem compartido. No hubo herramienta para enviarlo automáticamente al chat padre. Copiar estos archivos a ubicación durable al incorporarlos: /tmp puede limpiarse.
