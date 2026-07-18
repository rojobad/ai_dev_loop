# Phase 15.14 — Preflight SSH con `IdentityAgent` efectivo

## Goal

Corregir el preflight de publicación para que valide el mismo `ssh-agent` que
Git/SSH usará realmente cuando el worker detached no hereda `SSH_AUTH_SOCK`.

El agente persistente de este entorno sí contiene una clave en
`~/.ssh/ai-dev-loop-ssh-agent.sock`, configurado mediante `IdentityAgent` en
`~/.ssh/config`. Un `git push` funciona porque OpenSSH evalúa esa configuración.
Sin embargo, `verify_ssh_push_ready()` ejecuta `ssh-add -l` sin entorno: con
`SSH_AUTH_SOCK` vacío, `ssh-add` no lee `IdentityAgent` y falla antes del
commit/push. Phase 15.13 convirtió ese fallo en recuperable, pero aún no
selecciona correctamente el socket efectivo.

Esta fase debe resolver de forma segura el agente efectivo de OpenSSH para el
host remoto y pasar sólo ese socket a `ssh-add -l`, preservando el fallo tipado
si no hay identidad. Debe permitir recuperar y publicar PR #45 desde el
checkpoint `publication_pre_commit` sin pedir passphrase al worker.

## Non-Goals

- No cargar claves, solicitar passphrases, almacenar secretos, retirar la
  passphrase ni crear una deploy key sin cifrar.
- No reemplazar SSH por HTTPS/PAT ni cambiar `gh`, hooks, systemd, keychain o
  archivos SSH del usuario.
- No probar conectividad o autenticación contra GitHub real durante tests.
- No cambiar la recuperación `publication_pre_commit`, la semántica A/B,
  commit/push no-force, ni el flujo de PR/revisión.

## Scope

- `src/ai_dev_loop/runners/publish.py`: resolver host SSH, consultar la
  configuración efectiva de OpenSSH y ejecutar `ssh-add` con el socket
  correcto.
- Tests unitarios del runner de publicación y regresiones de `github doctor` /
  publicación donde corresponda.
- README y documentación operativa/CLI que hoy afirman que la configuración
  `IdentityAgent` basta, para que describan exactamente la validación efectiva.

## Out of Scope

- Ejecutar `recover`, `resume`, `launch`, Cursor, Codex, GitHub, SSH, commit o
  push reales durante implementación o validación.
- Ampliar el parser a esquemas de remotos no soportados por el contrato actual,
  ni aceptar entradas ambiguas/controladas como opciones de `ssh`.
- Cambiar modelos, schemas, estados o reason codes de Phase 15.13 salvo que un
  ajuste estrictamente necesario de tests pruebe una incompatibilidad real.

## Required Context

- Evidencia local: `SSH_AUTH_SOCK` está vacío; `ssh-add -l` falla; pero
  `SSH_AUTH_SOCK=$HOME/.ssh/ai-dev-loop-ssh-agent.sock ssh-add -l` encuentra la
  identidad y `ssh -G git@github.com` informa
  `identityagent /home/rojobad/.ssh/ai-dev-loop-ssh-agent.sock`.
- `git push` sí funciona porque SSH consulta `~/.ssh/config`. El worker se
  detiene antes de llegar a Git porque `verify_ssh_push_ready()` usa
  `run_process(["ssh-add", "-l"])` sin `env`.
- `run_process` acepta un `env` explícito y usa argv + `shell=False`.
- Phase 15.13 ya define `SshAgentNoIdentityError`, que debe seguir siendo el
  único error tipado recuperable durante publicación; no se debe convertir un
  `ValidationError` genérico en interrupción.
- El remote puede estar en formato SCP (`git@alias:owner/repo.git`) o URL SSH
  (`ssh://user@alias[:port]/owner/repo.git`); el alias, no sólo el hostname
  final, es el input que debe consultarse con `ssh -G` para respetar bloques
  `Host` del usuario.

## Cursor Rules And Skills

Leer y aplicar todas las reglas de `.cursor/rules/`: `ai-dev-loop-governance.mdc`,
`ai-dev-loop-orchestrator-contracts.mdc`, `ai-dev-loop-state-and-schema-contracts.mdc`,
`ai-dev-loop-loop-and-resume-contracts.mdc`, `ai-dev-loop-codex-review-contracts.mdc`,
`ai-dev-loop-abort-contracts.mdc`, `ai-dev-loop-global-integrations-contracts.mdc` y
`ai-dev-loop-docs-acceptance-contracts.mdc`.

Para documentación usar `ai-dev-loop-docs-acceptance-governance`. No usar la
skill staged-review durante la implementación. Usar fakes de `ssh`, `ssh-add`,
Git y `gh`; nunca agentes, sockets, claves ni red reales.

## Architecture Guardrails

- La fuente de verdad para el socket preferido es la configuración efectiva que
  OpenSSH devuelve para el destino remoto, obtenida con argv estructurado
  `ssh -G <destination>` y sin conexión de red. Nunca parsear `~/.ssh/config`
  directamente ni adivinar el socket fijo de ai_dev_loop.
- Extraer el destino de URL SSH con parser explícito y validación estricta. El
  resultado debe ser un único argumento de destino, sin whitespace, saltos de
  línea, NUL, prefijo de opción ni caracteres que puedan reinterpretarse como
  flags. Soportar sólo los dos formatos SSH que el producto ya acepta.
- Parsear sólo la entrada `identityagent` de la salida efectiva; rechazar salida
  ambigua, repetida, vacía, `none`, no absoluta, fuera de una ruta de socket
  válida o no existente. No exponer en errores, logs, eventos o JSON la salida
  completa de `ssh -G`, el socket ni el entorno.
- Si no hay `identityagent` efectivo, usar únicamente el `SSH_AUTH_SOCK`
  heredado si es un socket Unix válido. Si ambos faltan o no son utilizables,
  lanzar el mismo `SshAgentNoIdentityError` seguro y tipado. No hacer fallback
  a una ruta hard-codeada ni modificar el entorno del proceso padre.
- Ejecutar `ssh-add -l` con un entorno mínimo derivado de `os.environ` y el
  `SSH_AUTH_SOCK` elegido. Conservar `PATH` y variables necesarias para
  ejecución, pero no persistir/imprimir el entorno.
- Mantener timeout, argv estructurado y `shell=False`. Los problemas de formato
  de remote/configuración son `ValidationError` terminales; sólo "no identity"
  conserva el outcome `ssh_agent_no_identity` de Phase 15.13.
- `github doctor`, la publicación inicial y la publicación de correcciones
  deben usar el mismo helper, por lo que su diagnóstico debe coincidir con el
  push posterior. Ninguna ruta puede invocar Cursor/Codex o crear GitHub writes
  durante la comprobación.

## Implementation Plan

1. Extraer helpers privados y testeables para: identificar/validar un remote
   SSH; construir el destino de `ssh -G` conservando el alias; ejecutar y
   parsear su salida allowlisted; y elegir el socket efectivo entre
   `IdentityAgent` y el entorno heredado.
2. Integrar el helper en `verify_ssh_push_ready()`: consultar primero el remote,
   resolver el socket efectivo y pasar una copia del entorno con
   `SSH_AUTH_SOCK` explícito a `ssh-add -l`. Mantener los mensajes públicos
   concisos y sin rutas/salida SSH.
3. Definir los fallos seguros: formato remoto/configuraciones ambiguas o
   inseguras deben ser `ValidationError`; falta de socket/identidad y retorno
   no cero de `ssh-add` deben ser `SshAgentNoIdentityError`. Verificar que el
   clasificador Phase 15.13 no requiere cambios amplios.
4. Ajustar README, troubleshooting, flujo completo y referencia CLI para
   explicar que `IdentityAgent` se resuelve mediante OpenSSH efectivo y que el
   worker no depende de heredar `SSH_AUTH_SOCK`; conservar los comandos de
   carga de clave manual.
5. Añadir la fase a trazabilidad sólo después de que código, docs y tests estén
   alineados. No editar `ai_dev_loop.yaml` ni skills control-plane.

## Testing Criteria

- Unitario: con `SSH_AUTH_SOCK` vacío y fake `ssh -G` que devuelve un
  `identityagent` absoluto/socket válido, comprobar que `ssh-add -l` recibe ese
  socket y publication preflight pasa. No usar sockets reales; el fake debe
  registrar sólo un indicador seguro de que recibió la variable.
- Precedencia: un `IdentityAgent` efectivo válido vence al socket heredado; sin
  `IdentityAgent`, un socket heredado válido funciona; `none` + socket heredado
  válido sigue funcionando según el fallback documentado.
- Rechazos: remote HTTP/no SSH, SCP/URL malformados, destino que parece opción,
  salida `ssh -G` ambigua, `identityagent` relativo/no existente/no socket,
  ausencia de socket e `ssh-add` sin identidad. Confirmar que no se filtra
  ruta, stdout/stderr ni entorno y que sólo la ausencia de identidad usa
  `SshAgentNoIdentityError`.
- Regresión: publicación no crea commit cuando el preflight falla; con socket
  efectivo válido conserva la llamada normal de publicación. Probar también
  `github doctor` mediante el helper compartido, sin red ni GitHub real.
- Ejecutar regresiones Phase 15.13 para comprobar que los outcomes
  `interrupted`/`failed` y recovery `publication_pre_commit` siguen intactos.

## Validation

- Ejecutar los nuevos tests de publish/preflight, `tests/unit/test_phase15_13_ssh_agent_interrupt.py`, `tests/integration/test_phase15_13_publication_pre_commit_recovery.py` y los tests de `github doctor` relacionados, todos con `TMPDIR=/tmp TEMP=/tmp TMP=/tmp`.
- Ejecutar suite completo: `TMPDIR=/tmp TEMP=/tmp TMP=/tmp uv run pytest`.
- Ejecutar `uv run ruff format --check src tests`, `uv run ruff check src tests`, `uv run mypy src`, `uv run mkdocs build --strict` y `git diff --check`.
- No usar la clave, el socket, SSH ni GitHub reales como validación de Cursor.

## Risks Or Recovery Notes

- Phase 15.13 ya permite recuperar el run histórico, pero no hay que ejecutar
  aún su `resume`: con el preflight actual volvería a detenerse antes del push.
- Tras aceptación, instalación local y carga manual de la clave persistente,
  ejecutar `pr-review recover --dry-run` sobre
  `crypto-sentinel-20260718T212910Z-9488fe`, crear el sucesor y ejecutar su
  `resume_command` desde A. El resume debe publicar sin agentes.
- Si la configuración efectiva de SSH no puede resolverse de forma segura, el
  producto debe detenerse antes de commit/push y explicar sólo la acción segura
  al usuario.

## OpenQuestions

None.
