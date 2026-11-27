# Plan d'Exécution V3.6 — Expérience Utilisateur ×10 (TUI Delight, Thèmes, Queueing, Recherche, CLI Pro & Déterminisme Cross-OS)

> **Sources consolidées** : [AUDIT_UX_ET_ARCHITECTURE.md](AUDIT_UX_ET_ARCHITECTURE.md), [PLAN_V3_PROFESSIONNALISATION.md](PLAN_V3_PROFESSIONNALISATION.md), [PLAN_V3_5.md](PLAN_V3_5.md) et inspection directe du code V3.5.
>
> **Objectif** : faire passer Optigravity (`ogy`) d'un excellent outil technique testé (V3.5) à une **expérience développeur ×10**, fluide, adaptative, sans friction et irréprochable sur Windows, Linux et macOS — validée de bout en bout par la pyramide de tests V3.5 (4 étages).

---

## 0. Invariants V3.6 & Garde-Fous (Analyse & Vérification)

Avant toute modification, **6 invariants non négociables** sont verrouillés :

1. **Chemin chaud V1 100 % stdlib préservé** : `ogy_core/`, `ogy_hooks/`, `ogy_statusline/`, `ogy_cli/` conservent **0 dépendance externe** (latence hook `< 25 ms`, démarrage instantané).
2. **Isolation V3.1 intacte** : `OGY_ACTIVE=1`, `profile_mode = "isolated" | "shared"`, et sauvegarde/restauration `.gemini` restent strictement inchangés et testés.
3. **Zéro SQLite synchrone sur le thread UI Textual** : toute lecture/écriture SQLite ou commande `git` dans la TUI s'exécute via `@work(thread=True)` ou `asyncio.to_thread`.
4. **Zéro *scroll hijacking*** : le mécanisme `anchor()` / `release_anchor()` + pilule `↓ Nouveaux messages · End` est intégralement préservé.
5. **Conformité ToS Section 6** : aucun basculement automatique silencieux de modèle/effort en milieu de session, aucun mode `yolo`, toute escalade ou délégation reste 100 % pilotée par l'utilisateur.
6. **Déterminisme Cross-OS (Linux / macOS / Windows)** : zéro dépendance aux fins de ligne CRLF/LF ou aux chemins machine locaux dans la suite de tests.

### Audit Critique Pré-Implémentation (Risques identifiés & Mitigations)

| Risque identifié | Impact potentiel | Mitigation intégrée dans V3.6 |
| :--- | :--- | :--- |
| **Piège raccourcis 1 touche (`r`/`u`/`d`/`v`) dans `main.py`** | Quand un bandeau de proposition est affiché et le compositeur est vide et focalisé, taper `"refactor..."` ou `"debug..."` déclenche involontairement `[R]` ou `[D]`. | Raccourcis **`Alt+R` / `Alt+U` / `Alt+D` / `Alt+V`** actifs partout ; touches nues `r`/`u`/`d`/`v` actives **uniquement** lorsque le compositeur n'a pas le focus. |
| **Collision de prompts concurrents pendant le streaming** | `SessionRegistry.prompt_queues` existait mais n'était jamais branché ; soumettre un 2e prompt pendant un tour actif lançait un `send_prompt` concurrent. | Branchement complet du **Prompt Queueing** : file d'attente visuelle `#queue-bar` + dépilement séquentiel automatique à la fin du tour. |
| **Sidebar fixe 36 cols sur terminal 80×24** | Sur 80 colonnes, la sidebar occupe 45 % de l'écran et écrase le Markdown. | **Auto-collapse responsive** sous `100` colonnes (avec résumé télémétrie compact intégré dans `#session-bar`), toujours surchargeable manuellement via `Ctrl+B`. |
| **Clés `i18n.py` dupliquées & glossaire obsolète** | `section_subagents` était écrasée 2 fois ; le glossaire indiquait encore `Ctrl+R = /rotate` alors que `Ctrl+R` est la recherche d'historique. | Nettoyage complet de `i18n.py` + synchronisation exacte du glossaire `F1`. |
| **3 tests V3.5 fragiles sur Linux (LF vs CRLF + chemin `agy` Windows)** | `test_firebreak_obese_read_and_lockfile` (`24,000 B < 24,576 B` en LF), `firebreak_large_file` (`32.0 KB` vs `34.5 KB`), et `doctor_shared` (`C:/Users/.../agy.EXE`). | Écriture binaire `\n` déterministe + normalisation `agy_binary` dans `_normalize_cli_output()`. |

---

## PHASE 1 — TUI Ergonomie, Esthétique & Fluidité ×10 (`ogy tui`)

### Lot 3.6.1 — Welcome Dashboard, Barre de Session 1 Ligne & Layout Responsive
* **Fichiers** : `ogy_frontends/acp_tui/widgets/transcript.py`, `ogy_frontends/acp_tui/screens/main.py`, `ogy_frontends/acp_tui/styles.tcss`, `ogy_frontends/acp_tui/i18n.py`
* **Améliorations UX** :
  1. **`WelcomeCard` dans tout `SessionTranscript` vierge** : remplace l'écran noir vide au démarrage par une carte d'accueil sobre et utile (version `optigravity`, workspace + branche Git détectée en arrière-plan, badge profil `isolated` / `shared`, statut sous-agents, mémo des 6 raccourcis clés). Disparaît automatiquement dès le premier message de conversation.
  2. **Barre `#session-bar` compacte 1 ligne enrichie** : passe de `height: 2` à `height: 1` (gain d'espace vertical), affiche la pastille de zone colorée (`●` / `▲` / `■`), ID + compteur sessions, modèle/effort/mode (en conservant `Model:` / `Modèle :`), branche Git, badge `[isolated]`, et résumé télémétrie inline (`Ctx X% · Cache Y% · $Z`) lorsque la sidebar est masquée.
  3. **Layout Responsive (`on_resize`)** :
     * `< 100` cols : auto-masquage de `#telemetry-panel` (sauf bascule manuelle `Ctrl+B`) + métriques inline dans `#session-bar`.
     * `100–119` cols : sidebar compacte (`width: 30`).
     * `>= 120` cols : sidebar confort (`width: 36`).
  4. **Corrections `i18n.py`** : déduplication `section_subagents` (FR/EN) et mise à jour complète de `GLOSSARY_MARKDOWN` (`Ctrl+R` historique, `Ctrl+F` recherche, `/theme`, `/copy`, `/export`, `/status`, `/clear-queue`).

### Lot 3.6.2 — Thèmes Adaptatifs (`dark` / `light` / `high-contrast`), Toasts & Sécurité Hotkeys
* **Fichiers** : `ogy_core/config.py`, `ogy_cli/config_cmd.py`, `ogy_cli/main.py`, `ogy_frontends/acp_tui/app.py`, `ogy_frontends/acp_tui/screens/main.py`, `ogy_frontends/acp_tui/styles.tcss`
* **Améliorations UX** :
  1. **3 thèmes complets (`dark`, `light`, `high-contrast`)** :
     * Clé `tui.theme` dans `config.toml` + `OgyConfig.tui_theme` + flag `--theme` sur `ogy tui` / `ogy chat`.
     * Commande `/theme [dark|light|high-contrast]` et entrées dans la palette `Ctrl+P`, appliquant les classes CSS `.theme-light` / `.theme-high-contrast` instantanément sur toute l'interface (fond, texte, bulles, cartes outils, compositeur, sidebar, modales).
  2. **Notifications Toast (`self.notify`)** : feedback non intrusif (timeout 3 s) pour copie presse-papiers, changement de thème/verbosité, mise en file d'attente, export Markdown — ne pollue plus le flux de conversation.
  3. **Correction du piège hotkeys `[R]`/`[U]`/`[D]`/`[V]`** :
     * `Alt+R` / `Alt+U` / `Alt+D` / `Alt+V` fonctionnent à tout moment (même compositeur focalisé).
     * `r` / `u` / `d` / `v` nus ne déclenchent la proposition que lorsque le compositeur n'est **pas** focalisé.

### Lot 3.6.3 — Prompt Queueing, Brouillons par Session & Estimateur Tokens Temps Réel
* **Fichiers** : `ogy_frontends/acp_tui/state/sessions.py`, `ogy_frontends/acp_tui/screens/main.py`, `ogy_frontends/acp_tui/widgets/composer.py`, `ogy_frontends/acp_tui/styles.tcss`
* **Améliorations UX** :
  1. **File d'attente de prompts (`#queue-bar`)** :
     * Si l'utilisateur envoie un prompt alors que `self._working` est actif, le prompt `(text, display_text, extra_blocks)` est empilé proprement dans `SessionRegistry.prompt_queues[sid]`.
     * Affichage d'une barre dédiée `#queue-bar` (`⏳ N en attente : "..."` + bouton `Vider`) et commande `/clear-queue`.
     * Dépilement automatique séquentiel dès la fin du tour actif (jamais si annulé via `Ctrl+C`).
  2. **Préservation automatique des brouillons par session** :
     * `SessionRegistry.drafts[session_id]` sauvegarde le texte en cours de saisie lors d'un `/new`, `/resume`, `Ctrl+S` ou `/rotate` et le restaure au retour sur la session.
  3. **Estimateur de tokens en direct dans le compositeur** :
     * Dès que la saisie dépasse `300` caractères (ou contient des blocs collés/images), affiche un indicateur discret `~N tok` dans `#attachment-bar` avant l'envoi.

### Lot 3.6.4 — Recherche Transcript (`Ctrl+F`), Copie de Blocs de Code, `/copy`, `/export` & `/status`
* **Fichiers** : `ogy_frontends/acp_tui/widgets/transcript.py`, `ogy_frontends/acp_tui/screens/main.py`, `ogy_frontends/acp_tui/app.py`, `ogy_frontends/acp_tui/styles.tcss`
* **Améliorations UX** :
  1. **Recherche intra-transcript (`Ctrl+F` / `/find`)** :
     * Barre `#transcript-search-bar` avec compteur de correspondances (`1/4`), mise en surbrillance `.search-match-active`, scroll automatique (`scroll_visible()`), navigation `Enter` (suivant) / `Shift+Enter` ou `Up` (précédent) / `Esc` (fermer).
  2. **Copie directe de code (`Copy code` + `/copy [code|last]`)** :
     * Bouton contextuel `Copier code` / `Copy code` sur chaque `AssistantMessage` contenant au moins un bloc ` ```...``` ` (réutilise `extract_code_blocks()`).
     * Commande `/copy` (dernier message) et `/copy code` (dernier bloc de code) + entrée palette `Ctrl+P`.
  3. **Export propre de session (`/export [chemin]`)** :
     * Exporte le transcript actif en Markdown structuré (en-tête métadonnées modèle/tours/cache/coût + échanges) vers `.ogy/exports/<session_id>.md` (ou chemin personnalisé) via `asyncio.to_thread`.
  4. **Résumé inline (`/status`)** :
     * Affiche un récapitulatif compact de la session courante (ID, profil, modèle/effort/mode, contexte, cache %, coût OAuth & API sous-agents, branche Git).

---

## PHASE 2 — CLI, Onboarding & Observabilité Pro (`ogy`)

### Lot 3.6.5 — `ogy doctor --json`, Auto-Fix OAuth Isolé, ROI Économisé & Aide CLI
* **Fichiers** : `ogy_cli/main.py`, `ogy_cli/stats_render.py`
* **Améliorations UX** :
  1. **`ogy doctor` enrichi** :
     * Option `--json` pour sorties automatisables (`version`, `profile_mode`, `checks`, `anomalies`, `healthy`).
     * Verdict final explicite (`Summary: OK (0 anomalies)` ou `Summary: N anomaly(ies) detected — run 'ogy doctor --fix'`), colorisé uniquement en TTY (respecte `NO_COLOR`).
     * `ogy doctor --fix` répare désormais aussi les liens OAuth cassés (`oauth_links`) en mode `isolated` via `link_oauth_to_isolated_profile()`.
  2. **`ogy stats` ROI enrichi** :
     * Ajoute l'estimation des **économies de cache en USD (`est. cache savings`)** dans le bandeau ROI Summary à partir des `cache_read_tokens` évités (taux conservateur basé sur `pricing.py`).
  3. **Ergonomie `ogy --help` & Suggestions de fautes de frappe** :
     * Épilogue `Quick-start examples` dans `ogy --help` (5 commandes essentielles).
     * Suggestion intelligente (`Did you mean 'ogy stats'?`) en cas de sous-commande mal orthographiée (`difflib.get_close_matches`).

---

## PHASE 3 — Déterminisme Cross-OS & Couverture Tests V3.5 Maximale

### Lot 3.6.6 — Corrections Cross-OS & Batterie de Tests V3.6 (4 Étages)
* **Fichiers** : `tests/test_firebreaks_wrap.py`, `tests/test_cli_snapshots.py`, `tests/test_tui_app.py`, `tests/test_tui_snapshots.py`, `tests/test_cli.py`, `tests/__snapshots__/*`
* **Actions** :
  1. **Correction des 3 tests cross-OS** :
     * `test_firebreaks_wrap.py` : fichier > 25 Ko garantis en LF comme en CRLF.
     * `test_cli_snapshots.py` : `write_bytes(...\n)` déterministe + normalisation `agy_binary` dans `_normalize_cli_output()`.
  2. **Tests unitaires & CLI (Étage 1)** : `tui.theme` config, `ogy doctor --json`, `ogy doctor --fix` (réparation `oauth_links`), `ogy stats` ROI savings, suggestions de fautes de frappe CLI.
  3. **Tests TUI Pilot + Fake ACP stdio (Étage 2)** : `WelcomeCard` (apparition & disparition), layout responsive `80×24` vs `140×40`, `/theme` (dark/light/high-contrast), prompt queueing + `/clear-queue`, brouillons par session, sécurité hotkeys (`r`/`d` dans compositeur focalisé vs `Alt+R`), recherche `Ctrl+F`, `Copy code` + `/copy code`, `/export`, `/status`, estimateur tokens compositeur.
  4. **Snapshots visuels SVG (Étage 3)** : mise à jour des 7 SVGs existants + ajout de snapshots dédiés (**thème clair**, **barre recherche + queue**).
  5. **Validation E2E TTY (Étage 4)** : exécution complète de `uv run -m pytest -q --tb=short` (100 % vert, 0 échec, 0 warning bloquant).

---

## Récapitulatif de Séquencement V3.6

| Phase | Lots | Valeur Utilisateur (×10) | Validation V3.5 |
| :--- | :--- | :--- | :--- |
| **Phase 1 — TUI Delight** | `3.6.1` Welcome + Session bar + Responsive → `3.6.2` Thèmes + Toasts + Hotkeys → `3.6.3` Queueing + Drafts + Tokens → `3.6.4` `Ctrl+F` + Code Copy + `/export` + `/status` | Zéro écran vide, 80×24 lisible, 3 thèmes, 0 prompt perdu, recherche instantanée, copie de code en 1 clic | Tests Pilot + Fake ACP + SVGs |
| **Phase 2 — CLI Pro** | `3.6.5` `doctor --json`/`--fix` OAuth + ROI `$` économisés + `--help` & typos | Diagnostic automatisable, ROI financier lisible, onboarding immédiat | Tests CLI + `syrupy` |
| **Phase 3 — Qualité Totale** | `3.6.6` Fixes LF/CRLF & `agy_binary` + suite complète V3.6 | 100 % vert cross-OS (Linux/macOS/Windows) | Pyramide complète 4 étages |
