# Confianza de hooks

Codex exige que los hooks nuevos o modificados sean confiados manualmente. `ai_dev_loop` instala la definicion, pero no salta ni edita ese mecanismo de confianza.

## Que hace el hook

El hook `SessionStart` recibe JSON de Codex y escribe metadata minima:

```text
$XDG_STATE_HOME/ai_dev_loop/codex-sessions/<session-id>.json
```

Si `XDG_STATE_HOME` no esta definido:

```text
~/.local/state/ai_dev_loop/codex-sessions/<session-id>.json
```

Guarda:

- session ID;
- modelo;
- cwd;
- path de transcript como string, si Codex lo provee;
- source (`startup`, `resume`, `clear`, `compact`);
- timestamp.

No lee transcripts, no copia contenido y no guarda tokens.

## Contexto inyectado

Cuando el hook corre correctamente, agrega contexto para Codex indicando:

- que `ai_dev_loop` esta activo;
- el session ID exacto;
- el modelo actual;
- el directorio de trabajo;
- que el handoff debe usar ese ID exacto;
- que no se debe usar `--last`.

## Como confiarlo

Para `wsl-cli`:

1. Abre Codex CLI dentro de WSL.
2. Ejecuta `/hooks`.
3. Confia el hook `ai_dev_loop`.

Para `codex-desktop-wsl`:

1. Abre Codex Desktop en Windows.
2. Ejecuta `/hooks`.
3. Confia el hook `ai_dev_loop`.

Despues reinicia o reanuda la sesion para disparar `SessionStart`.

## Si falta el contexto

Ejecuta:

```bash
ai_dev_loop integrations status --target wsl-cli
ai_dev_loop integrations status --target codex-desktop-wsl --wsl-distro Ubuntu-22.04
ai_dev_loop integrations sessions status
```

Verifica:

- el target elegido corresponde a tu superficie de Codex;
- el hook esta instalado en la home que Codex realmente lee;
- `/hooks` ya fue confiado;
- la sesion fue reiniciada o reanudada despues de confiar;
- en Desktop, el puente de sesiones esta saludable si necesitas reanudar desde WSL.

Como fallback, puedes pasar `--codex-session-id` a `prepare`, pero solo con un ID exacto obtenido de una fuente confiable.
