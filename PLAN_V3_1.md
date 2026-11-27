# Plan d'Exécution V3.1 — Isolation `ogy` / `agy` & Backup Intégral `.gemini`

> **Objectif** : permettre d'utiliser `ogy` (CLI / TUI) et `agy` (natif) **en simultané sans qu'aucun n'affecte l'autre**, tout en garantissant une **sauvegarde et restauration complète** des fichiers `.gemini` d'origine.

---

## 0. Contexte & Invariants

Aujourd'hui, `ogy init` (`ogy_cli/init_audit.py`) écrit directement dans les répertoires globaux d'`agy` (`~/.gemini/antigravity-cli/settings.json`, `~/.gemini/config/hooks.json`, `mcp_config.json`, `rules/`, `skills/`) en ne créant qu'un `.ogy.bak` par fichier JSON. Conséquence : toute session `agy` lancée directement par l'utilisateur hérite des hooks, de la `statusLine`, des règles et des MCP d'`ogy`.

Invariants à respecter pour V3.1 :
1. **Zéro régression chemin chaud V1 (100 % stdlib, < 25 ms)** : `ogy_core/*`, `ogy_hooks/cli.py`, `ogy_statusline/render.py` et `ogy_cli/init_audit.py` restent sans dépendance tierce.
2. **Protocole `agy 1.5.16` vérifié** : en `PreToolUse`, tout *pass-through* doit retourner `{"decision": "allow"}` (jamais `{}`).
3. **Compatibilité Windows-first & POSIX** : gestion propre des chemins (`Path.home()`, `USERPROFILE` vs `HOME`), des jonctions/symlinks et des permissions.
4. **Rétrocompatibilité** : les utilisateurs existants en mode partagé (`shared`) conservent un comportement fluide et peuvent basculer vers le mode isolé (`isolated`) ou restaurer leur `.gemini` d'origine à tout moment.
5. **Préparation V3.5 (Testabilité automatisée, voir [PLAN_V3_5.md](PLAN_V3_5.md))** : exposer `OGY_GEMINI_HOME` en surcharge d'environnement (comme `OGY_DB_PATH`), conserver des horodatages compatibles `time-machine` pour les snapshots `ogy backup`, et isoler la construction d'env/args dans `AcpClient` et `wrapper.py` pour faciliter l'injection du Fake ACP Agent stdio et des tests E2E TTY.

---

## Architecture Cible (2 Modes Complémentaires + Backup Complet)

```text
┌─────────────────────────────────────────────────────────────────────────┐
│ Mode 1 : Profil Isolé (Recommandé pour usage simultané ogy + agy)       │
│                                                                         │
│   agy (natif) ──────────────────────► ~/.gemini/ (100% vierge d'ogy)    │
│                                                                         │
│   ogy run / ogy tui ──(--settings / env isolé + OAuth partagé)──►       │
│                       ~/.local/share/optigravity/gemini-profile/.gemini/│
├─────────────────────────────────────────────────────────────────────────┤
│ Mode 2 : Profil Partagé avec Garde-fou OGY_ACTIVE=1                     │
│                                                                         │
│   agy (natif) ──(sans OGY_ACTIVE)──► ogy-hook : pass-through immédiat   │
│                                      statusline : sortie vide, 0 écrit. │
│   ogy run / ogy tui ──(OGY_ACTIVE=1)► hooks + firebreaks + telemetry OK │
└─────────────────────────────────────────────────────────────────────────┘
```

---

### Lot 3.1.1 — Garde-fou d'Exécution `OGY_ACTIVE=1` (Isolation Logique Immédiate)
* **Fichiers** : `ogy_cli/wrapper.py`, `ogy_frontends/acp_tui/client.py`, `ogy_hooks/cli.py`, `ogy_statusline/render.py`, `ogy_core/config.py`
* **Actions précises** :
  1. **Marquage des sessions lancées par `ogy`** :
     * Dans `run_agy()` (`ogy_cli/wrapper.py`) et `AcpClient.start()` (`ogy_frontends/acp_tui/client.py`), injecter `OGY_ACTIVE=1` dans l'environnement du sous-processus `agy` / `agy acp`.
  2. **Court-circuit *fail-open* hors `ogy`** :
     * Dans `ogy_hooks/cli.py` et `ogy_statusline/render.py`, si `os.environ.get("OGY_ACTIVE") != "1"` (et `config.isolate_hooks` actif, `True` par défaut) :
       * `PreToolUse` retourne immédiatement `{"decision": "allow"}` sans évaluer les firebreaks ni écrire dans `ogy.db`.
       * Les autres hooks (`SessionStart`, `PreInvocation`, `PostToolUse`, `PostInvocation`, `Stop`) retournent `{}` en < 2 ms sans aucune écriture SQLite.
       * `ogy-hook statusline` retourne une sortie vide (0 séquence ANSI, 0 snapshot DB).
  3. **Paramètre de configuration** :
     * Ajouter `[general] isolate_hooks = true` dans `OgyConfig` (`ogy_core/config.py`) et `ogy_cli/config_cmd.py` pour permettre à un utilisateur qui le souhaite explicitement d'activer les hooks même dans `agy` natif (`isolate_hooks = false`).
* **Critères « Done »** : une session `agy` lancée directement dans un terminal ne déclenche ni firebreaks, ni télémétrie `ogy.db`, ni statusline `ogy` ; `ogy run` et `ogy tui` fonctionnent normalement avec `OGY_ACTIVE=1`.

---

### Lot 3.1.2 — Backup & Restauration Intégrale de `.gemini` (`ogy backup` / `ogy restore`)
* **Fichiers** : `ogy_cli/init_audit.py`, `ogy_cli/main.py`, `ogy_core/config.py`, `tests/test_cli.py`
* **Actions précises** :
  1. **Snapshots horodatés complets** :
     * Avant toute écriture dans `cmd_init()`, créer un snapshot complet sous `~/.local/share/optigravity/backups/<YYYYMMDD-HHMMSS>/` (en plus des `.ogy.bak` existants pour compatibilité).
     * Contenu sauvegardé : `antigravity-cli/settings.json`, `config/hooks.json`, `config/mcp_config.json`, `config/rules/`, `config/skills/`, ainsi qu'un `manifest.json` listant chaque fichier, son hash SHA-256 et s'il existait avant `ogy init`.
     * Éviter les doublons inutiles si l'état courant est identique au dernier snapshot.
  2. **Commandes CLI dédiées** :
     * `ogy backup [--list]` : crée un snapshot manuel à la demande ou liste les snapshots disponibles (date, nb de fichiers, mode).
     * `ogy restore [<snapshot_id> | --latest] [--dry-run]` : restaure exactement l'état `.gemini` d'origine (remet les fichiers originaux et supprime les fichiers qui n'existaient pas dans le snapshot, ex. `ogy-hygiene.md`, `ogy-budget/SKILL.md`).
     * `ogy uninstall --restore-backups` : privilégie le snapshot initial s'il existe, avec fallback sur les `*.ogy.bak`.
* **Critères « Done »** : cycle `init` → modifications → `ogy restore --latest` restitue l'arborescence `.gemini` octet pour octet ; test unitaire couvrant fichiers préexistants + fichiers créés par `ogy`.

---

### Lot 3.1.3 — Mode Profil Isolé (`ogy init --isolated`) pour Coexistence Simultanée `ogy` + `agy`
* **Fichiers** : `ogy_cli/init_audit.py`, `ogy_cli/wrapper.py`, `ogy_frontends/acp_tui/client.py`, `ogy_core/config.py`, `ogy_cli/main.py`, `tests/test_cli.py`, `tests/test_acp_client.py`
* **Actions précises** :
  1. **Vérification technique préalable (`agy 1.5.16`)** :
     * Valider exactement quelles variables d'environnement (`USERPROFILE`, `HOME`, `APPDATA`, etc.) `agy.exe` utilise pour localiser `~/.gemini/config/` (`hooks.json`, `mcp_config.json`, `rules/`, `skills/`) et les credentials OAuth, en complément du flag officiel `agy --settings <path>` (*"Settings for this session only, never saved"*).
  2. **Profil séparé géré par `ogy`** :
     * Ajouter `profile_mode: "isolated" | "shared"` dans `OgyConfig` (`config.toml` `[general]`) et le choix lors de `ogy init` (`--isolated` / `--shared`).
     * En mode `isolated` :
       * **Aucune écriture** dans `~/.gemini/antigravity-cli/` ni `~/.gemini/config/` : le profil `agy` natif reste **100 % vierge**.
       * Les fichiers gérés par `ogy` (`settings.json`, `hooks.json`, `mcp_config.json`, `rules/ogy-hygiene.md`, `skills/ogy-budget/SKILL.md`) sont installés dans `~/.local/share/optigravity/gemini-profile/.gemini/`.
  3. **Partage sécurisé de l'authentification OAuth (sans duplication de secrets)** :
     * Lier uniquement les fichiers/dossiers d'authentification OAuth (et optionnellement `conversations/` si souhaité) depuis `~/.gemini/` vers le profil isolé (jonction NTFS sous Windows / symlink POSIX, avec garde-fou anti-boucle et vérification dans `ogy doctor`).
  4. **Lancement isolé depuis `ogy run` et `ogy tui`** :
     * `build_agy_args()` (`wrapper.py`) et `build_agy_acp_args()` (`client.py`) passent `--settings <isolated_settings.json>` et positionnent l'environnement du profil isolé au spawn du sous-processus `agy` / `agy acp`.
     * `ogy` et `agy` peuvent ainsi tourner **en parallèle dans deux terminaux** sans aucune interférence de configuration, de hooks, de MCP ou de règles.
  5. **Audit & Diagnostic (`ogy doctor`)** :
     * Afficher le `profile_mode` actif (`isolated` vs `shared`), vérifier l'intégrité du lien OAuth en mode `isolated` et signaler si des résidus `ogy-managed` traînent dans `~/.gemini/`.
* **Critères « Done »** : `ogy init --isolated` laisse `~/.gemini/` intact ; `ogy run` et `ogy tui` chargent les hooks/statusline/MCP/rules depuis le profil isolé tandis qu'un `agy` lancé en parallèle reste totalement natif.

---

## Récapitulatif V3.1

| Lot | Périmètre | Effet utilisateur |
| :--- | :--- | :--- |
| **3.1.1** | `OGY_ACTIVE=1` + `isolate_hooks` | `agy` natif n'est plus affecté par les hooks/statusline d'`ogy` même en mode partagé |
| **3.1.2** | `ogy backup` / `ogy restore` | Snapshot complet horodaté de `.gemini` + restauration exacte à l'état d'origine |
| **3.1.3** | `ogy init --isolated` + `--settings` | `~/.gemini/` reste 100 % vierge ; `ogy` et `agy` tournent en simultané sans aucune interférence |
