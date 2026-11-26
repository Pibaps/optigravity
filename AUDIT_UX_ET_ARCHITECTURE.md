# Audit Complet d'Optigravity (`ogy`) — Produit, UX, Architecture & Intégration Sous-Agents Gemini API

> **Objectif de ce document** : dresser un état des lieux factuel, exhaustif et sans complaisance d'Optigravity (`ogy`) afin de servir de **base de travail pour un Deep Research orienté Expérience Utilisateur (UX) et professionnalisation produit**, sans dégrader l'efficacité tokens ni la fluidité d'usage.
>
> *Base auditée : commit `f2398f0` (branche locale, 35/35 tests `pytest` passants, ~5 500 LOC Python prod).*

---

## 1. Synthèse Exécutive

### Ce qui est déjà très solide (Fondations à préserver)
* **Positionnement clair et mesuré** : les benchmarks A/B (`BENCHMARKS.md`) démontrent des gains réels sur dépôts bruités et volumineux (-10 % à -58,8 % de tokens d'entrée, -21,8 % à -51,8 % de coût, 0 régression sur `pallets/click` et `yt-dlp`).
* **Discipline d'ingénierie sur le chemin chaud** : `ogy_hooks/`, `ogy_statusline/` et `ogy_core/` sont **100 % stdlib Python** avec garde-fou *fail-open* (`try/except BaseException` → JSON valide + `exit 0`).
* **Persistance SQLite WAL bien pensée** (`ogy_core/storage.py`) : schéma relationnel complet (7 tables, 3 vues KPI, 8 détecteurs `ogy waste`), `UPSERT` idempotents, `BEGIN IMMEDIATE`.
* **Socle V2 ACP prometteur** (`ogy tui`) : client JSON-RPC 2.0 conforme ACP v1 (`agent-client-protocol`), i18n FR/EN, palette `Ctrl+P`, glossaire `F1`, jauges live, respect explicite des ToS Section 6.

### Diagnostic global : pourquoi l'application ne fait pas encore « produit professionnel »
Aujourd'hui, Optigravity est un **excellent prototype d'ingénieur orienté télémétrie et économie de tokens**, mais son expérience utilisateur présente **4 failles majeures** :
1. **Des promesses UX inachevées dans `ogy tui`** : éditeur mono-ligne (au lieu de multiligne), `@fichier` annoncé dans le placeholder mais non implémenté, appels d'outils réduits à 1 ligne non dépliable (aucun diff ni détail), `elicitation/create` rejeté silencieusement, flux de conversation non isolé lors des rotations/changements de session.
2. **Des firebreaks qui créent de la friction inutile** : règles trop rigides ou naïves (blocage de `pytest -q --tb=short`, casse de commandes composées `;`, seuils uniques à 16 Ko même pour la doc, messages hardcodés en français citant `grep_search` même quand l'outil est absent).
3. **Des bugs silencieux dans la boucle de données & performance** : `ensure_schema()` exécute `DROP VIEW` + `CREATE VIEW` sous `BEGIN IMMEDIATE` à chaque connexion (y compris toutes les 5 s dans la TUI), `ogy run` ne relie jamais `task_id` aux hooks (le bandit Stage 2 n'apprend pas), `MIN(remaining_fraction)` lit tout l'historique quota, et `conversations/<id>.db` n'est pas encore décodé.
4. **Une délégation sous-agent encore théorique** : `ogy tui` recommande de déléguer au-delà de 60K tokens (`[D]`), mais n'offre aucun mécanisme concret d'exécution — alors qu'une intégration hybride via MCP + API Gemini (`GEMINI_API_KEY` Free Tier / Crédits Google AI Pro) permettrait de débloquer des sous-agents rapides, peu coûteux et isolés sans toucher au quota OAuth principal.

---

## 2. Cartographie Technique du Projet

| Couche | Fichiers clés | LOC | Rôle | État de maturité |
| :--- | :--- | :---: | :--- | :--- |
| **Core (stdlib)** | `ogy_core/{config,storage,ingest,pricing,router,context_health,firebreaks}.py` | ~2 150 | Config, SQLite WAL, ingestion, tarifs, routage S1/S2, santé contexte, firebreaks & `wrap` | **Solide**, mais bugs de plomberie (schema lock, ingest partiel, quota min) |
| **Hooks & HUD (stdlib)** | `ogy_hooks/cli.py`, `ogy_statusline/render.py` | ~610 | Hooks fail-open (`session-start`, `pre/post-tool`, `pre/post-invocation`, `stop`) + HUD 1 ligne | **Bon**, mais désalignement `camelCase`/DB et textes FR hardcodés |
| **CLI** | `ogy_cli/{main,wrapper,init_audit}.py` | ~660 | `run`, `tui/chat`, `init`, `doctor`, `stats`, `waste`, `audit mcp`, `wrap`, `recall`, `router-eval` | **Fonctionnel**, sortie brute (non formatée), pas d'`uninstall` ni `config` |
| **Serveur MCP** | `ogy_mcp/server.py` | ~100 | 3 outils stdio (`ogy_session_budget`, `ogy_recall`, `ogy_write_handoff`), `eager: false` | **Minimaliste**, prêt à être étendu ou doublé pour sous-agents |
| **TUI V2 (Textual)** | `ogy_frontends/acp_tui/{app,client,orchestrator,i18n}.py` | ~3 040 | Client ACP v1 + TUI interactive + orchestrateur Switch Tax / rotation | **Prototype avancé**, `app.py` monolithique (1 814 LOC), UX incomplète |
| **Templates & Tests** | `templates/*`, `tests/test_*.py` | ~1 400 | Règle `ogy-hygiene.md`, skill `/ogy-budget`, 9 fichiers de tests (35 tests) | **Tests verts**, mais couverture UX/e2e et cas limites shell faible |

---

## 3. Audit UX Détaillé (Priorité Produit)

### 3.1 Interface TUI (`ogy tui` / `ogy_frontends/acp_tui/app.py`)

| Sévérité | Constat factuel (code) | Impact utilisateur |
| :---: | :--- | :--- |
| **Critique** | **Champ de saisie mono-ligne (`Input`)** (`app.py:985`) au lieu d'un `TextArea` multiligne. Aucun historique de prompts (`↑`/`↓`). | Impossible de rédiger un prompt structuré, de coller un stacktrace ou de rappeler une commande précédente. En dessous des standards CLI modernes (Claude Code, OpenCode, Codex CLI). |
| **Critique** | **Promesse `@fichier` non implémentée** (`i18n.py:14` affiche `"Prompt, @fichier, ou /commande"` mais seule l'autocomplétion `/` existe dans `app.py:1209-1258`). | Perte immédiate de confiance produit dès le premier essai. |
| **Critique** | **Outils non inspectables et zéro diff inline** (`app.py:1430-1450`) : chaque appel d'outil est un `Static` d'une ligne (`✓ name · title`). | L'utilisateur ne voit ni la commande exécutée, ni les arguments, ni la sortie/erreur, ni le diff produit. Effet « boîte noire » anxiogène. |
| **Critique** | **Elicitations ACP rejetées silencieusement** (`app.py:1370-1372`) : `event.future.set_result({"action": "decline"})` sans modale. | Toute demande interactive d'`agy` ou d'un sous-agent échoue sans que l'utilisateur comprenne pourquoi. |
| **Élevée** | **Modale de permission pauvre et sans raccourcis** (`app.py:220-243`) : n'affiche que `tool_name` et `title` (pas `raw_input` / commande / chemin), pas de touches `Y`/`N`/`Esc`. | L'utilisateur doit approuver à l'aveugle ou saisir la souris. |
| **Élevée** | **Rotation & multi-session mélangent les flux** (`app.py:1685-1745`) : `#stream-scroll` et `_tool_widgets` ne sont ni vidés ni isolés par `session_id` lors de `/new`, `/rotate` ou `/resume`. `/resume` ne complète pas les sessions de `list_sessions()`. | Confusion visuelle entre ancienne et nouvelle session ; impossible de naviguer proprement entre sessions. |
| **Élevée** | **Streaming Markdown non throttlé + scroll forcé** (`app.py:1386-1388`) : re-render intégral de `Markdown` à chaque chunk + `scroll_end(animate=False)` systématique. | Lag CPU sur longues réponses et impossible de remonter lire le haut pendant que l'agent génère (« scroll hijacking »). |
| **Moyenne** | **Propositions d'orchestration incomplètes** (`orchestrator.py:68-69`, `app.py:1747-1781`) : bandeaux affichent `[R]`, `[U]`, `[D]`, `[V]`, mais pas de raccourcis directs ; `switch_to_v1_run` non géré ; `delegate_subagent` n'affiche qu'un conseil texte. | Bouton « Appliquer » décevant sur la délégation ou le basculement V1. |
| **Moyenne** | **Fonctions de confort absentes** : pas de copie presse-papiers (blocs de code / réponses), pas de pièces jointes/images (pourtant supporté côté `client.py:478`), pas de thème clair/adaptatif terminal. | Freine l'adoption quotidienne face à `agy` natif ou aux concurrents. |

### 3.2 Expérience CLI, Onboarding & Observabilité (`ogy_cli/`)

1. **Onboarding et cycle de vie incomplets** (`ogy_cli/init_audit.py`) :
   * `ogy init` crée des backups `.ogy.bak`, mais **aucune commande `ogy uninstall` / `ogy restore`** n'existe.
   * `ogy init` ne génère pas de `~/.config/optigravity/config.toml` initial commenté, et il n'existe pas de `ogy config` (show/set/validate).
   * Pas d'option `--version` sur `ogy`, et dérive de version (`pyproject.toml` = `0.1.0`, `client.py` = `0.2.0`).
2. **Sorties CLI austères et manque de « ROI visible »** (`ogy_cli/main.py:130-180`) :
   * `ogy stats` et `ogy waste` affichent des lignes texte brutes (pipe-separated), sans tableau, sans filtres (`--last`, `--since`, `--repo`), et sans indicateur simple de **valeur apportée** (ex. estimation des tokens/coûts économisés grâce au cache, à `ogy wrap` et aux firebreaks).
3. **Expérience `ogy wrap` & `ogy recall`** (`ogy_core/firebreaks.py:115-223`) :
   * `run_wrapped_command` fusionne `stderr` dans `stdout` et **bufférise toute la sortie** jusqu'à la fin (aucun feedback en direct sur les commandes longues).
   * Aucune politique de rétention/nettoyage sur `~/.cache/optigravity/stash/` ni `state_dir`.

### 3.3 Ergonomie des Firebreaks (Friction réelle observée en dogfooding)

Lors de cet audit même, les hooks `ogy-firebreaks` ont déclenché **deux frictions typiques** :
1. **Blocage de `read_file` sur `README.md` (17,6 Ko > 16 Ko)** (`firebreaks.py:87-94`) :
   * Le seuil fixe de 16 Ko s'applique indistinctement au code, aux logs et à la documentation.
   * Le message de refus (`"Utilise grep_search ou specifie StartLine/EndLine"`) cite un outil (`grep_search`) qui n'est pas toujours disponible dans l'environnement de l'agent.
2. **Blocage de `uv run pytest -q --tb=short; ...`** (`firebreaks.py:28-31, 97-110`) :
   * La regex `VERBOSE_CMD_RE` matche même des commandes **déjà bornées** (`pytest -q --tb=short`, `git log -n 10 --oneline`, `git status -s`).
   * Sur une commande composée (`cmd1; cmd2` ou `cmd1 && cmd2`), la réécriture/suggestion `ogy wrap -- cmd1; cmd2` **casse la syntaxe shell** (seul `cmd1` est passé à `ogy wrap`).
3. **Incohérence linguistique & configuration** :
   * Les messages de `firebreaks.py` et `ogy_hooks/cli.py` sont hardcodés en français, contrairement à la CLI/README (anglais) et la TUI (FR/EN).
   * `OgyConfig.overwrite_verified_ok` vaut `True` dans la dataclass (`config.py:23`) mais `False` dans `load_config()` (`config.py:85`).

---

## 4. Audit Technique, Performance & Fiabilité

### 4.1 Bugs et défauts d'architecture identifiés

1. **Contention SQLite sur `ensure_schema()` (`ogy_core/storage.py:170, 295-302`) — Priorité Haute**
   * `SCHEMA_SQL` contient `DROP VIEW IF EXISTS v_session_kpis; CREATE VIEW v_session_kpis AS ...`.
   * Chaque appel à `connect(db_path, init_schema=True)` exécute `ensure_schema()` dans un `BEGIN IMMEDIATE` (verrou d'écriture exclusif !).
   * Or `init_schema=True` est appelé **toutes les 5 secondes** par `TelemetryPanel` (`app.py:1510` → `orchestrator.read_quota_snapshots()`), à chaque hook, à chaque `ogy stats`, etc. Cela transforme des lectures pures en écritures DDL répétées.
2. **Boucle d'apprentissage du Routeur Stage 2 cassée en V1 (`wrapper.py:109-142` vs `ogy_hooks/cli.py:65-97`)**
   * `ogy run` insère une ligne dans `tasks` et passe `OGY_TASK_ID` via variable d'environnement, mais `agy` sanitise l'environnement des hooks et `_handle_session_start` ne relie jamais la session au `task_id` ni n'insère de `routing_decisions`. Le bandit Stage 2 reste donc à froid en V1.
3. **Bug de pression quota permanente (`ogy_cli/wrapper.py:83-88`)**
   * `SELECT MIN(remaining_fraction) FROM quota_snapshots` prend le minimum **historique sur toute la table** au lieu du dernier snapshot par bucket (`get_latest_quota_snapshots`). Un seul passage sous 15 % dans le passé force le downgrade d'effort indéfiniment.
4. **Désalignement `camelCase` entre statusline et ingestion (`render.py:168-183` vs `ingest.py:78-93`)**
   * `render.py` accepte `inputTokens` / `totalUsd` (camelCase), mais `ingest_statusline_payload()` ne lit que `snake_case`. Les métriques peuvent s'afficher dans le HUD tout en restant à `0` en base.
5. **Enrichissement `conversations/<id>.db` inachevé (`ogy_core/ingest.py:362-366`)**
   * La fonction ouvre la DB en lecture seule, marque `conv_db_found = True` et la referme sans lire `gen_metadata` (`thinking_tokens`, `tools_schema_tokens`, `ttft_ms`). Les détecteurs `W4` et `W7` manquent de données en V1.
6. **Catalogue tarifaire incomplet pour l'hybride API (`ogy_core/pricing.py`)**
   * Ne référence ni `gemini-3.5-flash-lite` (ni les modèles Gemini API standards), et ne distingue pas coût **OAuth (forfait/quota)** vs coût **API facturé / crédits Cloud**.
7. **Dette structurelle & Packaging**
   * `ogy_frontends/acp_tui/app.py` (1 814 LOC) mêle CSS, widgets, modales, complétion, command provider, logique de routage et gestion d'événements ACP.
   * `pyproject.toml` impose `textual` et `agent-client-protocol` en dépendances obligatoires même pour un usage V1 pur (alors que des extras `[tui]` et `[subagents]` allégeraient l'installation).

---

## 5. Analyse d'Intégration : Sous-Agents avec l'API Gemini (Free Tier & Crédits Cloud)

*Basé sur `guide_antigravity_api_gemini_cr_dits_cloud_et_sous_agents_mcp.md` et l'architecture actuelle d'Optigravity.*

### 5.1 Rappel de la contrainte structurante d'Antigravity
Les sous-agents natifs d'Antigravity (`.agents/agents/*.md`, `~/.gemini/config/agents/`) **héritent systématiquement de l'authentification de la session principale** (`oauth-personal`). On ne peut pas leur assigner une `GEMINI_API_KEY` directement.

### 5.2 Comparaison des options d'intégration dans Optigravity

| Option | Mécanisme | Intérêt pour Optigravity | Limites & Risques UX |
| :--- | :--- | :--- | :--- |
| **Option A — Session complète API** | `settings.json` (`modelProvider: "gemini"`) ou ACP (`auth.type: "gemini-api-key"`) + `GEMINI_API_KEY`. | Permet à `ogy run --api` ou `ogy tui --auth-mode api-key` d'exécuter des sessions entières hors quota AI Pro. | Fait perdre le forfait OAuth (Gemini 4 Pro / Claude 5.5) sur l'agent principal ; friction de bascule si `settings.json` est muté globalement. |
| **Option B — Architecture Hybride via Serveur MCP** *(Recommandée)* | Agent principal en `oauth-personal` (AI Pro/Ultra) + **serveur MCP local** authentifié via `GEMINI_API_KEY` (Free Tier 0 € ou Crédits AI Pro 10 $/mois Tier 1, ex. `gemini-3.5-flash-lite`). | **Synergie maximale** : préserve le quota OAuth et le cache parent (> 60K tokens), tout en déchargeant les tâches de volume/extraction/tests sur un modèle ultra-rapide et peu coûteux (0,30 $/1M in, 2,50 $/1M out). | Boîte noire pour l'agent principal si mal instrumenté (conflits d'édition disque, timeouts, boucles, erreurs `429` sur Free Tier). |

### 5.3 Les 3 niveaux d'autonomie MCP appliqués aux cas d'usage d'Optigravity

1. **Niveau 1 — Sous-agent Stateless (zéro accès disque/shell)** :
   * *Usages idéaux dans `ogy`* :
     * Résumé intelligent d'un log massif stashed par `ogy wrap` (complément sémantique d'`ogy_recall`).
     * Synthèse automatique d'un `.ogy/handoff.md` de haute qualité avant `/rotate` (aujourd'hui généré de façon très pauvre dans `app.py:1670`).
     * Extraction structurée / classification / triage de gros fichiers sans polluer le contexte principal.
   * *Risque* : quasi nul (pas d'effet de bord, `thinking_level="minimal"`).
2. **Niveau 2 — Boucle agentique Python (`MCPServer` + `google-genai` Automatic Function Calling)** :
   * *Usages idéaux dans `ogy`* :
     * Implémenter réellement l'action **`[D] Déléguer à un sous-agent`** dans `ogy tui` (au-delà de 60K tokens pour éviter la Switch Tax).
     * Recherche profonde dans la codebase (outils read-only : `search`, `read_slice`, `list_files`) retournant un rapport sourcé de 20 lignes à l'agent principal.
     * Génération/exécution de tests en boucle bornée (`maximum_remote_calls=10`, `timeout=30s`, `thinking_level="medium"`).
   * *Point fort* : contrôle fin des outils exposés (ex. mode *read-only* par défaut vs *workspace-write* opt-in), traçabilité directe dans `ogy.db` (tokens, latence, coût API séparé).
3. **Niveau 3 — Wrapper CLI Headless (`gemini` CLI avec `GEMINI_API_KEY`)** :
   * *Usage* : tâches autonomes plus longues nécessitant tout l'outillage natif d'un CLI agentique sans réinventer `grep`/édition par diff.
   * *Vigilance* : privilégier `gemini` CLI plutôt qu'`agy -p` (qui lit `~/.gemini/antigravity-cli/settings.json` et risque un conflit avec la session OAuth principale) ; encadrer strictement les permissions (`--mode=auto` + sandbox, jamais `--dangerously-skip-permissions` hors conteneur/VM).

### 5.4 Exigences UX & Produit pour une intégration « professionnelle » des sous-agents
* **Gestion double projet Free Tier vs Crédits Tier 1 (`429` resilience)** : permettre de configurer 2 clés distinctes (Free Tier par défaut + fallback optionnel sur clé Crédits Tier 1 en cas de `429 RESOURCE_EXHAUSTED`, avec alerte claire à l'utilisateur).
* **Sécurité des secrets** : jamais de clé en clair dans un fichier projet `.agents/mcp_config.json` ; passer par variables d'environnement ou trousseau système / fichier global protégé.
* **Observabilité temps réel** : ne pas laisser l'utilisateur devant un écran figé pendant 30 s — exploiter les notifications de progression MCP (`notifications/progress` / logging) et afficher l'état du sous-agent + coût API séparé dans la sidebar de `ogy tui` et `ogy stats`.
* **Contrat anti-conflit disque** : imposer au sous-agent de retourner systématiquement la liste exacte des fichiers créés/modifiés (voire un `git diff --stat`) afin que l'agent principal sache quoi relire avant d'éditer.
* **Respect des contraintes modèle `gemini-3.5-flash-lite`** : utiliser `thinking_level` (`minimal` / `medium` / `high`) et ne jamais envoyer les paramètres dépréciés (`temperature`, `top_p`, `top_k`).

---

## 6. Pistes & Axes pour le Deep Research (Orienté UX Pro)

L'objectif du Deep Research n'est pas d'optimiser encore les tokens à la marge, mais de **hisser Optigravity au niveau d'un outil développeur professionnel**, agréable et fluide, tout en préservant ses acquis d'efficience.

### Axe 1 — UX des TUI Agentiques Modernes (Benchmark & Design Patterns)
* *Références à étudier* : Claude Code, OpenCode, Toad (`batrachianai/toad`), Codex CLI, Aider, Goose, Textual best practices.
* *Questions de recherche* :
  1. Quels patterns d'interaction font qu'une TUI agentique est perçue comme « fluide et pro » ? (Éditeur multiligne `Enter` vs `Shift+Enter`, historique persistant, `@file` fuzzy finder rapide, gestion du copier-coller massif / paste collapse).
  2. Comment présenter les appels d'outils, les diffs et les blocs de réflexion (`thought`) pour offrir **transparence sans surcharge cognitive** ? (Groupement d'outils parallèles, cartes dépliables au clavier, diffs compacts colorés, smart auto-scroll qui s'arrête dès que l'utilisateur scrolle vers le haut).
  3. Comment designer une modale de permission et d'élicitation (`request_permission`, `elicitation/create`) rapide au clavier, lisible (aperçu exact de la commande/diff) et sécurisante ?
  4. Comment gérer l'UX multi-session et la rotation de contexte (`/rotate`) de manière **quasi transparente** (onglets/drawer de sessions, séparation visuelle nette des threads, aperçu/édition du `handoff.md` avant injection) ?

### Axe 2 — UX des Garde-Fous (Firebreaks) « Zéro Friction »
* *Questions de recherche* :
  1. Comment concevoir des garde-fous de sobriété qui **aident sans jamais bloquer abusivement** l'utilisateur ni l'agent ? (Distinguer commandes déjà bornées, parser proprement les pipelines/commandes composées, seuils adaptatifs par type de fichier : code vs markdown vs logs vs lockfiles).
  2. Quel est le meilleur équilibre entre *réécriture silencieuse* (`overwrite`), *avertissement doux*, et *refus strict* (`deny`) ? Comment formuler des messages d'erreur actionnables qui s'adaptent aux outils réellement disponibles ?
  3. Comment rendre `ogy wrap` totalement transparent pour l'utilisateur (streaming progressif ou spinner, préservation stdout/stderr, politique de rétention du stash) ?

### Axe 3 — UX & Architecture des Sous-Agents Hybrides (MCP + API Gemini)
* *Questions de recherche* :
  1. Quels sont les meilleurs patterns UX pour rendre un sous-agent MCP **observable et maîtrisable** (streaming de progression MCP, affichage d'un arbre parent → sous-agent dans la TUI, visibilité des fichiers modifiés, séparation claire quota OAuth vs coût API Gemini) ?
  2. Quelle frontière de responsabilité choisir entre **Niveau 1 (Stateless)**, **Niveau 2 (Boucle Python `google-genai` / Google ADK)** et **Niveau 3 (Wrapper `gemini` CLI)** pour maximiser la fiabilité et minimiser la complexité de maintenance ?
  3. Comment concevoir une configuration utilisateur « 1 minute chrono » pour les clés API Gemini (Free Tier vs Crédits Tier 1, gestion des `429`, test de connectivité dans `ogy doctor`, stockage sécurisé des clés) ?
  4. Comment orchestrer proprement la règle « > 60K tokens → déléguer » dans `ogy tui` pour que l'action `[D]` soit réellement exécutable en une touche tout en respectant les ToS Section 6 ?

### Axe 4 — Onboarding, Lisibilité Produit & Confiance (CLI & Telemetry UX)
* *Questions de recherche* :
  1. À quoi ressemble un onboarding CLI exemplaire (`ogy init` guidé/interactif, `ogy doctor` avec diagnostics actionnables + auto-fix, `ogy uninstall` propre, `ogy config`) ?
  2. Comment transformer les données SQLite (`ogy stats`, `ogy waste`) en **tableaux de bord lisibles et valorisants** (ex. coût évité estimé, tendance du cache hit, recommandations concrètes plutôt que codes bruts `W1..W8`) ?
  3. Quelles bonnes pratiques d'architecture Python/Textual adopter pour découper `app.py` (1 814 LOC) en composants testables, découpler les accès SQLite du thread UI (worker asynchrone + cache mémoire), et proposer un packaging modulaire (`optigravity[tui]`, `optigravity[subagents]`) ?

---

## 7. Brief Prêt à l'Emploi pour Lancer le Deep Research

> Tu peux copier-coller ce brief en pièce jointe de ce fichier `AUDIT_UX_ET_ARCHITECTURE.md` dans ton outil de Deep Research :
>
> **Contexte** : Je développe **Optigravity (`ogy`)**, un compagnon open-source en Python pour **Google Antigravity CLI (`agy`)** comprenant une couche V1 (CLI wrapper, hooks fail-open stdlib, statusline HUD, compression shell `ogy wrap`, serveur MCP minimal, SQLite WAL) et une couche V2 (TUI interactive Textual sur protocole ACP v1 `agy acp`). L'optimisation de tokens est déjà prouvée (-10 % à -58 % sur benchmarks A/B).
>
> **Objectif du Deep Research** : Concevoir un **plan de professionnalisation UX et produit** (sans dégrader l'économie de tokens ni le temps de démarrage du chemin chaud stdlib) en répondant de façon concrète et sourcée à :
> 1. **Refonte UX de la TUI Textual (`ogy tui`)** : meilleurs patterns issus des TUI agentiques modernes (Claude Code, OpenCode, Toad, Codex CLI) pour l'éditeur multiligne, `@file`, les tool cards/diffs inline, le streaming sans lag/scroll hijacking, les modales permissions/élicitations et le multi-sessions.
> 2. **Firebreaks non-intrusifs** : comment rendre les garde-fous (`PreToolUse`, `ogy wrap`) intelligents et sans faux positifs (commandes composées, commandes déjà bornées, seuils adaptatifs).
> 3. **Intégration professionnelle de sous-agents hybrides via MCP + API Gemini (`GEMINI_API_KEY` Free Tier / Crédits Google AI Pro)** : architecture recommandée (Stateless vs `google-genai` Function Calling / ADK vs wrapper `gemini` CLI), résilience `429`, observabilité temps réel (progress MCP, coûts séparés, anti-conflit disque), et expérience utilisateur fluide depuis `ogy tui` et `agy` natif.
> 4. **Onboarding, CLI UX & Architecture** : `init`/`doctor`/`uninstall`, tableaux de bord ROI (`stats`/`waste`), découpage modulaire de la TUI et bonnes pratiques SQLite non-bloquantes.
