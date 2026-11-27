# Plan d'Exécution V3.7 — Parité `agy` Complète × Super-Pouvoirs `ogy` × Finition Studio Pro

> **Sources consolidées** : [PLAN_V3_6.md](PLAN_V3_6.md), [PLAN_V3_5.md](PLAN_V3_5.md), [AUDIT_UX_ET_ARCHITECTURE.md](AUDIT_UX_ET_ARCHITECTURE.md) et inspection complète du code V3.6 (119/119 tests verts, 20 snapshots).
>
> **Objectif** : faire passer Optigravity (`ogy`) au niveau d'un **compagnon développeur Studio Pro** qui réunit **100 % des basiques quotidiens d'`agy`** (shell inline `!cmd`, plan/checklist agent ACP, commandes dynamiques `agy`, `/diff`, `/attach` multimodal, `/fork`, `--resume`/`--continue`, `Shift+Tab` mode cycle, `/context`, `/mcp`, `/compact`, `/config`) **ET** tous les **super-pouvoirs exclusifs d'`ogy`** (Switch Tax `$` visible dans le sélecteur de modèles, économies de cache `$` temps réel, `!w` via `ogy wrap`, inspection firebreaks/règles/MCP, isolation V3.1, alerte budget hard cap, 3 thèmes 100 % complets y compris modales).

---

## 0. Invariants V3.7 & Audit Critique Pré-Implémentation

### 6 Invariants Non Négociables
1. **Chemin chaud V1 100 % stdlib préservé** : `ogy_core/`, `ogy_hooks/`, `ogy_statusline/`, `ogy_cli/` conservent **0 dépendance externe** (latence hook `< 25 ms`, démarrage instantané).
2. **Isolation V3.1 intacte** : `OGY_ACTIVE=1`, `profile_mode = "isolated" | "shared"`, et sauvegarde/restauration `.gemini` restent strictement inchangés.
3. **Zéro blocage sur le thread UI Textual** : toute exécution shell (`!cmd`), commande `git` (`/diff`), scan fichiers (`/context`, `/mcp`, `/attach`), ou accès SQLite s'exécute via `asyncio.create_subprocess_shell`, `asyncio.to_thread` ou `@work(thread=True)`.
4. **Zéro *scroll hijacking*** : les nouveaux widgets (`PlanCard`, `ShellCard`) utilisent exclusivement `SessionTranscript._mark_new_content()` et respectent `anchor()` / `release_anchor()`.
5. **Conformité ToS Section 6 stricte** :
   * `Shift+Tab` cycle uniquement sur `default → accept-edits → auto → plan` (**jamais `yolo`**).
   * Toute bascule de modèle/effort ou rotation reste 100 % initiée par l'utilisateur.
   * L'alerte `budget.hard_cap_usd` est informative et non bloquante.
6. **Rétrocompatibilité totale des 119 tests existants** : les commandes `/model`, `/effort`, `/mode`, `/theme` sans argument conservent leur notice inline non bloquante ; le sélecteur interactif `QuickSelectModal` est accessible via `Alt+M`, `/select` et `Ctrl+P`.

### Analyse des Risques & Mitigations Intégrées

| Risque identifié | Impact potentiel | Mitigation V3.7 |
| :--- | :--- | :--- |
| **Modale poussée par `/model` sans argument** | Casserait les tests existants (`test_tui_app.py:124`) qui enchaînent `/model` puis `/btw` et `Ctrl+C` sans fermer de modale. | `/model` sans arg conserve la notice status (+ mention `Alt+M`) ; le sélecteur interactif s'ouvre via **`Alt+M`**, **`/select [model\|effort\|mode\|theme]`** ou la palette **`Ctrl+P`**. |
| **Commande shell `!cmd` longue ou interactive** | Pourrait geler la TUI ou attendre `stdin` indéfiniment. | `asyncio.create_subprocess_shell` avec `stdin=DEVNULL`, timeout 30 s, troncature sortie à 12 Ko dans `ShellCard` + option `!w <cmd>` via `ogy wrap`. |
| **Collision `Shift+Tab` dans les modales** | `Shift+Tab` sert à reculer le focus dans les formulaires (`ElicitationModal`, `DelegationModal`). | `action_cycle_mode` vérifie `if isinstance(self.screen, ModalScreen): return` afin de ne jamais perturber les modales. |
| **Thème clair / haut contraste incohérent sur modales** | En V3.6, `.theme-light` ne stylait pas `.modal-dialog`, `#proposal-bar`, `#activity-bar`, `ThoughtBlock` ni `.copy-md-btn` → boîtes noires sur fond blanc. | Couverture CSS exhaustive des 3 thèmes sur **100 %** des widgets, barres et modales. |
| **Commandes slash dynamiques d'`agy` rejetées** | Une commande serveur (ex: `/review`, `/plan`) renvoyait `Commande inconnue` au lieu d'être transmise à `agy`. | Capture de `available_commands_update` (ACP v1) : fusion dans l'autocomplétion `/` avec tag `[agy]` et routage direct vers `_dispatch_prompt`. |
| **Flag `--lang` de `ogy tui` ignorait `config.toml`** | `ogy tui` forçait `--lang fr` par défaut même si `general.language = "en"` dans `config.toml`. | `--lang` par défaut `None` → utilise `load_config().language` (fallback `"fr"` si absent). |

---

## PHASE 1 — Parité Protocole ACP Natif & Basiques `agy` (`ogy tui`)

### Lot 3.7.1 — Protocole ACP Natif Complet : `PlanCard`, Commandes Dynamiques `agy` & `current_mode_update`
* **Fichiers** : `ogy_frontends/acp_tui/client.py`, `ogy_frontends/acp_tui/widgets/transcript.py`, `ogy_frontends/acp_tui/screens/main.py`, `ogy_frontends/acp_tui/styles.tcss`, `ogy_frontends/acp_tui/i18n.py`
* **Livrables** :
  1. **`PlanUpdateEvent` + widget `PlanCard`** : intercepte `session_update == "plan"` (`entries: [{content, status, priority}]`), affiche et met à jour **en place** une carte dépliable `📋 Plan (X/Y · Z%)` avec icônes `✓` (completed), `●` (in_progress), `○` (pending) et priorité (`high` mise en avant).
  2. **`AvailableCommandsEvent`** : intercepte `available_commands_update`, stocke les commandes annoncées par `agy` (`name`, `description`, `input_hint`), les expose dans l'autocomplétion `/` (`[agy]`) et la palette `Ctrl+P`, et les transmet directement à l'agent via `_dispatch_prompt` lorsqu'elles sont exécutées.
  3. **`current_mode_update`** : synchronise `state.mode` sur réception de `current_mode_update` et rafraîchit immédiatement le badge mode coloré de `#session-bar`.

### Lot 3.7.2 — Basiques Quotidiens `agy` : Shell Inline `!cmd` / `!w`, `/diff`, `/attach` & `/compact`
* **Fichiers** : `ogy_frontends/acp_tui/widgets/transcript.py`, `ogy_frontends/acp_tui/screens/main.py`, `ogy_frontends/acp_tui/widgets/composer.py`, `ogy_frontends/acp_tui/styles.tcss`, `ogy_frontends/acp_tui/i18n.py`
* **Livrables** :
  1. **Shell Inline (`!<cmd>` et `!w <cmd>`) + `ShellCard`** :
     * `!<cmd>` exécute la commande dans `cwd` de manière 100 % asynchrone (`stdin=DEVNULL`, timeout 30 s) et monte une `ShellCard` dépliable (`✓ $ git status · 0.04s` ou `✗ $ pytest · exit 1 · 1.2s`).
     * `!w <cmd>` passe par `ogy wrap -- <cmd>` (compression déterministe + tee-on-failure — **super-pouvoir `ogy`**).
     * Bouton **`Insérer dans le prompt`** (`Attach to prompt`) sur chaque `ShellCard` pour réinjecter une sortie bornée dans le compositeur.
  2. **`/diff` (Git Diff Live, 0 token)** : exécute `git status --short` + `git diff` (+ staged) en `asyncio.to_thread` et ouvre `DiffPreviewModal` avec coloration unified diff (`+`/`-`/`@@`).
  3. **`/attach <path>` (Multimodal réel)** : complète les fichiers images du workspace (`.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`), valide la taille (`<= 10 Mo`), lit via `asyncio.to_thread` et appelle `PromptTextArea.attach_image()` (`ImageContentBlock` ACP + chip `[Image #N]`).
  4. **`/compact`** : alias `agy` intuitif vers `/rotate` (revue `HandoffReviewModal` + session fraîche).

### Lot 3.7.3 — Multi-Sessions Pro (`agy` Parity) : `/fork`, Close/Fork dans `Ctrl+S`, `ogy tui --resume` / `--continue`
* **Fichiers** : `ogy_frontends/acp_tui/screens/main.py`, `ogy_frontends/acp_tui/screens/modals.py`, `ogy_frontends/acp_tui/app.py`, `ogy_cli/main.py`, `ogy_frontends/acp_tui/i18n.py`
* **Livrables** :
  1. **`/fork` (Session Forking ACP)** : appelle `client.fork_session()` si `supports_fork`, crée un nouveau `SessionTranscript` isolé et active la branche sans toucher à la session parente (message propre si non supporté).
  2. **`SessionPickerModal` enrichi (`Ctrl+S`)** : affiche le compteur de sessions, conserve `Enter` pour reprendre, et ajoute les boutons/raccourcis **Fork** (`Ctrl+F`) et **Fermer session** (`Ctrl+X`) sur les sessions inactives.
  3. **Flags CLI `ogy tui --resume [ID]` (`-r`) & `--continue` (`-c`)** : reprend la dernière session connue (ou l'ID spécifié) dès le boot ACP, et respecte `config.toml` `general.language` lorsque `--lang` n'est pas fourni.

---

## PHASE 2 — Ergonomie Clavier Pro & Super-Pouvoirs `ogy`

### Lot 3.7.4 — `Shift+Tab` Mode Cycle, `QuickSelectModal` (`Alt+M`), `/context`, `/mcp`, `/config`, Économies Cache `$` & `Ctrl+T`
* **Fichiers** : `ogy_frontends/acp_tui/screens/modals.py`, `ogy_frontends/acp_tui/screens/main.py`, `ogy_frontends/acp_tui/widgets/telemetry.py`, `ogy_frontends/acp_tui/widgets/transcript.py`, `ogy_frontends/acp_tui/app.py`, `ogy_frontends/acp_tui/i18n.py`
* **Livrables** :
  1. **`Shift+Tab` (Cycle rapide de mode)** : bascule instantanément `default → accept-edits → auto → plan` (hors `yolo`) avec toast et **badge mode coloré** dans `#session-bar` (`plan` cyan, `accept-edits` ambre, `auto` vert, `default` neutre).
  2. **`QuickSelectModal` (`Alt+M` / `/select [model|effort|mode|theme]`)** : modale sélecteur clavier filtrable avec marqueur actif `●` et **estimation Switch Tax (`~$X.XXXX`)** affichée en face de chaque modèle lorsque `context_tokens > 0`.
  3. **`/context` (`ContextInspectorModal`)** : radiographie complète du contexte : fenêtre + zone santé + cache hit % + **économies cache estimées (`$`)**, fichiers règles/mémoire détectés (`GEMINI.md`, `AGENTS.md`, `CLAUDE.md`, `.agents/rules/*`, `.agents/skills/*/SKILL.md`), limites Firebreaks actives et compteur serveurs MCP.
  4. **`/mcp` & `/config`** :
     * `/mcp` : liste en lecture seule les serveurs MCP configurés (profil actif + `.agents/mcp_config.json`), leur statut `eager`/lazy et la présence d'`ogy-mcp` / `ogy-subagents`.
     * `/config [key value]` : affiche ou met à jour une clé `config.toml` (via `ogy_cli.config_cmd.set_config_value` en worker thread) et applique à chaud `tui.theme` / `general.language`.
  5. **Télémétrie ROI Temps Réel & Confort Transcript** :
     * Ligne **`Cache saved`** (`$X.XXXX` en vert) dans `TelemetryPanel` (calculée à partir de `cached_read_tokens` × prix modèle via `ogy_core.pricing`).
     * **Alerte Budget Hard Cap** (`budget.hard_cap_usd`) : passe `Est. Cost` en `val-danger` + toast d'alerte unique lorsqu'un plafond configuré est atteint.
     * **Chronomètre de tour sur `AssistantMessage`** : affiche la durée du tour (ex: `1.4s`) dans la barre d'actions de chaque réponse finalisée.
     * **`Ctrl+T`** : déplie ou replie d'un coup tous les `ThoughtBlock` de la session active.

### Lot 3.7.5 — Finition Visuelle Studio Pro : Complétude 100 % des 3 Thèmes & Glossaire Synchronisé
* **Fichiers** : `ogy_frontends/acp_tui/styles.tcss`, `ogy_frontends/acp_tui/i18n.py`
* **Livrables** :
  1. Styling complet et contrasté de `.theme-light` et `.theme-high-contrast` sur **tous** les composants (modales, barres de recherche/proposition/activité/attachements, `PlanCard`, `ShellCard`, `ThoughtBlock`, `ActivityGroup`, boutons `Copy`/`Copy code`/`Attach`).
  2. Mise à jour complète de `GLOSSARY_MARKDOWN` (FR/EN) couvrant l'ensemble des commandes et raccourcis V3.7.

---

## PHASE 3 — Couverture Tests Maximale V3.7 (Pyramide V3.5 à 4 Étages)

### Lot 3.7.6 — Suite de Tests Complète (`test_ux_v37.py` + `FakeAcpAgent` + 3 Nouveaux Snapshots SVG)
* **Fichiers** : `tests/test_acp_client.py`, `tests/fixtures/fake_acp_agent.py`, `tests/test_ux_v37.py`, `tests/test_tui_snapshots.py`, `tests/__snapshots__/*`
* **Validation** :
  1. **Étage 1 (Unitaires & CLI)** : `ogy tui --resume` / `--continue`, fallback `--lang` depuis `config.toml`, `PlanUpdateEvent`, `AvailableCommandsEvent`, `CurrentModeUpdateEvent`, calcul économies cache `$`, scanners `/context` & `/mcp`.
  2. **Étage 2 (TUI Pilot + Fake ACP stdio)** : `PlanCard` live update, commandes dynamiques `[agy]` dans `/` + dispatch, `!cmd` + `!w` `ShellCard` + attach-to-prompt, `/diff`, `/attach` image, `/fork` + `SessionPickerModal` Fork/Close, `Shift+Tab` mode cycle (0 `yolo`), `Alt+M` `QuickSelectModal` + Switch Tax `$`, `Ctrl+T` toggle thoughts, `/context`, `/mcp`, `/config`, `/compact`, live cache savings & budget hard cap alert, footer durée `AssistantMessage`.
  3. **Étage 3 (Snapshots SVG visuels)** : 9 SVGs existants vérifiés/mis à jour + **3 nouveaux snapshots V3.7** (`plan_and_shell_cards`, `context_inspector_modal`, `light_theme_quick_select_modal`).
  4. **Étage 4 (Validation complète)** : `uv run -m pytest -q --tb=short` — 100 % vert, 0 régression.
