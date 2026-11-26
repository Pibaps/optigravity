# Plan d'Exécution V3 — Professionnalisation UX, Fiabilité & Sous-Agents Hybrides (`ogy`)

> **Sources consolidées** : [AUDIT_UX_ET_ARCHITECTURE.md](AUDIT_UX_ET_ARCHITECTURE.md), [Professionnalisation UX et Architecture Optigravity.pdf](Professionnalisation%20UX%20et%20Architecture%20Optigravity.pdf) (incluant schémas d'architecture, métriques de dette et grille tarifaire Gemini fin 2026), [guide_antigravity_api_gemini_cr_dits_cloud_et_sous_agents_mcp.md](guide_antigravity_api_gemini_cr_dits_cloud_et_sous_agents_mcp.md), et [BENCHMARKS.md](BENCHMARKS.md).

---

## 0. Invariants Non Négociables (Garde-fous transversaux)

Avant chaque PR / lot, vérifier ces **4 invariants absolus** :
1. **Zéro régression Prompt Cache & Tokens** : aucune injection inutile dans le contexte, `@fichier` n'insère que le chemin relatif (jamais le contenu complet), transparence outils basée à 100 % sur les champs ACP existants (0 appel LLM annexe pour résumer les titres).
2. **Chemin chaud V1 100 % stdlib & fail-open (< 25 ms)** : `ogy_hooks/cli.py`, `ogy_statusline/render.py` et `ogy_core/*` n'importent aucune dépendance tierce (`textual`, `mcp`, `google-genai`, `rich`).
3. **Protocole `agy 1.5.16` vérifié (`BENCHMARKS.md`)** : en `PreToolUse`, le *pass-through* nominal doit **impérativement retourner `{"decision": "allow"}`** (et **jamais** `{}` ni `{"decision": ""}`, qui provoquent un refus sur `agy 1.5.16`). *(Point de vigilance corrigé par rapport à la note générique du PDF p. 10.)*
4. **Conformité ToS Section 6 & Windows-first** : client fin mono-utilisateur sous `oauth-personal` (`clientInfo.name = "optigravity"`), délégation programmatique uniquement via serveur MCP authentifié par `GEMINI_API_KEY` ; compatibilité Windows garantie (double thread `Popen` au lieu de `select()`, `keyring` Windows Credential Manager + gestion propre des permissions `secrets.env`).

---

## Architecture Cible (Synthèse)

```text
┌────────────────────────────────────────────────────────────────────────────┐
│  Couche Présentation (Textual 8+)              Contrôleur & État          │
│  ┌────────────────────────────────────┐   ┌────────────────────────────┐  │
│  │ PromptComposer (TextArea + @file)  │   │ SessionRegistry (par sid)  │  │
│  │ TranscriptView (MarkdownStream+anc)│◄─►│ AcpClient (JSON-RPC v1)    │◄─┼─► agy acp (stdio)
│  │ ToolCards & Diffs unifiés inline   │   │ Worker Télémétrie (thread) │  │
│  │ Modales (Perm/Elicit/Handoff/Sess) │   └─────────────┬──────────────┘  │
│  └────────────────────────────────────┘                 │ mode=ro (5s)    │
├ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ Frontière processus (0 dépendance TUI) ─ ─ ─ ─ ─ ┤
│  Chemin Chaud V1 (100 % stdlib)                         ▼                 │
│  ┌──────────────────────────────┐  écriture courte  ┌──────────────────┐  │
│  │ ogy-hook & statusline (<25ms)├──────────────────►│ SQLite WAL       │  │
│  └──────────────────────────────┘                   │ (ogy.db)         │  │
│                                                     └────────▲─────────┘  │
│  Serveur MCP Hybride (processus stdio isolé)                 │ subagent_  │
│  ┌───────────────────────────────────────────────────────────┴─────────┐  │
│  │ ogy-subagents (Niveau 1 Stateless + Niveau 2 google-genai async)    │  │
│  │ Projet A (Free Tier) ──(429 circuit-breaker)──► Projet B (Tier 1)   │  │
│  └─────────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────────┘
```

---

# PHASE 1 — Quick Wins, Fiabilité & Zéro Friction (~1 à 2 semaines)

**Objectif** : éliminer 100 % des bugs silencieux, la contention SQLite, les faux positifs de firebreaks et professionnaliser le cycle de vie CLI avant de toucher à la TUI.

### Lot 1.1 — Correctifs SQLite WAL & Boucle de Données
* **Fichiers** : `ogy_core/storage.py`, `ogy_core/ingest.py`, `ogy_core/config.py`, `ogy_cli/wrapper.py`, `ogy_hooks/cli.py`, `ogy_statusline/render.py`
* **Actions précises** :
  1. **Migrations versionnées via `PRAGMA user_version`** (`storage.py`) :
     * Définir `CURRENT_SCHEMA_VERSION = 2`.
     * Dans `ensure_schema(conn)`, lire `PRAGMA user_version` **en lecture pure avant toute transaction**. N'ouvrir `BEGIN IMMEDIATE` + `DROP VIEW`/`CREATE VIEW` + DDL que si `user_version < CURRENT_SCHEMA_VERSION`, puis poser `PRAGMA user_version = 2`.
     * Ajouter dès maintenant les tables préparatoires `prompt_history` (Phase 2) et `subagent_runs` (Phase 3) dans la migration v2.
  2. **Connexion lecture seule dédiée** (`storage.py`) :
     * Ajouter `connect_readonly(db_path)` utilisant `file:<path>?mode=ro` (`uri=True`, `PRAGMA busy_timeout = 5000`, `PRAGMA query_only = ON`).
     * Basculer `ogy stats`, `ogy waste`, `TelemetryPanel`/orchestrateur et le fallback `statusline` sur `connect_readonly()`.
  3. **Fix pression quota permanente** (`ogy_cli/wrapper.py:83-88`) :
     * Remplacer `SELECT MIN(remaining_fraction) FROM quota_snapshots` par `get_latest_quota_snapshots(conn)` et prendre le minimum uniquement sur les snapshots actifs les plus récents.
  4. **Corrélation `ogy run` ↔ `session-start` (réactivation du bandit Stage 2 en V1)** (`wrapper.py`, `ogy_hooks/cli.py`) :
     * Dans `run_agy()`, écrire `~/.cache/optigravity/pending_tasks/<sha256(cwd)[:16]>.json` contenant `task_id`, `decision` (`stage`, `category`, `model`, `effort`, `mode`, `features`) et `expires_at` (TTL 30 s).
     * Dans `_handle_session_start()`, consommer atomiquement ce fichier (s'il n'est pas expiré), rattacher `task_id` à la session et insérer la ligne dans `routing_decisions`.
  5. **Normalisation `camelCase` / `snake_case`** (`ogy_core/ingest.py:78-93`) :
     * Mutualiser un helper `_pick_int` / `_pick_float` gérant `input_tokens`/`inputTokens`, `cache_read_input_tokens`/`cacheReadInputTokens`, `total_usd`/`totalUsd`, etc. dans `ingest_statusline_payload()`.
  6. **Enrichissement `conversations/<id>.db`** (`ogy_core/ingest.py:362-366`) :
     * Décoder en best-effort (lecture seule `immutable=1`, fallback silencieux sur changement de schéma) `gen_metadata` pour alimenter `thinking_tokens`, `tools_schema_tokens` et `ttft_ms` (débloquant `W4` et `W7` en V1).
  7. **Alignement `config.py`** :
     * Harmoniser la valeur par défaut de `overwrite_verified_ok` entre la dataclass `OgyConfig` (`config.py:23`) et `load_config()` (`config.py:85`), ajouter `language: str = "en"` (utilisé par les hooks/CLI) et `tui.enter_behavior: str = "submit"`.
* **Critères « Done »** : 0 verrou DDL sur 100 polls consécutifs ; `ogy-hook` p95 < 20 ms ; test unitaire prouvant qu'un vieux quota à 5 % ne downgrade plus `ogy run` ; test de corrélation `pending_tasks` → `routing_decisions` ; test `camelCase` statusline → DB.

---

### Lot 1.2 — Firebreaks « Zéro Friction » (100 % stdlib)
* **Fichiers** : `ogy_core/firebreaks.py`, `ogy_hooks/cli.py`, `tests/test_firebreaks_wrap.py`
* **Actions précises** :
  1. **Tokenizer lexical léger (~120 LOC stdlib)** :
     * Découper uniquement les opérateurs top-level (`&&`, `||`, `;`, `|`) hors guillemets simples/doubles et parenthèses (compatible PowerShell et bash, sans dépendre de `shlex`).
     * **Syntaxe avancée** (`|&`, `>`, `>>`, `<<`, `$(...)`, blocs `{ ... }` PowerShell) → *pass-through* intégral (`{"decision": "allow"}`).
     * **Séquences** (`&&`, `||`, `;`) → évaluer et réécrire chirurgicalement **chaque segment indépendamment** (`pytest && ruff check` → `ogy wrap -- pytest && ruff check`).
     * **Pipelines** (`|`) → ne jamais muter les producteurs intermédiaires ; *pass-through* si le consommateur final borne déjà (`head`, `tail`, `wc`, `Select-Object -First`, `Select-String`).
  2. **Détecteur `bounded-aware`** :
     * Laisser passer sans blocage ni wrap les commandes déjà bornées : `pytest` avec `-q`, `--tb=short`, `--tb=line`, `--maxfail`, `-k` ; `git log` avec `-n`/`--oneline` ; `git diff --stat` ; `git status -s`/`--porcelain`.
     * Supporter l'échappatoire `OGY_NO_WRAP=1` et le préfixe `ogy raw --`.
  3. **Seuils adaptatifs par type de fichier sur `read_file`/`view_file`** :
     * Documentation / config (`.md`, `.rst`, `.toml`, `.yaml`, `.yml`) : **48 Ko** (~12K tokens) — libre ≤ 48 Ko.
     * Code source (`.py`, `.ts`, `.js`, `.go`, `.rs`, etc.) : **24 Ko** (~6K tokens) — refus guidé indiquant taille + **nombre total de lignes exact**.
     * Logs, données & lockfiles (`.log`, `.json`, `.lock`, `.min.js`, `LOCKFILE_NAMES`) : **8 Ko** (~2K tokens) — refus strict avec orientation vers lecture par tranche.
  4. **Hiérarchie à 3 niveaux & Messages i18n *tool-aware*** :
     * Niveau 1 : réécriture silencieuse (`{"decision": "allow", "overwrite": {"CommandLine": ...}}`) pour commandes shell verbeuses non bornées.
     * Niveau 2 : avertissement doux post-outil pour cas intermédiaires / 2 premiers retries.
     * Niveau 3 : `deny` réservé aux lectures massives de logs/lockfiles et aux boucles avérées (≥ 3 appels identiques).
     * Messages localisés selon `config.language` (`en` par défaut, `fr` optionnel), sans jamais citer d'outil codé en dur (`grep_search`) : recommander explicitement `StartLine`/`EndLine` (ou les paramètres de plage du schéma intercepté).
* **Critères « Done »** : 0 faux positif sur `README.md` (17,6 Ko), `uv run pytest -q --tb=short`, `git log -n 10 --oneline`, et commandes composées PowerShell/bash (`;`, `&&`, `|`).

---

### Lot 1.3 — `ogy wrap` Fluide, Multiplateforme & Purge Stash
* **Fichiers** : `ogy_core/firebreaks.py`, `ogy_cli/main.py`, `tests/test_firebreaks_wrap.py`
* **Actions précises** :
  1. **Double thread démon `Popen`** (`stdout=PIPE`, `stderr=PIPE`) : draine `stdout` et `stderr` en parallèle sans deadlock OS et 100 % compatible Windows/Linux/macOS.
  2. **Séparation stricte `stdout`/`stderr`** : archiver dans `~/.cache/optigravity/stash/<hash>/` sous forme `stdout.log`, `stderr.log` et `meta.json` (avec rétrocompatibilité `ogy recall <hash>` pour les anciens `.log`). Ne jamais noyer `stderr` dans un `stdout` bavard.
  3. **Feedback vivant en TTY uniquement** : si `sys.stderr.isatty()`, rafraîchir toutes les 250 ms sur une ligne (`\r⠋ ogy wrap · <cmd> · 14s · 312 lignes`) puis effacer proprement avant le résumé final. Hors TTY (agent `agy`), **0 séquence ANSI**.
  4. **Pass-through petits outputs** : toute sortie `< 40 lignes` **et** `< 3 Ko` passe en *pass-through* intégral sans compression ni création de stash (évite le surcoût +11–18 % documenté sur `rtk#582`).
  5. **Purge LRU opportuniste** : protégée par `~/.cache/optigravity/stash/.last_gc` (max 1×/heure, < 3 ms), supprime les entrées > 7 jours ou au-delà de 200 Mo cumulés.
* **Critères « Done »** : commande longue en TTY affiche le spinner live ; hors TTY sortie 100 % propre ; `stderr` intact ; GC LRU vérifié par test unitaire.

---

### Lot 1.4 — Packaging Modulaire & Cycle de Vie CLI Pro (`clig.dev`)
* **Fichiers** : `pyproject.toml`, `ogy_cli/main.py`, `ogy_cli/init_audit.py`, `ogy_cli/config_cmd.py` (nouveau), `ogy_cli/stats_render.py` (nouveau)
* **Actions précises** :
  1. **Packaging PEP 621 modulaire (`pyproject.toml`)** :
     * `dependencies = []` (V1 100 % stdlib, Python ≥ 3.12).
     * Extras : `[mcp]` (`mcp>=2,<3`), `[tui]` (`textual>=8.0.0`, `agent-client-protocol>=1.0.0`), `[subagents]` (`optigravity[mcp]`, `google-genai[aiohttp]`, `keyring`), `[all]` (`optigravity[mcp,tui,subagents]`), `[dev]` (`pytest>=8.0`, `pytest-asyncio`).
     * Import paresseux dans `ogy_cli/main.py` avec message d'installation clair si un extra manque (ex. `Install with: uv tool install "optigravity[tui]"`).
  2. **Source unique de version** : `ogy --version` et `CLIENT_VERSION` lisent `importlib.metadata.version("optigravity")` (fallback `"0.0.0-dev"`).
  3. **`ogy init` guidé & idempotent** : assistant court en 4 étapes (langue, hooks + statusline, config optionnelle sous-agents MCP avec choix explicite `tier1_only` vs `free_first`, génération d'un `~/.config/optigravity/config.toml` commenté), avec flag `--non-interactive` pour scripts/CI. Étiquetage `"ogy-managed": true` (ou namespace dédié) des entrées injectées.
  4. **`ogy uninstall [--purge-data]`** : symétrique exact d'`ogy init`, retire uniquement les entrées `ogy-managed` (sans toucher aux autres extensions), propose de restaurer `.ogy.bak`, et ne supprime `ogy.db`/stash que si `--purge-data`.
  5. **`ogy doctor --fix`** : vérifie `agy`, validité JSON, permissions `secrets.env`, latence mesurée `ogy-hook` (< 25 ms), `PRAGMA quick_check`, et connectivité clé Gemini API (0 token). `--fix` répare après confirmation les anomalies déterministes.
  6. **`ogy config [show|get|set|validate|edit]`** : affiche valeur + provenance (`default`, `config.toml`, `env`) et valide avant écriture.
  7. **Tableaux ROI `ogy stats` & `ogy waste`** : tableaux Unicode propres (ou `rich` si dispo + TTY, respect `NO_COLOR`, `--json` intact) avec filtres `--last`, `--since`, `--repo` ; en-tête ROI (tokens évités via cache/`wrap`, tendance 7j/30j, bloc séparé `Sessions agy (OAuth)` vs `Sous-agents MCP (API USD)`). `ogy waste` traduit `W1..W8` en conseils actionnables triés par impact avec commande exacte de remédiation.
* **Critères « Done »** : `pip install .` = 0 dépendance tierce ; cycle `init → doctor → uninstall` idempotent testé ; `ogy --version` cohérent partout ; `stats`/`waste` lisibles en TTY et propres en `--json`.

---

# PHASE 2 — Refonte Core UX Pro de `ogy tui` sous Textual 8+ (~2 à 3 semaines)

**Objectif** : démanteler le monolithe `app.py` (1 814 LOC) et hisser `ogy tui` au niveau d'ergonomie de Claude Code, OpenCode et Toad.

### Lot 2.1 — Découpage Modulaire d'`app.py` & Worker SQLite Non-Bloquant
* **Architecture cible sous `ogy_frontends/acp_tui/`** :
  * `app.py` + `styles.tcss` (~200 LOC) : point d'entrée `OptigravityApp`, thèmes adaptatifs terminal, `OptigravityCommandProvider`.
  * `screens/main.py` (~250 LOC) : layout principal, raccourcis globaux (`Ctrl+S`, `Ctrl+O`, `Ctrl+P`, `Ctrl+B`, `Ctrl+C`, `F1`, touches contextuelles `R`/`D`/`U`), liaison `AcpClient` ↔ `SessionRegistry`.
  * `screens/modals.py` (~280 LOC) : `PermissionModal`, `ElicitationModal`, `HandoffReviewModal`, `SessionPickerModal`, `HelpGlossaryModal`.
  * `widgets/composer.py` (~250 LOC) : `PromptTextArea` + popover `@fichier` & `/commande`.
  * `widgets/transcript.py` (~300 LOC) : `SessionTranscript`, `AssistantMessage`, `ThoughtBlock`, `ActivityGroup`, `ToolCard`.
  * `widgets/telemetry.py` (~200 LOC) : `TelemetryPanel`, `MetricGauge`, `MetricRow`, arbre sous-agents live.
  * `state/sessions.py` (~180 LOC) : `SessionRegistry` (isolé par `session_id`), file de messages, pont worker SQLite lecture seule (`@work(thread=True, exclusive=True)` mettant à jour l'UI via `call_from_thread()`).
* **Critères « Done »** : `app.py` < 250 LOC ; tous les tests Textual Pilot (`test_tui_app.py`) passent sans binaire `agy` live ; 0 accès SQLite synchrone sur le thread UI.

---

### Lot 2.2 — Composer Pro (`PromptTextArea`, Historique, Paste-Collapse, `@fichier`)
* **Fichiers** : `ogy_frontends/acp_tui/widgets/composer.py`, `ogy_core/storage.py` (`prompt_history`)
* **Actions précises** :
  1. **`PromptTextArea(TextArea)` multiligne** : hauteur auto-ajustable 1 à 8 lignes (scroll interne au-delà). Matrice clavier défensive : `Enter` soumet (configurable via `tui.enter_behavior`), saut de ligne accepte `Shift+Enter` (CSI u / Windows Terminal 1.25+), `Ctrl+J`, `Alt+Enter` et `\`+`Enter`. `Ctrl+G` ouvre dans `$EDITOR`.
  2. **Historique SQLite intelligent** : `↑`/`↓` déplacent d'abord le curseur dans le multiligne ; uniquement en première/dernière ligne visuelle, ils parcourent `prompt_history` (filtrable par dépôt) en préservant le brouillon courant en mémoire. Recherche inversée `Ctrl+R` (distincte de la rotation `R` en bannière / `/rotate`).
  3. **Paste-collapse & Images** : intercepter `events.Paste` ; si `≥ 8 lignes` ou `≥ 600 caractères`, stocker en mémoire et insérer la puce `[Pasted #1 · 42 lignes]` (supprimable d'un Backspace, inspectable avant envoi, réinjectée intégralement à `session/prompt`). Support `[Image #1]` via `extra_blocks` déjà présent dans `client.py`.
  4. **Autocomplétion `@fichier` asynchrone** : worker `@work(thread=True)` exécutant `git ls-files --cached --others --exclude-standard` (fallback `os.walk` respectant `.gitignore`), rafraîchi en fin de tour. Popover fuzzy au-dessus du composer priorisant noms exacts + fichiers `git status`, affichant la **taille estimée** (`storage.py · 11 Ko`) avec marqueur visuel au-delà du seuil firebreak, bascule `Tab` vers vue arborescente (pattern Toad). **N'insère que le chemin relatif** (`@ogy_core/storage.py`).
* **Critères « Done »** : collage de 200 lignes replié sans lag ; `@fichier` opérationnel avec taille affichée et 0 gonflement de tokens ; historique persisté entre redémarrages.

---

### Lot 2.3 — Streaming `MarkdownStream` + Smart Scroll `anchor()` + Copie Propre
* **Fichiers** : `ogy_frontends/acp_tui/widgets/transcript.py`
* **Actions précises** :
  1. Utiliser `stream = Markdown.get_stream(widget)` pour chaque réponse assistant (`await stream.write(chunk)` + `finally: await stream.stop()`), avec garde-fou texte brut tronqué au-delà de `200 000` caractères.
  2. Supprimer tous les `scroll_end(animate=False)` manuels ; activer `container.anchor()`.
  3. Sur scroll manuel vers le haut (molette, `PageUp`, flèches), appeler `container.release_anchor()` et afficher une pilule flottante `↓ Nouveaux messages · End`. Réactiver l'ancre dès retour en bas (`is_vertical_scroll_end == True` ou touche `End`).
  4. Action de copie presse-papiers du **Markdown source brut non wrappé** sur chaque réponse et bloc de code (en complément de la sélection multi-conteneurs Textual 8+).
* **Critères « Done »** : 0 *scroll hijacking* quand l'utilisateur remonte pendant un streaming actif ; copie de bloc de code sans espaces parasites.

---

### Lot 2.4 — Transparence Sans Bruit (`ActivityGroup`, `ToolCard` & Diffs Inline, `ThoughtBlock`)
* **Fichiers** : `ogy_frontends/acp_tui/client.py`, `ogy_frontends/acp_tui/widgets/transcript.py`
* **Actions précises** :
  1. Enrichir `ToolCallEvent` (`client.py`) pour transporter `locations` et `content` (blocs ACP `type: "diff"` avec `path`, `oldText`, `newText`).
  2. **`ActivityGroup(Collapsible)`** : regroupe les appels consécutifs d'exploration sans erreur (`read`, `search`, `fetch`) sur 1 ligne cliquable/clavier (`▸ 4 lectures, 2 recherches · 1,4 s`), basculable globalement via `Ctrl+O` (`verbosity: low / high`).
  3. **`ToolCard`** : toujours dépliable même sans `rawOutput` (révèle `rawInput`, évitant l'écueil `toad#146`). Sur `edit`/`write`, génère localement un diff unifié coloré via `difflib.unified_diff` (1-based) : ouvert par défaut si `≤ 25` lignes modifiées, plié en bilan (`+142 / -38 · app.py`, extensible plein écran) au-delà. Les appels en erreur (`status == "failed"`) restent **toujours dépliés**.
  4. **`ThoughtBlock`** : tiroir discret de 2–3 lignes en *dim italic* avec chronomètre pendant la génération, auto-replié dès le premier chunk de réponse/outil, consultable au clavier ou via `Ctrl+O`.
* **Critères « Done »** : diffs inline colorés sur chaque édition ; erreurs toujours visibles ; 0 appel LLM additionnel.

---

### Lot 2.5 — Modales Clavier-First, Élicitations ACP & Multi-Session Isolée
* **Fichiers** : `ogy_frontends/acp_tui/client.py`, `ogy_frontends/acp_tui/screens/modals.py`, `ogy_frontends/acp_tui/state/sessions.py`, `ogy_frontends/acp_tui/orchestrator.py`
* **Actions précises** :
  1. **`PermissionModal` enrichie** : transmettre `raw_input` et `content` (diff) dans `PermissionRequestEvent`. Afficher la commande complète ou le diff coloré + badge de risque (ambre/rouge `execute`/`delete`, bleu `edit`). Raccourcis 1-touche : `Y`/`1` (`allow_once`), `A`/`2` (`allow_always` avec portée explicite), `N`/`Esc` (`reject_once`), `X` (`reject_always`), `F` (aperçu plein écran).
  2. **`ElicitationModal` (modes `form` + `url`)** :
     * Déclarer les capacités non nulles `elicitation: {"form": {}, "url": {}}` lors du `initialize` ACP (si supporté par le SDK).
     * Mode `form` : génération dynamique depuis `requestedSchema` (`Input`, `Switch`, `Select`), validation `Ctrl+Enter`, refus `decline`, annulation `Esc` → `cancel`.
     * Mode `url` : affiche `urlparse(url).netloc`, consentement explicite avant ouverture navigateur (ou URL copiable en SSH/headless), écoute `elicitation/complete`, **jamais de repli vers `form` pour secrets/identifiants**.
  3. **Multi-session cloisonnée (`SessionRegistry` + `ContentSwitcher`)** :
     * 1 `SessionTranscript` indépendant par `session_id` (propre `MarkdownStream`, ancrage, `ToolCard`).
     * `SessionPickerModal` (`Ctrl+S` et `/resume`) interrogeant `client.list_sessions()` enrichi par `ogy.db` (date, titre, tours, coût) avec recherche fuzzy.
  4. **Rotation `/rotate` maîtrisée (`HandoffReviewModal`)** :
     * Brouillon `.ogy/handoff.md` enrichi par `git status --short` + dernière erreur de test (puis résumé Gemini Niveau 1 en Phase 3).
     * Modale éditable (`TextArea` + compteur de tokens estimés), validation `Ctrl+Enter` → ouvre une nouvelle session dans un conteneur vierge et conserve l'ancienne consultable dans `Ctrl+S`.
  5. **Câblage complet des propositions (`[R]`, `[U]`, `[D]`, `[V]`)** : raccourcis directs quand la bannière est visible, et gestion propre de `switch_to_v1_run` (affiche commande prête à copier / lancer).
* **Critères « Done »** : 0 élicitation rejetée silencieusement ; `/new`, `/rotate` et `/resume` cloisonnent totalement les flux ; `PermissionModal` pilotable 100 % au clavier.

---

# PHASE 3 — Sous-Agents Hybrides MCP + API Gemini (~2 à 3 semaines)

**Objectif** : implémenter la délégation `[D] > 60K`, le résumé intelligent de logs `ogy wrap` et le handoff sémantique hors quota OAuth, via un serveur MCP local dédié authentifié par `GEMINI_API_KEY`.

### Lot 3.1 — Serveur MCP Dédié `ogy-subagents` (Niveau 1 Stateless + Niveau 2 Async `google-genai`)
* **Fichiers** : `ogy_subagents/` (nouveau package : `server.py`, `gemini_client.py`, `tools_readonly.py`, `router.py`), `ogy_core/pricing.py`, `pyproject.toml` (entry point `ogy-subagents`)
* **Actions précises** :
  1. **Mise à jour `ogy_core/pricing.py`** :
     * Ajouter la grille API Gemini fin 2026 : `gemini-3.5-flash-lite` (`$0.30` in / `$2.50` out, Free Tier éligible), `gemini-3.1-flash-lite` (`$0.25` / `$1.50`), `gemini-3.6-flash` / `gemini-3.8-flash` (`$0.75` / `$3.75` → doublement `$1.50` / `$7.50` au `2027-01-01`), `gemini-3.1-pro` (`$2.00` / `$12.00`, >200K `$4.00` / `$18.00`).
     * Facturer explicitement `(candidates_token_count + thoughts_token_count) * output_rate` et distinguer `billing_source` (`oauth_quota` vs `gemini_api_free` vs `gemini_api_tier1`).
  2. **Encapsulation stricte des contraintes SDK `google-genai` (Gemini 3.5+)** :
     * Utiliser uniquement `thinking_level` (`minimal` en Niveau 1, `low`/`medium` en Niveau 2 ; traduction automatique `minimal` → `low` sur `gemini-3.8-flash`/Gemini 4 ; jamais `thinking_budget`, `temperature`, `top_p`, `top_k`).
     * Forcer `store=False` (évite la régression TTFT 1,2 s → 4,0 s documentée sur `python-genai#3059`).
     * Conserver intégralement chaque objet `Content` produit par le modèle avec ses **thought signatures** intactes en boucle multi-tours (évite `400 INVALID_ARGUMENT`).
  3. **Outils Niveau 1 (Stateless, 0 accès disque/shell)** :
     * `ogy_summarize_stash(hash_prefix, focus_query?)` : résume un log volumineux `ogy wrap` en ~15–20 lignes sourcées.
     * `ogy_build_handoff(goal, git_status, last_error, recent_context)` : produit un `.ogy/handoff.md` calibré à 600–900 tokens.
  4. **Outils Niveau 2 (Boucle Python `while` async sur `client.aio`, `max_steps=8`, `timeout=45s`, sans Google ADK)** :
     * Liste blanche d'outils **read-only par construction** (annotés `readOnlyHint: true`) : `rg_search` (ou fallback Python), `read_slice`, `list_files`, `git_status`.
     * `ogy_research_repo(question, scope_paths?)` et `ogy_triage_failure(error_summary, stash_hash?)` retournant un rapport concis sourcé (~20 lignes, chemins + lignes exactes).
* **Critères « Done »** : outils Niveau 1 et Niveau 2 exécutés hors quota OAuth via `GEMINI_API_KEY`, comptage exact incluant `thoughts_token_count`, zéro écriture disque par défaut.

---

### Lot 3.2 — Résilience `429`, Gestion Sécurisée des Secrets & Confidentialité
* **Fichiers** : `ogy_subagents/secrets.py`, `ogy_subagents/router.py`, `ogy_cli/init_audit.py`, `ogy_cli/main.py` (`ogy doctor`)
* **Actions précises** :
  1. **Cascade sécurisée de résolution des clés** :
     * 1) Variables d'env (`OGY_GEMINI_API_KEY_FREE`, `OGY_GEMINI_API_KEY_TIER1`, repli `GEMINI_API_KEY`).
     * 2) Trousseau OS via `keyring` (Windows Credential Manager / macOS Keychain / SecretService, vérifié non vide pour tolérer SSH/headless).
     * 3) Fichier `~/.config/optigravity/secrets.env` écrit via `getpass.getpass()` (POSIX `0600` strict avec refus de lecture si trop permissif ; contrôle adapté sur Windows).
     * **0 clé en clair** dans `.agents/mcp_config.json` ou en argument CLI + filtre de rédaction regex `AIza[0-9A-Za-z_-]{35}` sur tous les logs et retours LLM.
  2. **Politique de confidentialité explicite (`privacy_mode`)** :
     * Choix obligatoire à `ogy init` : `tier1_only` (recommandé pour code propriétaire, données non utilisées pour entraînement) vs `free_first` (projets personnels / open-source).
  3. **Routeur de résilience `429 RESOURCE_EXHAUSTED` (2 projets Google Cloud distincts)** :
     * Inspecter le champ `description` Google : distinguer RPM/TPM (max 2 retries courts backoff+jitter `1,5s` puis `4s`), RPD (circuit-breaker en cooldown jusqu'à minuit PT ou 15 min si ambigu), et `402` (solde prépayé épuisé).
     * En mode `free_first` + `fallback_to_tier1 = true`, basculer automatiquement du Projet A (Free Tier) vers le Projet B (Crédits AI Pro Tier 1) avec notification discrète du coût estimé.
  4. **Vérification dans `ogy doctor`** : test de connectivité clé API (ex. `models.get` sans consommer de tokens) + audit permissions `secrets.env`.
* **Critères « Done »** : bascule vérifiée sur `429` simulé (RPM vs RPD) ; 0 fuite de clé `AIza*` dans les logs/retours ; consentement Free Tier explicite.

---

### Lot 3.3 — Observabilité Temps Réel TUI, Branchement `[D] > 60K` & Opt-In Niveau 3
* **Fichiers** : `ogy_subagents/server.py`, `ogy_core/storage.py` (`subagent_runs`), `ogy_frontends/acp_tui/widgets/telemetry.py`, `ogy_frontends/acp_tui/screens/main.py`
* **Actions précises** :
  1. **Double canal de progression temps réel** :
     * Chaque tour de la boucle Niveau 2 émet `await ctx.report_progress(step, max_steps, msg)` + `await ctx.info(...)` (throttlé ~100 ms).
     * En parallèle, persistance temps réel dans SQLite WAL (`subagent_runs` : `run_id`, `parent_session_id`, `tool_name`, `model_id`, `tier`, `step`, `max_steps`, `status`, `input_tokens`, `output_tokens`, `thought_tokens`, `cost_usd`, `started_at`, `ended_at`).
  2. **Sidebar TUI enrichie** (`widgets/telemetry.py`) :
     * Arbre live `parent → sous-agent` (`↳ ogy_research_repo [3/8] · 3.5-flash-lite · Free`).
     * Deux compteurs/jauges strictement séparés : **Quota OAuth (`agy`)** et **Dépense API Gemini (USD / Free)**.
  3. **Activation concrète de la touche `[D] Déléguer (> 60K)`** :
     * Dans le strict respect des ToS Section 6, `D` ouvre une modale pré-remplie permettant soit d'insérer une consigne structurée invitant `agy` à appeler `ogy_research_repo`, soit de lancer directement la recherche read-only en parallèle dont le rapport sourcé est attaché au prompt suivant.
  4. **Opt-in avancé (édition déléguée)** :
     * Uniquement si activé explicitement : exécution dans un `git worktree` temporaire isolé (ou Niveau 3 `gemini -p ... --sandbox`), retournant un contrat JSON obligatoire (`files_touched`, `git_diff_stat`, `patch_path`) soumis à validation humaine avant application.
* **Critères « Done »** : progression étape par étape visible en direct dans `ogy tui` ; coûts API séparés dans `ogy tui` et `ogy stats` ; action `[D]` fonctionnelle de bout en bout.

---

## Récapitulatif de Séquencement

| Phase | Lots | Durée est. | Valeur livrée |
| :--- | :--- | :---: | :--- |
| **Phase 1 — Socle & Zéro Friction** | `1.1` SQLite/Data → `1.2` Firebreaks → `1.3` `ogy wrap` → `1.4` Packaging & CLI | ~1–2 sem. | 0 contention DB, 0 faux positif firebreaks, CLI pro (`init`/`doctor --fix`/`uninstall`/`stats` ROI) |
| **Phase 2 — TUI Pro Textual 8+** | `2.1` Découpage → `2.2` Composer Pro → `2.3` Stream/Scroll → `2.4` ToolCards/Diffs → `2.5` Modales & Multi-session | ~2–3 sem. | `ogy tui` au niveau Claude Code / OpenCode / Toad, 100 % testable via Textual Pilot |
| **Phase 3 — Sous-Agents Hybrides** | `3.1` `ogy-subagents` N1+N2 → `3.2` Résilience 429 & Secrets → `3.3` Observabilité & `[D] > 60K` | ~2–3 sem. | Délégation réelle hors quota OAuth, `gemini-3.5-flash-lite`, observabilité duale |
