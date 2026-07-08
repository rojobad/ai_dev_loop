# Integracion WSL CLI

Usa `wsl-cli` cuando la sesion interactiva de Codex se ejecuta dentro de WSL.

## Instalar

```bash
ai_dev_loop integrations install --target wsl-cli
```

Instala o actualiza:

```text
~/.agents/skills/ai-dev-loop-handoff/SKILL.md
~/.codex/hooks/ai_dev_loop_session_start.py
~/.codex/hooks.json
```

La entrada de hook es equivalente a:

```json
{
  "matcher": "startup|resume|clear|compact",
  "hooks": [
    {
      "type": "command",
      "command": "python3 /home/<usuario>/.codex/hooks/ai_dev_loop_session_start.py",
      "statusMessage": "Loading ai_dev_loop session context"
    }
  ]
}
```

El instalador:

- preserva hooks no relacionados;
- hace merge idempotente;
- evita duplicados;
- valida `hooks.json` antes de escribir;
- hace backup antes de modificar;
- no toca el estado de confianza de Codex.

## Confiar el hook

Despues de instalar:

1. Abre Codex en WSL.
2. Ejecuta `/hooks`.
3. Confia el hook `ai_dev_loop` si aparece pendiente.
4. Reinicia o reanuda Codex para que `SessionStart` inyecte contexto.

Hasta que el hook este confiado, Codex puede omitirlo y `prepare` no tendra session ID automatico.

## Verificar

```bash
ai_dev_loop integrations status --target wsl-cli
ai_dev_loop doctor
```

Si el skill o hook difiere del paquete actual:

```bash
ai_dev_loop integrations install --target wsl-cli
```

## Desinstalar

```bash
ai_dev_loop integrations uninstall --target wsl-cli
```

La desinstalacion elimina solo assets de `ai_dev_loop` y preserva:

- hooks ajenos;
- skills ajenos;
- historial de runs;
- prompts;
- reviews;
- logs;
- estado XDG.
